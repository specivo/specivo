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

The headers each forge sends with them (event, delivery id, signature or token) are set by the
tests; see `specivo/hooks/providers/` for which headers each provider reads.
