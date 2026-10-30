"""GitHub and GitLab provider adapters: signatures, events, delivery ids, push parsing."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest

from specivo.hooks.providers import SignatureError, get_provider
from tests.unit.hooks._forge import fixture, fixture_bytes, hmac_hex

pytestmark = pytest.mark.unit

SECRET = "s3cret-for-tests-only-0123456789abcdef"


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


class TestGitHubSignature:
    body = fixture_bytes("github_push.json")

    def test_valid_sha256_signature_passes(self) -> None:
        get_provider("github").verify(
            {"X-Hub-Signature-256": "sha256=" + hmac_hex(SECRET, self.body)}, self.body, SECRET
        )

    def test_header_name_is_case_insensitive(self) -> None:
        headers = {"x-hub-signature-256": "sha256=" + hmac_hex(SECRET, self.body)}
        get_provider("github").verify(headers, self.body, SECRET)

    def test_wrong_secret_fails(self) -> None:
        headers = {"X-Hub-Signature-256": "sha256=" + hmac_hex("another-secret", self.body)}
        with pytest.raises(SignatureError):
            get_provider("github").verify(headers, self.body, SECRET)

    def test_missing_header_fails(self) -> None:
        with pytest.raises(SignatureError):
            get_provider("github").verify({}, self.body, SECRET)

    def test_sha1_header_fails(self) -> None:
        import hmac

        sha1 = hmac.new(SECRET.encode(), self.body, hashlib.sha1).hexdigest()
        with pytest.raises(SignatureError):
            get_provider("github").verify({"X-Hub-Signature": "sha1=" + sha1}, self.body, SECRET)
        with pytest.raises(SignatureError):
            get_provider("github").verify({"X-Hub-Signature-256": "sha1=" + sha1}, self.body, SECRET)

    def test_bare_hex_without_prefix_fails(self) -> None:
        with pytest.raises(SignatureError):
            get_provider("github").verify({"X-Hub-Signature-256": hmac_hex(SECRET, self.body)}, self.body, SECRET)

    def test_signature_over_reserialized_body_fails(self) -> None:
        reserialized = json.dumps(json.loads(self.body)).encode()
        assert reserialized != self.body
        headers = {"X-Hub-Signature-256": "sha256=" + hmac_hex(SECRET, reserialized)}
        with pytest.raises(SignatureError):
            get_provider("github").verify(headers, self.body, SECRET)

    def test_non_ascii_header_fails_cleanly(self) -> None:
        with pytest.raises(SignatureError):
            get_provider("github").verify({"X-Hub-Signature-256": "sha256=é"}, self.body, SECRET)


class TestGitHubEvents:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [("push", "push"), ("ping", "ping"), ("pull_request", "pull_request"), ("issues", "issues"), (None, None)],
    )
    def test_event_type(self, header: str | None, expected: str | None) -> None:
        headers = {} if header is None else {"X-GitHub-Event": header}
        assert get_provider("github").event_type(headers) == expected

    def test_delivery_id_from_header(self) -> None:
        headers = {"X-GitHub-Delivery": "72d3162e-cc78-11e3-81ab-4c9367dc0958"}
        assert get_provider("github").delivery_id(headers, b"{}") == "72d3162e-cc78-11e3-81ab-4c9367dc0958"

    def test_delivery_id_falls_back_to_body_hash(self) -> None:
        body = fixture_bytes("github_push.json")
        assert get_provider("github").delivery_id({}, body) == "sha256:" + hashlib.sha256(body).hexdigest()

    def test_overlong_delivery_id_is_hashed(self) -> None:
        delivery = get_provider("github").delivery_id({"X-GitHub-Delivery": "x" * 500}, b"{}")
        assert len(delivery) <= 100
        assert delivery == "sha256:" + hashlib.sha256(b"x" * 500).hexdigest()


class TestGitHubPush:
    def test_parse_push(self) -> None:
        event = get_provider("github").parse_push(fixture("github_push.json"))
        assert event.repo_full_name == "example-org/widgets"
        assert event.ref == "refs/heads/feat/ACME-3-login-form"
        assert event.branch == "feat/ACME-3-login-form"
        assert event.default_branch == "main"
        assert event.pusher_login == "alex-example"
        assert [c.sha for c in event.commits] == [
            "0d1a26e67d8f5eaf1f6ba5c57fc3c7d91ac0fd1c",
            "59b20b8d5c6ff8d09518454d4dd8b7b30f095ab5",
        ]
        first = event.commits[0]
        assert first.message == "Add the login form\n\nRefs #ACME-1. Fixes ACME-2."
        assert first.author_name == "Alex Example"
        assert first.author_email == "alex@example.com"
        assert first.url.endswith("/commit/0d1a26e67d8f5eaf1f6ba5c57fc3c7d91ac0fd1c")
        assert first.timestamp == datetime(2019, 5, 15, 20, 20, 30, tzinfo=UTC)

    def test_repo_full_name(self) -> None:
        assert get_provider("github").repo_full_name(fixture("github_ping.json")) == "example-org/widgets"
        assert get_provider("github").repo_full_name({}) is None

    def test_tag_push_has_no_branch(self) -> None:
        payload = fixture("github_push.json") | {"ref": "refs/tags/v1.0"}
        assert get_provider("github").parse_push(payload).branch is None

    def test_malformed_commits_are_skipped(self) -> None:
        payload = fixture("github_push.json")
        payload["commits"] = [
            {"message": "no id"},
            "not an object",
            {"id": "NOT-HEX", "message": "x"},
            *payload["commits"],
        ]
        assert len(get_provider("github").parse_push(payload).commits) == 2


# ---------------------------------------------------------------------------
# GitLab
# ---------------------------------------------------------------------------


class TestGitLabToken:
    body = fixture_bytes("gitlab_push.json")

    def test_matching_token_passes(self) -> None:
        get_provider("gitlab").verify({"X-Gitlab-Token": SECRET}, self.body, SECRET)

    def test_wrong_token_fails(self) -> None:
        with pytest.raises(SignatureError):
            get_provider("gitlab").verify({"X-Gitlab-Token": SECRET + "x"}, self.body, SECRET)

    def test_missing_token_fails(self) -> None:
        with pytest.raises(SignatureError):
            get_provider("gitlab").verify({}, self.body, SECRET)

    def test_hmac_in_place_of_token_fails(self) -> None:
        with pytest.raises(SignatureError):
            get_provider("gitlab").verify({"X-Gitlab-Token": hmac_hex(SECRET, self.body)}, self.body, SECRET)


class TestGitLabEvents:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("Push Hook", "push"),
            ("Merge Request Hook", "pull_request"),
            ("Tag Push Hook", "tag_push"),
            ("Note Hook", "note"),
            (None, None),
        ],
    )
    def test_event_type(self, header: str | None, expected: str | None) -> None:
        headers = {} if header is None else {"X-Gitlab-Event": header}
        assert get_provider("gitlab").event_type(headers) == expected

    def test_delivery_id_prefers_event_uuid(self) -> None:
        headers = {"X-Gitlab-Event-UUID": "uuid-1", "Idempotency-Key": "key-1"}
        assert get_provider("gitlab").delivery_id(headers, b"{}") == "uuid-1"

    def test_delivery_id_from_idempotency_key(self) -> None:
        assert get_provider("gitlab").delivery_id({"Idempotency-Key": "key-1"}, b"{}") == "key-1"

    def test_old_gitlab_without_delivery_id_hashes_body(self) -> None:
        body = fixture_bytes("gitlab_push.json")
        assert get_provider("gitlab").delivery_id({}, body) == "sha256:" + hashlib.sha256(body).hexdigest()


class TestGitLabPush:
    def test_parse_push(self) -> None:
        event = get_provider("gitlab").parse_push(fixture("gitlab_push.json"))
        assert event.repo_full_name == "example-group/widgets"
        assert event.branch == "main"
        assert event.default_branch == "main"
        assert event.pusher_login == "alex-example"
        assert [c.sha for c in event.commits] == [
            "b6568db1bc1dcd7f8b4d5a946b0b91f9dacd7327",
            "da1560886d4f094c3e6c9ef40349f7d38b5d27d7",
        ]
        assert event.commits[0].message.startswith("Update the catalogue")
        assert event.commits[1].author_email == "sam@example.org"
        assert event.commits[1].url.endswith("/-/commit/da1560886d4f094c3e6c9ef40349f7d38b5d27d7")

    def test_repo_full_name(self) -> None:
        assert get_provider("gitlab").repo_full_name(fixture("gitlab_push.json")) == "example-group/widgets"


def test_unknown_provider() -> None:
    with pytest.raises(KeyError):
        get_provider("bitbucket")
