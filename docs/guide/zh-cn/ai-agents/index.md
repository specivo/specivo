---
description: 介绍 Specivo 内置的 MCP 服务器：AI 客户端通过它安全地读取和更新工作项、维基与搜索，每一次写入都会记入审计日志。
---

# AI 智能体与 MCP 服务器 {#ai-agents-the-mcp-server}

Specivo 内置了一个 **MCP 服务器**。MCP（Model Context Protocol，模型上下文协议）是 AI 客户端与外部工具通信的开放标准。借助它，AI 助手可以直接在你的项目管理系统*内部*工作：读取工作项（Issue）、搜索维基（Wiki）、留下评论、记录工时、更新状态，不再需要你来回复制粘贴文本。

任何支持 MCP 的客户端都可以接入：**Claude Code**、**Codex**、**Cursor**、**Windsurf**、**Cline** 等。AI 智能体（AI agent）通过绑定到真实 Specivo 用户的 API 密钥进行认证，因此它只能看到并修改该用户有权操作的内容，而且它的每一次写入都会被记录下来。

## 工作原理 {#how-it-works}

AI 客户端使用 API 密钥连接到你的 Specivo 服务器的 MCP 端点。连接后，客户端会发现一个包含约 40 个工具的目录（全部以 `specivo_` 为前缀），并在你提出请求时代你调用这些工具。控制权始终在你手中：你决定接入哪个客户端、它使用哪个账户的密钥，并且可以随时切断访问。

有两点保证了把工作交给智能体是安全的：

- **权限受到强制执行。** 智能体以密钥所属用户的身份行事。如果某个账户在某个项目中没有写入角色，用它的密钥只能读取该项目，不能修改。参见 [API 密钥与作用域](api-keys.md)。
- **一切都有审计。** 每一次 MCP 调用都会进入安全审计日志，并标记为 `source=mcp`，管理员可以准确看到智能体做了什么、何时做的。参见 [智能体能做什么](capabilities.md)。

## 两个端点 {#two-endpoints}

MCP 服务器通过两种传输方式提供相同的工具。选择你的客户端支持的那一种即可：

| 端点 | 传输方式 | 使用方 |
|---|---|---|
| `https://your-specivo-host/mcp/` | Streamable HTTP | Codex CLI |
| `https://your-specivo-host/mcp/sse/` | SSE（Server-Sent Events） | Claude Code、Cursor、Windsurf、Cline |

两个端点提供完全相同的工具目录，区别只在于客户端与服务器的通信方式。各客户端所需的具体配置见 [接入 AI 客户端](connecting.md)。

## 下一步 {#where-to-go-next}

- **[API 密钥与作用域](api-keys.md)**：创建密钥、把它限制在部分项目内，以及团队为什么要使用专用服务账户。
- **[接入 AI 客户端](connecting.md)**：可直接复制粘贴的 SSE 客户端和 Codex 配置。
- **[智能体能做什么](capabilities.md)**：按领域划分的工具目录、读写区分，以及审计。
- **[常见工作流](workflows.md)**：可以让智能体去做的具体事情。
- **[向 Specivo 上报 AI 使用](reporting-ai-usage.md)**：把 AI 编程工具的 token 用量发送到 Specivo，并归属到对应的工作项。

!!! note "智能体是一名团队成员"
    接入 MCP 的智能体并不是挂在 Specivo 旁边的一个独立集成，而是以成员身份登录，遵循与真人相同的权限规则，并留下同样的评论、日志条目和审计记录。请像对待任何账户的凭据一样对待它的密钥。
