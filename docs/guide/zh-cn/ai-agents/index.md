---
description: "了解 Specivo 内置的 MCP 服务器：AI 客户端可通过它安全地读取和更新工作项、搜索维基内容，所有写入操作都会记录到安全审计日志中。"
---

# AI 智能体与 MCP 服务器 {#ai-agents-the-mcp-server}

Specivo 内置了一个 **MCP 服务器**。MCP（Model Context Protocol，模型上下文协议）是一项开放标准，AI
客户端通过它与外部工具对话。有了它，AI 助手就能直接在你的项目管理系统*内部*工作：读取工作项（Issue）、搜索维基（Wiki）、留下评论、记录工时、更新状态，而不用你来回复制粘贴文本。

任何支持 MCP 的客户端都可以接入：**Claude Code**、**Codex**、**Cursor**、**Windsurf**、
**Cline** 等。AI 智能体（AI agent）使用绑定到真实 Specivo 用户的 API 密钥进行认证，因此它只能看到和修改该用户有权限的内容，而且它的每一次写入都会被记录下来。

## 工作原理 {#how-it-works}

AI 客户端使用 API 密钥连接到你 Specivo 服务器的 MCP 端点。连接后，客户端会发现一个包含约 40 个工具的目录（全部以 `specivo_` 为前缀），并在你提出要求时代你调用这些工具。控制权始终在你手里：由你决定接入哪个客户端、它使用哪个账户的密钥，并且你随时可以切断访问。

把这些交给智能体之所以安全，有两个原因：

- **权限受到强制约束。** 智能体以密钥背后的用户身份行事。如果密钥所属账户在某个项目中没有写入角色，智能体就只能读取而不能修改该项目。参见 [API 密钥与作用域](api-keys.md)。
- **一切都有审计。** 每次写入都会进入安全审计日志，并标记为 `source=mcp`，项目经理可以准确看到智能体在什么时候做了什么。参见[智能体能做什么](capabilities.md)。

## 两个端点 {#two-endpoints}

MCP 服务器通过两种传输方式提供同一套工具。选择你的客户端支持的那一种即可：

| 端点 | 传输方式 | 适用客户端 |
|---|---|---|
| `https://your-specivo-host/mcp/` | Streamable HTTP | Codex CLI |
| `https://your-specivo-host/mcp/sse/` | SSE（Server-Sent Events） | Claude Code、Cursor、Windsurf、Cline |

两者提供的工具目录完全相同，区别仅在于客户端与服务器的通信方式。各客户端所需的具体配置见[接入 AI 客户端](connecting.md)。

## 下一步 {#where-to-go-next}

- **[API 密钥与作用域](api-keys.md)**：创建密钥、把它限定到部分项目，以及团队为什么会使用专用的服务账户。
- **[接入 AI 客户端](connecting.md)**：SSE 客户端和 Codex 的配置，可直接复制粘贴。
- **[智能体能做什么](capabilities.md)**：按领域划分的工具目录、读写区分，以及审计。
- **[常见工作流](workflows.md)**：可以交给智能体去做的具体事情。

!!! note "智能体是团队的一员"
    通过 MCP 接入的智能体并不是挂在 Specivo 旁边的一个独立集成，它以成员身份登录，遵守和真人一样的权限规则，并留下同样的评论、日志条目和审计记录。请像对待任何账户的凭据一样对待它的密钥。
