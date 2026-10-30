"""Gitea and Forgejo provider adapters: bare-hex signatures in either header, events, push parsing."""

from __future__ import annotations

import hashlib

import pytest

from specivo.hooks.providers import SignatureError, get_provider
from tests.unit.hooks._forge import fixture, fixture_bytes, hmac_hex

pytestmark = pytest.mark.unit

SECRET = "s3cret-for-tests-only-0123456789abcdef"
PROVIDERS = ["gitea", "forgejo"]
SIGNATURE_HEADERS = ["X-Gitea-Signature", "X-Forgejo-Signature"]


@pytest.mark.parametrize("provider", PROVIDERS)
class TestSignature:
    @pytest.mark.parametrize("header", SIGNATURE_HEADERS)
    def test_bare_hex_in_either_header_passes(self, provider: str, header: str) -> None:
        body = fixture_bytes(f"{provider}_push.json")
        get_provider(provider).verify({header: hmac_hex(SECRET, body)}, body, SECRET)

    def test_both_headers_as_forgejo_sends_them(self, provider: str) -> None:
        body = fixture_bytes(f"{provider}_push.json")
        sig = hmac_hex(SECRET, body)
        get_provider(provider).verify({"X-Forgejo-Signature": sig, "X-Gitea-Signature": sig}, body, SECRET)

    @pytest.mark.parametrize("header", SIGNATURE_HEADERS)
    def test_tampered_body_fails(self, provider: str, header: str) -> None:
        body = fixture_bytes(f"{provider}_push.json")
        sig = hmac_hex(SECRET, body)
        with pytest.raises(SignatureError):
            get_provider(provider).verify({header: sig}, body.replace(b"ACME-1", b"ACME-9"), SECRET)

    def test_missing_signature_fails(self, provider: str) -> None:
        body = fixture_bytes(f"{provider}_push.json")
        with pytest.raises(SignatureError):
            get_provider(provider).verify({}, body, SECRET)

    @pytest.mark.parametrize("header", SIGNATURE_HEADERS)
    def test_sha256_prefixed_value_is_rejected(self, provider: str, header: str) -> None:
        body = fixture_bytes(f"{provider}_push.json")
        with pytest.raises(SignatureError):
            get_provider(provider).verify({header: "sha256=" + hmac_hex(SECRET, body)}, body, SECRET)

    def test_github_header_is_not_accepted(self, provider: str) -> None:
        body = fixture_bytes(f"{provider}_push.json")
        with pytest.raises(SignatureError):
            get_provider(provider).verify({"X-Hub-Signature-256": "sha256=" + hmac_hex(SECRET, body)}, body, SECRET)

    def test_one_bad_header_with_one_good_fails(self, provider: str) -> None:
        body = fixture_bytes(f"{provider}_push.json")
        headers = {"X-Forgejo-Signature": hmac_hex(SECRET, body), "X-Gitea-Signature": hmac_hex("other", body)}
        with pytest.raises(SignatureError):
            get_provider(provider).verify(headers, body, SECRET)


class TestEventDetection:
    def test_forgejo_header_first(self) -> None:
        headers = {"X-Forgejo-Event": "push", "X-Gitea-Event": "pull_request", "X-GitHub-Event": "ping"}
        assert get_provider("forgejo").event_type(headers) == "push"

    def test_gitea_header_before_gogs_and_github(self) -> None:
        headers = {"X-Gitea-Event": "push", "X-Gogs-Event": "pull_request", "X-GitHub-Event": "ping"}
        assert get_provider("gitea").event_type(headers) == "push"
        assert get_provider("forgejo").event_type(headers) == "push"

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_gogs_then_github_fallback(self, provider: str) -> None:
        assert get_provider(provider).event_type({"X-Gogs-Event": "push", "X-GitHub-Event": "ping"}) == "push"
        assert get_provider(provider).event_type({"X-GitHub-Event": "push"}) == "push"

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_no_event_header(self, provider: str) -> None:
        assert get_provider(provider).event_type({}) is None


class TestDeliveryId:
    def test_forgejo_delivery(self) -> None:
        headers = {"X-Forgejo-Delivery": "f-1", "X-Gitea-Delivery": "g-1", "X-GitHub-Delivery": "h-1"}
        assert get_provider("forgejo").delivery_id(headers, b"{}") == "f-1"

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_gitea_delivery(self, provider: str) -> None:
        assert (
            get_provider(provider).delivery_id({"X-Gitea-Delivery": "g-1", "X-GitHub-Delivery": "h-1"}, b"{}") == "g-1"
        )

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_fallback_hash(self, provider: str) -> None:
        body = fixture_bytes(f"{provider}_push.json")
        assert get_provider(provider).delivery_id({}, body) == "sha256:" + hashlib.sha256(body).hexdigest()


class TestPush:
    def test_parse_gitea_push(self) -> None:
        event = get_provider("gitea").parse_push(fixture("gitea_push.json"))
        assert event.repo_full_name == "example-org/widgets"
        assert event.branch == "acme-4-export"
        assert event.default_branch == "main"
        assert event.pusher_login == "alex-example"
        (commit,) = event.commits
        assert commit.sha == "bffeb74224043ba2feb48d137756c8a9331c449a"
        assert commit.message == "Export to CSV\n\nresolves ACME-1\n"
        assert commit.author_email == "alex@example.com"
        assert commit.url == "https://gitea.example.com/example-org/widgets/commit/" + commit.sha

    def test_parse_forgejo_push(self) -> None:
        event = get_provider("forgejo").parse_push(fixture("forgejo_push.json"))
        assert event.repo_full_name == "example-org/widgets"
        assert event.branch == "main"
        assert [c.author_name for c in event.commits] == ["Alex Example", "Sam Sample"]

    def test_names(self) -> None:
        assert get_provider("gitea").name == "gitea"
        assert get_provider("forgejo").name == "forgejo"
