"""Service tests for the legacy-password admin tooling in ``specivo.cli.admin``.

The report an administrator uses to see who still holds an imported hash, and
the bulk expiry that forces those accounts through password recovery.
"""

from __future__ import annotations

import pytest

import specivo.services.password_hashers as hashers_mod
from specivo.cli.admin import _hash_counts, expire_legacy_passwords, legacy_password_rows
from specivo.core.config import get_settings
from specivo.services.auth_utils import is_legacy_hash, verify_password
from specivo.services.password_hashers import ForeignBcryptHasher, RedmineSHA1WrappedHasher
from tests.factories.user import TEST_PASSWORD, ServiceAccountFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]

SALT = "00112233445566778899aabbccddeeff"
DIGEST = "aa" * 20


@pytest.fixture(autouse=True)
def _cheap_wrap(monkeypatch):
    monkeypatch.setattr(get_settings(), "legacy_password_wrap_rounds", 4)
    monkeypatch.setattr(hashers_mod, "_registry", None)


@pytest.fixture
async def accounts(db_session):
    import bcrypt

    php = "$2y$" + bcrypt.hashpw(b"x", bcrypt.gensalt(4)).decode()[4:]
    users = {
        "lp_redmine": UserFactory.build(
            login="lp_redmine",
            password_hash=RedmineSHA1WrappedHasher().from_source({"salt": SALT, "hashed_password": DIGEST}),
        ),
        "lp_foreign": UserFactory.build(
            login="lp_foreign", password_hash=ForeignBcryptHasher().from_source({"hash": php})
        ),
        "lp_native": UserFactory.build(login="lp_native"),
    }
    for user in users.values():
        db_session.add(user)
    await db_session.commit()
    return users


class TestReport:
    async def test_lists_only_legacy_hashes_with_their_algorithm(self, db_session, accounts):
        rows = {login: algorithm for login, _status, algorithm, _seen in await legacy_password_rows(db_session)}
        assert rows["lp_redmine"] == "redmine_sha1"
        assert rows["lp_foreign"] == "bcrypt_foreign"
        assert "lp_native" not in rows

    async def test_counts_by_algorithm(self, db_session, accounts):
        counts = await _hash_counts(db_session)
        assert counts["redmine_sha1"] >= 1
        assert counts["bcrypt_foreign"] >= 1
        assert counts["bcrypt"] >= 1


class TestExpire:
    async def test_limited_to_one_algorithm(self, db_session, accounts):
        expired = await expire_legacy_passwords(db_session, "redmine_sha1")
        await db_session.commit()
        assert expired >= 1

        for user in accounts.values():
            await db_session.refresh(user)
        assert not is_legacy_hash(accounts["lp_redmine"].password_hash)
        assert accounts["lp_redmine"].must_change_password is True
        assert is_legacy_hash(accounts["lp_foreign"].password_hash)

    async def test_every_legacy_hash_becomes_unusable(self, db_session, accounts):
        await expire_legacy_passwords(db_session)
        await db_session.commit()

        for user in accounts.values():
            await db_session.refresh(user)
        assert await legacy_password_rows(db_session) == []
        assert accounts["lp_foreign"].must_change_password is True
        assert not verify_password("x", accounts["lp_foreign"].password_hash)
        # A native account is untouched.
        assert accounts["lp_native"].must_change_password is False
        assert verify_password(TEST_PASSWORD, accounts["lp_native"].password_hash)

    async def test_a_service_account_just_loses_the_hash(self, db_session):
        bot = ServiceAccountFactory.build(
            login="lp_bot",
            password_hash=RedmineSHA1WrappedHasher().from_source({"salt": SALT, "hashed_password": DIGEST}),
        )
        db_session.add(bot)
        await db_session.commit()

        await expire_legacy_passwords(db_session)
        await db_session.commit()
        await db_session.refresh(bot)
        assert bot.password_hash is None
        assert bot.must_change_password is False
