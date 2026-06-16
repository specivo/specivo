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
embedding model that the operator downloads and an index that is built afterward.

If semantic results are empty, the model probably hasn't been fetched and the existing content hasn't been
indexed yet. Keyword search still works in the meantime. To enable semantic search, the operator runs the
model download (`make download-model`) and a backfill to index existing content. See
[Finding issues](../issues/finding.md).

## An AI agent gets a permission error

When a tool call through the [MCP server](../ai-agents/index.md) fails with a permission error, check two
things:

- **The key's user has a role on the project.** Every write enforces the calling user's project
  permission. If the API key belongs to a user (often a service account) with no membership on that project,
  add them with an appropriate role — typically **Agent** or **Developer**.
- **The key has the `mcp` scope.** A key without the `mcp` scope can't use the MCP server at all. Create a
  new key with the `mcp` scope under **Your profile → API keys**. See [API keys](../ai-agents/api-keys.md).

## A file upload was rejected

Specivo has no built-in file-type restriction, so a rejected upload is almost always a **size limit**. The
maximum upload size is set by the operator (the web server's `client_max_body_size`). Ask your administrator
what the limit is, or split the file. See [Attachments](../issues/attachments.md).

## Registration is closed

If the sign-up page is unavailable or rejects new accounts, the instance is running in a closed registration
mode — self-service sign-up is turned off. Ask an administrator to create an account for you or send you an
invite.
