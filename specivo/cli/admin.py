"""Admin user management: create and reset password.

Usage::

    # Create admin (from bootstrap file — used by entrypoint)
    uv run python -m specivo.cli.admin create

    # Create admin (explicit args)
    uv run python -m specivo.cli.admin create --login admin --email admin@localhost --password secret

    # Reset password
    uv run python -m specivo.cli.admin reset-password --login admin --password newpass

The ``create`` subcommand is idempotent: existing admins are skipped,
non-admin users are promoted.  When no CLI args are given it reads from
``/app/data/.bootstrap.json`` and deletes the file after success.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from specivo.core.config import get_settings
from specivo.core.utils import utcnow
from specivo.models.user import User
from specivo.services.auth_utils import hash_password

logger = logging.getLogger(__name__)

BOOTSTRAP_PATH = Path("/app/data/.bootstrap.json")


async def _create_admin(
    session: AsyncSession,
    login: str,
    email: str,
    password: str,
) -> None:
    """Create or promote an admin user."""
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

    user.password_hash = hash_password(password)
    user.password_changed_at = utcnow()
    user.failed_login_count = 0
    user.locked_until = None
    await session.commit()
    print(f"Password reset for user '{login}'.")


async def _run_create(args: argparse.Namespace) -> None:
    login = args.login
    email = args.email
    password = args.password
    bootstrap_used = False

    if not password and BOOTSTRAP_PATH.exists():
        data = json.loads(BOOTSTRAP_PATH.read_text())
        login = login or data.get("login", "admin")
        email = email or data.get("email", "admin@localhost")
        password = data.get("password", "")
        bootstrap_used = True

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

    if bootstrap_used and BOOTSTRAP_PATH.exists():
        BOOTSTRAP_PATH.unlink()
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


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Specivo admin management")
    sub = parser.add_subparsers(dest="command", required=True)

    create_parser = sub.add_parser("create", help="Create admin user")
    create_parser.add_argument("--login", default="admin")
    create_parser.add_argument("--email", default="admin@localhost")
    create_parser.add_argument("--password", default="")

    reset_parser = sub.add_parser("reset-password", help="Reset user password")
    reset_parser.add_argument("--login", required=True)
    reset_parser.add_argument("--password", required=True)

    args = parser.parse_args()

    if args.command == "create":
        asyncio.run(_run_create(args))
    elif args.command == "reset-password":
        asyncio.run(_run_reset(args))


if __name__ == "__main__":
    main()
