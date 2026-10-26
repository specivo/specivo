---
description: Practical answers to common Specivo problems — sign-in, empty search results, AI agent permission errors, rejected uploads, and closed registration.
---

# Troubleshooting & FAQ

Short answers to the problems users hit most often. If your question isn't here, check the relevant section
of the guide or ask your Specivo administrator.

## I can't sign in / I forgot my password

First confirm you're using the right username and the correct address for your instance. If you've
forgotten your password, use the **Forgot password** link on the sign-in page to receive a reset email —
this requires the operator to have configured outbound email (SMTP).

If no reset email arrives, or password reset isn't set up, ask your administrator to reset your password for
you. Repeated failed attempts may temporarily lock you out; wait a moment and try again.

## Search returns nothing

Specivo has two search modes. **Keyword search** works out of the box and matches text in issues, comments,
wiki pages, and attachment descriptions. **Semantic search** (finding results by meaning) needs an on-device
embedding model that the operator downloads, and an index that is built afterward.

If semantic results are empty, the model files are probably missing from the server's data directory, or
the existing content hasn't been indexed yet. Keyword search still works in the meantime. The operator
downloads the model (`make download-model`) and runs the indexing command
(`docker compose exec api python -m specivo.cli.backfill_embeddings`). After that, new and edited
content is indexed as it is saved: keyword search sees it at once, and semantic search a few seconds
later, provided the `celery-worker` container is running. The steps, a way to check each one, and
fixes are in
[Semantic search and embeddings](../install/semantic-search.md). See also [Finding issues](../issues/finding.md).

If searching for an issue key like `ACME-42` finds the issue itself but not older issues or wiki pages
that mention it, the search index predates key matching. An administrator can rebuild it with
**Reindex now** under **Administration → Settings**, or `make reindex-fts` from the command line. The
reindex runs in the background in small batches. Content you edit is updated as you save it.

## An AI agent gets a permission error

When a tool call through the [MCP server](../ai-agents/index.md) fails with a permission error, check two
things:

- **The key's user has a role on the project.** Every write enforces the calling user's project
  permission. If the API key belongs to a user (often a service account) with no membership on that project,
  add them with an appropriate role — typically **Agent** or **Developer**.
- **The key has the `mcp` scope.** A key without the `mcp` scope can't use the MCP server at all. Create a
  new key with the `mcp` scope under **Your profile → API keys**. See [API keys](../ai-agents/api-keys.md).

## I can't see time entries

Reading time needs the **View time entries** permission in the project, separately from **Log time**.
Out of the box, Reporters and people with no membership don't have it. A role can also be limited to
**Own time entries**, which shows only the hours you logged yourself. Ask a project manager or an
administrator to check your role. See [Time tracking](../projects/time-tracking.md#who-can-do-what).

## A script gets "CSRF validation failed"

A request made with the sign-in **cookie** has to send the `X-CSRF-Token` header on anything that
changes data, including change-password and logout. The value is the `csrf_token` cookie. Scripts and
agents should use an [API key](../ai-agents/api-keys.md) instead, which needs no token.

## Text is shown without formatting

A short notice appears above it when a description, comment or page is too large or too complex to
format. Nothing was lost. See [Size and complexity limits](markdown.md#size-and-complexity-limits).

## A file upload was rejected

Specivo has no built-in file-type restriction, so a rejected upload is almost always a **size limit**. The
maximum upload size is set by the operator (the web server's `client_max_body_size`). Ask your administrator
what the limit is, or split the file. See [Attachments](../issues/attachments.md).

## Registration is closed

If the sign-up page is unavailable or rejects new accounts, the instance is running in a closed registration
mode — self-service sign-up is turned off. Ask an administrator to create an account for you or send you an
invite.
