---
description: "介绍 Specivo 的出站 Webhook：项目可发送的事件、Payload 结构与示例、用 Python 和 Node.js 验证 X-Specivo-Signature 签名、重试机制、投递 ID、私有内容与投递日志。"
---

# 出站 Webhook {#outgoing-webhooks}

**出站 Webhook** 会在项目发生变化时向另一个系统发送经过签名的 HTTPS 请求：工作项（Issue）被创建、更新、评论或删除，或者维基（Wiki）页面被保存。可以用它向聊天频道推送消息、触发构建，或让另一个工具与 Specivo 保持同步。无论改动来自 Web 界面、REST API，还是 AI 智能体（AI agent）通过 MCP 做出的，发送的事件都相同。

## 创建 Webhook {#create-a-webhook}

你需要在该项目中拥有 **管理项目设置** 权限。打开项目的 **设置 → Webhooks** 标签页，选择 **新建 Webhook**：

- **Payload URL**：请求发送的目标地址。必须以 `https://` 开头，且不能指向内网（见 [Specivo 拒绝的地址](#addresses-specivo-refuses)）。
- **事件**：下表中的一个或多个事件。对话框和投递日志按名称显示它们：*工作项已创建*、*工作项已更新*、*工作项有新评论*、*工作项已删除*、*维基页面已更新*，以及对应 `ping` 的 *测试事件*。
- **包含私有工作项和私有备注**：仅管理员可见，见[私有内容](#private-content)。

保存后，Specivo 只显示一次 **签名密钥**。把它复制到接收方系统中，之后不会再显示。要更换密钥，用 **编辑** 打开该 Webhook 并选择 **生成新密钥**，旧密钥会立即失效。

![Webhooks 标签页](../../assets/img/project-settings-webhooks.webp)

每个 Webhook 有以下操作：**投递日志**、**发送测试**（发送一个 `ping` 事件，对已停用的 Webhook 同样有效）、**停用** / **启用**、**编辑** 和 **删除**（同时删除它的投递日志）。

REST API 在 `/api/v1/projects/{key}/webhooks/` 下提供同样的功能，权限要求相同。通过 API 还可以在创建或更新时用 `secret` 设置自己的密钥（16 到 100 个字符），而不使用自动生成的密钥；`rotate_secret: true` 会生成一个新密钥。只有在刚设置密钥的那次响应中才会返回密钥。

## 事件 {#events}

| 事件 | 发送时机 |
|---|---|
| `issue.created` | 工作项被创建。 |
| `issue.updated` | 工作项的字段被修改，或在更新时添加了备注。添加或删除附件和关联，以及[代码仓库集成](repositories.md)写入的 `commits`、`branches`、`pull_requests` 元数据，都不会发送事件。 |
| `issue.commented` | 工作项收到评论。 |
| `issue.deleted` | 工作项被删除。 |
| `wiki.page.updated` | 维基页面被创建，以及之后每次保存新版本时。Specivo 自动创建的 **首页** 不发送事件。 |
| `ping` | 你选择 **发送测试** 时。无法订阅此事件。 |

事件只在改动保存成功后发送：失败并回滚的改动不发送任何事件。通过[导入](../install/importing.md)带入的数据也不发送事件。

## Payload {#payload}

每个请求都是 `POST`，JSON 请求体结构如下：

| 字段 | 内容 |
|---|---|
| `event` | 事件名称，见上表。 |
| `delivery_id` | 投递 ID，一个 UUID；与 `X-Specivo-Delivery` 请求头相同。 |
| `occurred_at` | 改动发生的时间，UTC。 |
| `project` | 项目的 `key` 和 `name`。 |
| `actor` | 做出改动的用户（或智能体账户）的 `id`、`login` 和 `display_name`；改动由 Specivo 自动完成时为 `null`，例如重复任务替换重复项。 |
| `data` | 事件数据，见下表。 |

各事件的 `data`：

| 事件 | `data` |
|---|---|
| `issue.created` | `issue`：该工作项，字段与 REST API 的工作项对象相同（`GET /api/v1/issues/{key}/`，不含子工作项和其他附加内容）。 |
| `issue.updated` | `issue`；`changes`：每个被修改的字段及其 `old` 和 `new` 值；`journal_id`：本次更新写入的历史记录条目；`notes`：随更新添加的备注，没有则为 `null`。 |
| `issue.commented` | `issue`；`comment`：该评论，格式与工作项的 journal API 返回的一致。 |
| `issue.deleted` | `issue`：删除前一刻的工作项。 |
| `wiki.page.updated` | `page`：页面及其正文；`comments`：编辑摘要，没有则为 `null`；`created`：新建页面时为 `true`。 |
| `ping` | `webhook_id` 和 `message`。 |

`changes` 中的工作项字段名与 API 一致（`status_id`、`assigned_to_id`、`subject` 等），值的形式与工作项历史记录中存储的相同：ID 为字符串，`description` 为完整文本。

### 示例：工作项事件 {#example-an-issue-event}

工作项被设为 *In Progress*（进行中）并指派后的 `issue.updated`：

```json
{
  "event": "issue.updated",
  "delivery_id": "075a0356-82da-473f-80ab-0e0c10f6dce5",
  "occurred_at": "2026-10-30T22:04:28.014132Z",
  "project": {"key": "ACME", "name": "Acme Website"},
  "actor": {"id": 4, "login": "alex", "display_name": "Alex Example"},
  "data": {
    "issue": {
      "id": 1,
      "key": "ACME-1",
      "project_key": "ACME",
      "subject": "Add the login form",
      "description": null,
      "tracker": {"id": 2, "name": "Feature"},
      "status": {"id": 2, "name": "In Progress"},
      "priority": {"id": 2, "name": "Normal"},
      "category": null,
      "author": {"id": 4, "name": "Alex Example"},
      "assigned_to": {"id": 4, "name": "Alex Example"},
      "fixed_version": null,
      "fixed_version_id": null,
      "parent_id": null,
      "root_id": 1,
      "lft": 1,
      "rgt": 2,
      "sprint_id": null,
      "start_date": null,
      "due_date": null,
      "done_ratio": 0,
      "estimated_hours": null,
      "is_private": false,
      "metadata": {
        "commits": [
          "25031421b3a7f0640165a835b52ef48e76f49e1f",
          "826674632415a1a29012a1c1c594cf4ba01a2e17"
        ],
        "branches": ["feat/ACME-1-add-the-login-form"],
        "pull_requests": ["https://github.example.com/example-org/website/pull/14"]
      },
      "lock_version": 5,
      "created_at": "2026-10-30T22:02:55.722698Z",
      "updated_at": "2026-10-30T22:04:27.751427Z"
    },
    "changes": {
      "status_id": {"old": "1", "new": "2"},
      "assigned_to_id": {"old": null, "new": "4"}
    },
    "journal_id": 6,
    "notes": null
  }
}
```

`issue.created` 和 `issue.deleted` 只携带 `issue`。`issue.commented` 携带 `issue` 和 `comment`：

```json
"comment": {
  "id": 7,
  "sequence": 6,
  "issue_id": 1,
  "user": {"id": 4, "name": "Alex Example"},
  "notes": "Ready for review.",
  "details": [],
  "is_private": false,
  "reply_to_id": null,
  "is_resolved": false,
  "resolved_at": null,
  "resolved_by": null,
  "resolved_summary": null,
  "created_at": "2026-10-30T22:04:28.056324Z",
  "updated_at": "2026-10-30T22:04:28.056324Z"
}
```

### 示例：维基事件 {#example-a-wiki-event}

```json
{
  "event": "wiki.page.updated",
  "delivery_id": "b50a35ae-d784-4f03-9a08-9a3a256efe77",
  "occurred_at": "2026-10-30T22:04:28.328618Z",
  "project": {"key": "ACME", "name": "Acme Website"},
  "actor": {"id": 4, "login": "alex", "display_name": "Alex Example"},
  "data": {
    "page": {
      "id": 1,
      "slug": "release-checklist",
      "title": "Release checklist",
      "text": "1. Tag the release",
      "parent_id": null,
      "protected": false,
      "lock_version": 1,
      "content_version": 1,
      "content_author": {"id": 4, "name": "Alex Example"},
      "content_updated_at": "2026-10-30T22:04:28.269006Z",
      "created_at": "2026-10-30T22:04:28.269006Z",
      "updated_at": "2026-10-30T22:04:28.269006Z"
    },
    "comments": "First draft",
    "created": true
  }
}
```

### 示例：测试事件 {#example-the-test-event}

```json
{
  "event": "ping",
  "delivery_id": "3354d211-011b-44af-a802-906c38bc9827",
  "occurred_at": "2026-10-30T22:04:27.653584Z",
  "project": {"key": "ACME", "name": "Acme Website"},
  "actor": {"id": 4, "login": "alex", "display_name": "Alex Example"},
  "data": {"webhook_id": 2, "message": "Test event sent from Specivo."}
}
```

工作项和页面对象遵循 REST API，因此将来可能新增字段。只读取你需要的字段，忽略其余部分。

## 请求头与签名 {#headers-and-the-signature}

| 请求头 | 值 |
|---|---|
| `Content-Type` | `application/json`；请求体为 UTF-8 编码。 |
| `User-Agent` | `Specivo-Webhook/1` |
| `X-Specivo-Event` | 事件名称，例如 `issue.updated`。 |
| `X-Specivo-Delivery` | 投递 ID，与请求体中的 `delivery_id` 相同。 |
| `X-Specivo-Signature` | `sha256=` 后接请求体的 HMAC-SHA256 十六进制值，以签名密钥作为密钥计算。 |

要确认请求确实来自 Specivo 且在传输途中未被篡改，请对收到的**原始请求体字节**（在任何 JSON 解析之前）计算 HMAC，并与请求头做恒定时间比较。先解析 JSON 再重新序列化会改变字节内容，签名就对不上了。

**Python：**

```python
import hashlib
import hmac


def is_valid(secret: str, body: bytes, signature: str | None) -> bool:
    """True when *signature* (the X-Specivo-Signature header) matches *body*."""
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), (signature or "").encode())
```

Flask 中原始请求体是 `request.get_data()`；FastAPI 或 Starlette 中是 `await request.body()`。

**Node.js：**

```javascript
const crypto = require("node:crypto");

function isValid(secret, body, signature) {
  // body: the raw request body as a Buffer, before any JSON parsing
  const expected = "sha256=" + crypto.createHmac("sha256", secret).update(body).digest("hex");
  const a = Buffer.from(expected);
  const b = Buffer.from(signature || "");
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
```

使用 Express 时，在 Webhook 路由上用 `express.raw({ type: "application/json" })` 读取请求体，这样 `req.body` 就是 Buffer。

签名无效时返回错误，不要做任何其他处理。

## 投递、重试与重复 {#delivery-retries-and-duplicates}

投递在后台发送，每个投递串行进行，在改动保存后不久即发出。收到 **2xx** 响应即视为投递成功。其他情况：

| 结果 | 处理方式 |
|---|---|
| 连接错误、超时、主机名无法解析、**5xx**、**408** 或 **429** | 稍后重试。 |
| 重定向（**3xx**）或其他任何 **4xx** | 立即失败，不再重试。永不跟随重定向。 |
| Specivo 拒绝的地址、无效端口、无法再读取的密钥，或 Specivo 内部的意外错误 | 立即失败，不发送。 |

一次投递最多尝试 **8 次**。第 *n* 次重试等待 30 秒 × 2<sup>*n*−1</sup>（最长一小时），再加上最多四分之一的随机时长，使重试错开：大约在上一次尝试后 30 秒、1、2、4、8、16 和 32 分钟。第八次尝试后，该投递标记为**失败**。每次尝试中，请求的每个步骤（连接、发送、读取）最多 30 秒，总计最多 60 秒。Specivo 最多保留响应的前 4096 字节用于日志。

同一投递的各次重试携带**相同**的 `X-Specivo-Delivery` ID 和相同的请求体。一次投递可能到达多次（例如响应太慢被 Specivo 算作超时），因此请用该 ID 识别已处理过的投递。投递也可能乱序到达；如果顺序重要，请使用 `occurred_at`，或从 API 读取当前状态。

如果改动保存时任务队列不可用，投递会以**等待中**状态等候，队列恢复后再发送，不会丢失。这种情况下预计会延迟约五分钟或更久，因为 Specivo 要先确认原来的尝试确实没有仍在运行。

已停用的 Webhook 不会收到任何内容：停用时仍在等待的投递会以“Webhook 已停用，未发送。”失败。**发送测试** 对已停用的 Webhook 仍然有效，可在启用前先检查接收方。

### Specivo 拒绝的地址 {#addresses-specivo-refuses}

每次尝试都会先解析主机名，只要其中任何一个地址是私有地址、回环地址、链路本地地址或其他非公网地址，就拒绝发送；随后连接到检查过的那个地址。直接用这类地址写的 URL 在保存 Webhook 时就会被拒绝。因此 Webhook 不能用来访问你网络内部的机器，即使其 DNS 记录之后被改掉也不行。Specivo 环境中的代理设置不用于 Webhook。

## 私有内容 {#private-content}

默认情况下，Webhook 不会收到任何关于**私有工作项**（完全没有事件，包括对它们的评论和删除）或**私有备注**的内容。把工作项改为私有或改为公开的更新也算作私有。

只有**管理员**才能开启 **包含私有工作项和私有备注**，因为只有管理员能看到所有私有工作项。不是管理员的项目经理无法创建这样的 Webhook，也无法编辑、停用、启用或更换已开启该选项的 Webhook 的密钥。他们仍能在列表中看到它（标记为 *包含私有内容*）、查看其投递日志、向它发送测试事件以及删除它。维基事件从不属于私有内容。

## 投递日志 {#the-delivery-log}

Webhook 所在行的 **投递日志** 列出该 Webhook 的投递，最新的在前：

| 列 | 显示 |
|---|---|
| **时间** | 事件创建的时间。悬停可查看投递 ID。 |
| **事件** | 事件。 |
| **状态** | **等待中**（等待首次尝试或重试）、**成功** 或 **失败**。 |
| **尝试次数** | 到目前为止的尝试次数。 |
| **响应代码** | 最后一次响应的 HTTP 状态码；没有响应时为 `–`。 |
| **响应** | 最后一次尝试失败的原因，以及接收方响应的开头部分。 |

![某个 Webhook 的投递日志](../../assets/img/webhook-delivery-log.webp)

**响应** 中可能出现的消息：

| 消息 | 含义 |
|---|---|
| 接收方返回 HTTP *code*。 | 接收方未返回 2xx。 |
| *N* 秒后超时。 / 超时：60 秒内未收到完整响应。 | 接收方太慢。请尽快响应，之后再处理具体工作。 |
| 连接失败：*error*。 | 无法连接到接收方。*error* 是失败类型，例如 `ConnectError`。 |
| 无法解析主机 *name*。 | 主机名（暂时）无法解析。会重试。 |
| 主机解析到私有、回环或保留地址，未发送。 | 见 [Specivo 拒绝的地址](#addresses-specivo-refuses)。 |
| Webhook URL 的端口无效，未发送。 | 修正 URL。 |
| Webhook 已停用，未发送。 | Webhook 在投递发送前被停用。 |
| 无法用当前的 SECRET_KEY 读取 Webhook 密钥，请设置新密钥。 | 实例的 `SECRET_KEY` 已更改。请生成新密钥。 |
| 投递失败：*error*。 | Specivo 内部的意外错误，*error* 是错误类型。不重试；请联系管理员。 |

成功和失败的投递在 30 天后删除。等待中的投递会保留到有最终结果。REST API 通过 `GET /api/v1/projects/{key}/webhooks/{id}/deliveries/` 返回同样的日志，`POST /api/v1/projects/{key}/webhooks/{id}/test/` 发送测试事件。

每个步骤的超时时间由管理员通过 `WEBHOOK_TIMEOUT` 设置，见[配置](../install/configuration.md#repository-integrations-and-webhooks)。

要反过来接收*来自*代码托管平台的事件，请参阅[代码仓库集成](repositories.md)。
