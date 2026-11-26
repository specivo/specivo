---
description: 把 AI 客户端连接到 Specivo MCP 服务器，先获取服务器地址和 API 密钥，再添加 SSE 或 Codex 配置，并按需选择工具集、验证连接。
---

# 连接 AI 客户端 {#connecting-an-ai-client}

连接只需要两项信息，再改一处配置。本页涵盖两种传输方式：Claude Code、Cursor、Windsurf 和 Cline 使用 SSE，Codex CLI 使用 Streamable HTTP。

## 开始之前 {#before-you-start}

1. **确认服务器地址。** 把下面配置片段中的 `your-specivo-host` 替换为你实际的 Specivo 主机名（例如 `specivo.example.com`）。
2. **获取 API 密钥。** 如果还没有，在 **个人资料 → API 密钥** 中创建一个，
   参见 [API 密钥与作用域](api-keys.md)。每个片段都使用占位符 `spv_your_api_key_here`，
   请替换为你真实的 `spv_…` 密钥。

## 添加配置 {#add-the-configuration}

两个端点提供的工具完全相同，按你使用的客户端选择对应的标签页。

=== "SSE 客户端"

    对于 **Claude Code**、**Cursor**、**Windsurf** 和 **Cline**，让客户端指向 SSE
    端点（`/mcp/sse/`），并以 Bearer 令牌的形式传入密钥：

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

    把这段内容加入客户端的 MCP 配置，然后重启或重新加载客户端，让它识别新的服务器。

=== "Codex CLI"

    **Codex CLI** 使用 Streamable HTTP 端点（`/mcp/`）。把服务器添加到
    `~/.codex/config.toml`：

    ```toml
    [mcp_servers.specivo]
    url = "https://your-specivo-host/mcp/"
    bearer_token_env_var = "SPECIVO_API_KEY"
    ```

    然后在 shell 中导出密钥，Codex 会从上面指定的环境变量读取：

    ```bash
    export SPECIVO_API_KEY=spv_your_api_key_here
    ```

## 选择工具集 {#choose-toolsets}

很多客户端每一轮对话都会把服务器提供的所有工具描述放进 AI 智能体（AI agent）的提示词。为了缩短这部分内容，Specivo 把工具分成若干 **工具集**（toolset），每个连接只加载它请求的那些：

| 工具集 | 包含的内容 |
|---|---|
| `core` | 工作项（Issue）、评论、关联、维基（Wiki）、搜索与链接、工作项和页面上的元数据与标签、工时记录与计时器、项目、成员和查找表（lookups）。不做任何选择时加载这一组。 |
| `planning` | 创建、修改、开始、完成和删除版本与冲刺（Sprint），以及列出其中的工作项。 |
| `recurring` | 重复任务的重复模板及其重复项。 |
| `admin` | 创建、修改和删除元数据架构与标签。 |
| `ai-spend` | AI 使用记录与报告、AI 订阅、计费账户、报销申请和支出预算。 |

在服务器 URL 中加上 `toolsets` 参数，多个名称用逗号分隔；写 `all` 则加载全部工具：

```text
https://your-specivo-host/mcp/sse/?toolsets=core,ai-spend
https://your-specivo-host/mcp/?toolsets=all
```

如果智能体调用了当前连接未加载的工具，调用会失败，错误信息会指出需要添加哪个工具集。工具集名称无法识别时，连接会被拒绝。有些客户端（例如 Claude Code）会先搜索工具再加载，使用 `all` 几乎没有额外开销。

### 工具描述从简，完整帮助按需获取 {#short-tool-descriptions-full-help-on-request}

出于同样的原因，工具列表中每个工具只附带一段简短描述：它做什么、应该先调用什么、有哪些硬性规则。每段描述的末尾都指向完整帮助，智能体可以通过以下三种方式之一获取：

- 调用 `specivo_setup_guide`，并把 `tool` 设为工具名，例如
  `specivo_setup_guide(tool="specivo_create_issue")`；
- 读取资源 `specivo://docs/tools/<tool name>`；
- 调用时缺少参数或参数类型有误，返回的错误会列出该工具的全部参数及其完整描述，智能体可以据此修正调用。

每个工具都能获取完整帮助，包括所属工具集未被当前连接加载的工具。

### 读取、写入和删除提示 {#read-write-and-delete-hints}

每个工具还会通过 `tools/list` 中的标准 MCP 提示告诉客户端它属于哪类调用：`readOnlyHint`（列出、显示、读取、搜索，以及 `specivo_ai_usage_report`）、`destructiveHint`（删除类工具、`specivo_remove_relation`、`specivo_complete_sprint`、`specivo_skip_recurrence_occurrence`，以及 `specivo_ai_subscriptions`，它的 `delete` 操作会删除一个订阅）、`idempotentHint`（用相同参数重复调用不会再产生任何变化：按标识（key）更新的工具、`specivo_add_relation`、`specivo_tag`）和 `openWorldHint`（始终为 false：工具只操作你自己的 Specivo）。会话中断后，客户端可以直接重试只读工具，只在写入时请求批准。创建类工具、`specivo_add_comment`、`specivo_log_time`、`specivo_log_ai_usage` 和 `specivo_timer` 不是幂等的：超时之后，客户端应先检查再重复调用。

## 验证连接 {#verify-the-connection}

客户端重新加载后，应该能发现 `specivo_…` 系列工具。让智能体运行 `specivo_whoami`，它会返回密钥所属的用户，这样既确认了连接正常，也确认了智能体以哪个账户的身份操作。

!!! tip "让 Specivo 为你生成配置片段"
    配置不必手动抄写。服务器提供 **`specivo_setup_guide`** 工具和
    **`specivo://docs/agent-setup`** 资源，可以按你的客户端输出现成的配置片段，主机名已经填好。让已连接的智能体调用 setup guide，或者让客户端读取该资源，就能拿到可以直接复制粘贴的配置块。

!!! warning "MCP 请求返回权限错误"
    如果智能体已经连上，但某次工具调用被拒绝，最可能的原因是密钥所属账户在该项目中没有所需的角色，或者密钥被限制在其他项目范围内。检查项目的成员列表和密钥的[项目限制](api-keys.md#limit-a-key-to-some-projects)。
