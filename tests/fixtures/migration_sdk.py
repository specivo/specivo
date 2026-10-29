"""Bundles written with the migration SDK (``tools/migration-sdk``), for cross-checking core.

The SDK is a separate project that core does not depend on, so it is imported
from its source directory. Where that directory is absent (a checkout of core
alone) the tests that use it are skipped.
"""

from __future__ import annotations

import importlib
import os
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest

SDK_SRC = Path(__file__).resolve().parents[2] / "tools" / "migration-sdk" / "src"

LOG = b"2024-02-01T10:00:00Z deploy started\n2024-02-01T10:00:05Z login failed\n"
NOTES = b"Runbook notes\n"


def migration_sdk() -> ModuleType:
    """Import ``specivo_migration_sdk`` from its source tree, or skip the test.

    With ``SPECIVO_REQUIRE_MIGRATION_SDK=1`` (set in CI) a missing SDK fails the test
    instead, so the cross-check cannot quietly stop running.
    """
    if not SDK_SRC.is_dir():
        if os.environ.get("SPECIVO_REQUIRE_MIGRATION_SDK") == "1":
            pytest.fail(f"SPECIVO_REQUIRE_MIGRATION_SDK=1 but the migration SDK is not at {SDK_SRC}")
        pytest.skip("tools/migration-sdk is not in this checkout")
    if str(SDK_SRC) not in sys.path:
        sys.path.insert(0, str(SDK_SRC))
    return importlib.import_module("specivo_migration_sdk")


def write_sdk_bundle(target: Path) -> Path:
    """Write, with the SDK, a bundle that uses every file of the format; return its path.

    Logins, identifiers and names start with ``sdk`` so the rows are easy to
    tell from those other tests create.
    """
    sdk = migration_sdk()
    ph = sdk.placeholders
    with sdk.BundleWriter(
        target,
        source_system="sample",
        source_instance="sdk.example.com",
        source_version="4.2",
        generator_name="pytest",
        generator_version="1",
        exported_at=datetime(2024, 3, 1, 9, 30, tzinfo=UTC),
    ) as writer:
        writer.add_all(
            [
                sdk.Tracker(source_ref="bug", name="SDK Bug", default_status_ref="open"),
                sdk.Status(source_ref="open", name="SDK Open", category="backlog"),
                sdk.Status(source_ref="doing", name="SDK Doing", category="active", position=1),
                sdk.Status(source_ref="done", name="SDK Done", category="closed", position=2),
                sdk.Priority(source_ref="normal", name="SDK Normal", is_default=True),
                sdk.Activity(source_ref="dev", name="SDK Development"),
                sdk.Role(source_ref="dev", name="SDK Developer"),
            ]
        )
        writer.add(
            sdk.User(source_ref="u1", login="sdk_alice", display_name="Alice Example", email="sdk_alice@example.com")
        )
        writer.add(
            sdk.User(
                source_ref="u2",
                login="sdk_bob",
                display_name="Bob Example",
                email="sdk_bob@example.com",
                created_at="2023-01-10T08:00:00.000+0700",
            )
        )
        writer.add(sdk.Group(source_ref="g1", name="SDK Team", member_refs=["u1", "u2"]))
        writer.add(sdk.Project(source_ref="p1", identifier="sdk-web", name="SDK Website", is_public=True))
        writer.add(sdk.Project(source_ref="p2", identifier="sdk-api", name="SDK API", parent_ref="p1"))
        writer.add(
            sdk.Membership(
                source_ref="m1", project_ref="p1", principal_kind="group", principal_ref="g1", role_refs=["dev"]
            )
        )
        writer.add(sdk.Version(source_ref="v1", project_ref="p1", name="1.0", effective_date=date(2024, 6, 30)))
        writer.add(sdk.Category(source_ref="c1", project_ref="p1", name="Frontend", assigned_to_ref="u1"))
        writer.add(
            sdk.CustomField(
                source_ref="cf1",
                name="SDK Severity",
                key="sdk_severity",
                field_format="list",
                json_schema={"type": "string", "enum": ["Low", "High"]},
                is_for_all=True,
            )
        )
        writer.add(
            sdk.CustomField(
                source_ref="cf2",
                name="SDK Reviewer",
                key="sdk_reviewer",
                field_format="user",
                json_schema={"type": "integer"},
                is_for_all=True,
            )
        )

        log = writer.add_file(LOG)
        notes = writer.add_file(NOTES)
        writer.add(
            sdk.Issue(
                source_ref="i1",
                project_ref="p1",
                tracker_ref="bug",
                status_ref="doing",
                priority_ref="normal",
                subject="SDK login fails",
                author_ref="u2",
                assigned_to_ref="u1",
                description=(
                    f"Split into {ph.issue('i2')}; see {ph.wiki('w1')}. Log: {ph.attachment_link('a1', 'the log')}."
                ),
                category_ref="c1",
                fixed_version_ref="v1",
                estimated_hours=Decimal("2.5"),
                remaining_estimate=Decimal("0.75"),
                custom_values=[
                    sdk.CustomValue(field_ref="cf1", key="sdk_severity", value="High"),
                    sdk.CustomValue(field_ref="cf2", key="sdk_reviewer", value="u2", value_kind="user_ref"),
                ],
                created_at=datetime(2024, 2, 1, 10, 0, tzinfo=UTC),
                extra={"source_key": "10001"},
            )
        )
        writer.add(
            sdk.Issue(
                source_ref="i2",
                project_ref="p1",
                tracker_ref="bug",
                status_ref="open",
                priority_ref="normal",
                subject="SDK session cookie is not cleared",
                parent_ref="i1",
                created_at="2024-02-01T11:00:00Z",
            )
        )
        writer.add(
            sdk.Issue(
                source_ref="i3",
                project_ref="p2",
                tracker_ref="bug",
                status_ref="open",
                priority_ref="normal",
                subject="SDK rate limit headers are missing",
            )
        )
        writer.add(
            sdk.Journal(
                source_ref="j1",
                issue_ref="i1",
                user_ref="u1",
                notes=f"Reproduced, {ph.user('u2')}.",
                created_at="2024-02-02T17:00:00+07:00",
                details=[sdk.JournalDetail(property="attr", prop_key="status_id", old_value="open", new_value="doing")],
            )
        )
        writer.add(sdk.Relation(source_ref="r1", from_ref="i1", to_ref="i3", relation_type="blocks"))
        writer.add(
            sdk.WikiPage(
                source_ref="w1",
                project_ref="p1",
                title="Home",
                versions=[
                    sdk.WikiVersion(source_ref="w1-1", version=3, text="# Home", author_ref="u1"),
                    sdk.WikiVersion(
                        source_ref="w1-2",
                        version=7,
                        text=f"# Home\n\nSee {ph.wiki('w2')} and {ph.attachment_link('a3', 'the notes')}.",
                        comments="Link the runbook",
                        author_ref="u2",
                        created_at="2024-01-25T10:00:00Z",
                    ),
                ],
            )
        )
        writer.add(
            sdk.WikiPage(
                source_ref="w2",
                project_ref="p1",
                title="Runbook",
                parent_ref="w1",
                versions=[sdk.WikiVersion(source_ref="w2-1", version=1, text="On call: " + ph.user("u2"))],
            )
        )
        writer.add(sdk.WikiRedirect(project_ref="p1", title="Start", redirects_to="Home"))
        writer.add(sdk.Watcher(container_kind="issue", container_ref="i1", user_ref="u2"))
        writer.add(sdk.Watcher(container_kind="wiki_page", container_ref="w1", user_ref="u2"))
        writer.add(
            sdk.Attachment(
                source_ref="a1",
                container_kind="issue",
                container_ref="i1",
                filename="deploy.log",
                sha256=log.sha256,
                filesize=log.size,
                content_type="text/plain",
                author_ref="u2",
            )
        )
        writer.add(
            sdk.Attachment(
                source_ref="a2",
                container_kind="issue",
                container_ref="i2",
                filename="deploy copy.log",
                sha256=log.sha256,
                content_type="text/plain",
            )
        )
        writer.add(
            sdk.Attachment(
                source_ref="a3",
                container_kind="wiki_page",
                container_ref="w1",
                filename="notes.txt",
                sha256=notes.sha256,
                content_type="text/plain",
            )
        )
        writer.add(
            sdk.TimeEntry(
                source_ref="t1",
                project_ref="p1",
                issue_ref="i1",
                user_ref="u1",
                activity_ref="dev",
                hours=Decimal("1.25"),
                spent_on="2024-02-02",
            )
        )
        writer.not_carried_over("Sprint boards have no equivalent", count=2)
    return target
