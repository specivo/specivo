---
description: 介绍 Specivo 内置的 MCP 服务器，AI 客户端可通过这个端点安全地读取和更新工作项、维基并执行搜索，每次写入都会记入审计日志。
---

# AI 智能体与 MCP 服务器 {#ai-agents-the-mcp-server}

Specivo 内置 **MCP 服务器**。MCP（Model Context Protocol，模型上下文协议）是 AI 客户端与外部工具通信时使用的开放标准。有了它，AI 助手可以直接在你的项目管理系统*内部*工作：读取工作项（Issue）、搜索维基（Wiki）、发表评论、记录工时、更新状态，不用你来回复制粘贴文本。

任何支持 MCP 的客户端都可以连接，包括 **Claude Code**、**Codex**、**Cursor**、**Windsurf**、**Cline** 等。AI 智能体（AI agent）使用 API 密钥完成身份验证，密钥绑定到一个真实的 Specivo 用户，因此它只能查看和修改该用户有权访问的内容，每次写入也都会留下记录。

## 工作原理 {#how-it-works}

AI 客户端使用 API 密钥连接到 Specivo 服务器的 MCP 端点。连接后，客户端会发现一份约 40 个工具的目录（均以 `specivo_` 为前缀），并在你提出要求时代你调用这些工具。控制权始终在你手里：连接哪个客户端、使用哪个账户的密钥都由你决定，你也可以随时切断访问。

有两点保证了把这些能力交给智能体是安全的：

- **权限强制生效。** 智能体以密钥背后的用户身份操作。如果账户在某个项目中没有可写的角色，用它的密钥只能读取该项目，不能修改。参见 [API 密钥与作用域](api-keys.md)。
- **所有操作都有审计。** 每次 MCP 调用都会写入安全审计日志，并带有 `source=mcp` 标记，管理员可以准确看到智能体在什么时间做了什么。参见[智能体能做什么](capabilities.md)。

## 两个端点 {#two-endpoints}

MCP 服务器通过两种传输方式提供同一套工具。选择你的客户端支持的那一种即可：

| 端点 | 传输方式 | 适用客户端 |
|---|---|---|
| `https://your-specivo-host/mcp/` | Streamable HTTP | Codex CLI |
| `https://your-specivo-host/mcp/sse/` | SSE（Server-Sent Events） | Claude Code、Cursor、Windsurf、Cline |

两个端点提供的工具目录完全相同，区别只在于客户端与服务器的通信方式。各客户端所需的具体配置参见[连接 AI 客户端](connecting.md)。

## 接下来读什么 {#where-to-go-next}

- **[API 密钥与作用域](api-keys.md)**：创建密钥，把它限制在部分项目内，以及团队为什么使用专用的服务账户。
- **[连接 AI 客户端](connecting.md)**：可直接复制粘贴的 SSE 客户端和 Codex 配置。
- **[智能体能做什么](capabilities.md)**：按功能领域划分的工具目录、读取与写入的区别，以及审计。
- **[常用工作流](workflows.md)**：可以让智能体去做的具体事情。
- **[向 Specivo 上报 AI 使用情况](reporting-ai-usage.md)**：把 AI 编程工具的 token 用量发送到 Specivo，并归属到对应的工作项。
- **[Hermes Agent 动态](hermes-events.md)**：通过经过签名的 Hermes 接收端，把 Hermes Agent 的会话、工具调用、审批和子智能体记录为智能体会话。

!!! note "智能体就是一名队友"
    通过 MCP 连接的智能体并不是挂在 Specivo 旁边的独立集成：它以成员身份登录，遵守与人相同的权限规则，也同样留下评论、变更记录和审计记录。请像对待任何账户的凭据一样对待它的密钥。
