---
description: 通过 AI 使用上报 API 把 Claude Code、Codex、Gemini CLI 等 AI 编码工具的 token 用量上报到 Specivo，涵盖批次、幂等桶、UTC 日期、工作项归属和代队友上报。
---

# 向 Specivo 上报 AI 使用 {#reporting-ai-usage-to-specivo}

Specivo 可以记录你的 AI 编码工具用了什么：token、请求数、模型，以及这项工作对应的工作项（Issue）。Specivo 自行为这些用量计价，并把结果与工作项上记录的工时并列展示，同时计入项目的开支报表。用量通过唯一一个端点进入 Specivo，即 **AI 使用上报 API**。由一个上报程序读取工具在本地写入的日志，再发送到这个端点。

本页面向搭建或编写这类上报程序的开发者。完整契约（含 JSON Schema 和示例）发布在 Specivo 仓库的
[`docs/contracts/ai-usage/v1/`](https://github.com/specivo/specivo/tree/main/docs/contracts/ai-usage/v1) 目录下。任何人都可以实现它。

!!! note "智能体无法统计自己的 token"
    通过 MCP 连接的 AI 智能体（AI agent）看不到自己的 token 计数器，所以无法自行上报准确的用量。用量来自工具的本地日志，由会话结束后或定时运行的上报程序读取。

## 开始之前 {#before-you-start}

1. **API 密钥**。在 **个人资料 → API 密钥** 下创建一个（见
   [API 密钥与作用域](api-keys.md)）。拥有该密钥的账户，就是它发送的每条记录的*上报者*。
2. **权限**。该账户在用量所归属的项目中需要拥有 **记录 AI 使用** 权限。没有项目的用量不需要权限。要为其他人上报，还需要 **为他人记录 AI 使用** 权限（见[为队友上报](#reporting-for-teammates)）。
3. **检查服务器**。`GET /api/v1/ai-usage/ingest` 返回服务器接受的契约版本、限制、时钟，以及它认识的 AI 工具：

```bash
curl -s -H "Authorization: Bearer $SPECIVO_API_KEY" \
  https://your-specivo-host/api/v1/ai-usage/ingest
```

```json
{
  "versions": ["specivo.ai-usage.v1"],
  "max_records": 500,
  "max_bytes": 1048576,
  "max_age_days": 400,
  "rate_limit": {"requests": 60, "window_seconds": 60},
  "server_time": "2026-11-06T12:00:00Z",
  "tools": ["claude-code", "codex", "gemini-cli"],
  "currencies": ["EUR", "THB", "USD"]
}
```

## 发送批次 {#send-a-batch}

`POST /api/v1/ai-usage/ingest` 接受一个批次，最多 500 条记录：

```bash
curl -s -X POST https://your-specivo-host/api/v1/ai-usage/ingest \
  -H "Authorization: Bearer $SPECIVO_API_KEY" \
  -H "Content-Type: application/json" \
  -d @batch.json
```

```json
{
  "schema": "specivo.ai-usage.v1",
  "client": {"name": "my-usage-reporter", "version": "0.1.0"},
  "records": [
    {
      "idempotency_key": "752cc282663658a7ec3ea666eb7b7aa62c4aeda5191f2594c426edb9c558bcc4",
      "tool": "claude-code",
      "provider": "anthropic",
      "model": "claude-opus-4-6",
      "session_id": "0f6c2a1e-session",
      "usage_date": "2026-11-05",
      "started_at": "2026-11-05T09:10:00+07:00",
      "ended_at": "2026-11-05T10:40:00+07:00",
      "request_count": 42,
      "tokens": {"input": 1200, "output": 9000, "cache_write_5m": 30000, "cache_read": 900000},
      "attribution": {"issue_ref": "ACME-12", "git_branch": "feat/acme-12-login"}
    }
  ]
}
```

只有 `idempotency_key`、`tool`、`model`、`usage_date` 和 `tokens` 是必填的。响应为 HTTP 200，每条记录对应一个结果：

```json
{
  "schema": "specivo.ai-usage.v1",
  "server_time": "2026-11-06T12:00:01Z",
  "summary": {"received": 1, "created": 1, "updated": 0, "unchanged": 0, "rejected": 0},
  "results": [
    {
      "index": 0,
      "idempotency_key": "752cc282663658a7ec3ea666eb7b7aa62c4aeda5191f2594c426edb9c558bcc4",
      "status": "created",
      "entry_id": 1042,
      "revision": 1,
      "attribution": {"method": "explicit", "project_key": "ACME", "issue_ref": "ACME-12"},
      "warnings": [],
      "error": null
    }
  ],
  "warnings": []
}
```

每条记录单独存储或拒绝。被拒绝的记录带有 `status: "rejected"` 和一个 `error`，其中包含 `code`、`message` 和出错的 `field`；其余记录照常存储。

## 一条记录对应一个桶 {#one-record-one-bucket}

一条记录保存一个*桶*的**完整总量**：一个工具的一个会话，使用一个模型，在一个 **UTC** 日内。Specivo 为每个桶只保留一个条目，以其 `idempotency_key` 为键（按用户区分）。

- 每次都发送整个桶的总量，而不是上次发送以来的增量。Specivo 会**替换**总量，绝不累加。
- 再次发送相同的总量不会有任何变化（`unchanged`），所以重发是安全的。崩溃或断网之后，把当天重新发送一遍即可。
- 好的键可以取 `v1|<tool>|<session_id>|<model>|<usage_date>` 的 SHA-256。
- 如果有人在 Specivo 中把该条目排除出报表，之后的发送会更新它，但保持排除状态。

## 日期和时间均为 UTC {#days-and-times-are-utc}

- `usage_date` 是 **UTC** 日期。跨越 UTC 午夜的会话会变成两条记录，每天一条。例如在曼谷（UTC+7），UTC 日期在当地时间 07:00 切换。
- `started_at` 和 `ended_at` 需要带时区偏移（`Z` 或 `+07:00`）。按 UTC 计算，`started_at` 必须落在 `usage_date` 当天，`ended_at` 不得晚于当天结束。
- `usage_date` 不能是未来日期，也不能早于 400 天前。
- Specivo 按 `started_at`（或其 UTC 日的开始时刻）为每条记录计价，使用该时刻有效的价格。

Specivo 仍按每个人自己的时区显示日期；在午夜前后，两者可能相差几个小时。

## 用量归属到哪个工作项 {#which-issue-the-usage-counts-for}

Specivo 按以下顺序尝试这些线索，并记录实际采用了哪一条：

1. **`attribution.issue_ref`**：你指定的工作项，例如来自环境变量 `SPECIVO_ISSUE`（加上 `"issue_ref_source": "env"`）。如果上报程序是从智能体自己的 Specivo MCP 调用中读到工作项的，则随 `"issue_ref_source": "agent_log"` 一起发送；这样的工作项会先于下面各步尝试，但无法使用时会被跳过，而不是拒绝记录。
2. **智能体的 MCP 会话**：如果连接到 Specivo MCP 服务器的智能体在 `started_at` 和 `ended_at` 之间正在处理某个工作项，则归属到该工作项。
3. **`attribution.commits`**：会话中的提交所关联的工作项。
4. **`attribution.git_branch`**：分支名中的工作项编号，例如 `feat/acme-12-login`。
5. **`attribution.project_key`**，或者 **`attribution.repo`**（当它匹配到恰好只关联一个项目的仓库时）。
6. 否则该条目为 **未分配**，之后可以在 Specivo 中再分配。

你明确指定的内容（`issue_ref`、`project_key`）必须有效：未知的工作项，或无法记录用量的项目，会导致记录被拒绝。`agent_log` 工作项以及 Specivo 自行推断的结果（第 2 到 5 步）在无法使用时会被跳过，并给出警告 `attribution_dropped`，然后尝试下一条线索。

条目一旦确定归属，之后的发送会保留该归属，只有两个例外，而且都只是让归属更精确：

- 有项目但没有工作项的条目（例如智能体在选定工作项之前先做了调研），会采用后续发送中找到的**该项目**的工作项。
- Specivo 推断出的工作项（来自 MCP 会话、提交或分支），或者早先来自智能体日志的工作项，会被智能体日志中的工作项替换。

指定了不同工作项或项目的发送会以 `attribution_conflict` 被拒绝；请改在 Specivo 中移动该条目。有人在 Specivo 中手动移动过的条目，其归属方式显示为 `manual`，之后的发送不会再改变它。

## 为队友上报 {#reporting-for-teammates}

一个共享账户可以在每条记录上加 `"on_behalf_of": "<login>"`，为整个团队上报。为此，该账户需要在项目中拥有 **为他人记录 AI 使用** 权限，而且被代报的人必须是该项目的活跃成员。没有项目的用量，只有管理员才能代他人上报。

每个条目都显示是哪个账户和哪把密钥记录的，每个人都能看到以自己名义记录的所有用量，代他人记录会写入[审计日志](../install/audit-log.md)。

为防止有人利用 `on_behalf_of` 探测谁拥有账户，在记录所属项目中无权为他人上报的账户，无论登录名是否存在，都会得到同样的 `forbidden` 响应。代报记录请带上 `project_key`（或 `issue_ref`）：只有在该项目中拥有 **为他人记录 AI 使用** 权限的账户或管理员，才会得到精确的 `target_unknown`、`target_invalid` 和 `target_not_member` 代码。

## 费用 {#costs}

只需发送 token，Specivo 会根据 [AI 模型价格](../install/ai-model-prices.md) 计算费用。

- `cost_type` 说明用量如何付费：`api`、`subscription`、`local`、`free` 或 `unknown`。省略时，如果此人有覆盖该工具的 [AI 订阅](../install/ai-subscriptions.md)，Specivo 使用 `subscription`，否则使用 `unknown`。
- 如果工具知道服务商实际收取的费用，以 `reported_cost` 发送（`{"amount": "1.2345", "currency": "USD"}`）。对 `api` 用量来说，它就是实际开支。
- `input` token 不得包含缓存读写；缓存请放在 `cache_read`、`cache_write_5m` 和 `cache_write_1h` 中发送。
- 对于有长上下文价格的模型，发送两个**独立**的 token 桶。`tokens` 存放低于模型长上下文阈值的请求，`long_context_tokens` 存放超过阈值的请求。一个请求整体归入一个桶（它的全部输入、缓存和输出 token），并且 `long_context_tokens` 不是 `tokens` 的一部分：总用量是两者之和。Specivo 按普通价格为 `tokens` 计价，按长上下文价格为 `long_context_tokens` 计价，所以同时计入两个桶的请求会被重复收费。示例见[契约](https://github.com/specivo/specivo/blob/main/docs/contracts/ai-usage/v1/README.md#long-context-tokens)。
- Specivo 中的 token 总数（条目列表、报表、CSV 导出）包含两个桶。有长上下文用量的条目会显示其中有多少 token 按长上下文价格计费。

## 读取、修正和排除条目 {#read-correct-and-exclude-entries}

上报的用量存储为 AI 使用记录条目，与人和智能体手动记录的放在一起。REST API 与工时记录的接口对应；每个 URL 都需要末尾的斜杠：

| 请求 | 作用 |
|---|---|
| `GET /api/v1/my/ai-usage/` | 以你名义记录的条目，包括未分配的。筛选参数：`project_key`、`unassigned`、`tool`、`model`、`cost_type`、`source`、`excluded`、`from_date`、`to_date`、`offset`、`limit`。 |
| `GET /api/v1/projects/{key}/ai-usage/` | 项目中你有权读取的条目：你自己的和你上报的；如果 **查看 AI 使用** 设为*全部*，则是所有人的。筛选参数相同，另加 `user_id` 和 `issue_id`。 |
| `POST /api/v1/projects/{key}/ai-usage/` | 手动记录一个条目：`tool`、`model`、`usage_date`、`tokens`，可选 `issue_id`、`cost_type`、`cost`（`{"amount": "0.42", "currency": "USD"}`）、`on_behalf_of` 和 `comments`。`POST /api/v1/my/ai-usage/` 记录一个不属于任何项目的条目。 |
| `GET`、`PATCH`、`DELETE /api/v1/ai-usage/{id}/` | 读取、修改或删除一个条目。带 `project_key`（或 `null`）的 `PATCH` 会移动它；`tokens` 只改变发送了的类别。 |
| `POST /api/v1/ai-usage/{id}/exclude/`、`/include/` | 把一个条目排除出报表，或重新计入。 |

以这种方式记录的条目，来源为 `api`（使用 API 密钥）或 `manual`（登录后操作）。智能体使用 MCP 工具 `specivo_log_ai_usage`（来源 `mcp`）和 `specivo_ai_usage`（`op="list"`、`"get"`、`"update"`、`"exclude"`、`"include"`）；见[智能体能做什么](capabilities.md)。

- 你可以修改以你名义记录的条目；在项目中拥有 **管理 AI 使用** 权限，则可以修改该项目中任何人的条目。把条目移到另一个项目，会按在那里记录用量的权限来检查。
- 上报程序发送的条目会保留其 token、模型和工具：只有项目、工作项和评论可以修改（改其他字段会返回 `ai_usage_field_locked`）。错误的条目请排除而不是删除；之后再次发送同一个桶时，它会保持排除状态。只有管理员可以删除上报的条目。
- 修改手动记录条目的用量会重新计价。条目所有者以外的人进行的编辑、排除或删除会写入[审计日志](../install/audit-log.md)。

## 限制和错误 {#limits-and-errors}

| 响应 | 含义 |
|---|---|
| 200 | 批次已读取。检查每个结果的 `status`。 |
| 400 | 请求体不是 JSON（`invalid_json`）、信封格式错误（`invalid_batch`），或服务器不接受该 `schema` 版本（`unsupported_schema`）。 |
| 401 | 没有 API 密钥，或密钥无效。 |
| 413 | 超过 500 条记录或超过 1 MiB（`batch_too_large`）：请拆分批次。 |
| 429 | 批次过多；请等待 `Retry-After` 指定的秒数。默认限制为每个账户每分钟 60 个批次。 |

管理员可以通过环境变量 `AI_USAGE_INGEST_RATE_LIMIT_PER_MINUTE` 修改速率限制（见[配置](../install/configuration.md)）。

契约的 README 列出了所有错误和警告代码，以及新版本的规则：v1 随时可能新增可选字段，所以请忽略响应中你不认识的字段。
