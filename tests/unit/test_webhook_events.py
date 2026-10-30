"""Pure logic of outgoing webhooks: payloads, change sets, event names, signatures, backoff."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from specivo.core.constants import (
    WEBHOOK_BACKOFF_BASE_SECONDS,
    WEBHOOK_BACKOFF_MAX_SECONDS,
    WEBHOOK_MAX_ATTEMPTS,
)
from specivo.core.net_safety import is_public_address
from specivo.schemas.common import IdName
from specivo.schemas.issue import IssueOut
from specivo.schemas.journal import JournalOut
from specivo.schemas.webhook import WebhookCreate, WebhookEvent, WebhookUpdate
from specivo.schemas.wiki import WikiPageWithContent
from specivo.services import webhook_events as we

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def _issue(**overrides) -> IssueOut:
    values = {
        "id": 7,
        "key": "WEB-3",
        "project_key": "WEB",
        "subject": "Login fails",
        "description": "Steps to reproduce",
        "tracker": IdName(id=1, name="Bug"),
        "status": IdName(id=2, name="New"),
        "priority": IdName(id=3, name="Normal"),
        "author": IdName(id=4, name="Alice Example"),
        "assigned_to": None,
        "done_ratio": 0,
        "start_date": None,
        "due_date": None,
        "estimated_hours": None,
        "metadata": {},
        "is_private": False,
        "lock_version": 1,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    values.update(overrides)
    return IssueOut(**values)


def _journal(**overrides) -> JournalOut:
    values = {
        "id": 11,
        "sequence": 2,
        "issue_id": 7,
        "user": IdName(id=4, name="Alice Example"),
        "notes": "Looking into it",
        "is_private": False,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    values.update(overrides)
    return JournalOut(**values)


def _page() -> WikiPageWithContent:
    return WikiPageWithContent(
        id=5,
        title="Release notes",
        slug="release-notes",
        protected=False,
        lock_version=2,
        created_at=_NOW,
        updated_at=_NOW,
        text="# Notes",
        content_version=2,
        content_author=IdName(id=4, name="Alice Example"),
        content_updated_at=_NOW,
    )


class TestEnvelope:
    def test_envelope_shape(self):
        delivery = uuid.uuid4()
        payload = we.build_envelope(
            WebhookEvent.ISSUE_CREATED,
            delivery,
            _NOW,
            project=we.ProjectRef(key="WEB", name="Website"),
            actor=we.ActorRef(id=4, login="alice", display_name="Alice Example"),
            data={"issue": {"id": 7}},
        )
        assert payload == {
            "event": "issue.created",
            "delivery_id": str(delivery),
            "occurred_at": "2026-10-30T12:00:00Z",
            "project": {"key": "WEB", "name": "Website"},
            "actor": {"id": 4, "login": "alice", "display_name": "Alice Example"},
            "data": {"issue": {"id": 7}},
        }

    def test_envelope_without_actor(self):
        payload = we.build_envelope(
            WebhookEvent.PING, uuid.uuid4(), _NOW, project=we.ProjectRef(key="WEB", name="Website"), actor=None, data={}
        )
        assert payload["actor"] is None


class TestEventData:
    def test_issue_created(self):
        data = we.issue_event_data(_issue())
        assert data["issue"]["key"] == "WEB-3"
        assert data["issue"]["status"] == {"id": 2, "name": "New"}
        assert data["issue"]["created_at"] == "2026-10-30T12:00:00Z"
        assert "changes" not in data

    def test_issue_updated_has_changes_and_notes(self):
        data = we.issue_event_data(
            _issue(), changes={"status_id": {"old": "1", "new": "2"}}, journal_id=11, notes="Moved on"
        )
        assert data["changes"] == {"status_id": {"old": "1", "new": "2"}}
        assert data["journal_id"] == 11
        assert data["notes"] == "Moved on"

    def test_issue_commented(self):
        data = we.comment_event_data(_issue(), _journal())
        assert data["issue"]["key"] == "WEB-3"
        assert data["comment"]["id"] == 11
        assert data["comment"]["notes"] == "Looking into it"
        assert data["comment"]["user"] == {"id": 4, "name": "Alice Example"}

    def test_issue_deleted(self):
        data = we.issue_event_data(_issue())
        assert data["issue"]["id"] == 7

    def test_wiki_page_updated(self):
        data = we.wiki_event_data(_page(), comments="Typo", created=False)
        assert data["page"]["slug"] == "release-notes"
        assert data["page"]["content_version"] == 2
        assert data["page"]["text"] == "# Notes"
        assert data["comments"] == "Typo"
        assert data["created"] is False


class TestChangeSet:
    def test_changes_from_details(self):
        details = [("attr", "status_id", "1", "2"), ("attr", "subject", "Old", "New")]
        assert we.changes_from_details(details) == {
            "status_id": {"old": "1", "new": "2"},
            "subject": {"old": "Old", "new": "New"},
        }

    def test_non_attribute_details_are_namespaced(self):
        details = [("attachment", "12", None, "log.txt"), ("relation", "blocks", None, "8")]
        assert we.changes_from_details(details) == {
            "attachment:12": {"old": None, "new": "log.txt"},
            "relation:blocks": {"old": None, "new": "8"},
        }

    def test_empty(self):
        assert we.changes_from_details([]) == {}


class TestEventNames:
    def test_create_accepts_known_events(self):
        wh = WebhookCreate(url="https://hooks.example.com/x", events=["issue.created", "wiki.page.updated"])
        assert wh.events == [WebhookEvent.ISSUE_CREATED, WebhookEvent.WIKI_PAGE_UPDATED]

    def test_create_rejects_unknown_event(self):
        with pytest.raises(ValidationError):
            WebhookCreate(url="https://hooks.example.com/x", events=["issue.exploded"])

    def test_ping_is_not_subscribable(self):
        with pytest.raises(ValidationError):
            WebhookCreate(url="https://hooks.example.com/x", events=["ping"])

    def test_update_rejects_unknown_event(self):
        with pytest.raises(ValidationError):
            WebhookUpdate(events=["issue.created", "nope"])

    def test_duplicates_removed(self):
        wh = WebhookCreate(url="https://hooks.example.com/x", events=["issue.created", "issue.created"])
        assert wh.events == [WebhookEvent.ISSUE_CREATED]

    def test_secret_optional_but_bounded(self):
        assert WebhookCreate(url="https://hooks.example.com/x", events=["issue.created"]).secret is None
        with pytest.raises(ValidationError):
            WebhookCreate(url="https://hooks.example.com/x", events=["issue.created"], secret="short")
        with pytest.raises(ValidationError):
            WebhookCreate(url="https://hooks.example.com/x", events=["issue.created"], secret="x" * 101)


class TestSignature:
    def test_header_format(self):
        body = b'{"event":"ping"}'
        header = we.signature_header("s3cret-value", body)
        expected = hmac.new(b"s3cret-value", body, hashlib.sha256).hexdigest()
        assert header == f"sha256={expected}"

    def test_generated_secret_is_long_and_random(self):
        a, b = we.generate_secret(), we.generate_secret()
        assert a != b
        assert len(a) >= 64


class TestBackoff:
    def test_grows_exponentially_without_jitter(self):
        delays = [we.backoff_seconds(n, rand=lambda: 0.0) for n in range(1, 5)]
        base = WEBHOOK_BACKOFF_BASE_SECONDS
        assert delays == [base, base * 2, base * 4, base * 8]

    def test_jitter_stays_within_a_quarter(self):
        low = we.backoff_seconds(3, rand=lambda: 0.0)
        high = we.backoff_seconds(3, rand=lambda: 0.999)
        assert low < high <= low * 1.25

    def test_capped(self):
        assert we.backoff_seconds(40, rand=lambda: 0.999) <= WEBHOOK_BACKOFF_MAX_SECONDS * 1.25

    def test_attempt_budget(self):
        assert WEBHOOK_MAX_ATTEMPTS >= 5


class TestAddressSafety:
    def test_loopback_blocked(self):
        assert not is_public_address(ipaddress.ip_network("127.0.0.0/8")[1])

    def test_private_ranges_blocked(self):
        # Built from integers: the repository keeps private address literals out of its text.
        v4 = [
            (10 << 24, 8),
            ((172 << 24) | (16 << 16), 12),
            ((169 << 24) | (254 << 16), 16),
            ((100 << 24) | (64 << 16), 10),
        ]
        networks = [ipaddress.IPv4Network(spec) for spec in v4]
        networks += [ipaddress.ip_network("fc00::/7"), ipaddress.ip_network("fe80::/10")]
        for net in networks:
            assert not is_public_address(net[5]), net

    def test_mapped_ipv4_blocked(self):
        inner = ipaddress.ip_network("127.0.0.0/8")[1]
        assert not is_public_address(ipaddress.IPv6Address(f"::ffff:{inner}"))

    def test_public_allowed(self):
        assert is_public_address(ipaddress.ip_address("93.184.216.34"))
