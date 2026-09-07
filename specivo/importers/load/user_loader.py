"""Load users, the import service account, and group membership.

Three things happen here that are worth knowing about.

**No password survives.** Redmine salts and hashes with SHA1 and Specivo uses
bcrypt, so nothing is portable. Every imported account gets a random unusable
hash and its login is listed in the import report, because the accounts are
otherwise unreachable until somebody resets them.

**Identity has to be squeezed into narrower columns.** Specivo requires an email
and enforces case-insensitive uniqueness on both login and email, while Redmine
allows an account with no address at all. Anything that would collide or fail is
adjusted rather than dropped, and the adjustment is reported.

**Groups produce no rows.** Specivo cannot hang project roles off a group, so
membership is collected here and flattened into per-user grants once projects
exist.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.importers.core.backdate import backdate
from specivo.importers.core.ir import EntityType, IRUser
from specivo.importers.core.pipeline import PhaseContext
from specivo.models.user import User
from specivo.services.auth_utils import hash_password

logger = logging.getLogger(__name__)

# Where the group membership map is parked for the memberships phase.
GROUP_MEMBERS_STATE_KEY = "group_members"

# Report sections.
NOTE_PASSWORD_RESET = "password_reset_required"
NOTE_SYNTHETIC_EMAIL = "accounts_given_a_placeholder_email"
NOTE_GROUPS_FLATTENED = "groups_flattened_into_memberships"

# Identifier under which the fallback author account is mapped, so a resumed
# run finds the one it made rather than creating another.
IMPORT_ACCOUNT_REF = "import_account"

# RFC 2606 reserves .invalid precisely so it can never resolve, which is what a
# placeholder address must guarantee.
_PLACEHOLDER_EMAIL_DOMAIN = "invalid"

_MAX_LOGIN = 100
_MAX_EMAIL = 255
_MAX_DISPLAY_NAME = 255


async def ensure_import_account(ctx: PhaseContext) -> User:
    """Return the service account that owns anything with no resolvable author.

    Redmine allows a row whose author was deleted, and Specivo requires one, so
    the import needs an account to attribute those to. Marked as a service
    account so it is not mistaken for a person, and reused across runs.
    """
    mapped_id = await ctx.id_map.get(ctx.session, EntityType.SYSTEM, IMPORT_ACCOUNT_REF)
    if mapped_id is not None:
        account = await ctx.session.get(User, mapped_id)
        if account is not None:
            return account

    login = f"{ctx.adapter.source_system}-import"
    existing = await _find_user_by_login(ctx.session, login)
    if existing is None:
        existing = User(
            login=login,
            email=f"{login}@{_PLACEHOLDER_EMAIL_DOMAIN}",
            display_name=f"{ctx.adapter.source_system.title()} Import",
            password_hash=_unusable_password(),
            status="active",
            is_admin=False,
            is_service_account=True,
        )
        ctx.session.add(existing)
        await ctx.session.flush()
        ctx.summary.record_created(EntityType.SYSTEM)
    else:
        ctx.summary.record_reused(EntityType.SYSTEM)

    await ctx.id_map.put(ctx.session, EntityType.SYSTEM, IMPORT_ACCOUNT_REF, "users", existing.id)
    return existing


async def load_users(ctx: PhaseContext) -> None:
    """Create every source user that is not already imported."""
    async for ir in ctx.adapter.extract_users():
        if await ctx.id_map.get(ctx.session, EntityType.USER, ir.source_ref):
            ctx.summary.record_skipped(EntityType.USER)
            continue
        await load_user(ctx, ir)


async def load_user(ctx: PhaseContext, ir: IRUser) -> User:
    """Create one user from *ir*, adjusting identity where Specivo is stricter."""
    login = await _unique_login(ctx, ir)
    email, synthesised = await _unique_email(ctx, ir, login)

    user = User(
        login=login,
        email=email,
        display_name=(ir.display_name or login)[:_MAX_DISPLAY_NAME],
        password_hash=_unusable_password(),
        status=ir.status,
        is_admin=ir.is_admin,
        is_service_account=False,
        language=_supported_language(ir.language),
        last_login_at=ir.last_login_at,
    )
    ctx.session.add(user)
    await ctx.session.flush()

    await backdate(ctx.session, User, user.id, created_at=ir.created_at, updated_at=ir.updated_at)
    await ctx.id_map.put(ctx.session, EntityType.USER, ir.source_ref, "users", user.id)

    ctx.summary.record_created(EntityType.USER)
    ctx.summary.add_note(NOTE_PASSWORD_RESET, login)
    if synthesised:
        ctx.summary.add_note(NOTE_SYNTHETIC_EMAIL, f"{login} ({email})")
    ctx.tick()
    return user


async def load_groups(ctx: PhaseContext) -> None:
    """Collect group membership for the memberships phase to flatten.

    Creates nothing. Specivo has no group that can hold project roles, so the
    only thing a group can contribute is the list of people who inherit its
    grants, and that can only be applied once projects exist.
    """
    members: dict[str, list[str]] = {}
    async for group in ctx.adapter.extract_groups():
        members[group.source_ref] = list(group.member_refs)
        ctx.summary.add_note(NOTE_GROUPS_FLATTENED, f"{group.name} ({len(group.member_refs)} members)")
        ctx.tick()

    ctx.state[GROUP_MEMBERS_STATE_KEY] = members
    if members:
        ctx.warn(
            "Group memberships will be flattened into individual grants; the grouping itself is not imported",
            groups=len(members),
        )


def _unusable_password() -> str:
    """Return a bcrypt hash of a random secret nobody holds.

    A null hash would be indistinguishable from an account still being set up,
    so a real hash of an unknown value is used: the account exists and is
    locked out of password login until it is reset.
    """
    return hash_password(secrets.token_urlsafe(32))


def _supported_language(language: str | None) -> str:
    """Return *language* if this instance ships it, else the instance default."""
    settings = get_settings()
    if language and language in settings.available_languages:
        return language
    return settings.default_language


async def _find_user_by_login(session: AsyncSession, login: str) -> User | None:
    stmt = select(User).where(func.lower(User.login) == login.lower()).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _exists(session: AsyncSession, column: Any, value: str) -> bool:
    stmt = select(User.id).where(func.lower(column) == value.lower()).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def _unique_login(ctx: PhaseContext, ir: IRUser) -> str:
    """Return a login that fits the column and does not collide.

    The source login is kept wherever possible — people recognise their own —
    and only trimmed, replaced when empty, or suffixed on a clash.
    """
    base = (ir.login or "").strip()[:_MAX_LOGIN]
    if not base:
        base = f"user-{ir.source_ref}"
        ctx.warn("Source account has no login; one was generated", source_ref=ir.source_ref, login=base)

    candidate = base
    suffix = 2
    while await _exists(ctx.session, User.login, candidate):
        tail = f"-{suffix}"
        candidate = f"{base[: _MAX_LOGIN - len(tail)]}{tail}"
        suffix += 1

    if candidate != base:
        ctx.warn("Login already taken; imported under a suffixed login", wanted=base, login=candidate)
    return candidate


async def _unique_email(ctx: PhaseContext, ir: IRUser, login: str) -> tuple[str, bool]:
    """Return an address for the account and whether it had to be invented.

    Specivo requires an address and Redmine does not always have one. A
    placeholder under the reserved ``.invalid`` domain keeps the account
    importable while guaranteeing no mail is ever sent to a real inbox.
    """
    synthesised = False
    base = (ir.email or "").strip()[:_MAX_EMAIL]
    if not base:
        base = f"{login}@{_PLACEHOLDER_EMAIL_DOMAIN}"
        synthesised = True

    candidate = base
    suffix = 2
    while await _exists(ctx.session, User.email, candidate):
        local, _, domain = base.partition("@")
        candidate = f"{local}+{suffix}@{domain}"[:_MAX_EMAIL]
        suffix += 1

    if candidate != base:
        ctx.warn("Email already taken; imported under a suffixed address", wanted=base, email=candidate)
    return candidate, synthesised
