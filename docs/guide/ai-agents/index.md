---
description: What the Specivo MCP server is — a built-in endpoint that lets AI clients read and update issues, wiki, and search safely, with every write recorded in the audit log.
---

# AI agents & the MCP server

Specivo ships with a built-in **MCP server** — Model Context Protocol, the open standard that AI
clients use to talk to outside tools. With it, an AI assistant can work *inside* your tracker:
read an issue, search the wiki, leave a comment, log time, or update status — instead of you
copying text back and forth.

Any client that speaks MCP can connect: **Claude Code**, **Codex**, **Cursor**, **Windsurf**,
**Cline**, and others. The agent authenticates with an API key tied to a real Specivo user, so it
only sees and changes what that user is allowed to, and every write it makes is recorded.

## How it works

An AI client connects to your Specivo server's MCP endpoint using an API key. Once connected, the
client discovers a catalog of around 40 tools (all prefixed `specivo_`) and calls them on your
behalf when you ask it to. You stay in control: you decide which client to connect, which account's
key it uses, and you can cut off access at any time.

Two things make this safe to hand to an agent:

- **Permissions are enforced.** The agent acts as the user behind the key. A key from an account
  with no write role on a project can read but not change it. See [API keys & scopes](api-keys.md).
- **Everything is audited.** Every write goes to the security audit log tagged `source=mcp`, so a
  project manager can see exactly what an agent did and when. See [What agents can do](capabilities.md).

## Two endpoints

The MCP server exposes the same tools over two transports. Pick whichever your client supports:

| Endpoint | Transport | Used by |
|---|---|---|
| `https://your-specivo-host/mcp/` | Streamable HTTP | Codex CLI |
| `https://your-specivo-host/mcp/sse/` | SSE (Server-Sent Events) | Claude Code, Cursor, Windsurf, Cline |

Both serve the identical tool catalog — the difference is only how the client talks to the server.
See [Connecting an AI client](connecting.md) for the exact configuration each one needs.

## Where to go next

- **[API keys & scopes](api-keys.md)** — create a key with the `mcp` scope, and why teams use a
  dedicated service account.
- **[Connecting an AI client](connecting.md)** — copy-paste config for SSE clients and Codex.
- **[What agents can do](capabilities.md)** — the tool catalog by area, read vs write, and auditing.
- **[Common workflows](workflows.md)** — concrete things to ask your agent to do.

!!! note "The agent works as a teammate"
    An MCP-connected agent is not a separate integration sitting beside Specivo — it logs in as a
    member, follows the same permission rules as a person, and leaves the same trail of comments,
    journal entries, and audit records. Treat its key like you would any account's credentials.
