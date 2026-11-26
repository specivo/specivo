"""The evaluation tasks: a prompt each and the known correct tool sequence.

A scenario's ``steps`` are the calls a careful agent makes, in order. Each
step names the acceptable tools (the first is the reference), the reference
arguments (``ref``, run by ``--dry-run``) and the arguments a model's call must
match (``check``). Strings in ``check`` match when the model's value contains
them (case-insensitive), numbers by value, lists when every expected item is
matched by some item, objects key by key. ``{P}``, ``{Q}``, ``{login}`` and
``"@name"`` are filled from the fixture (``fixture.fill``).

A task succeeds when every required step is matched, in order, by a call that
did not fail. Optional steps (a lookup a model may skip when it already knows
the answer) count for the first-call check only. Scenarios run in order on one
fixture per listing mode, so a later one may rely on an earlier one's change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Step:
    tool: str | tuple[str, ...]
    ref: dict[str, Any] = field(default_factory=dict)
    check: dict[str, Any] = field(default_factory=dict)
    optional: bool = False

    @property
    def tools(self) -> tuple[str, ...]:
        return (self.tool,) if isinstance(self.tool, str) else self.tool


@dataclass(frozen=True)
class Scenario:
    id: str
    prompt: str
    steps: tuple[Step, ...]


def _lookups() -> Step:
    return Step("specivo_list_lookups", optional=True)


SCENARIOS: tuple[Scenario, ...] = (
    # --- Orientation -------------------------------------------------------
    Scenario("whoami", "What is my Specivo user id and login?", (Step("specivo_whoami"),)),
    Scenario("list-projects", "Which Specivo projects can I see?", (Step("specivo_list_projects"),)),
    Scenario(
        "list-members",
        "Who are the members of project {P}?",
        (Step("specivo_list_members", {"project_key": "{P}"}, {"project_key": "{P}"}),),
    ),
    Scenario(
        "list-tags",
        "Which tags exist in project {P}?",
        (Step("specivo_list_tags", {"project_key": "{P}"}, {"project_key": "{P}"}),),
    ),
    # --- Issues ------------------------------------------------------------
    Scenario(
        "create-bug",
        "In project {P}, create a Bug titled 'Search returns duplicate results' with High priority.",
        (
            _lookups(),
            Step(
                "specivo_create_issue",
                {
                    "project_key": "{P}",
                    "tracker_id": "@tracker_bug",
                    "subject": "Search returns duplicate results",
                    "priority_id": "@priority_high",
                },
                {
                    "project_key": "{P}",
                    "tracker_id": "@tracker_bug",
                    "priority_id": "@priority_high",
                    "subject": "duplicate",
                },
            ),
        ),
    ),
    Scenario(
        "create-with-metadata-and-tags",
        "Create a Feature in {P} titled 'Dark mode', with the metadata component set to 'frontend' and the tag 'ui'.",
        (
            _lookups(),
            Step("specivo_list_metadata_schemas", {"project_key": "{P}"}, optional=True),
            Step(
                "specivo_create_issue",
                {
                    "project_key": "{P}",
                    "tracker_id": "@tracker_feature",
                    "subject": "Dark mode",
                    "metadata": {"component": "frontend"},
                    "tags": ["ui"],
                },
                {
                    "tracker_id": "@tracker_feature",
                    "subject": "dark mode",
                    "metadata": {"component": "frontend"},
                    "tags": ["ui"],
                },
            ),
        ),
    ),
    Scenario(
        "create-sub-issue",
        "Create a Feature in {P} titled 'Export as XLSX' as a sub-issue of {P}-2.",
        (
            _lookups(),
            Step(
                "specivo_create_issue",
                {
                    "project_key": "{P}",
                    "tracker_id": "@tracker_feature",
                    "subject": "Export as XLSX",
                    "parent_ref": "{P}-2",
                },
                {"parent_ref": "{P}-2", "subject": "xlsx"},
            ),
        ),
    ),
    Scenario(
        "update-status",
        "Set the status of {P}-2 to In Progress.",
        (
            _lookups(),
            Step(
                "specivo_update_issue",
                {"issue_ref": "{P}-2", "status_id": "@status_in_progress"},
                {"issue_ref": "{P}-2", "status_id": "@status_in_progress"},
            ),
        ),
    ),
    Scenario(
        "assign-to-self",
        "Assign {P}-3 to me.",
        (
            Step("specivo_whoami"),
            Step(
                "specivo_update_issue",
                {"issue_ref": "{P}-3", "assigned_to_id": "@user"},
                {"issue_ref": "{P}-3", "assigned_to_id": "@user"},
            ),
        ),
    ),
    Scenario(
        "done-ratio-with-note",
        "Mark {P}-4 as 50% done and add the note 'Half of the notes are written'.",
        (
            Step(
                "specivo_update_issue",
                {"issue_ref": "{P}-4", "done_ratio": 50, "notes": "Half of the notes are written"},
                {"issue_ref": "{P}-4", "done_ratio": 50, "notes": "half"},
            ),
        ),
    ),
    Scenario(
        "set-version",
        "Put {P}-3 into version 1.0.",
        (
            Step("specivo_list_versions", {"project_key": "{P}"}),
            Step(
                "specivo_update_issue",
                {"issue_ref": "{P}-3", "fixed_version_id": "@version_1"},
                {"issue_ref": "{P}-3", "fixed_version_id": "@version_1"},
            ),
        ),
    ),
    Scenario(
        "show-issue",
        "What is the status of {P}-5, and what is it about?",
        (Step("specivo_show_issue", {"issue_ref": "{P}-5"}, {"issue_ref": "{P}-5"}),),
    ),
    Scenario(
        "edit-description",
        "In the description of {P}-1, change 'Safari 17' to 'Safari 18'.",
        (
            Step("specivo_show_issue", {"issue_ref": "{P}-1", "search": "Safari"}, optional=True),
            Step(
                "specivo_edit_description",
                {"issue_ref": "{P}-1", "search_text": "Safari 17", "replace_text": "Safari 18"},
                {"issue_ref": "{P}-1", "search_text": "Safari 17", "replace_text": "Safari 18"},
            ),
        ),
    ),
    Scenario(
        "close-with-comment",
        "Resolve {P}-5 (status Resolved) with the comment 'Fixed in CI'.",
        (
            _lookups(),
            Step(
                "specivo_update_issue",
                {"issue_ref": "{P}-5", "status_id": "@status_resolved", "notes": "Fixed in CI"},
                {"issue_ref": "{P}-5", "status_id": "@status_resolved", "notes": "fixed in ci"},
            ),
        ),
    ),
    Scenario(
        "move-issue",
        "Move issue {P}-6 to project {Q}.",
        (
            Step(
                "specivo_move_issue",
                {"issue_ref": "{P}-6", "target_project_key": "{Q}"},
                {"issue_ref": "{P}-6", "target_project_key": "{Q}"},
            ),
        ),
    ),
    Scenario(
        "list-open-mine",
        "List the open issues assigned to me in {P}.",
        (
            Step(
                "specivo_list_issues",
                {"project_key": "{P}", "assignee": "me"},
                {"project_key": "{P}", "assignee": "me"},
            ),
        ),
    ),
    Scenario(
        "list-by-tag",
        "List the issues in {P} tagged 'backend'.",
        (
            Step(
                "specivo_list_issues",
                {"project_key": "{P}", "tag": "backend"},
                {"project_key": "{P}", "tag": "backend"},
            ),
        ),
    ),
    Scenario(
        "list-children",
        "Which sub-issues does {P}-2 have?",
        (
            Step(
                ("specivo_show_issue", "specivo_list_issues"),
                {"issue_ref": "{P}-2"},
            ),
        ),
    ),
    Scenario(
        "issue-development",
        "Show the development activity (commits, pull requests, CI) of {P}-2.",
        (Step("specivo_issue_development", {"issue_ref": "{P}-2"}, {"issue_ref": "{P}-2"}),),
    ),
    # --- Comments ----------------------------------------------------------
    Scenario(
        "add-comment",
        "Add the comment 'Waiting for the design review.' to {P}-2.",
        (
            Step(
                "specivo_add_comment",
                {"issue_ref": "{P}-2", "notes": "Waiting for the design review."},
                {"issue_ref": "{P}-2", "notes": "design review"},
            ),
        ),
    ),
    Scenario(
        "list-comments",
        "Show me the comments on {P}-2.",
        (Step("specivo_list_comments", {"issue_ref": "{P}-2"}, {"issue_ref": "{P}-2"}),),
    ),
    Scenario(
        "comment-linking-wiki",
        "Add a comment to {P}-3 that links the wiki page 'Architecture Overview' of {P}.",
        (
            Step(
                "specivo_add_comment",
                {"issue_ref": "{P}-3", "notes": "See [[Architecture Overview]]."},
                {"issue_ref": "{P}-3", "notes": "architecture"},
            ),
        ),
    ),
    # --- Relations ---------------------------------------------------------
    Scenario(
        "add-relation",
        "Record that {P}-1 blocks {P}-4.",
        (
            Step(
                "specivo_add_relation",
                {"issue_ref": "{P}-1", "issue_to_key": "{P}-4", "relation_type": "blocks"},
                {"issue_ref": "{P}-1", "issue_to_key": "{P}-4", "relation_type": "blocks"},
            ),
        ),
    ),
    Scenario(
        "list-relations",
        "Which issues is {P}-1 related to?",
        # specivo_get_links answers it as well: its edges include the issue's relations.
        (
            Step(
                ("specivo_list_relations", "specivo_show_issue", "specivo_get_links"),
                {"issue_ref": "{P}-1"},
                {"issue_ref": "{P}-1"},
            ),
        ),
    ),
    Scenario(
        "remove-relation",
        "Remove the 'relates' relation between {P}-1 and {P}-5.",
        (
            Step("specivo_list_relations", {"issue_ref": "{P}-1"}, {"issue_ref": "{P}-1"}),
            Step(
                "specivo_remove_relation",
                {"issue_ref": "{P}-1", "relation_id": "@relation_1_5"},
                {"relation_id": "@relation_1_5"},
            ),
        ),
    ),
    # --- Search and links --------------------------------------------------
    Scenario(
        "search-issues",
        "Find the issues in {P} about Safari.",
        (Step("specivo_search", {"query": "Safari", "project_key": "{P}"}, {"query": "safari"}),),
    ),
    Scenario(
        "search-wiki",
        "Search the wiki of {P} for 'deployment'.",
        (Step("specivo_search", {"query": "deployment", "project_key": "{P}", "scope": "wiki"}, {"query": "deploy"}),),
    ),
    Scenario(
        "backlinks",
        "Which wiki pages mention {P}-3?",
        (Step(("specivo_get_links", "specivo_show_issue"), {"ref": "{P}-3"}),),
    ),
    # --- Metadata and tags -------------------------------------------------
    Scenario(
        "set-metadata",
        "Set the metadata key component of {P}-1 to 'backend'.",
        (
            Step("specivo_list_metadata_schemas", {"project_key": "{P}"}, optional=True),
            Step(
                "specivo_metadata",
                {"target_ref": "issue:{P}-1", "key": "component", "op": "set", "value": "backend"},
                {"target_ref": "{P}-1", "key": "component", "op": "set", "value": "backend"},
            ),
        ),
    ),
    Scenario(
        "append-commit",
        "Record commit 3f2a9c1e8b7d6a5f4e3d2c1b0a9f8e7d6c5b4a39 in the commits metadata list of {P}-2.",
        (
            Step(
                "specivo_metadata",
                {
                    "target_ref": "issue:{P}-2",
                    "key": "commits",
                    "op": "append",
                    "value": "3f2a9c1e8b7d6a5f4e3d2c1b0a9f8e7d6c5b4a39",
                },
                {"target_ref": "{P}-2", "key": "commits", "op": "append", "value": "3f2a9c1e"},
            ),
        ),
    ),
    Scenario(
        "tag-issue",
        "Tag {P}-4 with 'backend'.",
        (
            Step("specivo_list_tags", {"project_key": "{P}"}, optional=True),
            Step(
                "specivo_tag",
                {"target_ref": "{P}-4", "op": "add", "value": "backend"},
                {"target_ref": "{P}-4", "op": "add", "value": "backend"},
            ),
        ),
    ),
    Scenario(
        "tag-wiki-page",
        "Tag the wiki page 'Release Process' of {P} with 'docs'.",
        (
            Step(
                "specivo_tag",
                {"target_ref": "wiki:{P}/release-process", "op": "add", "value": ["docs"]},
                {"target_ref": "wiki:{P}/release-process", "op": "add", "value": "docs"},
            ),
        ),
    ),
    # --- Wiki --------------------------------------------------------------
    Scenario(
        "list-wiki",
        "List the wiki pages of {P}.",
        (Step("specivo_list_wiki_pages", {"project_key": "{P}"}, {"project_key": "{P}"}),),
    ),
    Scenario(
        "read-wiki",
        "Read the wiki page 'Release Process' in {P} and summarise it.",
        (
            Step("specivo_list_wiki_pages", {"project_key": "{P}"}, optional=True),
            Step(
                ("specivo_read_wiki", "specivo_read_wiki_section"),
                {"project_key": "{P}", "slug": "release-process"},
                {"slug": "release-process"},
            ),
        ),
    ),
    Scenario(
        "read-wiki-section",
        "What does the Deployment section of the 'Architecture Overview' wiki page in {P} say?",
        (
            Step(
                ("specivo_read_wiki_section", "specivo_read_wiki"),
                {"project_key": "{P}", "slug": "architecture-overview", "heading": "Deployment"},
                {"slug": "architecture-overview"},
            ),
        ),
    ),
    Scenario(
        "replace-wiki-section",
        "In the 'Architecture Overview' wiki page of {P}, replace the body of the Deployment section with "
        "'Deployed with Docker Compose.'",
        (
            Step(
                "specivo_read_wiki_section",
                {"project_key": "{P}", "slug": "architecture-overview", "heading": "Deployment"},
                optional=True,
            ),
            Step(
                "specivo_replace_wiki_section",
                {
                    "project_key": "{P}",
                    "slug": "architecture-overview",
                    "heading": "## Deployment",
                    "text": "Deployed with Docker Compose.",
                },
                {"slug": "architecture-overview", "heading": "deployment", "text": "docker compose"},
            ),
        ),
    ),
    Scenario(
        "edit-wiki",
        "In the 'Release Process' wiki page of {P}, change 'weekly' to 'every two weeks'.",
        (
            Step(
                ("specivo_read_wiki", "specivo_read_wiki_section"),
                {"project_key": "{P}", "slug": "release-process"},
                optional=True,
            ),
            Step(
                "specivo_edit_wiki",
                {
                    "project_key": "{P}",
                    "slug": "release-process",
                    "search_text": "weekly",
                    "replace_text": "every two weeks",
                },
                {"slug": "release-process", "search_text": "weekly", "replace_text": "every two weeks"},
            ),
        ),
    ),
    Scenario(
        "append-wiki",
        "Append a section '## FAQ' with the text 'Ask in the team channel.' to the 'Release Process' wiki page of {P}.",
        (
            Step(
                "specivo_append_wiki",
                {"project_key": "{P}", "slug": "release-process", "text": "## FAQ\n\nAsk in the team channel.\n"},
                {"slug": "release-process", "text": "## faq"},
            ),
        ),
    ),
    Scenario(
        "create-wiki-child",
        "Create a wiki page 'Onboarding' in {P} under the 'Release Process' page, with the text 'Start here.'",
        (
            Step(
                "specivo_create_wiki",
                {"project_key": "{P}", "title": "Onboarding", "text": "Start here.", "parent_slug": "release-process"},
                {"project_key": "{P}", "title": "onboarding", "parent_slug": "release-process"},
            ),
        ),
    ),
    Scenario(
        "rename-wiki",
        "Rename the wiki page 'Old Notes' in {P} to 'Sprint 1 Notes'.",
        (
            Step(
                "specivo_update_wiki_metadata",
                {"project_key": "{P}", "slug": "old-notes", "title": "Sprint 1 Notes"},
                {"slug": "old-notes", "title": "sprint 1 notes"},
            ),
        ),
    ),
    Scenario(
        "delete-wiki",
        "Delete the wiki page 'Scratch Pad' in {P}.",
        (
            Step(
                "specivo_delete_wiki",
                {"project_key": "{P}", "slug": "scratch-pad"},
                {"project_key": "{P}", "slug": "scratch-pad"},
            ),
        ),
    ),
    # --- Time --------------------------------------------------------------
    Scenario(
        "log-time",
        "Log 1.5 hours of Development on {P}-1 for today with the comment 'Investigated the crash'.",
        (
            _lookups(),
            Step(
                "specivo_log_time",
                {
                    "project_key": "{P}",
                    "issue_ref": "{P}-1",
                    "hours": 1.5,
                    "activity_id": "@activity_development",
                    "comments": "Investigated the crash",
                },
                {"issue_ref": "{P}-1", "hours": 1.5, "activity_id": "@activity_development"},
            ),
        ),
    ),
    Scenario(
        "timer-start-stop",
        "Start a timer on {P}-5, then stop it and log the time as Testing with the comment 'Reproduced the flake'.",
        (
            Step("specivo_timer", {"op": "start", "issue_ref": "{P}-5"}, {"op": "start", "issue_ref": "{P}-5"}),
            Step(
                "specivo_timer",
                {"op": "stop", "activity": "Testing", "comments": "Reproduced the flake"},
                {"op": "stop", "activity": "testing"},
            ),
        ),
    ),
    # --- Planning ----------------------------------------------------------
    Scenario(
        "list-version-issues",
        "List the issues in version 1.0 of {P}.",
        (
            Step("specivo_list_versions", {"project_key": "{P}"}),
            Step(
                ("specivo_list_version_issues", "specivo_list_issues"),
                {"project_key": "{P}", "version_id": "@version_1"},
                {"project_key": "{P}"},
            ),
        ),
    ),
    Scenario(
        "create-version",
        "Create version 2.0 in {P}, due on 2027-03-31.",
        (
            Step(
                "specivo_create_version",
                {"project_key": "{P}", "name": "2.0", "due_date": "2027-03-31"},
                {"project_key": "{P}", "name": "2.0", "due_date": "2027-03-31"},
            ),
        ),
    ),
    Scenario(
        "create-sprint",
        "Create a sprint 'Sprint 2' in {P} from 2027-01-18 to 2027-01-29.",
        (
            Step(
                "specivo_create_sprint",
                {"project_key": "{P}", "name": "Sprint 2", "start_date": "2027-01-18", "end_date": "2027-01-29"},
                {"name": "sprint 2", "start_date": "2027-01-18", "end_date": "2027-01-29"},
            ),
        ),
    ),
    Scenario(
        "start-sprint",
        "Start 'Sprint 1' in {P}.",
        (
            Step("specivo_list_sprints", {"project_key": "{P}"}),
            Step("specivo_start_sprint", {"project_key": "{P}", "sprint_id": "@sprint_1"}, {"sprint_id": "@sprint_1"}),
        ),
    ),
    Scenario(
        "backlog",
        "List the backlog of {P}: the issues in no sprint.",
        (
            Step(
                ("specivo_list_sprint_issues", "specivo_list_issues"),
                {"project_key": "{P}", "sprint_id": 0},
                {"project_key": "{P}"},
            ),
        ),
    ),
    # --- Recurring ---------------------------------------------------------
    Scenario(
        "create-recurring",
        "In {P}, create a recurring Task 'Weekly sync notes' that repeats every Monday at 09:00 Bangkok time "
        "starting 2027-01-04.",
        (
            _lookups(),
            Step(
                "specivo_create_recurring_pattern",
                {
                    "project_key": "{P}",
                    "name": "Weekly sync notes",
                    "template_subject": "Weekly sync notes",
                    "template_tracker_id": "@tracker_task",
                    "freq": "weekly",
                    "byday": "MO",
                    "dtstart": "2027-01-04T09:00:00",
                    "timezone": "Asia/Bangkok",
                },
                {"freq": "weekly", "byday": "MO", "timezone": "Asia/Bangkok", "template_tracker_id": "@tracker_task"},
            ),
        ),
    ),
    Scenario(
        "preview-occurrences",
        "Preview the upcoming occurrences of the 'Weekly status report' recurring pattern in {P}.",
        (
            Step("specivo_list_recurring_patterns", {"project_key": "{P}"}),
            Step(
                "specivo_list_recurrence_occurrences",
                {"project_key": "{P}", "pattern_id": "@pattern_weekly"},
                {"pattern_id": "@pattern_weekly"},
            ),
        ),
    ),
    # --- AI spend ----------------------------------------------------------
    Scenario(
        "log-ai-usage",
        "Log this AI usage on {P}-1: tool claude-code, model claude-sonnet-4-5, 1200 input and 300 output tokens "
        "(the numbers come from /cost).",
        (
            Step(
                "specivo_log_ai_usage",
                {
                    "tool": "claude-code",
                    "model": "claude-sonnet-4-5",
                    "input_tokens": 1200,
                    "output_tokens": 300,
                    "issue_ref": "{P}-1",
                },
                {"tool": "claude-code", "input_tokens": 1200, "output_tokens": 300, "issue_ref": "{P}-1"},
            ),
        ),
    ),
    Scenario(
        "ai-spend-report",
        "Show the AI spend of project {P} over the last 7 days, grouped by model.",
        (
            Step(
                "specivo_ai_usage_report",
                {"project_key": "{P}", "preset": "7d", "group_by": "model"},
                {"project_key": "{P}", "group_by": "model"},
            ),
        ),
    ),
)
