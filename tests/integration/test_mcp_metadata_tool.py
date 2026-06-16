"""Integration tests for the specivo_metadata MCP tool."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import PermissionDeniedError
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.schemas.issue import IssueCreate
from specivo.services.issue_service import IssueService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.serial]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def tracker(db_session: AsyncSession, status) -> Tracker:
    t = TrackerFactory.build(name="Bug", default_status_id=status.id)
    db_session.add(t)
    await db_session.commit()
    await db_session.refresh(t)
    return t


@pytest_asyncio.fixture
async def priority(db_session: AsyncSession) -> IssuePriority:
    p = PriorityFactory.build(name="Normal", is_default=True, position=2)
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="MMTA", name="Metadata MCP Test", is_public=True)
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="mmt_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def limited_user(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="mmt_viewer", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def role_view_only(db_session: AsyncSession) -> Role:
    from sqlalchemy import select

    result = await db_session.execute(select(Role).where(Role.name == "MMTViewOnly"))
    existing = result.scalar_one_or_none()
    if existing:
        return existing
    role = Role(
        name="MMTViewOnly",
        position=12,
        permissions=["view_issues"],
        issues_visibility="default",
        builtin=0,
    )
    db_session.add(role)
    await db_session.commit()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def issue(db_session, project, tracker, priority, status, admin):
    svc = IssueService()
    issue = await svc.create(
        db_session,
        project,
        IssueCreate(project_key=project.key, tracker_id=tracker.id, subject="MCP metadata"),
        admin,
    )
    await db_session.commit()
    return issue


async def _add_member(db, project, user, role):
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    mr = MemberRole(member_id=member.id, role_id=role.id)
    db.add(mr)
    await db.commit()


# ---------------------------------------------------------------------------
# set
# ---------------------------------------------------------------------------


class TestMetadataSet:
    async def test_set_new_key(self, db_session, admin, issue):
        from specivo.mcp.tools import _metadata

        result = await _metadata(
            db_session, admin, issue.display_key, "severity", "set", "high"
        )
        assert "Error" not in result
        assert "severity" in result
        await db_session.refresh(issue)
        assert issue.issue_metadata == {"severity": "high"}

    async def test_set_with_scheme_prefix(self, db_session, admin, issue):
        from specivo.mcp.tools import _metadata

        ref = f"issue:{issue.display_key}"
        result = await _metadata(db_session, admin, ref, "env", "set", "prod")
        assert "Error" not in result
        await db_session.refresh(issue)
        assert issue.issue_metadata == {"env": "prod"}

    async def test_set_overwrites_existing(self, db_session, admin, issue):
        from specivo.mcp.tools import _metadata

        await _metadata(db_session, admin, issue.display_key, "k", "set", "a")
        await _metadata(db_session, admin, issue.display_key, "k", "set", "b")
        await db_session.refresh(issue)
        assert issue.issue_metadata == {"k": "b"}

    async def test_set_complex_value(self, db_session, admin, issue):
        from specivo.mcp.tools import _metadata

        result = await _metadata(
            db_session, admin, issue.display_key, "obj", "set", {"a": 1, "b": [2, 3]}
        )
        assert "Error" not in result
        await db_session.refresh(issue)
        assert issue.issue_metadata == {"obj": {"a": 1, "b": [2, 3]}}


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


class TestMetadataDelete:
    async def test_delete_existing(self, db_session, admin, issue):
        from specivo.mcp.tools import _metadata

        await _metadata(db_session, admin, issue.display_key, "k", "set", "v")
        result = await _metadata(db_session, admin, issue.display_key, "k", "delete")
        assert "Error" not in result
        await db_session.refresh(issue)
        assert issue.issue_metadata == {}

    async def test_delete_missing_is_noop(self, db_session, admin, issue):
        from specivo.mcp.tools import _metadata

        result = await _metadata(db_session, admin, issue.display_key, "nope", "delete")
        assert "Error" not in result
        await db_session.refresh(issue)
        assert issue.issue_metadata == {}


# ---------------------------------------------------------------------------
# append
# ---------------------------------------------------------------------------


class TestMetadataAppend:
    async def test_append_to_missing_creates_list(self, db_session, admin, issue):
        from specivo.mcp.tools import _metadata

        result = await _metadata(db_session, admin, issue.display_key, "tags", "append", "x")
        assert "Error" not in result
        await db_session.refresh(issue)
        assert issue.issue_metadata == {"tags": ["x"]}

    async def test_append_scalar_pushes_one(self, db_session, admin, issue):
        from specivo.mcp.tools import _metadata

        await _metadata(db_session, admin, issue.display_key, "tags", "set", ["a"])
        await _metadata(db_session, admin, issue.display_key, "tags", "append", "b")
        await db_session.refresh(issue)
        assert issue.issue_metadata == {"tags": ["a", "b"]}

    async def test_append_list_extends(self, db_session, admin, issue):
        from specivo.mcp.tools import _metadata

        await _metadata(db_session, admin, issue.display_key, "tags", "set", ["a"])
        await _metadata(db_session, admin, issue.display_key, "tags", "append", ["b", "c"])
        await db_session.refresh(issue)
        assert issue.issue_metadata == {"tags": ["a", "b", "c"]}

    async def test_append_to_non_array_errors(self, db_session, admin, issue):
        from specivo.mcp.tools import _metadata

        await _metadata(db_session, admin, issue.display_key, "k", "set", "string_value")
        result = await _metadata(db_session, admin, issue.display_key, "k", "append", "x")
        assert result.startswith("Error")
        assert "append" in result


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


class TestMetadataRemove:
    async def test_remove_scalar_drops_item(self, db_session, admin, issue):
        from specivo.mcp.tools import _metadata

        await _metadata(db_session, admin, issue.display_key, "tags", "set", ["a", "b", "c"])
        await _metadata(db_session, admin, issue.display_key, "tags", "remove", "b")
        await db_session.refresh(issue)
        assert issue.issue_metadata == {"tags": ["a", "c"]}

    async def test_remove_list_drops_all_matching(self, db_session, admin, issue):
        from specivo.mcp.tools import _metadata

        await _metadata(db_session, admin, issue.display_key, "tags", "set", ["a", "b", "c"])
        await _metadata(db_session, admin, issue.display_key, "tags", "remove", ["a", "c"])
        await db_session.refresh(issue)
        assert issue.issue_metadata == {"tags": ["b"]}

    async def test_remove_nonmatching_noop(self, db_session, admin, issue):
        from specivo.mcp.tools import _metadata

        await _metadata(db_session, admin, issue.display_key, "tags", "set", ["a"])
        result = await _metadata(db_session, admin, issue.display_key, "tags", "remove", "z")
        assert "Error" not in result
        await db_session.refresh(issue)
        assert issue.issue_metadata == {"tags": ["a"]}

    async def test_remove_missing_key_noop(self, db_session, admin, issue):
        from specivo.mcp.tools import _metadata

        result = await _metadata(db_session, admin, issue.display_key, "nope", "remove", "x")
        assert "Error" not in result

    async def test_remove_from_non_array_errors(self, db_session, admin, issue):
        from specivo.mcp.tools import _metadata

        await _metadata(db_session, admin, issue.display_key, "k", "set", "str")
        result = await _metadata(db_session, admin, issue.display_key, "k", "remove", "x")
        assert result.startswith("Error")


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestMetadataErrors:
    async def test_invalid_op(self, db_session, admin, issue):
        from specivo.mcp.tools import _metadata

        result = await _metadata(db_session, admin, issue.display_key, "k", "clobber", "x")
        assert result.startswith("Error")
        assert "invalid op" in result

    async def test_empty_key(self, db_session, admin, issue):
        from specivo.mcp.tools import _metadata

        result = await _metadata(db_session, admin, issue.display_key, "", "set", "x")
        assert result.startswith("Error")

    async def test_unknown_scheme(self, db_session, admin, issue):
        from specivo.mcp.tools import _metadata

        result = await _metadata(db_session, admin, "nosuch:xyz", "k", "set", "v")
        assert result.startswith("Error")
        assert "scheme" in result

    async def test_missing_entity(self, db_session, admin):
        from specivo.mcp.tools import _metadata

        result = await _metadata(db_session, admin, "ZZZZ-999", "k", "set", "v")
        assert result.startswith("Error")
        assert "not found" in result

    async def test_permission_denied_raises(
        self, db_session, limited_user, role_view_only, project, issue
    ):
        from specivo.mcp.tools import _metadata

        await _add_member(db_session, project, limited_user, role_view_only)
        with pytest.raises(PermissionDeniedError, match="edit_issues"):
            await _metadata(db_session, limited_user, issue.display_key, "k", "set", "v")

    async def test_oversize_blob_rejected(self, db_session, admin, issue):
        from specivo.mcp.tools import _metadata

        big = "x" * (17 * 1024)
        result = await _metadata(db_session, admin, issue.display_key, "blob", "set", big)
        assert result.startswith("Error")
        assert "16384" in result or "exceed" in result
        await db_session.refresh(issue)
        assert "blob" not in issue.issue_metadata


# ---------------------------------------------------------------------------
# Lossy-number guard (identifier-shaped tokens parsed as JSON numbers)
# ---------------------------------------------------------------------------


class TestMetadataLossyNumberGuard:
    async def test_set_scientific_float_rejected(self, db_session, admin, issue):
        from specivo.mcp.tools import _metadata

        result = await _metadata(db_session, admin, issue.display_key, "commits", "set", 4.983e31)
        assert result.startswith("Error")
        assert "lost precision" in result
        await db_session.refresh(issue)
        assert "commits" not in issue.issue_metadata

    async def test_append_scientific_float_rejected(self, db_session, admin, issue):
        from specivo.mcp.tools import _metadata

        result = await _metadata(
            db_session, admin, issue.display_key, "commits", "append", 1.0e20
        )
        assert result.startswith("Error")
        assert "lost precision" in result
        await db_session.refresh(issue)
        assert "commits" not in issue.issue_metadata

    async def test_inf_rejected(self, db_session, admin, issue):
        from specivo.mcp.tools import _metadata

        result = await _metadata(
            db_session, admin, issue.display_key, "k", "set", float("inf")
        )
        assert result.startswith("Error")
        assert "lost precision" in result

    async def test_nan_rejected(self, db_session, admin, issue):
        from specivo.mcp.tools import _metadata

        result = await _metadata(
            db_session, admin, issue.display_key, "k", "set", float("nan")
        )
        assert result.startswith("Error")
        assert "lost precision" in result

    async def test_list_with_scientific_float_rejected(self, db_session, admin, issue):
        from specivo.mcp.tools import _metadata

        result = await _metadata(
            db_session,
            admin,
            issue.display_key,
            "commits",
            "append",
            ["abc1234567", 4.983e31],
        )
        assert result.startswith("Error")
        assert "lost precision" in result
        await db_session.refresh(issue)
        assert "commits" not in issue.issue_metadata

    async def test_integer_passes_through(self, db_session, admin, issue):
        from specivo.mcp.tools import _metadata

        result = await _metadata(db_session, admin, issue.display_key, "score", "set", 42)
        assert "Error" not in result
        await db_session.refresh(issue)
        assert issue.issue_metadata == {"score": 42}

    async def test_plain_float_passes_through(self, db_session, admin, issue):
        from specivo.mcp.tools import _metadata

        result = await _metadata(db_session, admin, issue.display_key, "ratio", "set", 1.5)
        assert "Error" not in result
        await db_session.refresh(issue)
        assert issue.issue_metadata == {"ratio": 1.5}

    async def test_quoted_sha_string_passes_through(self, db_session, admin, issue):
        from specivo.mcp.tools import _metadata

        # The agent's correct workaround: pass the SHA-shaped token as a string.
        result = await _metadata(
            db_session,
            admin,
            issue.display_key,
            "tags",
            "append",
            "49830031000000000000000000000000000e99999",
        )
        assert "Error" not in result
        await db_session.refresh(issue)
        assert issue.issue_metadata == {
            "tags": ["49830031000000000000000000000000000e99999"]
        }


# ---------------------------------------------------------------------------
# Journal integration
# ---------------------------------------------------------------------------


class TestMetadataGet:
    async def test_metadata_get_returns_value_when_set(self, db_session, admin, issue):
        from specivo.mcp.tools import _metadata

        await _metadata(db_session, admin, issue.display_key, "owner", "set", "alice")
        result = await _metadata(db_session, admin, issue.display_key, "owner", "get")
        assert result == '"alice"'

    async def test_metadata_get_returns_not_set_when_missing(
        self, db_session, admin, issue
    ):
        from specivo.mcp.tools import _metadata

        result = await _metadata(db_session, admin, issue.display_key, "nope", "get")
        assert result == "(not set)"

    async def test_metadata_get_returns_complex_value_as_json(
        self, db_session, admin, issue
    ):
        import json

        from specivo.mcp.tools import _metadata

        await _metadata(
            db_session, admin, issue.display_key, "tags", "set", ["a", "b"]
        )
        await _metadata(
            db_session, admin, issue.display_key, "obj", "set", {"x": 1}
        )

        list_result = await _metadata(
            db_session, admin, issue.display_key, "tags", "get"
        )
        dict_result = await _metadata(
            db_session, admin, issue.display_key, "obj", "get"
        )
        assert json.loads(list_result) == ["a", "b"]
        assert json.loads(dict_result) == {"x": 1}

    async def test_metadata_get_uses_read_permission_not_edit(
        self, db_session, admin, limited_user, role_view_only, project, issue
    ):
        """A user with only view_issues (not edit_issues) can read metadata.

        This proves the 'get' op is gated on read_permission rather than the
        write permission that mutating ops require.
        """
        from specivo.mcp.tools import _metadata

        await _metadata(db_session, admin, issue.display_key, "owner", "set", "alice")
        await _add_member(db_session, project, limited_user, role_view_only)

        # View-only user must NOT be able to set.
        with pytest.raises(PermissionDeniedError, match="edit_issues"):
            await _metadata(
                db_session, limited_user, issue.display_key, "owner", "set", "bob"
            )

        # But they MUST be able to get.
        result = await _metadata(
            db_session, limited_user, issue.display_key, "owner", "get"
        )
        assert result == '"alice"'

    async def test_metadata_get_does_not_bump_lock_version(
        self, db_session, admin, issue
    ):
        from specivo.mcp.tools import _metadata

        await _metadata(db_session, admin, issue.display_key, "k", "set", "v")
        await db_session.refresh(issue)
        before = issue.lock_version

        await _metadata(db_session, admin, issue.display_key, "k", "get")
        await db_session.refresh(issue)
        assert issue.lock_version == before

    async def test_metadata_get_does_not_write_audit_log(
        self, db_session, admin, issue
    ):
        from sqlalchemy import func, select

        from specivo.mcp.tools import _metadata
        from specivo.models.security_audit import SecurityAuditLog

        await _metadata(db_session, admin, issue.display_key, "k", "set", "v")
        await db_session.flush()

        before = (
            await db_session.execute(
                select(func.count()).select_from(SecurityAuditLog)
            )
        ).scalar_one()

        await _metadata(db_session, admin, issue.display_key, "k", "get")
        await db_session.flush()

        after = (
            await db_session.execute(
                select(func.count()).select_from(SecurityAuditLog)
            )
        ).scalar_one()
        assert after == before


class TestMetadataJournal:
    async def test_set_creates_journal_entry(self, db_session, admin, issue):
        from sqlalchemy import select

        from specivo.mcp.tools import _metadata
        from specivo.models.journal import Journal, JournalDetail

        await _metadata(db_session, admin, issue.display_key, "severity", "set", "high")
        await db_session.flush()

        rows = (
            await db_session.execute(
                select(JournalDetail)
                .join(Journal, Journal.id == JournalDetail.journal_id)
                .where(Journal.issue_id == issue.id, JournalDetail.prop_key == "issue_metadata")
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].new_value is not None
        assert "severity" in rows[0].new_value
