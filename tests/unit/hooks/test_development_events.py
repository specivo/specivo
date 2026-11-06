"""Parsing CI, deployment, tag, release and review payloads, and the pure rules over them."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from specivo.hooks import development as dev
from specivo.hooks.providers import get_provider
from specivo.hooks.providers.base import is_confidential
from specivo.hooks.refs import extract_ignored, extract_refs
from tests.fixtures import forge_activity as fa

pytestmark = pytest.mark.unit


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


class TestEventKinds:
    @pytest.mark.parametrize(
        ("provider", "event", "kind"),
        [
            ("github", "check_run", "ci"),
            ("github", "check_suite", "ci"),
            ("github", "workflow_run", "ci"),
            ("github", "workflow_job", "ci"),
            ("github", "deployment", "deployment"),
            ("github", "deployment_status", "deployment"),
            ("github", "create", "release"),
            ("github", "release", "release"),
            ("github", "pull_request_review", "review"),
            ("github", "issues", None),
            ("github", "star", None),
            ("gitlab", "pipeline", "ci"),
            ("gitlab", "job", "ci"),
            ("gitlab", "deployment", "deployment"),
            ("gitlab", "release", "release"),
            ("gitlab", "tag_push", "push"),
            ("gitlab", "confidential_issue", None),
            ("gitlab", "note", None),
            ("gitea", "workflow_run", "ci"),
            ("gitea", "workflow_job", "ci"),
            ("gitea", "pull_request_approved", "review"),
            ("gitea", "pull_request_review_rejected", "review"),
            ("gitea", "create", "release"),
            ("gitea", "deployment", None),
            ("forgejo", "release", "release"),
        ],
    )
    def test_kind(self, provider, event, kind):
        assert get_provider(provider).event_kind(event) == kind

    @pytest.mark.parametrize(
        ("header", "event"), [("Job Hook", "job"), ("Build Hook", "job"), ("Pipeline Hook", "pipeline")]
    )
    def test_gitlab_event_names(self, header, event):
        assert get_provider("gitlab").event_type({"X-Gitlab-Event": header}) == event


class TestCiParsing:
    def test_github_workflow_run(self):
        run = get_provider("github").parse_ci("workflow_run", fa.workflow_run(pull_requests=(7,), attempt=2))
        assert run is not None
        assert (run.run_type, run.external_id, run.name, run.status) == ("workflow_run", 501, "CI", "success")
        assert (run.head_sha, run.branch, run.pull_request_numbers, run.attempt) == (fa.SHA_A, "feat/login", (7,), 2)
        assert run.updated_at == _at("2026-10-05T10:00:00")

    def test_github_workflow_job_names_the_workflow(self):
        run = get_provider("github").parse_ci("workflow_job", fa.workflow_job())
        assert run is not None
        assert (run.name, run.status) == ("CI / test", "failure")

    def test_github_check_run_and_suite(self):
        github = get_provider("github")
        check = github.parse_ci("check_run", fa.check_run(status="in_progress", conclusion=None, completed_at=None))
        assert check is not None and check.status == "running" and check.branch == "feat/login"
        suite = github.parse_ci("check_suite", fa.check_suite(conclusion="timed_out"))
        assert suite is not None and (suite.name, suite.status) == ("GitHub Actions", "failure")

    def test_gitea_workflow_run_is_github_shaped(self):
        run = get_provider("gitea").parse_ci("workflow_run", fa.workflow_run(conclusion="failure"))
        assert run is not None and run.status == "failure"

    def test_gitlab_pipeline_takes_the_latest_build_time(self):
        payload = fa.gitlab_pipeline(
            status="running", finished_at=None, build_times=("2026-10-05 10:30:00 UTC",), mr_iid=7
        )
        run = get_provider("gitlab").parse_ci("pipeline", payload)
        assert run is not None
        assert (run.status, run.pull_request_numbers) == ("running", (7,))
        assert run.updated_at == _at("2026-10-05T10:30:00")
        assert run.url.endswith("/-/pipelines/3101")

    def test_gitlab_job(self):
        run = get_provider("gitlab").parse_ci("job", fa.gitlab_job())
        assert run is not None
        assert (run.run_type, run.name, run.status) == ("job", "test / rspec", "failure")
        assert run.url == f"https://gitlab.example.com/{fa.REPO}/-/jobs/3201"

    def test_a_payload_without_an_id_is_no_run(self):
        payload = fa.workflow_run()
        del payload["workflow_run"]["id"]
        assert get_provider("github").parse_ci("workflow_run", payload) is None

    def test_a_script_url_is_never_kept(self):
        payload = fa.workflow_run()
        payload["workflow_run"]["html_url"] = "javascript:alert(1)"
        run = get_provider("github").parse_ci("workflow_run", payload)
        assert run is not None and run.url == ""


class TestDeploymentParsing:
    def test_github_deployment_then_status(self):
        github = get_provider("github")
        created = github.parse_deployment("deployment", fa.github_deployment())
        assert created is not None and (created.status, created.environment) == ("pending", "production")
        done = github.parse_deployment("deployment_status", fa.github_deployment_status(state="success"))
        assert done is not None and (done.status, done.url) == ("success", "https://app.example.com")
        assert done.updated_at == _at("2026-10-06T08:05:00")

    @pytest.mark.parametrize(
        ("state", "status"), [("in_progress", "running"), ("error", "failure"), ("inactive", "inactive")]
    )
    def test_github_states(self, state, status):
        event = get_provider("github").parse_deployment("deployment_status", fa.github_deployment_status(state=state))
        assert event is not None and event.status == status

    def test_gitlab_deployment(self):
        event = get_provider("gitlab").parse_deployment("deployment", fa.gitlab_deployment(status="failed"))
        assert event is not None
        assert (event.external_id, event.environment, event.status, event.ref) == (3301, "staging", "failure", "main")


class TestReleaseParsing:
    def test_github_create_of_a_branch_is_not_a_tag(self):
        payload = fa.github_create_tag()
        payload["ref_type"] = "branch"
        assert get_provider("github").parse_release("create", payload) is None

    def test_github_create_tag_has_no_sha_gitea_does(self):
        assert get_provider("github").parse_release("create", fa.github_create_tag()).sha is None
        assert get_provider("gitea").parse_release("create", fa.github_create_tag(sha=fa.SHA_B)).sha == fa.SHA_B

    def test_github_release_draft_and_deleted(self):
        github = get_provider("github")
        assert github.parse_release("release", fa.github_release()).is_release is True
        assert github.parse_release("release", fa.github_release(draft=True, action="created")).is_release is False
        deleted = github.parse_release("release", fa.github_release(action="deleted"))
        assert (deleted.is_release, deleted.release_deleted) == (False, True)

    def test_gitlab_release_and_tag_push(self):
        gitlab = get_provider("gitlab")
        release = gitlab.parse_release("release", fa.gitlab_release())
        assert (release.tag_name, release.sha, release.is_release) == ("v1.2.0", fa.SHA_A, True)
        push = gitlab.parse_push(fa.gitlab_tag_push())
        assert (push.tag, push.after, push.deleted) == ("v1.2.0", fa.SHA_A, False)
        gone = gitlab.parse_push(fa.gitlab_tag_push(deleted=True))
        assert (gone.tag, gone.after, gone.deleted) == ("v1.2.0", None, True)


class TestReviewParsing:
    def test_github_review(self):
        (review,) = get_provider("github").parse_reviews(
            "pull_request_review", fa.github_review(state="CHANGES_REQUESTED")
        )
        assert (review.number, review.reviewer_login, review.state) == (7, "sam-example", "changes_requested")

    def test_github_dismissed(self):
        (review,) = get_provider("github").parse_reviews("pull_request_review", fa.github_review(action="dismissed"))
        assert review.state == "dismissed"

    def test_gitea_review_events(self):
        gitea = get_provider("gitea")
        (approved,) = gitea.parse_reviews("pull_request_approved", fa.gitea_review())
        (rejected,) = gitea.parse_reviews("pull_request_rejected", fa.gitea_review(approved=False))
        assert (approved.state, rejected.state) == ("approved", "changes_requested")

    def test_gitlab_approval_and_withdrawal(self):
        from tests.fixtures.forge_hooks import pull_request_payload

        gitlab = get_provider("gitlab")
        payload = pull_request_payload("gitlab")
        payload["object_attributes"]["action"] = "approved"
        payload["user"]["username"] = "sam-example"
        (review,) = gitlab.parse_reviews("pull_request", payload)
        assert (review.reviewer_login, review.state) == ("sam-example", "approved")
        payload["object_attributes"]["action"] = "unapproved"
        (withdrawn,) = gitlab.parse_reviews("pull_request", payload)
        assert withdrawn.state is None

    def test_gitlab_reviewer_states(self):
        from tests.fixtures.forge_hooks import pull_request_payload

        payload = pull_request_payload("gitlab")
        payload["reviewers"] = [
            {"username": "kim-example", "state": "requested_changes"},
            {"username": "x", "state": "unreviewed"},
        ]
        (review,) = get_provider("gitlab").parse_reviews("pull_request", payload)
        assert (review.reviewer_login, review.state) == ("kim-example", "changes_requested")


class TestOrdering:
    def test_a_rerun_is_newer_than_anything_before(self):
        late = dev.ci_order_key(1, _at("2026-10-05T12:00:00"), "failure")
        rerun = dev.ci_order_key(2, _at("2026-10-05T11:00:00"), "pending")
        assert rerun > late

    def test_running_never_undoes_a_finished_run_of_the_same_time(self):
        at = _at("2026-10-05T12:00:00")
        assert dev.ci_order_key(1, at, "running") < dev.ci_order_key(1, at, "success")

    def test_an_event_without_time_is_older(self):
        assert dev.deployment_order_key(None, "success") < dev.deployment_order_key(
            _at("2026-10-05T12:00:00"), "pending"
        )

    def test_reviews(self):
        t1, t2 = _at("2026-10-04T10:00:00"), _at("2026-10-04T11:00:00")
        assert dev.review_replaces("approved", t1, "changes_requested", t2)
        assert not dev.review_replaces("changes_requested", t2, "approved", t1)
        # A comment does not withdraw a verdict.
        assert not dev.review_replaces("approved", t1, "commented", t2)
        assert dev.review_replaces("commented", t1, "approved", t2)


class TestEnvironmentTypes:
    @pytest.mark.parametrize(
        ("name", "env_type"),
        [
            ("production", "production"),
            ("prod", "production"),
            ("Production-EU", "production"),
            ("live", "production"),
            ("staging", "staging"),
            ("stage", "staging"),
            ("pre-prod", "staging"),
            ("preprod", "staging"),
            ("pre_production", "staging"),
            ("test", "testing"),
            ("qa", "testing"),
            ("uat", "testing"),
            ("dev", "development"),
            ("development", "development"),
            ("review/feat-login", "development"),
            ("github-pages", None),
            ("eu-west-1", None),
        ],
    )
    def test_guess(self, name, env_type):
        assert dev.guess_environment_type(name) == env_type

    def test_override_wins_over_the_guess(self):
        overrides = {"Pages": "production", "qa*": "staging", "qa-fast*": "testing"}
        assert dev.environment_type("pages", overrides) == "production"
        assert dev.environment_type("qa-eu", overrides) == "staging"
        assert dev.environment_type("qa-fast-1", overrides) == "testing"  # the longer pattern
        assert dev.environment_type("production", overrides) == "production"
        assert dev.environment_type("eu-west-1", overrides) is None


class TestCombined:
    def test_pull_requests(self):
        assert dev.combined_pull_request_state(["merged", "draft"]) == "open"
        assert dev.combined_pull_request_state(["closed", "merged"]) == "merged"
        assert dev.combined_pull_request_state(["closed"]) == "closed"
        assert dev.combined_pull_request_state([]) is None

    def test_reviews(self):
        assert dev.combined_review_state(["approved", "changes_requested"]) == "changes_requested"
        assert dev.combined_review_state(["approved", "commented"]) == "approved"
        assert dev.combined_review_state(["dismissed"]) is None

    def test_ci_counts_only_the_latest_run_of_each_check(self):
        t1, t2 = _at("2026-10-05T10:00:00"), _at("2026-10-05T11:00:00")
        failed_then_fixed = [
            dev.CiLatest(("ci",), "failure", dev.ci_order_key(1, t1, "failure")),
            dev.CiLatest(("ci",), "success", dev.ci_order_key(1, t2, "success")),
        ]
        assert dev.combined_ci_state(failed_then_fixed) == "passing"
        other_failing = [*failed_then_fixed, dev.CiLatest(("lint",), "failure", dev.ci_order_key(1, t1, "failure"))]
        assert dev.combined_ci_state(other_failing) == "failing"
        running = [*failed_then_fixed, dev.CiLatest(("e2e",), "running", dev.ci_order_key(1, t1, "running"))]
        assert dev.combined_ci_state(running) == "running"
        assert dev.combined_ci_state([dev.CiLatest(("x",), "skipped", dev.ci_order_key(1, t1, "skipped"))]) == "neutral"
        assert dev.combined_ci_state([]) is None


class TestConfidential:
    def test_gitlab_confidential_payloads(self):
        assert is_confidential("confidential_issue", fa.gitlab_confidential_issue())
        assert is_confidential("confidential_note", fa.gitlab_confidential_note())
        # Flagged in the payload even under another event name.
        assert is_confidential("issue", fa.gitlab_confidential_issue())
        assert is_confidential("note", fa.gitlab_confidential_note())
        assert not is_confidential("pipeline", fa.gitlab_pipeline())


class TestIgnoreKeyword:
    KEYS = frozenset({"ACME"})

    def test_ignore_drops_the_issue(self):
        assert extract_refs("Ignore ACME-1", self.KEYS) == []
        assert extract_ignored("Ignore ACME-1", self.KEYS) == {"ACME-1"}

    def test_is_case_insensitive_and_shares_a_list(self):
        text = "IGNORE: ACME-1, ACME-2 and ACME-3; refs ACME-4"
        assert [r.display_key for r in extract_refs(text, self.KEYS)] == ["ACME-4"]
        assert extract_ignored(text, self.KEYS) == {"ACME-1", "ACME-2", "ACME-3"}

    def test_ignore_wins_over_another_mention_in_the_same_text(self):
        assert extract_refs("Fixes ACME-1. ignore ACME-1", self.KEYS) == []

    def test_a_word_containing_ignore_is_no_keyword(self):
        assert [r.display_key for r in extract_refs("ignored ACME-1", self.KEYS)] == ["ACME-1"]
