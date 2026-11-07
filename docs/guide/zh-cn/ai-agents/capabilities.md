---
description: 介绍 AI 智能体通过 Specivo MCP 服务器能做什么，包括按领域分组的工具目录、读取与写入操作的区别、权限校验方式以及审计日志的记录范围。
---

# 智能体能做什么 {#what-agents-can-do}

通过 MCP 服务器，AI 智能体（AI agent）可以完成你在 Specivo 界面里能做的大部分操作。服务器提供一个约 40 个工具的目录，全部以 `specivo_` 为前缀。你很少需要按名字直接调用它们：只需描述想做的事，客户端会自动选择合适的工具。本页按领域对这些工具分组，让你了解大致能做到什么。

## 按领域划分的工具目录 {#the-tool-catalog-by-area}

| 领域 | 智能体能做什么 |
|---|---|
| **工作项与评论** | 列出、读取、创建和更新工作项（Issue）；编辑描述和核心字段；设置或修改工作项的[父工作项](../issues/subtasks.md)并列出其子工作项；[把工作项移动到其他项目](../issues/updating.md#move-an-issue-to-another-project)；添加评论、读取评论历史，或读取完整的变更历史，其中会带上变更所涉及的状态、人员和版本的名称。 |
| **搜索** | 使用 Specivo 的混合搜索（关键词 + 语义）在工作项、维基等内容中搜索。 |
| **维基** | 列出、读取、创建、编辑维基页面并向其追加内容，支持按章节读取和编辑，以及查看历史记录和恢复。 |
| **元数据与架构** | 读取和设置[工作项元数据](../metadata/values.md)的值，列出可用的元数据架构。 |
| **标签** | 列出项目的[标签](../projects/tags.md)，给工作项和维基页面打标签（创建时也可以）；拥有 **管理项目** 权限时，还能创建、重命名、改色和删除标签。 |
| **关联** | 用带类型的[关联](../issues/relations.md)把工作项连接起来（关联到、阻塞、重复于等），也可以移除关联。 |
| **链接** | 沿着工作项、维基页面和评论之间的链接导航：`specivo_show_issue` 和 `specivo_read_wiki` 会列出某项内容链接到了什么，以及哪些内容链接到它（反向链接）；`specivo_get_links` 以 JSON 返回其周边链接网络，最多两跳，可按类型筛选。智能体只能看到密钥有权打开的内容之间的链接。 |
| **开发** | `specivo_issue_development(issue_ref, kinds=[...], limit, offset)` 以 JSON 返回工作项的[开发动态](../integrations/repositories.md#reading-development-activity-through-the-api-and-mcp)，与 REST API 返回的是同一份文档：提交、拉取请求、代码评审、CI 运行、部署（含环境类型）和发布，附带摘要和分页时间线。`specivo_show_issue` 会把这些汇总成一行 `Development:`。智能体只能看到密钥有权打开的工作项的开发动态。 |
| **版本** | 列出和管理版本（里程碑/发布），查看每个版本包含哪些工作项。 |
| **冲刺** | 列出冲刺（Sprint），查看冲刺中的工作项，管理计划、开始、完成的生命周期。 |
| **重复任务** | 创建、列出、更新和删除[重复任务模板](../projects/recurring-tasks.md)；跳过某一次计划中的重复项；预览即将到来的重复项。模板的主题和描述支持日期宏（`{{month}}`、`{{weekday}}` 等），每个重复项会按模板所在时区展开。 |
| **时间** | 针对工作项或项目按活动记录工时，或使用自己的计时器：在工作项上启动、查看、停止（停止时记录工时）或丢弃。 |
| **AI 使用** | `specivo_log_ai_usage` 记录 AI 工具在某个工作项或项目上消耗的 token；`specivo_ai_usage` 用于列出、读取、修正、排除和重新纳入 [AI 使用记录](reporting-ai-usage.md#read-correct-and-exclude-entries)。智能体看不到自己的 token 计数器，所以数字必须来自钩子、[上报工具](reporting-ai-usage.md)或工具自身的 `/cost` 输出，绝不能靠估算。不指定工作项或项目时，记录会归到智能体会话正在处理的工作项（见[智能体会话](#agent-sessions)），否则保持未分配。 |
| **AI 订阅** | `specivo_ai_subscriptions` 用于列出、添加、修正、结束和删除密钥所属用户的 [AI 订阅](../install/ai-subscriptions.md#with-your-ai-agent)，例如根据账单一次录入多个套餐周期。它会提示同一套餐各周期之间的重叠和空隙，并拒绝受项目限制的密钥和服务账户。 |
| **基础信息** | `specivo_whoami`（密钥对应的账户）、`specivo_list_lookups`（追踪器/状态/优先级 ID）和 `specivo_setup_guide`（配置帮助）。 |

!!! tip "先调用基础信息工具"
    行为良好的智能体会在创建或更新工作项之前先调用 `specivo_list_lookups` 了解项目的追踪器（Tracker）、状态和优先级 ID，并调用 `specivo_whoami` 确认自己以哪个账户在操作。接入新智能体时，让它先运行这两个工具。

## 读取与写入 {#read-vs-write}

工具大致一半是**读取**（列出、查看、搜索、读维基），一半是**写入**（创建工作项、添加评论、更新状态、记录工时或 AI 使用、编辑维基）。这个区分之所以重要，是因为权限：

- **读取** 只返回密钥所属账户有权看到的内容。
- **写入** 则*每一次*都会按该账户在项目中的角色校验。账户在界面里做不了的修改，智能体通过 MCP 同样做不了。

正因如此，[配置好角色的专用服务账户](api-keys.md)是限定智能体权限的安全做法：只需查看的地方给只读角色，需要操作的地方才给写入角色。

## 每次写入都有审计 {#every-write-is-audited}

智能体的每一次 MCP 工具调用，无论读写，都会记录到[安全审计日志](../install/audit-log.md)中，标记为 `source=mcp`，并附带操作账户、时间和客户端地址。管理员可以在 **管理 → 审计日志** 中查看智能体到底做了什么。工作项的修改同样会出现在常规的工作项历史和评论记录里，和真人的修改一样，智能体的任何操作都不会被隐藏。

审计日志覆盖的是 MCP。智能体如果用同一把密钥调用 REST API，会被当作普通用户处理：对工作项和维基的修改记录在各自的历史中，而不在审计日志里。

## 智能体会话 {#agent-sessions}

Specivo 还会把每把密钥的调用归入**智能体会话**：相邻两次调用间隔不到 30 分钟（`AGENT_SESSION_TIMEOUT`，单位为秒）即属于同一会话。会话会记录：

- **发起调用的客户端**：以它连接时宣告的名称和版本为准（例如 `claude-code` 和 `2.1.0`），否则取 `User-Agent` 头中的第一个产品名；
- **智能体正在处理的工作项**：通过 MCP，指它最近一次满足以下条件的工作项：以*进行中*状态（活动组中的任意状态）创建或创建时指派给自己的账户；移动到此类状态；指派给自己的账户；在进行中且已指派给自己账户时更新（因此智能体恢复处理某个工作项时，即使什么也没改也会重新标记）；或在其上启动了计时器。以最近一次为准。只会记录密钥所属账户能看到的工作项。

会话中的工作项只是用来把 AI 使用归属到工作项的提示（`specivo_log_ai_usage` 未指定工作项或项目时会使用它，前提是该账户有权在那里记录使用量），不授予任何权限。提示词和对话内容一概不会存储。

## 文件上传走 REST API {#file-uploads-go-through-the-rest-api}

MCP 工具**只处理文本**：它们能读写工作项、评论和维基文本，但不能上传二进制文件。要添加附件，请用 `curl` 调用 Specivo 的 REST API，复用同一把 `spv_…` 密钥：

```bash
curl -X POST https://your-specivo-host/api/v1/attachments/ \
  -H "Authorization: Bearer spv_your_api_key_here" \
  -F "file=@./diagram.png" \
  -F "container_type=Issue" \
  -F "container_id=123"
```

`container_type` 为 `Issue`、`WikiPage` 或 `Journal`（评论），`container_id` 是对应的数字 id。添加文件需要具备修改所附对象的权限：

| 附加到 | 所需权限 |
|---|---|
| 工作项 | **编辑工作项** 或 **添加评论**；或对该账户自己创建的工作项拥有 **创建工作项** |
| 维基页面 | **管理维基页面** |
| 评论 | **编辑任意评论**，或对该账户自己的评论拥有 **编辑自己的评论** |

工作项上的文件会记录在工作项历史中（“已添加文件 diagram.png”）。要把它和所属的编辑或评论记在同一条目中，加上 `-F "journal_id=…"`：`journal_id` 来自 `PATCH /api/v1/issues/{ref}/` 的返回（编辑未改动任何内容时为 null），或来自 `POST /api/v1/issues/{ref}/journals/` 返回的评论 `id`。每次上传的响应也带有 `journal_id`，所以同一批后续文件可以并入同一条目。

!!! note "REST API 基础"
    REST 认证**只**支持 `Authorization: Bearer spv_…` 头，而且每个 API URL 都必须以**斜杠结尾**。连接 MCP 客户端的那把密钥在这里同样可用。
