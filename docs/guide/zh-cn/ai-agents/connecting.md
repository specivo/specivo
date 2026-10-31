---
description: "介绍如何把 Claude Code、Cursor、Codex CLI 等 AI 客户端连接到 Specivo 的 MCP 服务器：获取服务器地址和 API 密钥，再添加 SSE 或 Streamable HTTP 配置。"
---

# 连接 AI 客户端 {#connecting-an-ai-client}

连接只需要两条信息和一次配置修改。本页介绍两种传输方式：Claude Code、Cursor、Windsurf 和 Cline 使用 SSE，Codex CLI 使用 Streamable HTTP。

## 开始之前 {#before-you-start}

1. **确认服务器地址。** 把下面示例中的 `your-specivo-host` 替换成你实际的 Specivo 主机名（例如 `specivo.example.com`）。
2. **准备一个 API 密钥。** 如果还没有，在 **个人资料 → API 密钥** 中创建一个，详见 [API 密钥与作用域](api-keys.md)。各示例中的 `spv_your_api_key_here` 只是占位符，请换成你真实的 `spv_…` 密钥。

## 添加配置 {#add-the-configuration}

两个端点提供的工具完全相同，按你的客户端选择对应的标签页即可。

=== "SSE 客户端"

    **Claude Code**、**Cursor**、**Windsurf** 和 **Cline** 使用 SSE 端点（`/mcp/sse/`），并以 bearer token 的形式传递密钥：

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

    把这段配置加入客户端的 MCP 配置中，然后重启或重新加载客户端，让它识别新的服务器。

=== "Codex CLI"

    **Codex CLI** 使用 Streamable HTTP 端点（`/mcp/`）。在 `~/.codex/config.toml` 中添加服务器：

    ```toml
    [mcp_servers.specivo]
    url = "https://your-specivo-host/mcp/"
    bearer_token_env_var = "SPECIVO_API_KEY"
    ```

    然后在 shell 中导出密钥，Codex 会从这个环境变量读取它：

    ```bash
    export SPECIVO_API_KEY=spv_your_api_key_here
    ```

## 验证连接 {#verify-the-connection}

客户端重新加载后，应该能发现 `specivo_…` 系列工具。让智能体运行 `specivo_whoami`，它会返回密钥所属的用户，既确认连接成功，也确认智能体是以哪个账户在操作。

!!! tip "让 Specivo 帮你生成配置"
    你不必手动复制配置。服务器提供了 **`specivo_setup_guide`** 工具和 **`specivo://docs/agent-setup`** 资源，可以生成适用于你客户端的配置片段，主机名也已经填好。让已连接的智能体调用这个设置指南工具，或者把客户端指向该资源，就能得到可直接粘贴的配置块。

!!! warning "MCP 请求返回权限错误"
    如果智能体已连接，但工具调用被拒绝，最可能的原因是密钥所属账户在该项目中缺少所需角色，或者密钥被限制在其他项目。请检查该项目的成员列表以及密钥的[项目限制](api-keys.md#limit-a-key-to-some-projects)。
