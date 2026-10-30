# Forge webhook fixtures

Webhook payloads in the shape each forge sends, for the repository integration tests. They are
written by hand after the providers' published webhook documentation and examples; every name,
address and URL is made up (`example.com`, `example.org`), and file lists are trimmed.

| File | Event | Shaped after |
|---|---|---|
| `github_push.json` | GitHub `push` | GitHub Docs, "Webhook events and payloads", `push`, and the example delivery in "Testing webhooks" |
| `github_ping.json` | GitHub `ping` | GitHub Docs, "Webhook events and payloads", `ping` |
| `gitlab_push.json` | GitLab `Push Hook` | GitLab Docs, "Webhook events", "Push events" example |
| `gitea_push.json` | Gitea `push` | Gitea Docs, "Webhooks", the example push payload, with the repository fields current releases send |
| `forgejo_push.json` | Forgejo `push` | Forgejo Docs, "Webhooks" (the payload is the Gitea one) |
| `github_pull_request.json` | GitHub `pull_request` (`closed`, merged) | GitHub Docs, "Webhook events and payloads", `pull_request`, and the REST "Get a pull request" response it embeds |
| `gitlab_merge_request.json` | GitLab `Merge Request Hook` (`open`, draft) | GitLab Docs, "Webhook events", "Merge request events" example |
| `gitea_pull_request.json` | Gitea `pull_request` (`opened`) | Gitea Docs, "Webhooks", and the `PullRequest` structure of the Gitea API the payload embeds |
| `forgejo_pull_request.json` | Forgejo `pull_request` (`closed`, merged) | Forgejo Docs, "Webhooks" (the payload is the Gitea one), with the merge fields the API documents |

The headers each forge sends with them (event, delivery id, signature or token) are set by the
tests; see `specivo/hooks/providers/` for which headers each provider reads.

`pull_request_payload(..., external=True)` in `tests/fixtures/forge_hooks.py` turns any of the
pull request fixtures into one opened from a fork: a different head repository id on GitHub,
Gitea and Forgejo (`head.repo.id`, `head.repo_id`), a different `source_project_id` on GitLab.
