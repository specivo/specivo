"""Recall evaluation of suggested links against hybrid search (the ship gate of suggested links).

Given labelled cases (an issue and the wiki pages known to be right for it),
it measures recall@k of three rankings of the issue's neighbours and of hybrid
search with the issue's subject as the query:

- ``chunk_mean``: the cosine similarity of document vectors (the mean of an
  item's chunk embeddings, L2-normalised), as suggestions are stored;
- ``chunk_mean_threshold``: the same, keeping only the pairs whose centred
  score passes the project's threshold (and nothing in a project with too
  few vectors), as the suggestions are gated, i.e. what could actually be suggested;
- ``best_chunk``: the best cosine similarity between any chunk of the issue
  and any chunk of the other item;
- ``search``: ``SearchService.hybrid_search`` with the issue subject, the
  issue itself skipped.

Each runs twice: over issues and wiki pages (``all``, what the suggestions and
a plain search show) and over wiki pages only (``wiki``). Vectors are
computed on the fly from ``chunk_embeddings``, so the result does not depend
on the stored suggestions or on when the sweep last ran. Nothing is written:
the caller runs it in a read-only transaction. Everything is filtered for the
user it runs as, like the link graph.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.user import User

Item = tuple[str, int]

METHODS = ("chunk_mean", "chunk_mean_threshold", "best_chunk", "search")
SCOPES = ("all", "wiki")
#: Extra candidates read per ranking, so items the user may not see do not leave it short.
_CANDIDATE_MARGIN = 50


@dataclass(slots=True)
class Case:
    """One labelled issue: its ref and the wiki pages known to be right for it."""

    issue_ref: str
    expected: list[str]


def parse_labels(raw: str) -> list[Case]:
    """Read labelled cases from JSON or from lines.

    JSON: ``{"ACME-12": ["wiki:ACME/runbook", "deploy-guide"], ...}`` or a
    list of ``{"issue": "ACME-12", "pages": [...]}``. Lines:
    ``ACME-12: wiki:ACME/runbook, deploy-guide``; blank lines and lines
    starting with ``#`` are skipped. A page is ``wiki:KEY/slug`` or a bare
    slug of the issue's own project.
    """
    stripped = raw.strip()
    cases: list[Case] = []
    if stripped.startswith(("{", "[")):
        data = json.loads(stripped)
        entries: Iterable[tuple[Any, Any]]
        if isinstance(data, dict):
            entries = data.items()
        elif isinstance(data, list):
            entries = ((d.get("issue"), d.get("pages")) for d in data if isinstance(d, dict))
        else:
            raise ValueError("The labels must be a JSON object or a list")
        for issue_ref, pages in entries:
            if not isinstance(issue_ref, str) or not isinstance(pages, list) or not pages:
                raise ValueError(f"Each issue needs a list of pages: {issue_ref!r}")
            cases.append(Case(issue_ref.strip(), [str(p).strip() for p in pages if str(p).strip()]))
        return cases
    for number, line in enumerate(stripped.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        issue_ref, sep, rest = line.partition(":")
        pages = [p.strip() for p in rest.split(",") if p.strip()]
        if not sep or not pages:
            raise ValueError(f"Line {number}: expected 'ISSUE-REF: page, page'")
        cases.append(Case(issue_ref.strip(), pages))
    return cases


@dataclass(slots=True)
class CaseResult:
    issue_ref: str
    expected: list[str]
    missing_pages: list[str] = field(default_factory=list)
    already_linked: list[str] = field(default_factory=list)
    # scope -> method -> the refs ranked in the top k
    top: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    # scope -> method -> how many of *expected* are in the top k
    hits: dict[str, dict[str, int]] = field(default_factory=dict)
    error: str | None = None


@dataclass(slots=True)
class Report:
    k: int
    model: str | None
    cases: list[CaseResult]

    def recall(self, scope: str, method: str) -> float | None:
        """Mean recall@k over the cases that could be evaluated (macro average)."""
        values = [
            c.hits[scope][method] / len(c.expected) for c in self.cases if c.error is None and c.expected and c.hits
        ]
        return sum(values) / len(values) if values else None

    def found_only_by_suggestions(self, scope: str) -> list[tuple[str, str]]:
        """``(issue, page)`` pairs a suggestion ranking found in its top k and search did not."""
        out: list[tuple[str, str]] = []
        for case in self.cases:
            if case.error is not None or not case.top:
                continue
            by_search = set(case.top[scope]["search"])
            for page in case.expected:
                if page in by_search:
                    continue
                if any(page in case.top[scope][m] for m in ("chunk_mean", "best_chunk")):
                    out.append((case.issue_ref, page))
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "k": self.k,
            "model": self.model,
            "evaluated": sum(1 for c in self.cases if c.error is None),
            "recall": {scope: {m: self.recall(scope, m) for m in METHODS} for scope in SCOPES},
            "found_only_by_suggestions": {
                scope: [{"issue": i, "page": p} for i, p in self.found_only_by_suggestions(scope)] for scope in SCOPES
            },
            "cases": [
                {
                    "issue": c.issue_ref,
                    "expected": c.expected,
                    "missing_pages": c.missing_pages,
                    "already_linked": c.already_linked,
                    "top": c.top,
                    "hits": c.hits,
                    "error": c.error,
                }
                for c in self.cases
            ],
        }

    def as_text(self) -> str:
        lines = [f"Recall@{self.k} of suggested links vs hybrid search (model: {self.model or 'none'})"]
        evaluated = [c for c in self.cases if c.error is None]
        lines.append(f"Cases: {len(self.cases)}, evaluated: {len(evaluated)}")
        for scope in SCOPES:
            title = "issues and wiki pages" if scope == "all" else "wiki pages only"
            lines.append("")
            lines.append(f"Ranked over {title}:")
            for method in METHODS:
                value = self.recall(scope, method)
                shown = "n/a" if value is None else f"{value:.3f}"
                lines.append(f"  {method:<22} {shown}")
            found = self.found_only_by_suggestions(scope)
            lines.append(f"  pages found by suggestions that search missed: {len(found)}")
            lines.extend(f"    {issue} -> {page}" for issue, page in found)
        problems = [c for c in self.cases if c.error or c.missing_pages or c.already_linked]
        if problems:
            lines.append("")
            lines.append("Notes:")
            for case in problems:
                if case.error:
                    lines.append(f"  {case.issue_ref}: skipped ({case.error})")
                if case.missing_pages:
                    lines.append(f"  {case.issue_ref}: pages not found or not visible: {', '.join(case.missing_pages)}")
                if case.already_linked:
                    lines.append(
                        f"  {case.issue_ref}: already linked (never suggested): {', '.join(case.already_linked)}"
                    )
        return "\n".join(lines)


# Rankings, computed from chunk_embeddings with no stored state.

_CHUNK_MEAN_SQL = """
WITH vec AS (
    SELECT ss.source_type, ss.entity_id, l2_normalize(avg(ce.embedding)) AS v
    FROM search_sources ss
    JOIN search_chunks sc ON sc.source_id = ss.id
    JOIN chunk_embeddings ce ON ce.chunk_id = sc.id AND ce.model_id = :model_id
    WHERE ss.project_id = :project_id AND ss.source_type IN ('issue', 'wiki_page')
    GROUP BY ss.source_type, ss.entity_id
),
centre AS (SELECT avg(v) AS m, count(*) AS n FROM vec)
SELECT o.source_type, o.entity_id, 1 - (o.v <=> s.v) AS score, {centred} AS centred, centre.n AS n
FROM vec s
CROSS JOIN centre
JOIN vec o ON NOT (o.source_type = s.source_type AND o.entity_id = s.entity_id)
WHERE s.source_type = 'issue' AND s.entity_id = :issue_id AND o.source_type = ANY(:types)
ORDER BY o.v <=> s.v
LIMIT :n
"""

_BEST_CHUNK_SQL = """
WITH q AS (
    SELECT ce.embedding
    FROM search_sources s
    JOIN search_chunks c ON c.source_id = s.id
    JOIN chunk_embeddings ce ON ce.chunk_id = c.id AND ce.model_id = :model_id
    WHERE s.source_type = 'issue' AND s.entity_id = :issue_id
)
SELECT ss.source_type, ss.entity_id, max(1 - (ce.embedding <=> q.embedding)) AS score
FROM q
CROSS JOIN search_sources ss
JOIN search_chunks sc ON sc.source_id = ss.id
JOIN chunk_embeddings ce ON ce.chunk_id = sc.id AND ce.model_id = :model_id
WHERE ss.project_id = :project_id
  AND ss.source_type = ANY(:types)
  AND NOT (ss.source_type = 'issue' AND ss.entity_id = :issue_id)
GROUP BY ss.source_type, ss.entity_id
ORDER BY score DESC
LIMIT :n
"""


def _chunk_mean_sql() -> str:
    from specivo.services.link_suggestion_service import CENTRED_COSINE_SQL

    return _CHUNK_MEAN_SQL.format(centred=CENTRED_COSINE_SQL.format(a="o.v", b="s.v", m="centre.m"))


def _types(scope: str) -> list[str]:
    return ["issue", "wiki_page"] if scope == "all" else ["wiki_page"]


async def _ranked(
    session: AsyncSession, sql: str, model_id: int, project_id: int, issue_id: int, scope: str, n: int
) -> list[Any]:
    """Rows of a ranking: ``source_type``, ``entity_id``, ``score`` (and ``centred``, ``n`` for the chunk mean)."""
    rows = await session.execute(
        text(sql),
        {"model_id": model_id, "project_id": project_id, "issue_id": issue_id, "types": _types(scope), "n": n},
    )
    return list(rows)


def _gated(rows: list[Any], threshold: float) -> list[Item]:
    """The chunk mean ranking as suggestions gate it: centred score and the project's size."""
    from specivo.core.constants import SIMILAR_LINKS_MIN_ITEMS
    from specivo.services.link_suggestion_service import passes_threshold

    return [
        (r.source_type, r.entity_id)
        for r in rows
        if r.n >= SIMILAR_LINKS_MIN_ITEMS and passes_threshold(float(r.centred), threshold)
    ]


async def _search_ranked(
    session: AsyncSession, user: User, subject: str, project_id: int, issue_id: int, scope: str, n: int
) -> list[Item]:
    from specivo.services.search_service import SearchService

    results, _total, _counts = await SearchService().hybrid_search(
        session, subject, user, project_id=project_id, scope=scope, limit=n
    )
    out: list[Item] = []
    for result in results:
        if result.result_type == "issue" and result.id != issue_id:
            out.append(("issue", result.id))
        elif result.result_type == "wiki":
            out.append(("wiki_page", result.id))
    return out


async def evaluate(
    session: AsyncSession, user: User, cases: Sequence[Case], *, k: int = 5, model_id: int | None = None
) -> Report:
    """Run every case as *user*; see the module docstring. Reads only."""
    from specivo.core.exceptions import AppError
    from specivo.models.issue import Issue
    from specivo.models.link_suggestion import pair_key
    from specivo.models.project import Project
    from specivo.models.search import EmbeddingModel
    from specivo.services.entity_link_service import _Reader, resolve_root
    from specivo.services.link_suggestion_service import default_model_id, linked_pairs, similarity_threshold

    model_id = model_id if model_id is not None else await default_model_id(session)
    model = await session.get(EmbeddingModel, model_id) if model_id is not None else None
    reader = _Reader(session, user)
    results: list[CaseResult] = []
    n = k + _CANDIDATE_MARGIN
    for case in cases:
        result = CaseResult(case.issue_ref, [])
        results.append(result)
        try:
            node = await resolve_root(session, user, case.issue_ref)
        except (AppError, ValueError) as exc:
            result.error = f"issue not found: {exc}"
            continue
        if node[0] != "issue":
            result.error = "not an issue"
            continue
        issue = await session.get(Issue, node[1])
        if issue is None:
            result.error = "issue not found"
            continue
        project = await session.get(Project, issue.project_id)
        threshold = similarity_threshold(project.settings if project else None)
        refs: dict[Item, str] = {}
        for page_ref in case.expected:
            full = page_ref if page_ref.lower().startswith("wiki:") else f"wiki:{issue.project_key}/{page_ref}"
            try:
                page = await resolve_root(session, user, full)
            except (AppError, ValueError):
                result.missing_pages.append(page_ref)
                continue
            refs[(page[0], int(page[1]))] = full
        result.expected = sorted(set(refs.values()))
        if not refs:
            result.error = "none of its pages was found"
            continue
        if model_id is None:
            result.error = "no embedding model"
            continue
        linked = await linked_pairs(session, items=[("issue", issue.id)])
        result.already_linked = sorted(
            ref for item, ref in refs.items() if pair_key(("issue", issue.id), item) in linked
        )

        for scope in SCOPES:
            mean = await _ranked(session, _chunk_mean_sql(), model_id, issue.project_id, issue.id, scope, n)
            best = await _ranked(session, _BEST_CHUNK_SQL, model_id, issue.project_id, issue.id, scope, n)
            search = await _search_ranked(session, user, issue.subject, issue.project_id, issue.id, scope, n)
            rankings: dict[str, list[Item]] = {
                "chunk_mean": [(r.source_type, r.entity_id) for r in mean],
                "chunk_mean_threshold": _gated(mean, threshold),
                "best_chunk": [(r.source_type, r.entity_id) for r in best],
                "search": search,
            }
            result.top[scope] = {}
            result.hits[scope] = {}
            for method, items in rankings.items():
                visible = await _visible(reader, items)
                top = visible[:k]
                described = await _refs(session, top, refs)
                result.top[scope][method] = described
                result.hits[scope][method] = sum(1 for item in top if item in refs)
    return Report(k=k, model=model.name if model else None, cases=results)


async def _visible(reader: Any, items: list[Item]) -> list[Item]:
    await reader._load_issues({i for t, i in items if t == "issue"})
    await reader._load_pages({i for t, i in items if t == "wiki_page"})
    return [item for item in items if reader.visible(item)]


async def _refs(session: AsyncSession, items: list[Item], known: dict[Item, str]) -> list[str]:
    from sqlalchemy import select

    from specivo.models.issue import Issue
    from specivo.models.project import Project
    from specivo.models.wiki import Wiki, WikiPage

    out: list[str] = []
    for item in items:
        if item in known:
            out.append(known[item])
        elif item[0] == "issue":
            issue = await session.get(Issue, item[1])
            out.append(issue.display_key if issue else f"issue:{item[1]}")
        else:
            row = (
                await session.execute(
                    select(Project.key, WikiPage.slug)
                    .join(Wiki, Wiki.project_id == Project.id)
                    .join(WikiPage, WikiPage.wiki_id == Wiki.id)
                    .where(WikiPage.id == item[1])
                )
            ).one_or_none()
            out.append(f"wiki:{row.key}/{row.slug}" if row else f"wiki_page:{item[1]}")
    return out
