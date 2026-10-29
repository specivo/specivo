"""Bound the cost of rendering user-authored Markdown.

Python-Markdown is not linear on every input. Unclosed link, image, code-span
and HTML openers make it rescan the rest of the paragraph (or of the document)
once per opener, many headings with the same text make ``toc`` probe ids one
by one, and deep list or quote nesting recurses until Python's stack runs out.
A few kilobytes are enough to tie up a worker for seconds on every view of an
issue or wiki page, or to crash the render outright.

Two layers keep a render bounded:

1. :func:`render_hazard` — a cheap, linear pre-scan run before every render.
   It estimates the render time from counts of the constructs that cost the
   most, plus the rescans that unclosed openers and duplicate headings
   trigger, and checks the nesting depth. Over the budget, the caller shows
   the text as plain text instead of rendering it.
2. :class:`RenderDeadline` — a backstop for anything the estimate misses: a
   cooperative limit on the CPU time one render may use
   (``MARKDOWN_RENDER_DEADLINE_SECONDS``), plus a wall-clock ceiling of
   :data:`WALL_CLOCK_FACTOR` times that. It checks the clocks each time
   Python-Markdown tries an inline pattern or a block processor and aborts
   the render by raising :class:`RenderDeadlineError`. Nothing is interrupted
   from another thread; the Markdown instance is discarded with the
   exception. Counting CPU time means a busy host does not turn a normal page
   into plain text, and the default is far above what any document the
   pre-scan admits needs.
3. A per-request budget (``MARKDOWN_RENDER_REQUEST_BUDGET_SECONDS``) shared
   by every field rendered for one request, so a page of many fields cannot
   multiply the per-field limit. Once it is spent, the remaining fields are
   shown as plain text.

The time limits of layers 2 and 3 are multiplied by :func:`speed_factor`, how
much slower this host renders a reference document than the calibration
machine, so slow hardware gets proportionally more time. The pre-scan is not
scaled: what it rejects is rejected on every host.

The unit costs below were measured with Python-Markdown 3.11.2 on a developer
laptop. They only need to be right to within a small factor: the budget leaves
room for realistic documents (thousands of links, 200 KB wiki pages) and
pathological ones exceed it by orders of magnitude.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import Counter
from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from markdown import Markdown  # type: ignore[import-untyped]
from markdown.extensions import Extension  # type: ignore[import-untyped]

from specivo.core.config import get_settings
from specivo.core.exceptions import ValidationError
from specivo.core.i18n import gettext as _

logger = logging.getLogger(__name__)

# Longest Markdown text (in characters) that may be stored in an issue
# description, a comment or a wiki page, or sent to the preview endpoint.
# Imported content bypasses the write check; longer text renders as plain text.
MAX_MARKDOWN_CHARS = 256 * 1024

# Estimated render time (microseconds) above which text is shown as plain text.
RENDER_BUDGET_US = 750_000

# Container nesting (block quotes, list items, 4-space indents) above which text
# is shown as plain text. Python-Markdown recurses once per level and hits the
# recursion limit somewhere between 250 and 500 levels; real documents stay
# well below 20.
MAX_NESTING_DEPTH = 50

# Microseconds per occurrence.
_US_PER_BRACKET = 40.0  # ``[``: link, image or wiki link
_US_PER_ISSUE_REF = 40.0  # ``KEY-123`` the issue-ref inline pattern links
_US_PER_TAG = 35.0  # ``<`` starting a tag, comment or autolink
_US_PER_ESCAPE = 30.0  # ``\``
_US_PER_ENTITY = 25.0  # ``&``
_US_PER_LINE = 20.0  # non-blank line: paragraph, heading, list item, row
_US_PER_BACKTICK_RUN = 15.0
_US_PER_EMPHASIS_RUN = 12.0  # run of ``*`` or ``_``
_US_PER_FENCE = 60.0  # fence line; each code block is highlighted on its own
_US_PER_TOC_ENTRY = 3.5  # each ``[TOC]`` marker copies every heading

# Microseconds per character rescanned, or per probe.
_US_PER_BRACKET_SCAN = 0.16  # link text: from ``[`` to its ``]``
_US_PER_TARGET_SCAN = 0.1  # link target: from ``](`` to its ``)``
_US_PER_BACKTICK_SCAN = 0.025  # unclosed backtick, to the end of the block
_US_PER_TAG_SCAN = 0.015  # ``<`` to the next ``>``, across the whole text
_US_PER_HEADING_SPLIT = 0.001  # heading line times the size of its block
_US_PER_DUPLICATE_ID = 0.4  # ``toc`` probing ``id_1``, ``id_2``, ...
_US_PER_HASH_BACKTRACK = 0.01  # squared length of a ``#`` run in a heading

_BLOCK_SPLIT_RE = re.compile(r"\n[ \t]*\n")
_INLINE_TOKEN_RE = re.compile(r"\\.|`+|\]\(|[\[\]()]", re.DOTALL)
_TAG_TOKEN_RE = re.compile(r"<[A-Za-z/!?]|>")
_BACKTICK_RUN_RE = re.compile(r"`+")
_EMPHASIS_RUN_RE = re.compile(r"[*_]+")
_HASH_RUN_RE = re.compile(r"#+")
_CONTAINER_MARKER_RE = re.compile(r"[ \t]*(?:>|[-*+](?=[ \t])|\d{1,9}[.)](?=[ \t]))")
_SETEXT_UNDERLINE_RE = re.compile(r" {0,3}(?:=+|-+)[ \t]*$")
_NON_WORD_RE = re.compile(r"\W+")


class RenderDeadlineError(Exception):
    """Raised inside a render that ran past its :class:`RenderDeadline`."""


def check_markdown_length(text: str | None, field: str) -> None:
    """Raise ``ValidationError`` if *text* is longer than :data:`MAX_MARKDOWN_CHARS`.

    Called by the services that store Markdown, so the API, the web forms and
    the MCP tools share one limit.
    """
    if text is not None and len(text) > MAX_MARKDOWN_CHARS:
        raise ValidationError(
            _("Text is too long ({length} characters); the limit is {limit}.").format(
                length=len(text), limit=MAX_MARKDOWN_CHARS
            ),
            field=field,
            details={"max_length": MAX_MARKDOWN_CHARS},
        )


def render_hazard(text: str, original_length: int | None = None, issue_refs: int = 0) -> str | None:
    """Return why *text* should not be rendered as Markdown, or None if it may be.

    The reasons are ``"size"`` (longer than :data:`MAX_MARKDOWN_CHARS`),
    ``"nesting"`` (deeper than :data:`MAX_NESTING_DEPTH`) and ``"cost"`` (an
    estimated render time over :data:`RENDER_BUDGET_US`). Runs in linear time.

    *original_length* is the length of what the author wrote, when *text* is
    that after wiki-link substitution (which lengthens it); the size limit
    applies to the author's text. *issue_refs* is the number of ``KEY-123``
    refs the render will link, each of which becomes a link element.
    """
    if (len(text) if original_length is None else original_length) > MAX_MARKDOWN_CHARS:
        return "size"
    lines_cost, depth = _line_costs(text)
    if depth > MAX_NESTING_DEPTH:
        return "nesting"
    if lines_cost + _inline_costs(text) + issue_refs * _US_PER_ISSUE_REF > RENDER_BUDGET_US:
        return "cost"
    return None


def estimate_render_cost(text: str) -> float:
    """Return the estimated render time of *text*, in microseconds.

    Exposed for tests and for tuning the budget; :func:`render_hazard` is the
    check callers use.
    """
    lines_cost, _depth = _line_costs(text)
    return lines_cost + _inline_costs(text)


def _inline_costs(text: str) -> float:
    """Cost of inline constructs, and of the rescans unclosed openers trigger."""
    cost = (
        text.count("[") * _US_PER_BRACKET
        + text.count("\\") * _US_PER_ESCAPE
        + text.count("&") * _US_PER_ENTITY
        + len(_BACKTICK_RUN_RE.findall(text)) * _US_PER_BACKTICK_RUN
        + len(_EMPHASIS_RUN_RE.findall(text)) * _US_PER_EMPHASIS_RUN
    )
    cost += _tag_scan_cost(text)
    if "[" in text or "`" in text:
        for block in _BLOCK_SPLIT_RE.split(text):
            cost += _block_scan_cost(block)
    return cost


def _tag_scan_cost(text: str) -> float:
    """Cost of ``<`` openers: raw HTML handling looks for the next ``>`` in the whole text."""
    tags = 0
    distance = 0
    pending: list[int] = []
    for m in _TAG_TOKEN_RE.finditer(text):
        pos = m.start()
        if text[pos] == ">":
            distance += sum(pos - p for p in pending)
            pending.clear()
        else:
            tags += 1
            pending.append(pos)
    distance += sum(len(text) - p for p in pending)
    return tags * _US_PER_TAG + distance * _US_PER_TAG_SCAN


def _block_scan_cost(block: str) -> float:
    """Cost of the inline rescans in one block (inline parsing never crosses a blank line).

    A link's text is read from ``[`` to the matching ``]`` and its target from
    ``](`` to the matching ``)``; an opener without a match is read to the end
    of the block, once per opener. A run of backticks without a closing run of
    the same length is read to the end of the block once per backtick.
    """
    end = len(block)
    brackets: list[int] = []
    parens: list[tuple[int, bool]] = []
    runs: list[tuple[int, int]] = []
    bracket_chars = 0
    target_chars = 0
    for m in _INLINE_TOKEN_RE.finditer(block):
        token = m.group()
        pos = m.start()
        first = token[0]
        if first == "\\":
            continue
        if first == "`":
            runs.append((pos, len(token)))
        elif first == "[":
            brackets.append(pos)
        elif first == "]":
            if brackets:
                bracket_chars += pos - brackets.pop()
            if token == "](":
                parens.append((pos + 1, True))
        elif first == "(":
            parens.append((pos, False))
        elif parens:  # ")"
            start, is_target = parens.pop()
            if is_target:
                target_chars += pos - start
    bracket_chars += sum(end - p for p in brackets)
    target_chars += sum(end - p for p, is_target in parens if is_target)
    return (
        bracket_chars * _US_PER_BRACKET_SCAN
        + target_chars * _US_PER_TARGET_SCAN
        + _backtick_scan_chars(runs, end) * _US_PER_BACKTICK_SCAN
    )


def _backtick_scan_chars(runs: list[tuple[int, int]], end: int) -> int:
    """Characters read while pairing backtick runs (a code span needs equal-length runs)."""
    if not runs:
        return 0
    next_same: list[int | None] = [None] * len(runs)
    seen: dict[int, int] = {}
    for i in range(len(runs) - 1, -1, -1):
        length = runs[i][1]
        next_same[i] = seen.get(length)
        seen[length] = i
    chars = 0
    i = 0
    while i < len(runs):
        pos, length = runs[i]
        closer = next_same[i]
        if closer is None:
            chars += length * (end - pos)
            i += 1
        else:
            chars += runs[closer][0] - pos
            i = closer + 1
    return chars


def _line_costs(text: str) -> tuple[float, int]:
    """Return the cost of the line-level constructs in *text* and its deepest nesting."""
    cost = 0.0
    depth = 0
    lines = 0
    fences = 0
    toc_markers = 0
    hash_backtrack = 0
    heading_split = 0
    block_size = 0
    block_headings = 0
    heading_ids: Counter[str] = Counter()
    previous = ""
    for line in text.split("\n"):
        if not line.strip():
            heading_split += block_headings * block_size
            block_size = block_headings = 0
            previous = ""
            continue
        lines += 1
        block_size += len(line) + 1
        first = line[0]
        if first == "#":
            block_headings += 1
            heading_ids[_heading_key(line)] += 1
            hash_backtrack += sum(len(run) ** 2 for run in _HASH_RUN_RE.findall(line))
        elif previous and _SETEXT_UNDERLINE_RE.match(line):
            heading_ids[_heading_key(previous)] += 1
        if first in " \t>-*+0123456789":
            depth = max(depth, _nesting_depth(line))
            if depth > MAX_NESTING_DEPTH:
                break
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            fences += 1
        elif stripped.rstrip() == "[TOC]":
            toc_markers += 1
        previous = line
    heading_split += block_headings * block_size
    headings = sum(heading_ids.values())
    duplicate_probes = sum(n * (n - 1) // 2 for n in heading_ids.values())
    cost = (
        lines * _US_PER_LINE
        + fences * _US_PER_FENCE
        + toc_markers * headings * _US_PER_TOC_ENTRY
        + hash_backtrack * _US_PER_HASH_BACKTRACK
        + heading_split * _US_PER_HEADING_SPLIT
        + duplicate_probes * _US_PER_DUPLICATE_ID
    )
    return cost, depth


def _nesting_depth(line: str) -> int:
    """Count the containers a line opens: quote markers, list markers and 4-column indents."""
    indent = len(line) - len(line.lstrip(" \t"))
    depth = (indent + 3 * line.count("\t", 0, indent)) // 4
    pos = 0
    while depth <= MAX_NESTING_DEPTH:
        m = _CONTAINER_MARKER_RE.match(line, pos)
        if m is None:
            break
        depth += 1
        pos = m.end()
    return depth


def _heading_key(line: str) -> str:
    """Approximate the ``toc`` id of a heading line; equal keys mean colliding ids."""
    return _NON_WORD_RE.sub("", line.strip(" \t#").lower())


# Clocks, looked up at call time so tests can substitute them.
_cpu_clock: Callable[[], float] = time.thread_time
_wall_clock: Callable[[], float] = time.monotonic

# A render may take this many times its CPU allowance in wall-clock time, so
# a thread starved of the CPU cannot hold a request indefinitely.
WALL_CLOCK_FACTOR = 4.0


class RenderDeadline(Extension):
    """Abort a render that uses too much CPU time, or takes too long in all.

    Wraps every inline pattern's ``handleMatch`` and every block processor's
    ``run`` so they check the clocks first and raise
    :class:`RenderDeadlineError` once either limit has passed. Each check
    reads two clocks, and the work between two checks is at most one scan of
    the text. Register it last, so it sees every pattern and processor the
    other extensions add.

    *cpu_seconds* limits the CPU time of the calling thread, which is the
    thread that renders (``markdown.markdown`` runs synchronously); it is not
    inflated when other processes compete for the CPU. The wall-clock limit is
    :data:`WALL_CLOCK_FACTOR` times that, for a thread that gets almost no CPU.
    """

    def __init__(self, cpu_seconds: float) -> None:
        super().__init__()
        self.cpu_deadline = _cpu_clock() + cpu_seconds
        self.wall_deadline = _wall_clock() + cpu_seconds * WALL_CLOCK_FACTOR

    def extendMarkdown(self, md: Markdown) -> None:  # noqa: N802 - Python-Markdown API
        for pattern in md.inlinePatterns:
            pattern.handleMatch = self._checked(pattern.handleMatch)
        for processor in md.parser.blockprocessors:
            processor.run = self._checked(processor.run)

    def _checked(self, method: Callable[..., Any]) -> Callable[..., Any]:
        cpu_deadline = self.cpu_deadline
        wall_deadline = self.wall_deadline

        def checked(*args: Any, **kwargs: Any) -> Any:
            if _cpu_clock() > cpu_deadline or _wall_clock() > wall_deadline:
                raise RenderDeadlineError
            return method(*args, **kwargs)

        return checked


# ---------------------------------------------------------------------------
# Per-request budget
# ---------------------------------------------------------------------------


@dataclass
class RenderBudget:
    """CPU seconds left for rendering Markdown while handling one request."""

    remaining: float


_request_budget: ContextVar[RenderBudget | None] = ContextVar("markdown_render_budget", default=None)


def start_request_budget(seconds: float | None = None) -> Token[RenderBudget | None]:
    """Give the current request a fresh render budget; pass the token to :func:`end_request_budget`.

    *seconds* defaults to the ``markdown_render_request_budget_seconds``
    setting. Set by ``MarkdownRenderBudgetMiddleware`` for every HTTP request,
    so the description, every comment and every history entry on a page
    share one budget.
    """
    if seconds is None:
        seconds = request_budget_seconds()
    return _request_budget.set(RenderBudget(seconds))


def end_request_budget(token: Token[RenderBudget | None]) -> None:
    """Drop the budget set by :func:`start_request_budget`."""
    _request_budget.reset(token)


def field_deadline() -> float:
    """CPU seconds one field may use on this host: the setting, scaled by :func:`speed_factor`.

    Never less than twice what the slowest text the pre-scan admits
    (:data:`RENDER_BUDGET_US`, in calibration-machine time) needs on this host,
    so whatever passes the pre-scan also renders.
    """
    base = max(get_settings().markdown_render_deadline_seconds, 2 * RENDER_BUDGET_US / 1_000_000)
    return base * speed_factor()


def request_budget_seconds() -> float:
    """CPU seconds all the fields of one request may use on this host."""
    return get_settings().markdown_render_request_budget_seconds * speed_factor()


def render_allowance() -> float:
    """CPU seconds the next render may use: the per-field deadline, capped by the request budget.

    Zero or less means the request's budget is spent. Outside a request (a
    background task, a script) only the per-field deadline applies.
    """
    allowance = field_deadline()
    budget = _request_budget.get()
    return allowance if budget is None else min(allowance, budget.remaining)


# ---------------------------------------------------------------------------
# Host speed
# ---------------------------------------------------------------------------

# Best-of-3 CPU time of rendering ``_reference_document()`` on the machine the
# costs and limits in this module were calibrated on (a current laptop).
REFERENCE_RENDER_SECONDS = 0.012

# The measured factor is clamped to this range: limits never get tighter than
# the settings on a fast machine, and a wildly slow measurement (a host busy
# at start-up) cannot make them unbounded.
MIN_SPEED_FACTOR = 1.0
MAX_SPEED_FACTOR = 20.0

_speed_factor: float | None = None
_speed_factor_lock = threading.Lock()


def _reference_document() -> str:
    """About 9 KB of ordinary Markdown: headings, links, emphasis, lists, tables, code."""
    sections = []
    for i in range(30):
        sections.append(
            f"## Section {i}\n\n"
            f"See [the docs](https://example.com/p/{i}) and ![chart](https://example.com/c{i}.png), "
            f"**bold** and *emphasis* with `code {i}`.\n\n"
            f"- item [link](https://example.com/{i})\n- item two\n  - nested item\n\n"
            f"| key | value |\n|---|---|\n| {i} | [file](https://example.com/f{i}.pdf) |\n\n"
            f"```python\nprint({i})\n```\n\n> quote {i}\n\n"
        )
    return "".join(sections)


def measure_render_seconds() -> float:
    """Best-of-3 CPU time of rendering the reference document with the real Markdown configuration."""
    from specivo.services.markdown_service import _MD_EXTENSION_CONFIGS, _MD_EXTENSIONS, _sanitize_html

    document = _reference_document()
    best = float("inf")
    for _attempt in range(3):
        started = _cpu_clock()
        _sanitize_html(Markdown(extensions=_MD_EXTENSIONS, extension_configs=_MD_EXTENSION_CONFIGS).convert(document))
        best = min(best, _cpu_clock() - started)
    return best


def speed_factor() -> float:
    """How much slower than the calibration machine this host renders Markdown (at least 1).

    ``MARKDOWN_RENDER_SPEED_FACTOR`` overrides it. Otherwise it is measured
    once per process, on first use, by rendering a reference document, and
    logged. The pre-scan threshold is not scaled: it is a cost model, so text
    it rejects is rejected on every host.
    """
    override = get_settings().markdown_render_speed_factor
    if override is not None:
        return override
    global _speed_factor
    if _speed_factor is None:
        with _speed_factor_lock:
            if _speed_factor is None:
                measured = measure_render_seconds()
                _speed_factor = min(max(measured / REFERENCE_RENDER_SECONDS, MIN_SPEED_FACTOR), MAX_SPEED_FACTOR)
                logger.info(
                    "Markdown render speed factor %.2f (reference document took %.3f s of CPU)",
                    _speed_factor,
                    measured,
                )
    return _speed_factor


def reset_speed_factor() -> None:
    """Forget the measured factor, so the next use measures again (for tests)."""
    global _speed_factor
    _speed_factor = None


def charge_render(cpu_seconds: float) -> None:
    """Deduct CPU time a render used from the current request's budget, if any."""
    budget = _request_budget.get()
    if budget is not None:
        budget.remaining -= cpu_seconds
