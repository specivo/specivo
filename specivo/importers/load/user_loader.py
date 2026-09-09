"""Load users, the import service account, and user groups.

Three things happen here that are worth knowing about.

**No password survives.** Redmine salts and hashes with SHA1 and Specivo uses
bcrypt, so nothing is portable. Every imported account gets a random unusable
hash and is flagged ``must_change_password``, so whatever password it is
eventually given — by an administrator, or by the person completing an email
recovery — is not the password it keeps. The logins are listed in the report so
the operator knows which accounts are waiting for one.

**Identity has to be squeezed into narrower columns.** Specivo requires an email
and enforces case-insensitive uniqueness on both login and email, while Redmine
allows an account with no address at all. Anything that would collide or fail is
adjusted rather than dropped, and the adjustment is reported.

**Groups become groups.** A Specivo ``UserGroup`` holds project roles the same
way a user does, so a source group is imported as one, with its members, and the
memberships phase grants roles to the group itself. Group names are unique
case-insensitively in Specivo and are not in Redmine, so a name that is already
taken is suffixed rather than merged onto whatever holds it.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.exceptions import ConflictError
from specivo.importers.core.backdate import backdate
from specivo.importers.core.ir import EntityType, IRGroup, IRUser
from specivo.importers.core.pipeline import PhaseContext
from specivo.models.user import User
from specivo.models.user_group import UserGroup
from specivo.services.auth_utils import hash_password
from specivo.services.user_group_service import UserGroupService

logger = logging.getLogger(__name__)

# Where the fallback author account's id is parked for later phases. Its id and
# not the object: each phase runs in its own session, so an instance loaded in
# an earlier one is detached by the time a later phase would use it.
IMPORT_ACCOUNT_STATE_KEY = "import_account_id"

# Report sections. The section name is printed as the heading of its list, so it
# has to read as a statement about the accounts under it.
NOTE_PASSWORD_SET_AT_FIRST_SIGN_IN = "accounts_that_will_be_asked_to_set_a_password_at_first_sign_in"
NOTE_SYNTHETIC_EMAIL = "accounts_given_a_placeholder_email"

# Identifier under which the fallback author account is mapped, so a resumed
# run finds the one it made rather than creating another.
IMPORT_ACCOUNT_REF = "import_account"

# RFC 2606 reserves .invalid precisely so it can never resolve, which is what a
# placeholder address must guarantee.
_PLACEHOLDER_EMAIL_DOMAIN = "invalid"

_MAX_LOGIN = 100
_MAX_EMAIL = 255
_MAX_DISPLAY_NAME = 255
_MAX_GROUP_NAME = 255

_user_group_service = UserGroupService()


async def ensure_import_account(ctx: PhaseContext) -> User:
    """Return the service account that owns anything with no resolvable author.

    Redmine allows a row whose author was deleted, and Specivo requires one, so
    the import needs an account to attribute those to. Marked as a service
    account so it is not mistaken for a person, and reused across runs.

    Always returns an instance attached to the current session, and always
    records the id, including when the account already existed. Returning early
    without recording it was a real defect: on a second or resumed run the
    account is found rather than created, and every later phase then looked for
    something that had never been put there.
    """
    cached_id = ctx.state.get(IMPORT_ACCOUNT_STATE_KEY)
    if isinstance(cached_id, int):
        cached = await ctx.session.get(User, cached_id)
        if cached is not None:
            return cached

    mapped_id = await ctx.id_map.get(ctx.session, EntityType.SYSTEM, IMPORT_ACCOUNT_REF)
    if mapped_id is not None:
        account = await ctx.session.get(User, mapped_id)
        if account is not None:
            ctx.state[IMPORT_ACCOUNT_STATE_KEY] = account.id
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
            # Deliberately not flagged for a password change, and the CHECK on
            # users would reject the row if it were: this account authenticates
            # with an API key and has no password anyone could replace.
        )
        ctx.session.add(existing)
        await ctx.session.flush()
        ctx.summary.record_created(EntityType.SYSTEM)
    else:
        ctx.summary.record_reused(EntityType.SYSTEM)

    await ctx.id_map.put(ctx.session, EntityType.SYSTEM, IMPORT_ACCOUNT_REF, "users", existing.id)
    ctx.state[IMPORT_ACCOUNT_STATE_KEY] = existing.id
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
        # Nobody holds this password, so the first one the account actually has
        # will have been chosen by somebody else. Making the owner replace it is
        # the point; a person who recovers the account by email picks their own
        # and the flag clears itself.
        must_change_password=True,
        language=_supported_language(ir.language),
        last_login_at=ir.last_login_at,
    )
    ctx.session.add(user)
    await ctx.session.flush()

    await backdate(ctx.session, User, user.id, created_at=ir.created_at, updated_at=ir.updated_at)
    await ctx.id_map.put(ctx.session, EntityType.USER, ir.source_ref, "users", user.id)

    ctx.summary.record_created(EntityType.USER)
    ctx.summary.add_note(NOTE_PASSWORD_SET_AT_FIRST_SIGN_IN, login)
    if synthesised:
        ctx.summary.add_note(NOTE_SYNTHETIC_EMAIL, f"{login} ({email})")
    ctx.tick()
    return user


async def load_groups(ctx: PhaseContext) -> None:
    """Create a user group per source group, with the members that exist.

    Runs after :func:`load_users`, because a group can only hold the people the
    run has already imported. The group itself is what the memberships phase
    grants roles to, so this has to happen before projects are reached.
    """
    async for ir in ctx.adapter.extract_groups():
        if await ctx.id_map.get(ctx.session, EntityType.GROUP, ir.source_ref):
            # A resumed run finds the group the earlier attempt created. The
            # mapping and the group are written in the same transaction, so
            # either both survived or neither did.
            ctx.summary.record_skipped(EntityType.GROUP)
            continue
        await load_group(ctx, ir)


async def load_group(ctx: PhaseContext, ir: IRGroup) -> UserGroup | None:
    """Create one group from *ir* and put its imported members in it.

    Returns ``None`` when the group could not be created, which leaves its
    project grants unimported and reported rather than ending the run.

    A name collision is settled before the insert, twice: ``_unique_group_name``
    picks a free one, and ``UserGroupService.create`` checks again and raises
    :class:`ConflictError` rather than letting ``uq_user_groups_name_ci`` reject
    the row. That matters because the index is the only backstop left, and an
    ``IntegrityError`` from it cannot be handled here — it poisons the session,
    so the phase would have to roll back whole rather than skip one group.
    """
    name = await _unique_group_name(ctx, ir)
    try:
        group = await _user_group_service.create(ctx.session, name)
    except ConflictError:
        # Only reachable if something claimed the name between the two checks.
        # Reported and passed over: retrying under yet another name is the loop
        # _unique_group_name just ran, and it would race the same way.
        ctx.warn("Group name was taken while it was being imported; group skipped", group=ir.name, name=name)
        return None

    await ctx.id_map.put(ctx.session, EntityType.GROUP, ir.source_ref, "user_groups", group.id)
    ctx.summary.record_created(EntityType.GROUP)

    for member_ref in ir.member_refs:
        user_id = await ctx.id_map.get(ctx.session, EntityType.USER, member_ref)
        if user_id is None:
            ctx.warn("Group member was not imported; left out of the group", group=name, source_ref=member_ref)
            continue
        await _user_group_service.add_user(ctx.session, group.id, user_id)

    ctx.tick()
    return group


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


async def _group_name_taken(session: AsyncSession, name: str) -> bool:
    stmt = select(UserGroup.id).where(func.lower(UserGroup.name) == name.lower()).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def _unique_group_name(ctx: PhaseContext, ir: IRGroup) -> str:
    """Return a group name that fits the column and is not already taken.

    ``user_groups`` is unique on the lowercased name and Redmine is not, so two
    source groups differing only in case, and a source group whose name matches
    one this instance already has, both land on the same row otherwise.

    A taken name is suffixed rather than reused. Adding the source group's
    members and grants to whichever group already holds the name would hand the
    people already in it access nobody asked to give them, which is the one
    outcome an import must not produce quietly.
    """
    base = (ir.name or "").strip()[:_MAX_GROUP_NAME]
    if not base:
        base = f"group-{ir.source_ref}"
        ctx.warn("Source group has no name; one was generated", source_ref=ir.source_ref, name=base)

    candidate = base
    suffix = 2
    while await _group_name_taken(ctx.session, candidate):
        tail = f"-{suffix}"
        candidate = f"{base[: _MAX_GROUP_NAME - len(tail)]}{tail}"
        suffix += 1

    if candidate != base:
        ctx.warn("Group name already taken; imported under a suffixed name", wanted=base, name=candidate)
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
