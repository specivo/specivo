"""E2E tests for Mermaid diagrams in rendered Markdown.

The server renders a ```mermaid fence as ``pre.sp-mermaid`` holding the
source; ``frontend/js/modules/mermaid.js`` loads the vendored library only
when a page holds one and draws it as an SVG. A diagram that fails keeps its
source visible under a notice, and nothing in a stored diagram runs script.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from specivo.testing.e2e_base import api_post_with_retry, create_issue, create_project, unique_key

pytestmark = [pytest.mark.e2e]

FLOWCHART = "```mermaid\nflowchart LR\n    Draft --> Review --> Published\n```"
SCRIPT_SRC = 'script[src*="/static/vendor/mermaid."]'


def _wiki_project(api: httpx.Client) -> dict:
    project = create_project(api, prefix="MMD")
    api.patch(f"/api/v1/projects/{project['key']}/modules/", json={"modules": {"wiki": True}})
    return project


def _wiki_page(api: httpx.Client, text: str) -> str:
    """Create a wiki page holding *text* and return its path."""
    project = _wiki_project(api)
    resp = api_post_with_retry(
        api,
        f"/api/v1/projects/{project['key']}/wiki/",
        json={"title": f"Diagram {unique_key()}", "text": text},
    )
    assert resp.status_code == 201, resp.text
    return f"/projects/{project['key']}/wiki/{resp.json()['slug']}/"


def test_wiki_flowchart_renders_svg(admin_page: Page, api_client: httpx.Client) -> None:
    admin_page.goto(_wiki_page(api_client, f"Intro\n\n{FLOWCHART}\n"))

    diagram = admin_page.locator(".wiki-content .sp-mermaid-diagram svg")
    expect(diagram).to_be_visible(timeout=15000)
    expect(diagram).to_contain_text("Review")
    expect(admin_page.locator(".wiki-content pre.sp-mermaid")).to_be_hidden()
    expect(admin_page.locator(".sp-mermaid-error")).to_have_count(0)


def test_library_is_not_loaded_without_a_diagram(admin_page: Page, api_client: httpx.Client) -> None:
    admin_page.goto(_wiki_page(api_client, "```python\nprint('no diagram')\n```"))
    admin_page.wait_for_load_state("networkidle")

    expect(admin_page.locator(SCRIPT_SRC)).to_have_count(0)
    assert admin_page.evaluate("() => typeof window.mermaid") == "undefined"


def test_invalid_diagram_keeps_the_source(admin_page: Page, api_client: httpx.Client) -> None:
    admin_page.goto(_wiki_page(api_client, "```mermaid\nflowchart LR\n    A --> (((\n```"))

    error = admin_page.locator(".wiki-content .sp-mermaid-error")
    expect(error).to_be_visible(timeout=15000)
    expect(error).to_contain_text("This diagram could not be drawn")
    source = admin_page.locator(".wiki-content pre.sp-mermaid")
    expect(source).to_be_visible()
    expect(source).to_contain_text("A --> (((")
    expect(admin_page.locator(".wiki-content .sp-mermaid-diagram")).to_have_count(0)


def test_diagram_cannot_run_script(admin_page: Page, api_client: httpx.Client) -> None:
    text = (
        "```mermaid\n"
        '%%{init: {"securityLevel": "loose", "flowchart": {"htmlLabels": true}}}%%\n'
        "flowchart LR\n"
        '    A["<img src=x onerror=window.__spXss=1>"] --> B["<b>bold</b>"]\n'
        "    click A call window.__spXssCall()\n"
        '    click B href "javascript:window.__spXssHref=1"\n'
        "```"
    )
    admin_page.add_init_script("window.__spXssCall = function () { window.__spXssCalled = 1; };")
    admin_page.goto(_wiki_page(api_client, text))

    svg = admin_page.locator(".wiki-content .sp-mermaid-diagram svg")
    expect(svg).to_be_visible(timeout=15000)
    expect(svg.locator("img")).to_have_count(0)
    expect(svg.locator("foreignObject")).to_have_count(0)
    hrefs = svg.evaluate(
        "e => Array.from(e.querySelectorAll('a')).map(a => a.getAttribute('href') || a.getAttribute('xlink:href'))"
    )
    assert not any(h and h.strip().lower().startswith("javascript:") for h in hrefs), hrefs
    # Label markup is shown as text.
    expect(svg.locator("g.node").first).to_contain_text("<img")

    for node in svg.locator("g.node").all():
        node.click(force=True)
    admin_page.wait_for_timeout(300)
    flags = admin_page.evaluate(
        "() => [window.__spXss, window.__spXssCalled, window.__spXssHref].map(v => v === undefined)"
    )
    assert flags == [True, True, True]


def test_comment_with_diagram_renders(admin_page: Page, api_client: httpx.Client) -> None:
    project = create_project(api_client, prefix="MMD")
    issue = create_issue(api_client, project["key"], f"Diagram comment {unique_key()}")
    admin_page.goto(f"/issue/{issue['key']}/")
    admin_page.wait_for_load_state("networkidle")

    admin_page.locator(".comment-form textarea").fill(f"See the flow:\n\n{FLOWCHART}\n")
    with admin_page.expect_navigation(timeout=15000):
        admin_page.locator(".comment-form button.sp-btn-primary").click()

    diagram = admin_page.locator(".sp-mermaid-diagram svg", has_text="Published")
    expect(diagram).to_be_visible(timeout=15000)


def test_block_added_later_renders(admin_page: Page, api_client: httpx.Client) -> None:
    """A block swapped in after load (htmx, preview) loads the library and is drawn."""
    admin_page.goto(_wiki_page(api_client, "No diagram yet."))
    admin_page.wait_for_load_state("networkidle")
    expect(admin_page.locator(SCRIPT_SRC)).to_have_count(0)

    admin_page.evaluate(
        """() => {
            const pre = document.createElement('pre');
            pre.className = 'sp-mermaid';
            pre.textContent = 'flowchart LR\\n    Later --> Drawn';
            document.querySelector('.wiki-content').appendChild(pre);
        }"""
    )

    expect(admin_page.locator(".wiki-content .sp-mermaid-diagram svg")).to_contain_text("Drawn", timeout=15000)
    expect(admin_page.locator(SCRIPT_SRC)).to_have_count(1)


def test_editor_preview_renders_diagram(admin_page: Page, api_client: httpx.Client) -> None:
    admin_page.goto(_wiki_page(api_client, FLOWCHART) + "edit/")
    admin_page.wait_for_function("() => document.querySelector('.CodeMirror') !== null", timeout=10000)

    admin_page.locator(".editor-toolbar button.preview").click()

    preview = admin_page.locator(".editor-preview .sp-mermaid-diagram svg")
    expect(preview).to_be_visible(timeout=15000)
    expect(preview).to_contain_text("Published")


def test_diagram_follows_a_dark_background(admin_page: Page, api_client: httpx.Client) -> None:
    admin_page.goto(_wiki_page(api_client, FLOWCHART))
    svg = admin_page.locator(".wiki-content .sp-mermaid-diagram svg")
    expect(svg).to_be_visible(timeout=15000)
    light = svg.get_attribute("id")
    assert "#ececff" in (svg.locator("style").text_content() or "").lower(), "expected the default theme's node fill"

    # A theme switch toggles a class on <html> or <body>; the diagram is drawn
    # again with the dark theme once the background behind it is dark.
    admin_page.add_style_tag(content=".sp-dark-test .wiki-content { background: #111; color: #eee; }")
    admin_page.evaluate("() => document.body.classList.add('sp-dark-test')")

    redrawn = admin_page.locator(".wiki-content .sp-mermaid-diagram svg")
    expect(redrawn).not_to_have_id(light or "", timeout=15000)
    expect(admin_page.locator(".wiki-content .sp-mermaid-diagram")).to_have_count(1)
    style = redrawn.locator("style").text_content() or ""
    assert "#1f2020" in style.lower(), "expected the dark theme's node fill"


def test_front_matter_config_cannot_loosen_security(admin_page: Page, api_client: httpx.Client) -> None:
    text = (
        "```mermaid\n"
        "---\n"
        "config:\n"
        "  securityLevel: loose\n"
        "  htmlLabels: true\n"
        "  secure: []\n"
        "  dompurifyConfig:\n"
        "    ADD_ATTR: [onerror]\n"
        "---\n"
        "flowchart LR\n"
        '    A["<img src=x onerror=window.__spXss=1>"] --> B\n'
        "    click A call window.__spXssCall()\n"
        "```"
    )
    admin_page.add_init_script("window.__spXssCall = function () { window.__spXssCalled = 1; };")
    admin_page.goto(_wiki_page(api_client, text))

    svg = admin_page.locator(".wiki-content .sp-mermaid-diagram svg")
    expect(svg).to_be_visible(timeout=15000)
    expect(svg.locator("img")).to_have_count(0)
    expect(svg.locator("foreignObject")).to_have_count(0)
    for node in svg.locator("g.node").all():
        node.click(force=True)
    admin_page.wait_for_timeout(300)
    assert admin_page.evaluate("() => [window.__spXss, window.__spXssCalled]") == [None, None]


def test_diagram_css_cannot_style_the_page(admin_page: Page, api_client: httpx.Client) -> None:
    breakout = "} body, .wiki-content h2 { color: rgb(255, 0, 0) !important; display: none !important } x {"
    text = (
        "## Heading\n\n"
        "```mermaid\n"
        f'%%{{init: {{"themeCSS": "{breakout}", "themeVariables": {{"primaryColor": "{breakout}"}},'
        f' "fontFamily": "{breakout}"}}}}%%\n'
        "flowchart LR\n"
        "    A --> B\n"
        "```"
    )
    admin_page.goto(_wiki_page(api_client, text))

    expect(admin_page.locator(".wiki-content .sp-mermaid-diagram svg")).to_be_visible(timeout=15000)
    heading = admin_page.locator(".wiki-content h2")
    expect(heading).to_be_visible()
    assert heading.evaluate("e => getComputedStyle(e).color") != "rgb(255, 0, 0)"


def test_drawing_never_removes_page_elements_by_id(admin_page: Page, api_client: httpx.Client) -> None:
    """Element ids the library uses for its scratch nodes cannot match ids in the content."""
    headings = "".join(f"## sp-mermaid-{n}\n\n## dsp-mermaid-{n}\n\n" for n in range(1, 4))
    admin_page.goto(_wiki_page(api_client, f"{headings}{FLOWCHART}\n\n{FLOWCHART}\n"))

    expect(admin_page.locator(".wiki-content .sp-mermaid-diagram svg")).to_have_count(2, timeout=15000)
    expect(admin_page.locator(".wiki-content h2")).to_have_count(6)


WIDE_FLOWCHART = (
    "```mermaid\nflowchart LR\n"
    "    A[Collect the requirements] --> B[Write the design document] --> C[Review the design together]\n"
    "    C --> D[Build the first version] --> E[Test it on every platform] --> F[Release it to everyone]\n"
    "```"
)

_SIZES = """e => {
    const svg = e.querySelector('svg');
    return {
        scrollWidth: e.scrollWidth,
        clientWidth: e.clientWidth,
        svgWidth: svg.getBoundingClientRect().width,
        naturalWidth: svg.viewBox.baseVal.width,
    };
}"""


def test_wide_diagram_scrolls_sideways_on_a_phone(admin_page: Page, api_client: httpx.Client) -> None:
    admin_page.set_viewport_size({"width": 375, "height": 812})
    admin_page.goto(_wiki_page(api_client, WIDE_FLOWCHART))

    figure = admin_page.locator(".wiki-content .sp-mermaid-diagram")
    expect(figure.locator("svg")).to_be_visible(timeout=15000)
    sizes = figure.evaluate(_SIZES)
    assert sizes["naturalWidth"] > sizes["clientWidth"], sizes
    assert sizes["scrollWidth"] > sizes["clientWidth"], sizes
    assert sizes["svgWidth"] >= sizes["naturalWidth"] - 1, sizes
    assert figure.evaluate("e => getComputedStyle(e).overflowX") == "auto"

    figure.evaluate("e => { e.scrollLeft = e.scrollWidth; }")
    assert figure.evaluate("e => e.scrollLeft") > 0


def test_wide_diagram_fits_the_page_on_a_desktop(admin_page: Page, api_client: httpx.Client) -> None:
    admin_page.set_viewport_size({"width": 1280, "height": 900})
    admin_page.goto(_wiki_page(api_client, WIDE_FLOWCHART))

    figure = admin_page.locator(".wiki-content .sp-mermaid-diagram")
    expect(figure.locator("svg")).to_be_visible(timeout=15000)
    sizes = figure.evaluate(_SIZES)
    assert sizes["svgWidth"] <= sizes["clientWidth"] + 1, sizes
    assert sizes["scrollWidth"] <= sizes["clientWidth"] + 1, sizes
