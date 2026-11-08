"""Payloads for development activity webhooks: CI, deployments, tags and releases, reviews.

Minimal but realistic: the fields each forge sends that Specivo reads, in the
forge's own shape, plus the repository block the endpoint checks.
"""

from __future__ import annotations

from typing import Any

from tests.fixtures.forge_hooks import REPO

SHA_A = "a" * 40
SHA_B = "b" * 40


def _github_repo(repo: str = REPO) -> dict[str, Any]:
    return {"id": 1001, "full_name": repo, "default_branch": "main", "private": True}


def _gitlab_project(repo: str = REPO) -> dict[str, Any]:
    return {
        "id": 2002,
        "path_with_namespace": repo,
        "default_branch": "main",
        "web_url": f"https://gitlab.example.com/{repo}",
    }


# -- GitHub (and the GitHub-shaped Gitea/Forgejo Actions events) ------------


def workflow_run(
    *,
    run_id: int = 501,
    name: str = "CI",
    status: str = "completed",
    conclusion: str | None = "success",
    sha: str = SHA_A,
    branch: str = "feat/login",
    attempt: int = 1,
    updated_at: str = "2026-10-05T10:00:00Z",
    pull_requests: tuple[int, ...] = (),
    repo: str = REPO,
) -> dict[str, Any]:
    return {
        "action": "completed" if status == "completed" else "in_progress",
        "workflow_run": {
            "id": run_id,
            "name": name,
            "head_branch": branch,
            "head_sha": sha,
            "status": status,
            "conclusion": conclusion,
            "html_url": f"https://forge.example.com/{repo}/actions/runs/{run_id}",
            "run_attempt": attempt,
            "run_started_at": "2026-10-05T09:55:00Z",
            "created_at": "2026-10-05T09:55:00Z",
            "updated_at": updated_at,
            "pull_requests": [{"number": n} for n in pull_requests],
        },
        "repository": _github_repo(repo),
        "sender": {"login": "alex-example"},
    }


def workflow_job(
    *,
    job_id: int = 601,
    name: str = "test",
    status: str = "completed",
    conclusion: str | None = "failure",
    sha: str = SHA_A,
    branch: str = "feat/login",
    completed_at: str | None = "2026-10-05T10:01:00Z",
    repo: str = REPO,
) -> dict[str, Any]:
    return {
        "action": "completed",
        "workflow_job": {
            "id": job_id,
            "run_id": 501,
            "workflow_name": "CI",
            "name": name,
            "head_sha": sha,
            "head_branch": branch,
            "status": status,
            "conclusion": conclusion,
            "html_url": f"https://forge.example.com/{repo}/actions/runs/501/job/{job_id}",
            "run_attempt": 1,
            "started_at": "2026-10-05T09:56:00Z",
            "completed_at": completed_at,
            "created_at": "2026-10-05T09:55:30Z",
        },
        "repository": _github_repo(repo),
    }


def check_run(
    *,
    check_id: int = 701,
    name: str = "lint",
    status: str = "completed",
    conclusion: str | None = "success",
    sha: str = SHA_A,
    started_at: str = "2026-10-05T09:57:00Z",
    completed_at: str | None = "2026-10-05T09:58:00Z",
    pull_requests: tuple[int, ...] = (),
) -> dict[str, Any]:
    return {
        "action": "completed",
        "check_run": {
            "id": check_id,
            "name": name,
            "head_sha": sha,
            "status": status,
            "conclusion": conclusion,
            "started_at": started_at,
            "completed_at": completed_at,
            "html_url": f"https://forge.example.com/{REPO}/runs/{check_id}",
            "check_suite": {"id": 801, "head_branch": "feat/login"},
            "pull_requests": [{"number": n} for n in pull_requests],
        },
        "repository": _github_repo(),
    }


def check_suite(*, suite_id: int = 801, conclusion: str = "success", sha: str = SHA_A) -> dict[str, Any]:
    return {
        "action": "completed",
        "check_suite": {
            "id": suite_id,
            "head_branch": "feat/login",
            "head_sha": sha,
            "status": "completed",
            "conclusion": conclusion,
            "created_at": "2026-10-05T09:55:00Z",
            "updated_at": "2026-10-05T10:02:00Z",
            "app": {"name": "GitHub Actions"},
            "pull_requests": [],
        },
        "repository": _github_repo(),
    }


def github_deployment(
    *, deployment_id: int = 901, environment: str = "production", sha: str = SHA_A, ref: str = "main"
) -> dict[str, Any]:
    return {
        "action": "created",
        "deployment": {
            "id": deployment_id,
            "sha": sha,
            "ref": ref,
            "environment": environment,
            "created_at": "2026-10-06T08:00:00Z",
            "updated_at": "2026-10-06T08:00:00Z",
        },
        "repository": _github_repo(),
    }


def github_deployment_status(
    *,
    deployment_id: int = 901,
    state: str = "success",
    environment: str = "production",
    sha: str = SHA_A,
    ref: str = "main",
    at: str = "2026-10-06T08:05:00Z",
) -> dict[str, Any]:
    payload = github_deployment(deployment_id=deployment_id, environment=environment, sha=sha, ref=ref)
    payload["action"] = "created"
    payload["deployment_status"] = {
        "state": state,
        "environment": environment,
        "environment_url": "https://app.example.com",
        "log_url": f"https://forge.example.com/{REPO}/actions/runs/1",
        "created_at": at,
        "updated_at": at,
    }
    return payload


def github_create_tag(tag: str = "v1.2.0", *, sha: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ref": tag,
        "ref_type": "tag",
        "master_branch": "main",
        "repository": _github_repo(),
        "sender": {"login": "alex-example"},
    }
    if sha:
        payload["sha"] = sha  # Gitea and Forgejo send it
    return payload


def github_release(
    tag: str = "v1.2.0", *, action: str = "published", draft: bool = False, name: str = "Version 1.2"
) -> dict[str, Any]:
    return {
        "action": action,
        "release": {
            "id": 1201,
            "tag_name": tag,
            "target_commitish": "main",
            "name": name,
            "draft": draft,
            "prerelease": False,
            "html_url": f"https://forge.example.com/{REPO}/releases/tag/{tag}",
            "created_at": "2026-10-07T09:00:00Z",
            "published_at": "2026-10-07T09:00:00Z",
        },
        "repository": _github_repo(),
    }


def github_review(
    *,
    number: int = 7,
    reviewer: str = "sam-example",
    state: str = "approved",
    submitted_at: str = "2026-10-04T12:00:00Z",
    action: str = "submitted",
) -> dict[str, Any]:
    return {
        "action": action,
        "review": {
            "id": 1301,
            "user": {"login": reviewer},
            "state": state,
            "submitted_at": submitted_at,
            "html_url": f"https://forge.example.com/{REPO}/pull/{number}#pullrequestreview-1301",
        },
        "pull_request": {"number": number, "html_url": f"https://forge.example.com/{REPO}/pull/{number}"},
        "repository": _github_repo(),
    }


def gitea_review(*, number: int = 7, reviewer: str = "sam-example", approved: bool = True) -> dict[str, Any]:
    return {
        "action": "reviewed",
        "number": number,
        "pull_request": {
            "number": number,
            "updated_at": "2026-10-04T12:00:00Z",
            "html_url": f"https://forge.example.com/{REPO}/pulls/{number}",
        },
        "review": {"type": "pull_request_review_approved" if approved else "pull_request_review_rejected"},
        "repository": _github_repo(),
        "sender": {"login": reviewer, "username": reviewer},
    }


# -- GitLab -----------------------------------------------------------------


def gitlab_pipeline(
    *,
    pipeline_id: int = 3101,
    status: str = "success",
    sha: str = SHA_A,
    ref: str = "feat/login",
    finished_at: str | None = "2026-10-05 10:00:00 UTC",
    build_times: tuple[str, ...] = (),
    mr_iid: int | None = None,
    repo: str = REPO,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "object_kind": "pipeline",
        "object_attributes": {
            "id": pipeline_id,
            "ref": ref,
            "tag": False,
            "sha": sha,
            "status": status,
            "created_at": "2026-10-05 09:50:00 UTC",
            "finished_at": finished_at,
            "url": f"https://gitlab.example.com/{repo}/-/pipelines/{pipeline_id}",
        },
        "project": _gitlab_project(repo),
        "builds": [{"id": 1, "started_at": t, "finished_at": None} for t in build_times],
    }
    if mr_iid is not None:
        payload["merge_request"] = {"iid": mr_iid}
    return payload


def gitlab_job(*, job_id: int = 3201, status: str = "failed", sha: str = SHA_A, ref: str = "feat/login") -> dict:
    return {
        "object_kind": "build",
        "build_id": job_id,
        "build_name": "rspec",
        "build_stage": "test",
        "build_status": status,
        "build_created_at": "2026-10-05 09:50:00 UTC",
        "build_started_at": "2026-10-05 09:51:00 UTC",
        "build_finished_at": "2026-10-05 09:59:00 UTC",
        "sha": sha,
        "ref": ref,
        "tag": False,
        "pipeline_id": 3101,
        "project": _gitlab_project(),
    }


def gitlab_deployment(
    *,
    deployment_id: int = 3301,
    status: str = "success",
    environment: str = "staging",
    sha: str = SHA_A,
    ref: str = "main",
    at: str = "2026-10-06 08:05:00 UTC",
) -> dict[str, Any]:
    return {
        "object_kind": "deployment",
        "status": status,
        "status_changed_at": at,
        "deployment_id": deployment_id,
        "deployable_url": f"https://gitlab.example.com/{REPO}/-/jobs/42",
        "environment": environment,
        "environment_external_url": "https://staging.example.com",
        "sha": sha,
        "ref": ref,
        "project": _gitlab_project(),
    }


def gitlab_release(tag: str = "v1.2.0", *, sha: str = SHA_A, action: str = "create") -> dict[str, Any]:
    return {
        "object_kind": "release",
        "action": action,
        "tag": tag,
        "name": f"Release {tag}",
        "url": f"https://gitlab.example.com/{REPO}/-/releases/{tag}",
        "released_at": "2026-10-07 09:00:00 UTC",
        "created_at": "2026-10-07 09:00:00 UTC",
        "commit": {"id": sha},
        "project": _gitlab_project(),
    }


def gitlab_tag_push(tag: str = "v1.2.0", *, sha: str = SHA_A, deleted: bool = False) -> dict[str, Any]:
    return {
        "object_kind": "tag_push",
        "ref": f"refs/tags/{tag}",
        "before": "0" * 40,
        "after": "0" * 40 if deleted else sha,
        "checkout_sha": None if deleted else sha,
        "user_username": "alex-example",
        "commits": [],
        "project": _gitlab_project(),
    }


def gitlab_confidential_issue() -> dict[str, Any]:
    return {
        "object_kind": "issue",
        "event_type": "confidential_issue",
        "object_attributes": {"iid": 3, "title": "Secret incident", "confidential": True, "description": "x"},
        "project": _gitlab_project(),
    }


def gitlab_confidential_note() -> dict[str, Any]:
    return {
        "object_kind": "note",
        "event_type": "confidential_note",
        "object_attributes": {"note": "Leaked credentials rotated", "noteable_type": "Issue", "internal": True},
        "issue": {"iid": 3, "title": "Secret incident", "confidential": True},
        "project": _gitlab_project(),
    }


GITLAB_EVENT_HEADERS = {
    "pipeline": "Pipeline Hook",
    "job": "Job Hook",
    "deployment": "Deployment Hook",
    "release": "Release Hook",
    "tag_push": "Tag Push Hook",
    "confidential_issue": "Confidential Issue Hook",
    "confidential_note": "Confidential Note Hook",
    "issue": "Issue Hook",
}
