"""Admin user management: create and reset password.

Usage::

    # Create admin (from bootstrap file — used by entrypoint)
    uv run python -m specivo.cli.admin create

    # Create admin (explicit args)
    uv run python -m specivo.cli.admin create --login admin --email admin@localhost --password secret

    # Reset password
    uv run python -m specivo.cli.admin reset-password --login admin --password newpass

    # Password hashers: what is registered and enabled, and how many accounts use each
    uv run python -m specivo.cli.admin password-hashers
    # Check a hasher against a known source row, without touching any account
    uv run python -m specivo.cli.admin password-hashers test redmine_sha1 \
        --field salt=... --field hashed_password=... --password '...'

    # Accounts still holding an imported (legacy) password hash
    uv run python -m specivo.cli.admin legacy-passwords
    # Expire them now: each has to set a new password through recovery
    uv run python -m specivo.cli.admin legacy-passwords expire --yes

    # Imports in the identifier map: one scope per source system and instance
    uv run python -m specivo.cli.admin import-map list
    # Move a scope to the key the next import will use (a dry run without --yes)
    uv run python -m specivo.cli.admin import-map rename --system redmine \
        --from 'postgresql://redmine:***@db.internal/redmine' --to tracker.example.com --yes

The ``create`` subcommand is idempotent: existing admins are skipped,
non-admin users are promoted.  When no CLI args are given it reads the
bootstrap file and deletes it after success: ``/app/data/state/bootstrap.json``,
where the container's root step moves it, or ``/app/data/.bootstrap.json``,
where the installer writes it.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import secrets
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from specivo.core.config import get_settings
from specivo.core.utils import utcnow
from specivo.importers.core.id_map import IdMapScope, ScopeRenameError, list_scopes, rename_scope
from specivo.models.user import User
from specivo.services.anonymous_user_service import RESERVED_LOGIN_PREFIX, is_reserved_login
from specivo.services.auth_utils import hash_password, legacy_hashes_expired
from specivo.services.password_hashers import (
    BCRYPT_PREFIXES,
    CredentialImportError,
    PasswordHasherRegistry,
    get_password_hasher_registry,
)
from specivo.services.session_service import SessionService

logger = logging.getLogger(__name__)

# In the order they are tried. The entrypoint, when started as root, moves the
# installer's file from the top of the data directory into state/, which the
# unprivileged app user owns and so can delete it from.
BOOTSTRAP_PATHS = (Path("/app/data/state/bootstrap.json"), Path("/app/data/.bootstrap.json"))


def _bootstrap_path() -> Path | None:
    return next((path for path in BOOTSTRAP_PATHS if path.exists()), None)


async def _create_admin(
    session: AsyncSession,
    login: str,
    email: str,
    password: str,
) -> None:
    """Create or promote an admin user."""
    if is_reserved_login(login):
        print(f"Error: logins starting with '{RESERVED_LOGIN_PREFIX}' are reserved for the system.", file=sys.stderr)
        sys.exit(1)

    result = await session.execute(select(User).where(func.lower(User.login) == login.lower()))
    user = result.scalar_one_or_none()

    if user is not None:
        if user.is_admin:
            print(f"Admin user '{login}' already exists — skipping.")
            return
        user.is_admin = True
        await session.commit()
        print(f"User '{login}' promoted to admin.")
        return

    now = utcnow()
    user = User(
        login=login,
        email=email,
        password_hash=hash_password(password),
        display_name=login.capitalize(),
        status="active",
        is_admin=True,
        email_verified_at=now,
        password_changed_at=now,
    )
    session.add(user)
    await session.commit()
    print(f"Admin user '{login}' created.")


async def _reset_password(
    session: AsyncSession,
    login: str,
    password: str,
) -> None:
    """Reset password for an existing user."""
    result = await session.execute(select(User).where(func.lower(User.login) == login.lower()))
    user = result.scalar_one_or_none()

    if user is None:
        print(f"Error: user '{login}' not found.", file=sys.stderr)
        sys.exit(1)
    if user.is_anonymous:
        print("Error: the anonymous user cannot have a password.", file=sys.stderr)
        sys.exit(1)

    user.password_hash = hash_password(password)
    user.password_changed_at = utcnow()
    user.failed_login_count = 0
    user.locked_until = None
    # An operator reset is compromise recovery: every existing session of the
    # account ends now, its access tokens included.
    revoked = await SessionService().revoke_user_sessions(session, user.id, reason="admin_cli_password_reset")
    await session.commit()
    print(f"Password reset for user '{login}'. Sessions revoked: {revoked}.")


async def _run_create(args: argparse.Namespace) -> None:
    login = args.login
    email = args.email
    password = args.password
    bootstrap_path = _bootstrap_path() if not password else None

    if bootstrap_path is not None:
        data = json.loads(bootstrap_path.read_text())
        login = login or data.get("login", "admin")
        email = email or data.get("email", "admin@localhost")
        password = data.get("password", "")

    if not password:
        print("Error: password required (via --password or bootstrap file).", file=sys.stderr)
        sys.exit(1)

    if len(password) < 8:
        print("Error: password must be at least 8 characters.", file=sys.stderr)
        sys.exit(1)

    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        await _create_admin(session, login, email, password)

    await engine.dispose()

    if bootstrap_path is not None:
        try:
            bootstrap_path.unlink(missing_ok=True)
        except OSError as exc:
            print(f"Warning: could not delete {bootstrap_path} ({exc}); delete it by hand.", file=sys.stderr)
        else:
            print("Bootstrap file consumed and deleted.")


async def _run_reset(args: argparse.Namespace) -> None:
    if not args.login:
        print("Error: --login is required.", file=sys.stderr)
        sys.exit(1)
    if not args.password:
        print("Error: --password is required.", file=sys.stderr)
        sys.exit(1)
    if len(args.password) < 8:
        print("Error: password must be at least 8 characters.", file=sys.stderr)
        sys.exit(1)

    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        await _reset_password(session, args.login, args.password)

    await engine.dispose()


# ---------------------------------------------------------------------------
# Password hashers and legacy hashes
# ---------------------------------------------------------------------------


def _legacy_hash_clause():
    """Rows whose hash is stored under a legacy ``<algorithm>$`` prefix."""
    return User.password_hash.is_not(None) & ~User.password_hash.startswith("$")


def _algorithm_of_hash_expr():
    """SQL for the ``<algorithm>`` part of a legacy hash."""
    return func.split_part(User.password_hash, "$", 1)


async def _hash_counts(session: AsyncSession) -> dict[str, int]:
    """Return how many accounts hold a hash under each algorithm id."""
    counts: dict[str, int] = {}
    native = (
        await session.execute(
            select(func.count()).where(
                User.password_hash.is_not(None),
                func.substr(User.password_hash, 1, 4).in_(BCRYPT_PREFIXES),
            )
        )
    ).scalar_one()
    counts["bcrypt"] = native
    algorithm = _algorithm_of_hash_expr()
    rows = await session.execute(select(algorithm, func.count()).where(_legacy_hash_clause()).group_by(algorithm))
    for name, count in rows.all():
        counts[name] = count
    return counts


def _print_hashers(registry: PasswordHasherRegistry, counts: dict[str, int]) -> None:
    settings = get_settings()
    enabled = set(settings.legacy_password_algorithms)
    rows = [("bcrypt", "core (preferred)", "yes", counts.get("bcrypt", 0))]
    for algorithm, entry in sorted(registry.legacy().items()):
        rows.append((algorithm, entry.origin, "yes" if algorithm in enabled else "no", counts.get(algorithm, 0)))
    for algorithm in sorted(set(counts) - {"bcrypt"} - set(registry.legacy())):
        rows.append((algorithm, "NOT REGISTERED", "no", counts[algorithm]))

    print(f"{'ALGORITHM':<24} {'ORIGIN':<40} {'ENABLED':<8} USERS")
    for algorithm, origin, is_enabled, count in rows:
        print(f"{algorithm:<24} {origin:<40} {is_enabled:<8} {count}")

    missing = sorted(enabled - set(registry.legacy()))
    if missing:
        print(f"\nWarning: LEGACY_PASSWORD_ALGORITHMS lists unregistered ids: {', '.join(missing)}")
    cutoff = settings.legacy_password_hashes_expire_at
    if cutoff is not None:
        state = "passed: legacy hashes no longer verify" if legacy_hashes_expired() else "not reached yet"
        print(f"\nLEGACY_PASSWORD_HASHES_EXPIRE_AT: {cutoff.isoformat()} ({state})")


async def _run_password_hashers(args: argparse.Namespace) -> None:
    registry = get_password_hasher_registry()
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        counts = await _hash_counts(session)
    await engine.dispose()
    _print_hashers(registry, counts)


def _parse_fields(pairs: list[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not separator or not key:
            raise SystemExit(f"Error: --field expects name=value, got {pair!r}")
        fields[key] = value
    return fields


def _run_password_hasher_test(args: argparse.Namespace) -> int:
    """Build a stored value from source fields and check a password against it.

    Touches no account and does not require the algorithm to be enabled: it is
    for checking a hasher against a known vector before turning it on.
    """
    registry = get_password_hasher_registry()
    hasher = registry.get(args.algorithm)
    if hasher is None:
        known = ", ".join(sorted(registry.legacy())) or "none"
        print(f"Error: no password hasher '{args.algorithm}' is registered (registered: {known}).", file=sys.stderr)
        return 2

    password = args.password if args.password is not None else getpass.getpass("Password to check: ")
    try:
        encoded = hasher.from_source(_parse_fields(args.field or []))
    except CredentialImportError as exc:
        print(f"Error: the source fields were rejected: {exc}", file=sys.stderr)
        return 2

    matched = bool(hasher.verify(password, encoded))
    enabled = args.algorithm in get_settings().legacy_password_algorithms
    print(f"Stored form: {encoded.split('$', 1)[0]}$... ({len(encoded)} characters)")
    print("Result: MATCH" if matched else "Result: NO MATCH")
    if not enabled:
        print(f"Note: '{args.algorithm}' is not in LEGACY_PASSWORD_ALGORITHMS, so sign-in would not use it yet.")
    return 0 if matched else 1


async def legacy_password_rows(session: AsyncSession) -> list[tuple[str, str, str, object]]:
    """Return ``(login, status, algorithm, last_login_at)`` for every legacy hash."""
    algorithm = _algorithm_of_hash_expr()
    stmt = (
        select(User.login, User.status, algorithm, User.last_login_at)
        .where(_legacy_hash_clause())
        .order_by(algorithm, User.login)
    )
    return [tuple(row) for row in (await session.execute(stmt)).all()]


async def expire_legacy_passwords(session: AsyncSession, algorithm: str | None = None) -> int:
    """Replace legacy hashes with an unusable one and flag a password change.

    Limited to *algorithm* when given. Returns how many accounts were expired;
    the caller commits.
    """
    clause = _legacy_hash_clause()
    if algorithm:
        clause = clause & (_algorithm_of_hash_expr() == algorithm)
    user_ids = list((await session.execute(select(User.id).where(clause))).scalars().all())
    count = len(user_ids)
    if not count:
        return 0
    # The password is replaced by one nobody knows: every session of these
    # accounts ends now (a session left behind could not even complete the
    # forced change, which asks for the current password).
    sessions = SessionService()
    for user_id in user_ids:
        await sessions.revoke_user_sessions(session, user_id, reason="legacy_password_expired")
    # One hash of a discarded random secret serves every row: nobody can
    # produce a password matching it, so sharing it reveals nothing.
    unusable = hash_password(secrets.token_urlsafe(32))
    # Service accounts authenticate with API keys and cannot carry the
    # forced-change flag (a CHECK forbids it); they simply lose the hash.
    await session.execute(
        update(User)
        .where(clause, User.is_service_account.is_(True))
        .values(password_hash=None)
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(User)
        .where(clause)
        .values(password_hash=unusable, must_change_password=True)
        .execution_options(synchronize_session=False)
    )
    return count


async def _run_legacy_report(args: argparse.Namespace) -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        rows = await legacy_password_rows(session)
    await engine.dispose()

    if not rows:
        print("No account holds a legacy password hash.")
        return
    by_algorithm: dict[str, int] = {}
    print(f"{'LOGIN':<32} {'STATUS':<22} {'ALGORITHM':<24} LAST SIGN-IN")
    for login, status, name, last_login in rows:
        by_algorithm[name] = by_algorithm.get(name, 0) + 1
        seen = last_login.isoformat() if isinstance(last_login, datetime) else "never"
        print(f"{login:<32} {status:<22} {name:<24} {seen}")
    print()
    for name, count in sorted(by_algorithm.items()):
        print(f"{name}: {count} account(s)")


async def _run_legacy_expire(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        if not args.yes:
            rows = await legacy_password_rows(session)
            count = sum(1 for row in rows if not args.algorithm or row[2] == args.algorithm)
            print(f"{count} account(s) would lose their imported password. Re-run with --yes to expire them.")
        else:
            count = await expire_legacy_passwords(session, args.algorithm)
            await session.commit()
            print(f"Expired {count} legacy password hash(es). Those accounts must now use password recovery.")
    await engine.dispose()
    return 0


def format_scopes(scopes: list[IdMapScope]) -> str:
    """Return the ``import-map list`` table: one block per scope, its rows counted per entity type."""
    if not scopes:
        return "The identifier map is empty: nothing has been imported into this database."
    lines: list[str] = []
    for scope in scopes:
        last = scope.last_imported_at.isoformat(timespec="seconds") if scope.last_imported_at else "unknown"
        lines.append(f"{scope.source_system} @ {scope.source_instance}")
        lines.append(f"  {scope.total} row(s) from {scope.runs} run(s), last written {last}")
        width = max(len(name) for name in scope.counts)
        lines.extend(f"  {name:<{width}}  {count}" for name, count in sorted(scope.counts.items()))
        lines.append("")
    return "\n".join(lines).rstrip()


def _session_factory() -> tuple[async_sessionmaker[AsyncSession], AsyncEngine]:
    engine = create_async_engine(get_settings().database_url, echo=False)
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False), engine


async def _run_import_map_list(args: argparse.Namespace) -> int:
    factory, engine = _session_factory()
    try:
        async with factory() as session:
            scopes = await list_scopes(session)
    finally:
        await engine.dispose()
    print(format_scopes(scopes))
    return 0


def rename_targets(args: argparse.Namespace) -> tuple[str, str, str, str]:
    """Return ``(from_system, from_instance, to_system, to_instance)`` from the ``import-map rename`` options.

    ``--system`` gives both systems; ``--from-system``/``--to-system`` set one
    side each. An instance left out stays as it is.
    """
    from_system = args.from_system or args.system
    to_system = args.to_system or args.system or from_system
    if not from_system:
        raise ScopeRenameError("Give the scope's system with --system (or --from-system)")
    if args.from_instance is None:
        raise ScopeRenameError("Give the scope's current instance with --from")
    to_instance = args.from_instance if args.to_instance is None else args.to_instance
    return from_system, args.from_instance, to_system, to_instance


async def _run_import_map_rename(args: argparse.Namespace) -> int:
    try:
        from_system, from_instance, to_system, to_instance = rename_targets(args)
    except ScopeRenameError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    dry_run = args.dry_run or not args.yes
    factory, engine = _session_factory()
    try:
        async with factory() as session:
            try:
                moved = await rename_scope(
                    session,
                    from_system=from_system,
                    from_instance=from_instance,
                    to_system=to_system,
                    to_instance=to_instance,
                    dry_run=dry_run,
                )
            except ScopeRenameError as exc:
                await session.rollback()
                print(f"Error: {exc}", file=sys.stderr)
                return 1
            await session.commit()
    finally:
        await engine.dispose()
    move = f"{from_system} @ {from_instance} to {to_system} @ {to_instance}"
    if dry_run:
        print(f"{moved} row(s) would move from {move}. Nothing was changed; re-run with --yes to move them.")
    else:
        print(f"Moved {moved} row(s) from {move}.")
    return 0


def _add_import_map_commands(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    import_map = sub.add_parser(
        "import-map",
        help="Inspect and rename the scopes of the import identifier map",
        description=(
            "Every import records which source record became which Specivo row, under a scope: the source "
            "system and instance. A later import with the same scope skips what is already there."
        ),
    )
    import_map_sub = import_map.add_subparsers(dest="import_map_command", required=True)
    import_map_sub.add_parser(
        "list", help="List the scopes: system, instance, rows per entity type, runs and when the last one wrote"
    )
    rename = import_map_sub.add_parser(
        "rename",
        help="Move a scope to a new system and/or instance, so the next import under that key finds its rows",
        description=(
            "Move every row of one scope to another in one transaction. Use it when the next import will be keyed "
            "differently from an earlier one: a direct Redmine import was keyed by its masked database URL, a "
            "bundle by the instance its manifest names. Refused when the target scope already holds rows. "
            "Without --yes it is a dry run: it checks and counts, and changes nothing."
        ),
    )
    rename.add_argument("--system", help="The source system on both sides, e.g. redmine")
    rename.add_argument("--from-system", help="The scope's current system, when it changes (e.g. bundle)")
    rename.add_argument("--to-system", help="The new system, when it changes")
    rename.add_argument(
        "--from", "--from-instance", dest="from_instance", metavar="INSTANCE", help="The scope's current instance"
    )
    rename.add_argument(
        "--to", "--to-instance", dest="to_instance", metavar="INSTANCE", help="The new instance (default: unchanged)"
    )
    rename.add_argument("--dry-run", action="store_true", help="Only check and count (the default without --yes)")
    rename.add_argument("--yes", action="store_true", help="Actually move the rows")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Specivo admin management")
    sub = parser.add_subparsers(dest="command", required=True)

    create_parser = sub.add_parser("create", help="Create admin user")
    create_parser.add_argument("--login", default="admin")
    create_parser.add_argument("--email", default="admin@localhost")
    create_parser.add_argument("--password", default="")

    reset_parser = sub.add_parser("reset-password", help="Reset user password")
    reset_parser.add_argument("--login", required=True)
    reset_parser.add_argument("--password", required=True)

    hashers_parser = sub.add_parser(
        "password-hashers", help="List password hashers, whether each is enabled, and how many accounts use it"
    )
    hashers_sub = hashers_parser.add_subparsers(dest="hashers_command")
    hashers_test = hashers_sub.add_parser("test", help="Check a hasher against a known source row")
    hashers_test.add_argument("algorithm", help="Algorithm id, e.g. redmine_sha1")
    hashers_test.add_argument(
        "--field",
        "--fields",
        action="append",
        metavar="NAME=VALUE",
        help="A source credential column, repeatable (e.g. --field salt=... --field hashed_password=...)",
    )
    hashers_test.add_argument("--password", help="The password to check. Prompted for when omitted.")

    legacy_parser = sub.add_parser("legacy-passwords", help="Report accounts still holding an imported password hash")
    legacy_sub = legacy_parser.add_subparsers(dest="legacy_command")
    legacy_expire = legacy_sub.add_parser(
        "expire", help="Replace legacy hashes with unusable ones; the accounts must use password recovery"
    )
    legacy_expire.add_argument("--algorithm", help="Only expire hashes under this algorithm id")
    legacy_expire.add_argument("--yes", action="store_true", help="Actually do it (otherwise only count)")

    _add_import_map_commands(sub)
    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    args = build_parser().parse_args()

    # Load plugins as the web app does, so plugin-provided hashers are listed.
    from specivo.core.plugin_manager import bootstrap

    bootstrap()

    if args.command == "create":
        asyncio.run(_run_create(args))
    elif args.command == "reset-password":
        asyncio.run(_run_reset(args))
    elif args.command == "password-hashers":
        if args.hashers_command == "test":
            sys.exit(_run_password_hasher_test(args))
        asyncio.run(_run_password_hashers(args))
    elif args.command == "legacy-passwords":
        if args.legacy_command == "expire":
            sys.exit(asyncio.run(_run_legacy_expire(args)))
        asyncio.run(_run_legacy_report(args))
    elif args.command == "import-map":
        runner = _run_import_map_rename if args.import_map_command == "rename" else _run_import_map_list
        sys.exit(asyncio.run(runner(args)))


if __name__ == "__main__":
    main()
