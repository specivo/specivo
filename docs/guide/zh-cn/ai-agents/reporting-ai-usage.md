---
description: 通过 AI 使用上报 API 把 Claude Code、Codex、Gemini CLI 等 AI 编程工具的 token 用量上报到 Specivo，了解批量发送、幂等桶、UTC 日、工作项归属和代同事上报的规则。
---

# 向 Specivo 上报 AI 使用 {#reporting-ai-usage-to-specivo}

Specivo 可以记录 AI 编程工具的用量：token 数、请求数、所用模型，以及这些工作对应的工作项（Issue）。Specivo 会自行为这些用量计价，并把结果与工作项上记录的工时一起显示，同时计入项目的支出报表。用量只通过一个端点进入系统，即 **AI 使用上报 API**。上报程序读取各工具写在本地的日志，再发送到这个端点。

本页面向配置或编写这类上报程序的开发者。完整的契约（含 JSON Schema 和示例）发布在 Specivo 仓库的 [`docs/contracts/ai-usage/v1/`](https://github.com/specivo/specivo/tree/main/docs/contracts/ai-usage/v1) 目录下，任何人都可以实现。

!!! note "智能体无法统计自己的 token"
    通过 MCP 连接的 AI 智能体（AI agent）看不到自己的 token 计数器，因此无法自行上报准确的用量。用量来自工具的本地日志，由上报程序在会话结束后或按定时任务读取。

## 开始之前 {#before-you-start}

1. **API 密钥**。在 **个人资料 → API 密钥** 下创建（参见 [API 密钥与作用域](api-keys.md)）。密钥所属的账户就是它发送的每条记录的*上报者*。
2. **权限**。用量归属到哪些项目，账户就需要在这些项目中拥有 **记录 AI 使用** 权限。不归属任何项目的用量不需要权限。要替其他人上报，还需要 **为他人记录 AI 使用** 权限（参见[代同事上报](#reporting-for-teammates)）。
3. **检查服务器**。`GET /api/v1/ai-usage/ingest` 返回服务器接受的契约版本、各项限制、服务器时钟以及它识别的 AI 工具：

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

## 发送一批记录 {#send-a-batch}

`POST /api/v1/ai-usage/ingest` 接收一批记录，每批最多 500 条：

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

只有 `idempotency_key`、`tool`、`model`、`usage_date` 和 `tokens` 是必填字段。响应为 HTTP 200，每条记录对应一个结果：

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

每条记录单独保存或拒绝。被拒绝的记录带有 `status: "rejected"` 和一个 `error`，其中包含 `code`、`message` 以及出错的 `field`；其余记录照常保存。

## 一条记录对应一个桶 {#one-record-one-bucket}

一条记录包含一个*桶*的**完整总量**：一个工具的一次会话，使用一个模型，落在一个 **UTC** 日内。Specivo 为每个桶保留一个条目，以 `idempotency_key` 作为键（按用户区分）。

- 每次都发送整个桶，而不是上次发送以来的变化量。Specivo 会**替换**总量，从不累加。
- 再次发送相同的总量不会改变任何内容（`unchanged`），所以重发是安全的。程序崩溃或连接中断后，把当天的数据重发一遍即可。
- 推荐用 `v1|<tool>|<session_id>|<model>|<usage_date>` 的 SHA-256 作为键。
- 如果有人在 Specivo 中把该条目排除在报表之外，之后的发送会更新它，但它仍保持排除状态。

## 日期和时间均为 UTC {#days-and-times-are-utc}

- `usage_date` 是 **UTC** 日期。会话跨过 UTC 午夜时要拆成两条记录，每天一条。例如在曼谷（UTC+7），UTC 日在当地时间 07:00 切换。
- `started_at` 和 `ended_at` 必须带时区偏移（`Z` 或 `+07:00`）。`started_at` 按 UTC 计必须落在 `usage_date` 当天，`ended_at` 不得晚于当天结束。
- `usage_date` 不能是未来的日期，也不能早于 400 天前。
- Specivo 按 `started_at`（没有则按该 UTC 日的起始时刻）为每条记录计价，采用当时有效的价格。

Specivo 仍按每个人自己的时区显示日期；在午夜前后，两者可能相差几个小时。

## 用量计入哪个工作项 {#which-issue-the-usage-counts-for}

Specivo 按顺序尝试以下线索，并记录最终采用的是哪一个：

1. **`attribution.issue_ref`**：你指定的工作项，例如取自环境变量 `SPECIVO_ISSUE`（此时加上 `"issue_ref_source": "env"`）。如果上报程序是从智能体自己的 Specivo MCP 调用中读出工作项的，则用 `"issue_ref_source": "agent_log"` 发送；这种工作项会先于下面的步骤尝试，但无法使用时只会被跳过，不会导致记录被拒绝。
2. **智能体的 MCP 会话**：如果某个连接到 Specivo MCP 服务器的智能体在 `started_at` 到 `ended_at` 之间处理过某个工作项，就取该工作项。
3. **`attribution.commits`**：本次会话中的提交所关联的工作项。
4. **`attribution.git_branch`**：分支名中的工作项编号，例如 `feat/acme-12-login`。
5. **`attribution.project_key`**；或者 **`attribution.repo`**，前提是它匹配的仓库只连接到一个项目。
6. 否则条目为 **未分配**，之后可以在 Specivo 中分配。

你指定的内容（`issue_ref`、`project_key`）必须有效：工作项不存在，或者无法在该项目中记录用量，记录都会被拒绝。`agent_log` 工作项以及 Specivo 自行推断的结果（第 2 到 5 步）无法使用时会被跳过，并给出 `attribution_dropped` 警告，然后尝试下一条线索。

条目一旦确定归属，之后的发送会保留该归属。只有两种例外，且都只会让归属更精确：

- 条目有项目但没有工作项（例如智能体在选定工作项之前先做了调研），之后的发送若找到**该项目**下的工作项，条目就会采用它。
- Specivo 推断出的工作项（来自 MCP 会话、提交或分支），或者较早来自智能体日志的工作项，会被智能体日志中的工作项替换。

如果某次发送指定了不同的工作项或项目，会被拒绝并返回 `attribution_conflict`；这种情况请改在 Specivo 中移动条目。由人在 Specivo 中移动过的条目，归属方式显示为 `manual`，之后的发送不会再改动它。

## 代同事上报 {#reporting-for-teammates}

一个共享账户可以替整个团队上报，只需在每条记录上加 `"on_behalf_of": "<login>"`。为此，该账户需要在项目中拥有 **为他人记录 AI 使用** 权限，并且被代报的人必须是该项目的活跃成员。不归属任何项目的用量，只有管理员才能替他人上报。

每个条目都会显示是哪个账户、哪个密钥记录的；每个人都能看到以自己名义记录的全部用量；替他人记录的操作会写入[审计日志](../install/audit-log.md)。

为了防止有人借 `on_behalf_of` 探测哪些人有账户，如果账户无权在记录所属的项目中替他人上报，那么无论登录名是否存在，它得到的都是同样的 `forbidden` 响应。代报记录请同时发送 `project_key`（或 `issue_ref`）：只有在该项目中拥有 **为他人记录 AI 使用** 权限的账户或管理员，才会得到精确的 `target_unknown`、`target_invalid` 和 `target_not_member` 错误码。

## 成本 {#costs}

只需发送 token 数，Specivo 会根据 [AI 模型价格](../install/ai-model-prices.md)计算成本。

- `cost_type` 说明用量的付费方式：`api`、`subscription`、`local`、`free` 或 `unknown`。省略时，如果此人有覆盖该工具的 [AI 订阅](../install/ai-subscriptions.md)，Specivo 使用 `subscription`，否则使用 `unknown`。
- 如果工具知道服务商实际收取的费用，用 `reported_cost` 发送（`{"amount": "1.2345", "currency": "USD"}`）。对 `api` 类用量，这个金额就是实际支出。
- `input` token 不得包含缓存读取或写入；这些放在 `cache_read`、`cache_write_5m` 和 `cache_write_1h` 中发送。
- 对于有长上下文价格的模型，发送两个**相互独立**的 token 桶。`tokens` 存放未超过模型长上下文阈值的请求，`long_context_tokens` 存放超过阈值的请求。一个请求整体归入其中一个桶（包括它的全部输入、缓存和输出 token），而且 `long_context_tokens` 不是 `tokens` 的一部分：用量是两者之和。Specivo 按普通价格为 `tokens` 计价，按长上下文价格为 `long_context_tokens` 计价，所以同时计入两个桶的请求会被重复计费。示例见[契约](https://github.com/specivo/specivo/blob/main/docs/contracts/ai-usage/v1/README.md#long-context-tokens)。
- Specivo 中的 token 总量（条目列表、报表、CSV 导出）包含两个桶。有长上下文用量的条目会显示其中多少 token 按长上下文价格计费。

## 读取、更正和排除条目 {#read-correct-and-exclude-entries}

上报的用量保存为 AI 使用记录，与人和智能体手动记录的条目放在一起。REST API 的设计与工时记录一致；每个 URL 都必须带末尾的斜杠：

| 请求 | 作用 |
|---|---|
| `GET /api/v1/my/ai-usage/` | 以你的名义记录的条目，包括未分配的。筛选参数：`project_key`、`unassigned`、`tool`、`model`、`cost_type`、`source`、`excluded`、`from_date`、`to_date`、`offset`、`limit`。 |
| `GET /api/v1/projects/{key}/ai-usage/` | 项目中你有权读取的条目：你自己的和你上报的；如果 **查看 AI 使用** 设为*全部*，则是所有人的。筛选参数同上，另加 `user_id` 和 `issue_id`。 |
| `POST /api/v1/projects/{key}/ai-usage/` | 手动记录一个条目：`tool`、`model`、`usage_date`、`tokens`，以及可选的 `issue_id`、`cost_type`、`cost`（`{"amount": "0.42", "currency": "USD"}`）、`on_behalf_of` 和 `comments`。`POST /api/v1/my/ai-usage/` 记录不归属项目的条目。 |
| `GET`, `PATCH`, `DELETE /api/v1/ai-usage/{id}/` | 读取、修改或删除单个条目。`PATCH` 带 `project_key`（或 `null`）可移动条目；`tokens` 只修改所发送的那几类；`billing_account_id` 或 `payer` 用于手动设置计费（见下文）。 |
| `POST /api/v1/ai-usage/{id}/exclude/`, `/include/` | 把条目排除在报表之外，或重新计入。 |

以这种方式记录的条目，来源为 `api`（使用 API 密钥）或 `manual`（登录后操作）。智能体使用 MCP 工具 `specivo_log_ai_usage`（来源为 `mcp`）和 `specivo_ai_usage`（`op="list"`、`"get"`、`"update"`、`"exclude"`、`"include"`）；参见[智能体能做什么](capabilities.md)。

- 你可以修改以你的名义记录的条目；项目中的 **管理 AI 使用** 权限允许你修改该项目中任何人的条目。把条目移到另一个项目时，权限检查与在那里记录条目相同。
- 上报程序发送的条目，其 token、模型和工具保持不变：只有项目、工作项、评论和计费账户可以修改（修改其他字段会返回 `ai_usage_field_locked`）。条目有误时应排除，而不是删除；之后重发同一个桶，它仍保持排除状态。只有管理员可以删除上报的条目。
- 修改手动记录条目的用量会重新计价。条目所有者以外的人编辑、排除或删除条目，都会写入[审计日志](../install/audit-log.md)。
- 每个条目都会显示 `payer`、`billing_method`（计费的判定依据；由人设置时为 `manual`）和 `billing_account`（`id`、`slug`、`name`、`kind`、`instance`；没有账户或账户是别人的个人账户时为 `null`）。要更正计费，`PATCH` 时带上 `billing_account_id`（必须是条目所属人自己的账户或公司账户，处于启用状态，并且你能看到；该账户的付款方成为条目的付款方），或者只带 `payer`（`employer` 或 `employee`，不关联账户）。通过 MCP 时，`specivo_ai_usage` 的 `op="update"` 接受 `billing_account`（即 slug）或 `payer`。上报程序重发该桶或账户发生变化时，这一选择都会保留。`billing_account_id: null`（MCP 中为 `billing_account=""`）会清除这一选择，Specivo 将重新判定计费。如果条目所属人无权使用该账户，返回 `ai_usage_billing_account_unknown`。每次修改都会以 `ai_usage_billing_overridden` 记入审计。

## 限制与错误 {#limits-and-errors}

| 响应 | 含义 |
|---|---|
| 200 | 批次已读取。请检查每个结果的 `status`。 |
| 400 | 请求体不是 JSON（`invalid_json`），外层结构有误（`invalid_batch`），或服务器不接受该 `schema` 版本（`unsupported_schema`）。 |
| 401 | 没有 API 密钥，或密钥无效。 |
| 413 | 超过 500 条记录或超过 1 MiB（`batch_too_large`）：请拆分批次。 |
| 429 | 批次过多；请等待 `Retry-After` 指定的秒数。默认限制为每个账户每分钟 60 个批次。 |

管理员可以通过环境变量 `AI_USAGE_INGEST_RATE_LIMIT_PER_MINUTE` 修改速率限制（参见[配置](../install/configuration.md)）。

契约的 README 列出了全部错误码和警告码，以及新版本的规则：v1 随时可能新增可选字段，因此请忽略响应中你不认识的字段。
