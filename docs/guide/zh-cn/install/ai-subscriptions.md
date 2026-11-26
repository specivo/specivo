---
description: 在 Specivo 中登记个人付费的固定费用 AI 套餐（Claude Pro/Max、ChatGPT、Cursor、Copilot 等）和公司团队套餐及其席位，支持任意货币，让 AI 使用计为订阅覆盖，并在支出报表中分摊费用。
---

# AI 订阅 {#ai-subscriptions}

很多人使用 AI 时购买的是 **固定费用套餐**，而不是按 token 计费，例如 Claude Pro 或 Max、ChatGPT Plus 或 Pro、Cursor Pro、GitHub Copilot、Google AI Pro。Specivo 会登记这些个人套餐（即“席位”），用途有两个：

- 如果 AI 使用来自套餐覆盖的工具，就计为 **由订阅覆盖**，而不是按量付费支出或来源未知。
- [支出报表](../projects/ai-spend-reports.md)会把每个套餐的费用分摊到它覆盖的使用上，这些使用按 [API 价格](ai-model-prices.md)估值，方便你对比席位的实际花费与同样的使用按 token 计费要花多少。

订阅属于个人：每个订阅只归一个人所有。由公司付费、多人共用的套餐（例如 Team 或 Enterprise 套餐）属于[公司席位套餐](#company-seat-plans)。每个订阅都是某个个人[计费账户](ai-billing-accounts.md)的费用计划，该账户随订阅一同创建，记录套餐由谁付费、雇主报销多大比例；**计费账户** 列会链接到该账户。

## 谁可以查看和修改 {#who-sees-and-changes-them}

| | 可查看 | 可添加、修改、删除 |
|---|---|---|
| 本人 | 自己的 | 自己的（需开启自助管理，默认开启） |
| 管理员 | 所有人的 | 所有人的 |
| 其他人（包括项目经理） | 无 | 无 |

服务账户不持有订阅：AI 智能体（AI agent）代某人上报的使用，会匹配到这个人的套餐。

管理员可以在 **管理 → AI 订阅 → 谁可以修改订阅** 中关闭 **自助管理**。关闭后，用户仍能看到自己的订阅，但只有管理员可以修改。管理员添加、修改或删除 **其他人** 的订阅时，变更会写入[审计日志](audit-log.md#ai-subscriptions)。

## 添加你的订阅 {#add-your-subscription}

从用户菜单或[个人资料](../getting-started/your-profile.md)打开 **我的 AI 订阅**。

1. 在 **从套餐开始** 下选择你的套餐，点击 **填入**。表单会自动填好该套餐的提供商、覆盖的工具和建议价格。
2. 把 **费用** 和 **货币** 改成你实际支付的金额，例如每月 `3500` THB 或 `90` EUR。如果按年付费，选择 **每年**。
3. 设置 **首日**。套餐仍在使用时，**末日** 留空；取消套餐时再填写。
4. 点击 **添加订阅**。

!!! note "建议价格仅供参考"
    列表中各套餐的价格，是当前 Specivo 版本制作时各提供商公布的美国标价。这些价格可能已经过时，也不含税费和地区定价。请务必填写你实际被收取的金额。

列表中的套餐有 Claude Pro、Claude Max 5x、Claude Max 20x、ChatGPT Plus、ChatGPT Pro、Cursor Pro、GitHub Copilot Pro、Pro+ 和 Max、Google AI Pro、Google AI Ultra（5x 和 20x）以及 SuperGrok。其他套餐请选择 **自定义**，然后输入套餐名称、提供商和费用。

费用的小数位数不能超过该货币的小数位数（USD 和 THB 为两位，JPY 没有小数位），且必须大于零。任何已启用的[货币](currencies.md)都可以使用。

要修改或结束订阅，点击 **编辑**。如果要记录你已取消套餐，请设置 **末日**，不要删除订阅：删除会把该套餐从过去的支出报表中一并移除。

## 订阅覆盖哪些使用 {#which-usage-a-subscription-covers}

每个订阅都指定一个 **提供商**，还可以选择它覆盖的 **工具**。

- **勾选了工具**：套餐覆盖来自这些工具的使用，无论它们用的是哪个模型。Cursor Pro 覆盖 Cursor，即使 Cursor 运行的是 Claude 或 GPT 模型。
- **未勾选任何工具**：套餐覆盖 **其提供商的工具**，也就是默认提供商与套餐提供商相同的工具，且仅限这些工具使用该提供商的模型时。Claude Pro 覆盖 Claude Code，ChatGPT Plus 覆盖 Codex，Google AI Pro 覆盖 Gemini CLI。其他厂商的工具即使调用了同样的模型，也不在覆盖范围内。

如果一个人有多个套餐都能覆盖同一笔使用，列出了该工具的套餐优先于覆盖提供商工具的套餐；两个套餐属于同一类时，开始日期较晚的优先。

### 日期与时区 {#days-and-time-zones}

首日和末日均 **按 UTC 日计**，且首尾两天都算在内。一个从 3 月 1 日到 3 月 31 日的套餐，覆盖从 UTC 时间 3 月 1 日 00:00 起、到 4 月 1 日 00:00 之前（不含该时刻）的使用。使用按其发生时间所在的 UTC 日匹配，因此曼谷时间 4 月 1 日 06:30（UTC 时间 3 月 31 日 23:30）的使用仍在覆盖范围内。AI 使用计价时采用的也是这一日界线。

### 费用如何按月分摊 {#how-the-fee-is-spread-over-months}

报表按自然月统计套餐费用：

- 按月付费的套餐，在它完整覆盖的每个月计入全部费用。
- 按年付费的套餐，每月计入费用的十二分之一。
- 如果套餐在某个月中途开始或结束，只覆盖了该月的一部分，则按天数比例计入：一个 20 USD 的套餐从 4 月 21 日开始，4 月计入 20 的 10/30，约 6.67 USD。

金额保持套餐的货币不变；报表会按[汇率](exchange-rates.md)换算。

## 公司席位套餐 {#company-seat-plans}

**公司席位套餐** 是由公司付费的 Team 或 Enterprise 套餐：一笔费用对应若干个 **席位**，每个席位在一段日期内由一人持有。管理员在 **管理 → 公司 AI 套餐** 中管理这些套餐；每一次变更（包括成员变更）都会写入[审计日志](audit-log.md#ai-subscriptions)。

### 添加套餐 {#add-a-plan}

1. 打开 **管理 → 公司 AI 套餐**。
2. 在 **添加公司套餐** 下，输入套餐名称和提供商、每月或每年的 **整个套餐的费用**、货币、所购 **席位** 的数量以及首日。勾选套餐覆盖的工具；如果覆盖的是其提供商的工具，则一个都不勾选，规则与个人套餐相同。
3. 点击 **添加套餐**。套餐页面随即打开。

费用可以为 **零**，适用于成本记在别处、但仍希望其使用显示为已覆盖并由公司支付的套餐。个人订阅的费用则必须大于零。

每个套餐都会获得一个类型为 **订阅** 的公司[计费账户](ai-billing-accounts.md)，由 **雇主** 支付。它的付款方不能改为员工。

### 为成员分配席位 {#give-people-their-seats}

在套餐页面的 **席位** 下，输入成员的 **登录名** 和席位的 **首日**（默认为今天），然后点击 **添加席位**。末日可以不填。

- **有人离职**：设置其席位的 **末日**，然后点击 **结束席位**。此前各月仍保留各自分摊的费用，过去月份的报表不会变化。
- **席位录入有误**：点击 **移除**。对应日期不再有该席位，就像从未有人持有过一样。
- 同一时间，一个人在一个套餐中最多持有一个席位；任何一天，持有席位的人数都不能超过套餐的席位数。请先为套餐增加席位，或结束某人的席位。

席位被添加、修改或移除时，从受影响的第一天起，该成员的 AI 使用会重新解析。因此，在席位登记之前上报的使用会相应地归入该套餐，或从该套餐中移出。

### 覆盖哪些使用 {#which-usage-it-covers}

公司套餐覆盖成员在持有席位期间的使用，规则与个人套餐相同：覆盖它列出的工具，或其提供商的工具。如果一个人持有席位，**同时** 自己也有覆盖同一笔使用的套餐，则 **优先使用公司套餐**。如果想把部分使用改记到个人套餐上，可以由使用上报方指定该套餐的计费账户，也可以由本人手动设置某条记录的计费方式。

### 费用如何分担 {#how-the-fee-is-shared}

费用拆分为 **等额席位**：成员每完整持有席位一个月，承担 `fee ÷ seats`；只持有了一部分的月份，按天数比例承担。例如，一个 750 USD、25 个席位的套餐，每位成员每月承担 30 USD；4 月 16 日加入的成员（4 月共 30 天，持有 15 天）在 4 月承担 15 USD。

每位成员承担的份额会分摊到 **本人** 当月对该套餐的使用上，与个人套餐的费用一样。成员当月没有这类使用时的份额，以及无人持有的席位，都属于 **未分摊的席位费用**。无人持有的席位在实例报表中单独显示为一行，不对应任何人。

套餐由公司付费，因此其使用属于 **雇主** 支出，其席位费用 **永远不可报销**。

## AI 工具 {#ai-tools}

上报 AI 使用的工具列在 **管理 → AI 订阅 → AI 工具** 下。Specivo 初始内置 Claude Code、Codex、Gemini CLI、Cursor、GitHub Copilot、OpenCode 和 Other。

| 字段 | 含义 |
|---|---|
| Slug | 工具和上报方发送的名称，例如 `claude-code`。创建后不可更改。 |
| 名称 | 用户看到的名称。 |
| 默认提供商 | 上报中未指明提供商时，为该工具的使用假定的模型提供商；该提供商的套餐无需列出此工具即可覆盖它。使用多个提供商的工具（例如 Cursor）此项留空。 |
| 位置 | 在列表中的排序。 |
| 启用 | 未启用的工具不会出现在选择器中。 |

如果使用上报方发来一个 Specivo 尚不认识的工具，该工具会 **自动添加，且处于未启用状态**，因此新的上报方无需升级即可工作。要让它出现在选择器中，请启用它，并为它设置名称和默认提供商。工具永远不会被删除。

## 通过 AI 智能体 {#with-your-ai-agent}

通过 [MCP](../ai-agents/connecting.md) 连接到 Specivo 的智能体可以替你登记订阅。把账单交给它，然后提出要求，例如：

> 这是我今年的 Claude 和 ChatGPT 账单，请把它们登记为我的 AI 订阅。

智能体使用 `specivo_ai_subscriptions` 工具：

| 操作 | 作用 |
|---|---|
| `presets` | 列出各套餐及其建议价格，便于智能体把每张账单对应到套餐。 |
| `list` | 显示你的订阅。管理员可以指定其他人（`user`）。 |
| `add` | 一次调用登记一个或多个周期，例如账单上的每一次套餐变更。每个周期包含套餐（或带提供商和名称的自定义套餐）、费用、货币、计费周期、首日和末日，以及备注。 |
| `update` | 更正一个订阅。 |
| `end` | 设置一个订阅的末日，默认为今天。 |
| `delete` | 删除一个订阅。建议优先使用 `end`：删除会把该套餐从过去的报表中移除。 |
| `list`、`add` 加 `company=true` | 管理员专用：列出或添加公司席位套餐。`add` 的每个周期都要给出 `seat_count`；费用可以为 `"0"`。`update`、`end` 和 `delete` 也接受公司套餐的 `subscription_id`。 |
| `members` | 管理员专用：列出公司套餐（`subscription_id`）中谁持有席位。 |
| `add_member` | 管理员专用：为某人（`user`，即登录名）分配席位，从 `starts_on`（默认为今天）开始，可选择到 `ends_on` 结束。 |
| `end_member` | 管理员专用：设置席位（`member_id` 或 `user`）的末日，默认为今天。 |
| `remove_member` | 管理员专用：移除录入有误的席位（`member_id`）。建议优先使用 `end_member`。 |

把每个账单周期登记为一个独立的订阅，金额填实际收取的含税金额，货币用账单上的货币；凡是后面还接着另一个周期的，都要填写末日。例如：1 月 21 日至 2 月 25 日为 Claude Max 5x，100 USD；3 月 26 日至 5 月 25 日为 Max 20x，200 USD；之后又换回 Max 5x。

`add` 要么全部成功，要么全部不执行。只要有一个周期有误（套餐未知、费用不是数字、末日早于首日），就什么都不会添加，智能体会收到每个周期需要修正的内容。

每次返回的结果都会列出各订阅的套餐、费用、货币、日期和覆盖的工具，并在同一套餐系列的两个周期重叠或出现空档时给出 **警告**。套餐系列是指你名下提供商相同、覆盖工具相同的套餐，因此 Claude Pro、Max 5x 和 Max 20x 是一个系列，ChatGPT Plus 是另一个系列。在上面的例子中，智能体会对 2 月 26 日至 3 月 25 日的空档发出警告。警告不会阻止任何操作，因为空档往往是真实情况（某个月你没有付费），重叠也是（在计费周期中途升级）。

该工具遵循与页面相同的规则：它代表智能体所用 API 密钥的所有者执行操作；写入需要开启自助管理，除非此人是管理员；管理员修改其他人的订阅会被审计。有两类密钥会被拒绝，并各自返回说明原因的消息：

- **仅限部分项目的密钥**。订阅属于账户，请使用不受限制的个人密钥。
- **服务账户的密钥**。订阅属于个人。请用你自己的个人密钥连接智能体，或请管理员代为登记。

该工具所做的每一次变更也会以 `ai_subscriptions_changed` 写入[审计日志](audit-log.md#ai-subscriptions)，与智能体所做的其他变更一样。

## 通过 API {#through-the-api}

金额是十进制 **字符串**，例如 `"20.00"`，绝不能是 JSON 数字；日期格式为 `YYYY-MM-DD`。

```bash
# Add your own subscription from a plan, with your own price
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  https://specivo.example.com/api/v1/my/ai-subscriptions/ \
  -d '{"plan_key": "claude-max-5x", "amount": "3500", "currency": "THB", "starts_on": "2026-11-01"}'

# Record that it ended
curl -X PATCH -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  https://specivo.example.com/api/v1/my/ai-subscriptions/42/ -d '{"ends_on": "2027-01-31"}'
```

| 端点 | 谁可以调用 |
|---|---|
| `GET /api/v1/ai-tools/`（`?include_inactive=true`） | 任何已登录用户 |
| `GET /api/v1/ai-subscription-presets/` | 任何已登录用户 |
| `GET`、`POST /api/v1/my/ai-subscriptions/`；`GET`、`PATCH`、`DELETE /api/v1/my/ai-subscriptions/{id}/` | 本人，仅限自己的订阅；写入需要开启自助管理 |
| `GET`、`POST /api/v1/admin/ai-subscriptions/`（`?user_id=`、`?provider=`、`?active_on=`）；`GET`、`PATCH`、`DELETE /api/v1/admin/ai-subscriptions/{id}/` | 管理员 |
| `GET`、`PATCH /api/v1/admin/ai-subscriptions/settings/`（`{"self_service": false}`） | 管理员 |
| `GET`、`POST /api/v1/admin/ai-company-plans/`（`?active_on=`）；`GET`、`PATCH`、`DELETE /api/v1/admin/ai-company-plans/{id}/` | 管理员 |
| `GET`、`POST /api/v1/admin/ai-company-plans/{id}/members/`（`{"login": "alice", "starts_on": "2026-11-01"}`）；`PATCH`、`DELETE /api/v1/admin/ai-company-plans/{id}/members/{member_id}/`（`{"ends_on": "2027-01-31"}`） | 管理员 |
| `GET`、`POST /api/v1/admin/ai-tools/`；`PATCH /api/v1/admin/ai-tools/{slug}/` | 管理员 |

使用套餐的 `plan_key` 时，省略的字段取该套餐的值；使用 `custom` 时，需要提供 `provider`、`plan_name`、`amount` 和 `currency`。仅限部分项目的 API 密钥不能调用这些端点：订阅属于账户，而不属于项目。
