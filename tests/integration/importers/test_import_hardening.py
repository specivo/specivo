"""The gaps between a validated bundle and its load: stored files, odd strings, logins."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from sqlalchemy import select

from specivo.importers.bundle.adapter import BundleSourceAdapter
from specivo.importers.bundle.report import ProblemCode, ValidationReport
from specivo.importers.bundle.scan import scan_bundle
from specivo.models.attachment import Attachment
from specivo.models.issue import Issue
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.attachment_service import AttachmentService
from tests.factories.user import UserFactory
from tests.fixtures.import_bundle import issue, project, stored, user, write_bundle

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

LOG = b"deploy started\ndeploy finished\n"


def _attachment(sha: str, **fields) -> dict:
    return {
        "source_ref": "a1",
        "container_kind": "issue",
        "container_ref": "i1",
        "filename": "deploy.log",
        "sha256": sha,
        "content_type": "text/plain",
        **fields,
    }


async def test_a_stored_file_changed_after_the_scan_is_not_imported(
    db_session, build_bundle_import, tmp_path, monkeypatch
) -> None:
    sha = stored(LOG)
    root = write_bundle(
        tmp_path / "b",
        projects=[project("p1", identifier="swapped")],
        issues=[issue("i1")],
        attachments=[_attachment(sha)],
        files={sha: LOG},
    )
    secret = tmp_path / "secret.env"
    secret.write_bytes(b"SECRET_KEY=do-not-ship\n")
    # The scan saw the right bytes; by load time the path holds something else.
    monkeypatch.setattr(BundleSourceAdapter, "resolve_attachment_path", lambda _self, _ir: secret)

    summary = await build_bundle_import(root).run()

    assert (await db_session.execute(select(Attachment))).scalars().all() == []
    assert any(w.message.startswith("Attachment file could not be read") for w in summary.warnings)


async def test_upload_from_path_refuses_links_and_mismatched_bytes(db_session, tmp_path, monkeypatch) -> None:
    import specivo.services.attachment_service as attachment_service

    monkeypatch.setattr(attachment_service, "_upload_dir", tmp_path / "store")
    author = UserFactory.build(login="hard_author", status="active")
    db_session.add(author)
    await db_session.flush()
    real = tmp_path / "real.log"
    real.write_bytes(LOG)
    link = tmp_path / "link.log"
    link.symlink_to(real)
    svc = AttachmentService()

    with pytest.raises(OSError):
        await svc.upload_from_path(db_session, "Issue", 1, link, "x.log", author)
    with pytest.raises(OSError, match="does not match its sha256"):
        await svc.upload_from_path(db_session, "Issue", 1, real, "x.log", author, expected_sha256="0" * 64)
    # Nothing half-written is left in the store.
    assert [p.name for p in (tmp_path / "store").iterdir()] == []


async def test_strings_the_columns_cannot_hold_do_not_break_the_load(db_session, build_bundle_import, tmp_path) -> None:
    root = write_bundle(
        tmp_path / "b",
        projects=[project("p1", identifier="a" * 99 + "-tail", name="N" * 300)],
        issues=[
            issue("i1", subject="Null\u0000byte"),
            issue("i2", subject="   "),
            issue("i3", subject="S" * 2000),
        ],
    )

    await build_bundle_import(root).run()

    (proj,) = (await db_session.execute(select(Project).where(Project.name.like("NNN%")))).scalars().all()
    assert len(proj.name) == 255
    assert not proj.identifier.endswith("-")
    subjects = {
        i.subject for i in (await db_session.execute(select(Issue).where(Issue.project_id == proj.id))).scalars()
    }
    assert "Nullbyte" in subjects
    assert "Imported issue i2" in subjects
    assert "S" * 1024 in subjects


async def test_imported_logins_follow_the_login_rule(db_session, build_bundle_import, tmp_path) -> None:
    root = write_bundle(
        tmp_path / "b",
        users=[
            user("u1", login="аlex_homoglyph"),  # Cyrillic а
            user("u2", login="John.Doe"),
            user("u3", login="..."),
        ],
        projects=[project("p1", identifier="logins")],
    )

    summary = await build_bundle_import(root).run()

    logins = {u.login for u in (await db_session.execute(select(User))).scalars()}
    assert "lex_homoglyph" in logins and "аlex_homoglyph" not in logins
    assert "john-doe" in logins
    assert any(re.fullmatch(r"user-u3", login) for login in logins)
    assert all(re.fullmatch(r"[a-z0-9_$-]+", login) for login in logins if not login.startswith("$"))
    assert any(w.message == "Login changed to the allowed characters" for w in summary.warnings)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads files whatever their mode")
async def test_scan_reports_an_unreadable_stored_file_against_that_file(tmp_path) -> None:
    sha = stored(LOG)
    other = b"second file\n"
    root = write_bundle(
        tmp_path / "b",
        projects=[project("p1")],
        issues=[issue("i1")],
        attachments=[_attachment(sha), _attachment(stored(other), source_ref="a2", sha256="f" * 64)],
        files={sha: LOG},
    )
    (root / "files" / sha).chmod(0)
    report = ValidationReport(path=str(root))
    try:
        scan_bundle(Path(root), report)
    finally:
        (root / "files" / sha).chmod(0o644)

    unreadable = [p for p in report.problems if p.code == ProblemCode.UNREADABLE]
    assert [p.file for p in unreadable] == [f"files/{sha}"]
    # The rest of attachments.jsonl was still checked.
    assert any(p.code == ProblemCode.MISSING_FILE for p in report.problems)
