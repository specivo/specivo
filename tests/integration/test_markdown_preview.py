"""Integration tests for the markdown preview endpoint.

The whole point of this endpoint is round-trip parity with the Jinja
``wiki_markdown`` filter: the editor's live preview must show exactly
what saved content will render to. The parity tests here protect against
silent drift if either renderer is changed without updating the other.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from specivo.services.markdown_service import render_wiki_markdown

pytestmark = pytest.mark.integration


_PREVIEW_URL = "/api/v1/markdown/preview/"


# ---------------------------------------------------------------------------
# Round-trip parity vs the Jinja filter
# ---------------------------------------------------------------------------


# A representative set of inputs covering the markdown features in active use.
# Add to this list whenever a new feature lands — the parity check is the
# single test that protects against silent drift between saved-content
# rendering and the editor preview.
_PARITY_INPUTS: list[tuple[str, str]] = [
    ("heading", "# Top heading\n\n## Subheading"),
    (
        "list",
        "- one\n- two\n- three\n\n1. first\n2. second\n",
    ),
    (
        "fenced_code",
        "```python\ndef hello() -> str:\n    return 'world'\n```\n",
    ),
    (
        "table",
        "| col a | col b |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |\n",
    ),
    (
        "blockquote",
        "> a quote\n>\n> with two paragraphs\n",
    ),
    (
        "image",
        "![alt text](https://example.com/img.png)",
    ),
    (
        "issue_autolink",
        "Fixed in PROJ-123 and also PROJ-9 (see notes).",
    ),
    (
        "issue_autolink_already_linked",
        "Already linked: [PROJ-1](/issue/PROJ-1/) — no double link.",
    ),
    (
        "mixed",
        (
            "# Release notes\n\n"
            "Fixes PROJ-42 and PROJ-43.\n\n"
            "## Steps\n\n"
            "1. Run `make migrate`\n"
            "2. Restart workers\n\n"
            "```bash\nuv run pytest\n```\n\n"
            "| feature | status |\n| --- | --- |\n| preview | done |\n"
        ),
    ),
    ("empty", ""),
    ("plain_text", "Just a sentence with no formatting."),
]


@pytest.mark.parametrize(("label", "text"), _PARITY_INPUTS, ids=[p[0] for p in _PARITY_INPUTS])
async def test_preview_matches_jinja_filter(
    auth_client: AsyncClient, label: str, text: str
) -> None:
    """Endpoint output must equal what the shared renderer produces directly.

    This is the load-bearing test: if a future change forks the two paths,
    this test fails and forces the change to either update both or be reverted.
    """
    expected = str(render_wiki_markdown(text))

    resp = await auth_client.post(_PREVIEW_URL, json={"text": text, "context": "issue"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["html"] == expected, f"drift detected for input '{label}'"


async def test_wiki_and_issue_context_produce_identical_html(auth_client: AsyncClient) -> None:
    """Today both contexts share the same renderer; this guards that contract."""
    text = "Fixes PROJ-42.\n\n# heading\n\n- a\n- b"

    issue_resp = await auth_client.post(_PREVIEW_URL, json={"text": text, "context": "issue"})
    wiki_resp = await auth_client.post(_PREVIEW_URL, json={"text": text, "context": "wiki"})

    assert issue_resp.status_code == 200
    assert wiki_resp.status_code == 200
    assert issue_resp.json()["html"] == wiki_resp.json()["html"]


# ---------------------------------------------------------------------------
# Success-path basics
# ---------------------------------------------------------------------------


async def test_returns_json_with_html_key(auth_client: AsyncClient) -> None:
    resp = await auth_client.post(_PREVIEW_URL, json={"text": "# Hello", "context": "issue"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert set(body.keys()) == {"html"}
    # TOC extension adds id="..." to headings; assert the visible content only
    assert ">Hello</h1>" in body["html"]


async def test_default_context_is_issue(auth_client: AsyncClient) -> None:
    """Omitting ``context`` should default to ``"issue"`` and succeed."""
    resp = await auth_client.post(_PREVIEW_URL, json={"text": "# Hi"})
    assert resp.status_code == 200, resp.text
    # TOC extension adds id="..." to headings
    assert ">Hi</h1>" in resp.json()["html"]


async def test_empty_text_returns_empty_html(auth_client: AsyncClient) -> None:
    """Preview of empty text is empty — not an error."""
    resp = await auth_client.post(_PREVIEW_URL, json={"text": "", "context": "issue"})
    assert resp.status_code == 200, resp.text
    # markupsafe.Markup("") -> "" — no surrounding whitespace, no error
    assert resp.json()["html"] == ""


async def test_issue_autolink_in_preview(auth_client: AsyncClient) -> None:
    """The KEY-123 autolink extension must apply in preview output."""
    resp = await auth_client.post(
        _PREVIEW_URL, json={"text": "See PROJ-99", "context": "issue"}
    )
    assert resp.status_code == 200, resp.text
    html = resp.json()["html"]
    assert 'href="/issue/PROJ-99/"' in html
    assert ">PROJ-99<" in html


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


async def test_anonymous_request_is_rejected(client: AsyncClient) -> None:
    resp = await client.post(_PREVIEW_URL, json={"text": "# Hi", "context": "issue"})
    assert resp.status_code == 401, resp.text
    body = resp.json()
    # Structured error envelope from core.exceptions
    assert "errors" in body
    assert body["errors"][0]["code"] in {"unauthorized", "http_error"}


# ---------------------------------------------------------------------------
# Size cap
# ---------------------------------------------------------------------------


async def test_oversize_body_is_rejected(auth_client: AsyncClient) -> None:
    # 256 KiB + 1 byte of plain ASCII -> 256 KiB + 1 bytes UTF-8
    too_big = "a" * (256 * 1024 + 1)
    resp = await auth_client.post(
        _PREVIEW_URL, json={"text": too_big, "context": "issue"}
    )
    assert resp.status_code == 413, resp.text
    body = resp.json()
    assert "errors" in body
    err = body["errors"][0]
    assert err["code"] == "payload_too_large"
    assert err["details"]["max_bytes"] == 256 * 1024


async def test_size_limit_boundary_accepts_max_size(auth_client: AsyncClient) -> None:
    """Exactly 256 KiB must still be accepted — only > 256 KiB is rejected."""
    at_limit = "a" * (256 * 1024)
    resp = await auth_client.post(
        _PREVIEW_URL, json={"text": at_limit, "context": "issue"}
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Pydantic validation
# ---------------------------------------------------------------------------


async def test_invalid_context_value_returns_422(auth_client: AsyncClient) -> None:
    resp = await auth_client.post(
        _PREVIEW_URL, json={"text": "x", "context": "garbage"}
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert "errors" in body
    # Field path should point at "context"
    fields = {e.get("field") for e in body["errors"]}
    assert "context" in fields
