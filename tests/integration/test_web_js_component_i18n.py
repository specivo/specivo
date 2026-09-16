"""Translated strings reach the Alpine components that display them.

Several components under ``frontend/js/components/`` built status messages,
confirmations, fallback errors and placeholders from English literals in
JavaScript, which the gettext catalogues never see. They now receive those
strings from the template, in one of the two forms the codebase already used:

- an ``i18n`` (or ``labels``) object inside the ``x-data`` expression, whose
  values are ``_()`` strings serialised with ``tojson``;
- ``data-msg-*`` attributes on the component root, read in ``init()``.

Each test renders a page in Thai and checks that the Thai catalogue entry,
not the English msgid, is what the component is handed. ``tojson`` escapes
non-ASCII characters, so ``x-data`` values are compared in their encoded form.

Project keys, identifiers and lookup names carry a module-local prefix: test
modules run in parallel inside uncommitted transactions.
"""

from __future__ import annotations

from html.parser import HTMLParser

import pytest
from httpx import AsyncClient
from jinja2.utils import htmlsafe_json_dumps
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.i18n import activate, deactivate, gettext
from specivo.models.project import EnabledModule, Project
from specivo.models.user import User
from specivo.schemas.issue import IssueCreate
from specivo.schemas.project import ProjectCreate
from specivo.services.issue_service import IssueService
from specivo.services.project_service import ProjectService
from specivo.services.wiki_service import WikiService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.user import UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_project_svc = ProjectService()
_issue_svc = IssueService()
_wiki_svc = WikiService()

_PREFIX = "jsi18n"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _th(msgid: str) -> str:
    """The Thai translation of *msgid*, which must exist and differ from English."""
    activate("th")
    try:
        translated = gettext(msgid)
    finally:
        deactivate()
    assert translated and translated != msgid, f"the Thai catalogue does not translate {msgid!r}"
    return translated


class _ComponentGrabber(HTMLParser):
    """Collects the attributes of every element whose ``x-data`` names *component*."""

    def __init__(self, component: str) -> None:
        super().__init__()
        self._component = component
        self.found: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        x_data = values.get("x-data") or ""
        if x_data.split("(", 1)[0].strip() == self._component:
            self.found.append(values)


def _component(html: str, component: str) -> dict[str, str | None]:
    grabber = _ComponentGrabber(component)
    grabber.feed(html)
    assert grabber.found, f"no x-data element for {component!r} in the rendered page"
    return grabber.found[0]


def _assert_x_data_carries(html: str, component: str, msgids: list[str]) -> None:
    """Every msgid's Thai translation is inside the component's x-data expression."""
    x_data = _component(html, component)["x-data"] or ""
    # A double-quoted attribute or an unescaped apostrophe would cut the
    # expression short (ADR-0001 section 3); the parser would then see a
    # truncated value that no longer ends with the factory call's close.
    assert x_data.rstrip().endswith(")"), f"the {component} x-data attribute was truncated"
    for msgid in msgids:
        encoded = str(htmlsafe_json_dumps(_th(msgid)))[1:-1]
        assert encoded in x_data, f"{component} is not handed the Thai translation of {msgid!r}"


def _assert_data_msgs(html: str, component: str, expected: dict[str, str]) -> None:
    """Each ``data-msg-*`` attribute on the component root holds the Thai translation."""
    attrs = _component(html, component)
    for attr, msgid in expected.items():
        assert attrs.get(attr) == _th(msgid), f"{component} {attr} is not the Thai translation of {msgid!r}"


async def _speak_thai(client: AsyncClient, db: AsyncSession) -> None:
    user: User = client.state.user
    user.language = "th"
    db.add(user)
    await db.commit()


async def _get(client: AsyncClient, url: str) -> str:
    resp = await client.get(url, cookies={"access_token": client.state.token})
    assert resp.status_code == 200, f"GET {url} returned {resp.status_code}"
    return resp.text


async def _make_project(db: AsyncSession, client: AsyncClient, key: str) -> Project:
    data = ProjectCreate(name=f"Thai Strings {key}", identifier=f"{_PREFIX}-{key.lower()}", key=key)
    project = await _project_svc.create(db, data, client.state.user)
    await db.commit()
    await db.refresh(project)
    return project


async def _enable_modules(db: AsyncSession, project: Project, names: list[str]) -> None:
    for name in names:
        db.add(EnabledModule(project_id=project.id, name=name))
    await db.commit()


async def _make_tracker(db: AsyncSession, stem: str):
    status = StatusFactory.build(name=f"{_PREFIX}-{stem}-New", position=1, category="backlog")
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name=f"{_PREFIX}-{stem}-Task", default_status_id=status.id)
    priority = PriorityFactory.build(name=f"{_PREFIX}-{stem}-Normal", position=1)
    db.add_all([tracker, priority])
    await db.commit()
    await db.refresh(tracker)
    return tracker


# ---------------------------------------------------------------------------
# Admin pages
# ---------------------------------------------------------------------------


async def test_admin_settings_components_receive_thai_strings(admin_client: AsyncClient, db_session: AsyncSession):
    await _speak_thai(admin_client, db_session)
    html = await _get(admin_client, "/admin/settings/")

    _assert_x_data_carries(
        html,
        "ftsReindex",
        ["Inherit (%(language)s)", "Language saved.", "Reindex complete.", "%(count)s issues", "%(count)s min ago"],
    )
    _assert_x_data_carries(html, "adminSettings", ["Setting updated.", "Failed to save."])


async def test_admin_metadata_presets_receive_thai_strings(admin_client: AsyncClient, db_session: AsyncSession):
    await _speak_thai(admin_client, db_session)
    html = await _get(admin_client, "/admin/metadata-presets/")

    _assert_x_data_carries(
        html,
        "adminMetadataPresets",
        ['Root schema type must be "object".', "Preset deleted", "Network error: %(error)s", "%(count)s fields"],
    )


async def test_admin_user_pages_receive_thai_strings(admin_client: AsyncClient, db_session: AsyncSession):
    other = UserFactory.build(login=f"{_PREFIX}-target")
    db_session.add(other)
    await db_session.commit()
    await db_session.refresh(other)
    await _speak_thai(admin_client, db_session)

    html = await _get(admin_client, "/admin/users/")
    _assert_x_data_carries(
        html,
        "adminUsers",
        ["Lock user %(login)s?", "Password reset successfully.", "%(count)s hours ago", "Yesterday"],
    )

    html = await _get(admin_client, f"/admin/users/{other.id}/")
    _assert_x_data_carries(
        html,
        "adminUserDetail",
        ["Revoke this API key? This cannot be undone.", "Failed to create key", "%(count)s days ago"],
    )


async def test_admin_projects_and_versions_receive_thai_strings(admin_client: AsyncClient, db_session: AsyncSession):
    await _speak_thai(admin_client, db_session)

    html = await _get(admin_client, "/admin/projects/")
    _assert_x_data_carries(html, "adminProjects", ["Archive %(name)s?", "Failed to save"])

    html = await _get(admin_client, "/admin/versions/")
    _assert_x_data_carries(html, "adminVersions", ['Delete version "%(name)s"?'])


async def test_admin_test_email_form_receives_thai_strings(admin_client: AsyncClient, db_session: AsyncSession):
    await _speak_thai(admin_client, db_session)
    html = await _get(admin_client, "/admin/email/")

    _assert_data_msgs(
        html,
        "testEmailForm",
        {
            "data-msg-subject": "Specivo test email",
            "data-msg-sent": "Test email sent to %(email)s",
            "data-msg-request-failed": "Request failed: %(error)s",
        },
    )


# ---------------------------------------------------------------------------
# Account pages
# ---------------------------------------------------------------------------


async def test_api_key_manager_receives_thai_strings(admin_client: AsyncClient, db_session: AsyncSession):
    await _speak_thai(admin_client, db_session)
    html = await _get(admin_client, "/my/api-keys/")

    _assert_data_msgs(
        html,
        "apiKeyManager",
        {
            "data-msg-create-failed": "Failed to create key",
            "data-msg-confirm-delete": "Are you sure you want to permanently delete this API key?",
        },
    )


async def test_forgot_password_rate_limit_message_is_thai(unauth_client: AsyncClient):
    resp = await unauth_client.get("/forgot-password/", cookies={"specivo_lang": "th"})
    assert resp.status_code == 200

    _assert_data_msgs(resp.text, "forgotPasswordForm", {"data-msg-rate-limited": "Too many requests. Please wait."})


# ---------------------------------------------------------------------------
# Project pages
# ---------------------------------------------------------------------------


async def test_project_settings_tabs_receive_thai_strings(admin_client: AsyncClient, db_session: AsyncSession):
    project = await _make_project(db_session, admin_client, "JSISET")
    await _speak_thai(admin_client, db_session)
    html = await _get(admin_client, f"/projects/{project.key}/settings/")

    _assert_x_data_carries(html, "projectGeneralSettings", ["Saved successfully.", "Error: %(message)s"])
    _assert_x_data_carries(html, "projectModules", ["Module updated."])
    _assert_x_data_carries(html, "projectVersions", ['Version "%(name)s" created.', "Failed to delete version."])
    _assert_x_data_carries(html, "recurringPatterns", ["Every %(count)s weeks", "Pattern deleted."])
    _assert_x_data_carries(
        html,
        "projectMetadataSettings",
        ["(all trackers)", "Cannot delete: %(count)s issue(s) use this schema."],
    )
    _assert_x_data_carries(html, "projectComputedMetadata", ["Duplicate field name: %(name)s"])
    _assert_x_data_carries(html, "projectTagsSettings", ["Failed to save tag."])
    _assert_x_data_carries(html, "ftsReindex", ["Reindex started.", "%(count)s chunks"])


async def test_project_create_modals_receive_thai_strings(admin_client: AsyncClient, db_session: AsyncSession):
    project = await _make_project(db_session, admin_client, "JSIMOD")
    await _speak_thai(admin_client, db_session)

    html = await _get(admin_client, "/projects/")
    _assert_x_data_carries(html, "projectCreateModal", ["Failed to create project"])

    # The subproject modal used a double-quoted x-data; it now carries tojson
    # output, so it must be single-quoted to survive.
    html = await _get(admin_client, f"/projects/{project.key}/")
    _assert_x_data_carries(html, "projectCreateModal", ["Failed to create project"])


async def test_issue_pages_receive_thai_strings(admin_client: AsyncClient, db_session: AsyncSession):
    project = await _make_project(db_session, admin_client, "JSIISS")
    tracker = await _make_tracker(db_session, "issue")
    issue = await _issue_svc.create(
        db_session,
        project,
        IssueCreate(project_key=project.key, tracker_id=tracker.id, subject="Broken export"),
        admin_client.state.user,
    )
    await db_session.commit()
    await db_session.refresh(issue)
    await _speak_thai(admin_client, db_session)

    html = await _get(admin_client, f"/issue/{issue.display_key}/")
    _assert_x_data_carries(html, "descriptionEditor", ["Title cannot be empty.", "Network error. Please retry."])
    _assert_x_data_carries(html, "markdownEditor", ["Rendering...", "Preview unavailable."])
    _assert_x_data_carries(html, "timeLogForm", ["Enter at least 1 minute"])
    _assert_x_data_carries(html, "relationForm", ["Failed to add relation"])

    html = await _get(admin_client, f"/projects/{project.key}/issues/new/")
    _assert_x_data_carries(html, "issueForm", ["Blocked by", "Duplicated by"])

    html = await _get(admin_client, f"/partials/issues/{issue.display_key}/attachments/")
    _assert_data_msgs(
        html,
        "issueAttachments",
        {"data-msg-upload-failed": "Upload failed", "data-msg-file-label": "FILE"},
    )


async def test_wiki_pages_receive_thai_strings(admin_client: AsyncClient, db_session: AsyncSession):
    project = await _make_project(db_session, admin_client, "JSIWIK")
    page, _content = await _wiki_svc.create_page(
        db_session, project.id, "Release notes", "Notes for the next release.", admin_client.state.user
    )
    await db_session.commit()
    await _speak_thai(admin_client, db_session)

    html = await _get(admin_client, f"/projects/{project.key}/wiki/{page.slug}/edit/")
    _assert_x_data_carries(html, "wikiForm", ["Failed to save"])
    _assert_x_data_carries(html, "markdownEditor", ["Write your content here (Markdown)...", "Rendering..."])

    html = await _get(admin_client, f"/projects/{project.key}/wiki/{page.slug}/")
    _assert_data_msgs(
        html,
        "wikiAttachments",
        {"data-msg-delete-failed": "Delete failed", "data-msg-connect-failed": "Unable to connect."},
    )


async def test_sprint_edit_duration_labels_are_thai(admin_client: AsyncClient, db_session: AsyncSession):
    project = await _make_project(db_session, admin_client, "JSISPR")
    resp = await admin_client.post(
        f"/api/v1/projects/{project.key}/sprints/",
        json={"name": "Sprint 1", "start_date": "2026-04-01", "end_date": "2026-04-14"},
    )
    assert resp.status_code == 201, resp.text
    sprint = resp.json()
    await _speak_thai(admin_client, db_session)

    html = await _get(admin_client, f"/projects/{project.key}/sprints/{sprint['id']}/edit/")
    _assert_x_data_carries(html, "sprintEdit", ["%(count)s days", "%(days)s (~%(weeks)s)", "Invalid range"])


async def test_recurring_pages_receive_thai_strings(admin_client: AsyncClient, db_session: AsyncSession):
    project = await _make_project(db_session, admin_client, "JSIREC")
    tracker = await _make_tracker(db_session, "recurring")
    resp = await admin_client.post(
        f"/api/v1/projects/{project.key}/recurring-patterns/",
        json={
            "name": "Weekly report",
            "template_tracker_id": tracker.id,
            "template_subject": "Weekly report",
            "freq": "weekly",
            "rrule_interval": 2,
            "byday": ["MO"],
            "dtstart": "2026-01-05T09:00:00+07:00",
            "timezone": "Asia/Bangkok",
            "creation_lead_time_days": 30,
        },
    )
    assert resp.status_code == 201, resp.text
    pattern = resp.json()
    await _speak_thai(admin_client, db_session)

    html = await _get(admin_client, f"/projects/{project.key}/recurring-patterns/")
    _assert_x_data_carries(html, "recurringPatterns", ["Every %(count)s weeks", "Pattern enabled."])

    html = await _get(admin_client, f"/projects/{project.key}/recurring-patterns/{pattern['id']}/")
    _assert_x_data_carries(html, "recurringPatternDetail", ["Occurrence skipped.", "Failed to skip occurrence."])
