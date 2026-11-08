---
description: 在 Specivo 中登记你为 AI 支付的包月或包年套餐（Claude Pro/Max、ChatGPT Plus/Pro、Cursor、Copilot、Google AI 等），支持任意货币，让 AI 使用记入订阅覆盖并在支出报表中分摊费用。
---

# AI 订阅 {#ai-subscriptions}

很多人为 AI 付费的方式不是按 token 计费，而是**固定费用的套餐**：Claude Pro 或 Max、ChatGPT Plus 或 Pro、Cursor Pro、GitHub Copilot、Google AI Pro。Specivo 可以登记这些个人套餐（“席位”），用途有两个：

- 套餐所覆盖的工具产生的 AI 使用，会被计为**由订阅覆盖**，而不是按量付费支出或来源不明。
- [支出报表](../projects/ai-spend-reports.md)会把每个套餐的费用分摊到它覆盖的使用上，并按 [API 价格](ai-model-prices.md)估值，让你比较这个席位花了多少钱、同样的使用按 token 计费又要花多少钱。

订阅是个人的：每条订阅只属于一个人。团队共享的套餐暂不支持。

## 谁能查看和修改 {#who-sees-and-changes-them}

| | 可查看 | 可添加、修改、删除 |
|---|---|---|
| 本人 | 自己的 | 自己的，前提是自助管理已开启（默认开启） |
| 管理员 | 所有人的 | 所有人的 |
| 其他人，包括项目经理 | 无 | 无 |

服务账户不持有订阅：AI 智能体（AI agent）代表某个人上报的使用，会匹配到那个人的套餐。

管理员可以在 **管理 → AI 订阅 → 谁可以更改订阅** 下关闭**自助管理**。关闭后，用户仍能看到自己的订阅，但只有管理员能修改。管理员添加、修改或删除**他人**的订阅时，这次更改会写入[审计日志](audit-log.md#ai-subscriptions)。

## 添加你的订阅 {#add-your-subscription}

从用户菜单或[个人资料](../getting-started/your-profile.md)打开 **我的 AI 订阅**。

1. 在 **从套餐开始** 下选择你的套餐，点击 **填入**。表单会自动填上套餐的提供商、覆盖的工具和建议价格。
2. 把 **费用** 和 **货币** 改成你实际支付的金额，例如每月 `3500` THB 或 `90` EUR。如果按年付费，选择 **每年**。
3. 设置 **首日**。套餐仍在使用期间，**最后一日** 留空；取消时再填写。
4. 点击 **添加订阅**。

!!! note "建议价格仅供参考"
    列出套餐的价格，是制作这个 Specivo 版本时各提供商公布的美国标价。它们可能已经过时，也不包含税费和各地区的本地定价。请始终填写你实际被扣的金额。

列出的套餐有 Claude Pro、Claude Max 5x、Claude Max 20x、ChatGPT Plus、ChatGPT Pro、Cursor Pro、GitHub Copilot Pro、Pro+ 和 Max、Google AI Pro、Google AI Ultra（5x 和 20x），以及 SuperGrok。其他套餐选择 **自定义**，然后输入套餐名称、提供商和费用。

费用的小数位数不能超过该货币的小数位数（USD 或 THB 为两位，JPY 为零位），并且必须大于零。可以使用任何已启用的[货币](currencies.md)。

要修改或结束一条订阅，点击 **编辑**。要记录你已取消，请设置 **最后一日**，不要删除：删除会把这个套餐从过去的支出报表中一并移除。

## 订阅覆盖哪些使用 {#which-usage-a-subscription-covers}

每条订阅都指定一个**提供商**，还可以选填它覆盖的**工具**。

- **勾选了工具**：套餐覆盖这些工具产生的使用，无论它们用的是什么模型。Cursor Pro 覆盖 Cursor，即使 Cursor 运行的是 Claude 或 GPT 模型。
- **未勾选工具**：套餐覆盖**该提供商的工具**，也就是默认提供商为该提供商的那些工具，且仅限它们使用该提供商模型的情况。Claude Pro 覆盖 Claude Code；ChatGPT Plus 覆盖 Codex；Google AI Pro 覆盖 Gemini CLI。它不覆盖恰好调用同样模型的其他厂商工具。

当一个人有多个套餐都能覆盖同一项使用时，明确列出该工具的套餐优先于只覆盖提供商工具的套餐；两个同类套餐之间，开始日期较晚的优先。

### 日期与时区 {#days-and-time-zones}

首日和最后一日都**按 UTC 日计**，且都包含在内。一个从 3 月 1 日到 3 月 31 日的套餐，覆盖从 3 月 1 日 00:00 UTC 起、到 4 月 1 日 00:00 UTC（不含）为止的使用。使用按其时间所在的 UTC 日匹配，所以曼谷时间 4 月 1 日 06:30 的使用（UTC 为 3 月 31 日 23:30）仍在覆盖范围内。这与 AI 使用计价所用的日期边界相同。

### 费用如何按月分摊 {#how-the-fee-is-spread-over-months}

报表按自然月计算套餐费用：

- 月付套餐在它完整覆盖的每个月都计入全部费用。
- 年付套餐每月计入费用的十二分之一。
- 套餐只覆盖部分天数的月份（因为在月中开始或结束），按天数比例计入：一个 20 USD 的套餐从 4 月 21 日开始，4 月计入 20 的 10/30，约 6.67 USD。

金额保持套餐自身的货币；报表使用[汇率](exchange-rates.md)换算。

## AI 工具 {#ai-tools}

上报 AI 使用的工具列在 **管理 → AI 订阅 → AI 工具** 下。Specivo 初始自带 Claude Code、Codex、Gemini CLI、Cursor、GitHub Copilot、OpenCode 和 Other。

| 字段 | 含义 |
|---|---|
| Slug | 工具和上报程序发送的名称，例如 `claude-code`。创建后不可更改。 |
| 名称 | 用户看到的名称。 |
| 默认提供商 | 未上报提供商时，为该工具的使用假定的模型提供商；也是未列出该工具却能覆盖它的套餐所属的提供商。使用多个提供商的工具（例如 Cursor）留空。 |
| 位置 | 在列表中的顺序。 |
| 启用 | 未启用的工具不会出现在选择器中。 |

当上报程序发送了 Specivo 尚不认识的工具时，该工具会被**自动添加，且处于未启用状态**，这样新的上报程序无需升级 Specivo 就能工作。启用它并设置名称和默认提供商后，它就会出现在选择器中。工具永远不会被删除。

## 借助你的 AI 智能体 {#with-your-ai-agent}

通过 [MCP](../ai-agents/connecting.md) 连接到 Specivo 的智能体可以替你登记订阅。把账单交给它并提出请求，例如：

> 这是我今年的 Claude 和 ChatGPT 账单。把它们登记为我的 AI 订阅。

智能体使用 `specivo_ai_subscriptions` 工具：

| 操作 | 作用 |
|---|---|
| `presets` | 列出各套餐及其建议价格，便于智能体把每张账单对应到套餐。 |
| `list` | 显示你的订阅。管理员可以指定其他人（`user`）。 |
| `add` | 一次调用登记一个或多个计费周期，例如账单上的每一次套餐变更。每个周期包含套餐（或带提供商和名称的自定义套餐）、费用、货币、计费周期、首日和最后一日，以及备注。 |
| `update` | 修正一条订阅。 |
| `end` | 设置一条订阅的最后一日，默认为今天。 |
| `delete` | 删除一条订阅。优先使用 `end`：删除会把这个套餐从过去的报表中移除。 |

把每个账单周期登记为单独的一条订阅，填写实际扣款金额（含税）及其货币，并为后面紧接另一个周期的每个周期设置最后一日。例如：1 月 21 日到 2 月 25 日 Claude Max 5x 100 USD，接着 3 月 26 日到 5 月 25 日 Max 20x 200 USD，然后又回到 Max 5x。

`add` 要么全部成功，要么全部失败。只要有一个周期有误（未知套餐、费用不是数字、最后一日早于首日），就什么都不会添加，并告知智能体每个周期需要修正什么。

每次返回都会显示每条订阅的套餐、费用、货币、日期和覆盖的工具，并在同一套餐族的两个周期重叠或留有空档时**发出警告**。套餐族指你在同一提供商下覆盖相同工具的套餐，所以 Claude Pro、Max 5x 和 Max 20x 是一个族，ChatGPT Plus 是另一个族。在上面的例子里，智能体会对 2 月 26 日到 3 月 25 日的空档发出警告。警告从不阻止任何操作，因为空档往往是真实的（有一个月没付费），重叠也一样（在计费周期中途升级）。

这个工具遵循与页面相同的规则：它以智能体所用 API 密钥所属的人的身份操作；除非该人是管理员，否则写入需要开启自助管理；管理员对他人订阅的更改会被审计。两类密钥会被拒绝，并各自给出说明原因的消息：

- **限定了项目范围的密钥。** 订阅属于账户，请使用不限项目的个人密钥。
- **服务账户的密钥。** 订阅属于个人。请用你自己的个人密钥连接智能体，或请管理员代为登记。

工具做出的每一次更改也会以 `ai_subscriptions_changed` 写入[审计日志](audit-log.md#ai-subscriptions)，与智能体的其他所有更改一样。

## 通过 API {#through-the-api}

金额是十进制**字符串**，例如 `"20.00"`，绝不是 JSON 数字；日期格式为 `YYYY-MM-DD`。

```bash
# Add your own subscription from a plan, with your own price
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  https://specivo.example.com/api/v1/my/ai-subscriptions/ \
  -d '{"plan_key": "claude-max-5x", "amount": "3500", "currency": "THB", "starts_on": "2026-11-01"}'

# Record that it ended
curl -X PATCH -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  https://specivo.example.com/api/v1/my/ai-subscriptions/42/ -d '{"ends_on": "2027-01-31"}'
```

| 端点 | 谁可以用 |
|---|---|
| `GET /api/v1/ai-tools/`（`?include_inactive=true`） | 任何已登录用户 |
| `GET /api/v1/ai-subscription-presets/` | 任何已登录用户 |
| `GET`、`POST /api/v1/my/ai-subscriptions/`；`GET`、`PATCH`、`DELETE /api/v1/my/ai-subscriptions/{id}/` | 本人，只针对自己的订阅；写入需要开启自助管理 |
| `GET`、`POST /api/v1/admin/ai-subscriptions/`（`?user_id=`、`?provider=`、`?active_on=`）；`GET`、`PATCH`、`DELETE /api/v1/admin/ai-subscriptions/{id}/` | 管理员 |
| `GET`、`PATCH /api/v1/admin/ai-subscriptions/settings/`（`{"self_service": false}`） | 管理员 |
| `GET`、`POST /api/v1/admin/ai-tools/`；`PATCH /api/v1/admin/ai-tools/{slug}/` | 管理员 |

传入套餐的 `plan_key` 时，省略的字段取该套餐的值；使用 `custom` 时，需要提供 `provider`、`plan_name`、`amount` 和 `currency`。限定了项目范围的 API 密钥不能调用这些端点：订阅属于账户，而不是某个项目。
