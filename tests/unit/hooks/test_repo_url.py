"""Repository addresses (``specivo.hooks.repo_url``) and the server check built on them.

The cases live in ``tests/fixtures/repo_urls.json``, shared with the E2E test
of the form's JavaScript parser, so the two stay in step.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from specivo.core.exceptions import ValidationError
from specivo.hooks.repo_url import is_ssh_address, parse_repo_address, repo_name_in_path
from specivo.services.repository_service import check_repository_address

pytestmark = pytest.mark.unit

_CASES = json.loads((Path(__file__).resolve().parents[2] / "fixtures" / "repo_urls.json").read_text())["cases"]


@pytest.mark.parametrize("case", _CASES, ids=[repr(c["input"])[:70] for c in _CASES])
def test_parse_table(case: dict) -> None:
    address = parse_repo_address(case["input"], case["provider"])
    if case["expected"] is None:
        assert address is None
        return
    assert address is not None
    got = {"provider": address.provider, "repo_full_name": address.repo_full_name, "url": address.url}
    assert got == case["expected"]


@pytest.mark.parametrize("value", [None, 12, "x" * 3000])
def test_odd_input_is_none(value) -> None:
    assert parse_repo_address(value) is None  # type: ignore[arg-type]


def test_known_host_is_flagged() -> None:
    assert parse_repo_address("https://github.com/a/b").known_host is True  # type: ignore[union-attr]
    assert parse_repo_address("https://git.example.org/a/b", "gitea").known_host is False  # type: ignore[union-attr]


def test_self_hosted_without_a_chosen_provider_reads_owner_and_repo() -> None:
    address = parse_repo_address("https://git.example.org/a/b/c")
    assert address is not None
    assert (address.provider, address.repo_full_name) == (None, "a/b")


@pytest.mark.parametrize(
    ("raw", "ssh"),
    [
        ("git@github.com:a/b.git", True),
        ("ssh://git@host/a/b", True),
        ("git://host/a/b", True),
        ("https://github.com/a/b", False),
        ("a/b", False),
    ],
)
def test_is_ssh_address(raw: str, ssh: bool) -> None:
    assert is_ssh_address(raw) is ssh


@pytest.mark.parametrize(
    ("name", "path", "found"),
    [
        ("owner/repo", ("owner", "repo"), True),
        ("Owner/Repo", ("owner", "repo", "src", "branch", "main"), True),
        ("owner/repo", ("git", "owner", "repo"), True),
        ("group/sub/repo", ("group", "sub", "repo", "-", "tree", "main"), True),
        ("owner/repo", ("owner", "other"), False),
        ("owner/repo", ("repo", "owner"), False),
        ("owner/repo", ("owner",), False),
    ],
)
def test_repo_name_in_path(name: str, path: tuple[str, ...], found: bool) -> None:
    assert repo_name_in_path(name, path) is found


class TestCheckRepositoryAddress:
    def test_known_host_url_is_cut_to_the_root(self) -> None:
        url = check_repository_address("github", "specivo/specivo", "https://github.com/specivo/specivo/tree/main")
        assert url == "https://github.com/specivo/specivo"

    def test_ssh_address_becomes_the_web_url(self) -> None:
        assert (
            check_repository_address("gitlab", "group/sub/repo", "git@gitlab.com:group/sub/repo.git")
            == "https://gitlab.com/group/sub/repo"
        )
        assert (
            check_repository_address("forgejo", "owner/repo", "ssh://git@git.example.org:2222/owner/repo.git")
            == "https://git.example.org/owner/repo"
        )

    def test_self_hosted_url_is_kept_as_given(self) -> None:
        url = "https://example.org/git/owner/repo/"
        assert check_repository_address("gitea", "owner/repo", url) == url

    @pytest.mark.parametrize(
        ("provider", "name", "url", "field"),
        [
            ("gitlab", "owner/repo", "https://github.com/owner/repo", "provider"),
            ("gitea", "owner/repo", "https://github.com/owner/repo", "provider"),
            ("github", "group/repo", "https://gitlab.com/group/repo", "provider"),
            ("github", "owner/other", "https://github.com/owner/repo", "repo_full_name"),
            ("gitlab", "group/repo", "https://gitlab.com/group/sub/repo", "repo_full_name"),
            ("gitea", "owner/other", "https://git.example.org/owner/repo", "repo_full_name"),
            ("gitea", "owner/repo", "https://git.example.org/", "repo_url"),
        ],
    )
    def test_mismatch_names_the_field(self, provider: str, name: str, url: str, field: str) -> None:
        with pytest.raises(ValidationError) as exc:
            check_repository_address(provider, name, url)
        assert exc.value.status_code == 422
        assert exc.value.field == field

    def test_codeberg_may_be_connected_as_gitea(self) -> None:
        assert check_repository_address("gitea", "owner/repo", "https://codeberg.org/owner/repo") == (
            "https://codeberg.org/owner/repo"
        )

    def test_name_case_is_ignored_on_known_hosts(self) -> None:
        assert check_repository_address("github", "Owner/Repo", "https://github.com/owner/repo")
