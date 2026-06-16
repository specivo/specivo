---
description: Connect an AI client to the Specivo MCP server — get the server URL and an mcp-scoped key, then add the SSE or Codex configuration.
---

# Connecting an AI client

Connecting takes two pieces of information and one config edit. This page covers both transports —
SSE for Claude Code, Cursor, Windsurf, and Cline, and Streamable HTTP for the Codex CLI.

## Before you start

1. **Know your server URL.** Replace `your-specivo-host` in the snippets below with your actual
   Specivo host (for example, `specivo.example.com`).
2. **Get an `mcp`-scoped key.** Create one under **Your profile → API keys** if you haven't already.
   See [API keys & scopes](api-keys.md). Each snippet uses the placeholder `spv_your_api_key_here` —
   substitute your real `spv_…` key.

## Add the configuration

The two endpoints serve the same tools; choose the tab that matches your client.

=== "SSE clients"

    For **Claude Code**, **Cursor**, **Windsurf**, and **Cline**, point the client at the SSE
    endpoint (`/mcp/sse/`) and pass the key as a bearer token:

    ```json
    {
      "mcpServers": {
        "specivo": {
          "type": "sse",
          "url": "https://your-specivo-host/mcp/sse/",
          "headers": { "Authorization": "Bearer spv_your_api_key_here" }
        }
      }
    }
    ```

    Add this to your client's MCP configuration, then restart or reload the client so it picks up
    the new server.

=== "Codex CLI"

    For the **Codex CLI**, use the Streamable HTTP endpoint (`/mcp/`). Add the server to
    `~/.codex/config.toml`:

    ```toml
    [mcp_servers.specivo]
    url = "https://your-specivo-host/mcp/"
    bearer_token_env_var = "SPECIVO_API_KEY"
    ```

    Then export the key in your shell so Codex can read it from the named environment variable:

    ```bash
    export SPECIVO_API_KEY=spv_your_api_key_here
    ```

## Verify the connection

Once the client reloads, it should discover the `specivo_…` tools. Ask the agent to run
`specivo_whoami` — it returns the user the key belongs to, confirming both the connection and which
account the agent is acting as.

!!! tip "Let Specivo generate the snippet for you"
    You don't have to copy the config by hand. The server provides a **`specivo_setup_guide`** tool
    and a **`specivo://docs/agent-setup`** resource that emit a ready-made configuration snippet for
    your client, already filled in for your host. Ask a connected agent to call the setup guide, or
    point your client at the resource, to get a copy-paste-ready block.

!!! warning "MCP request returns a permission error"
    If the agent connects but a tool call is refused, the key's account most likely lacks the
    required role on that project, or the key is missing the **`mcp`** scope. Check both on the
    [API keys](api-keys.md) page and the project's member list.
