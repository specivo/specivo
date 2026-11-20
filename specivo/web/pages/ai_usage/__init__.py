"""Web pages for AI usage: the project tab, My AI usage and the manual entry form.

- ``/projects/{key}/ai-usage/``: the project's entries the reader may see,
  with filters and totals in the project's reporting currency.
- the issue tab (``/partials/issues/{key}/ai-usage/``) is :mod:`specivo.web.pages.ai_issue_usage`.
- ``/my/ai-usage/``: the entries logged in the reader's name across projects,
  unassigned ones included, with bulk reassignment to a project or issue.
- ``/projects/{key}/ai-usage/new/`` and ``/my/ai-usage/new/``: log usage by hand.
- ``/ai-usage/{id}/edit/``, ``/exclude/``, ``/include/``, ``/delete/``.

Every rule is :class:`AiUsageService`'s (visibility, who may log, edit,
exclude and delete); the pages only hide the controls a reader could not use.
Plain server-rendered forms: a form that fails validation is rendered again
with what was typed and the error, a successful write redirects with a
``notice`` code. Usage dates are UTC days.
"""

from __future__ import annotations

from fastapi import APIRouter

from specivo.web.pages.ai_usage import entry_form, listing

router = APIRouter()
router.include_router(listing.router)
router.include_router(entry_form.router)
