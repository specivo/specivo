---
description: 介绍 Specivo 安全审计日志记录哪些事件、不记录哪些事件、日志保留多久、管理员如何在后台和 API 中查看，以及日志中包含哪些个人数据。
---

# 安全审计日志 {#security-audit-log}

Specivo 会记录一份安全审计日志，涵盖登录、访问变更、管理操作、单点登录（SSO）与账户预配，以及 AI 智能体（AI agent）通过 MCP 执行的所有操作。每个实例都会记录，无需额外开启。管理员可在 **管理 → 审计日志** 中查看，也可以通过 API 读取。

## 记录的内容 {#what-is-recorded}

每一行记录包含时间、**事件类型**（如 `login_failure` 或 `scim_user_created` 这样的代码）、执行操作的账户，以及请求来源：客户端地址、请求 ID 和浏览器或客户端名称（user agent）。涉及项目或某个对象的记录还会注明该项目或对象。**详情** 中保存 ID、键、计数和简短名称，详见[详情中保存的内容](#what-the-details-hold)。

查看器把事件类型分为以下几类：

| 类别 | 包含的事件 |
|---|---|
| 登录 | 密码登录及登录失败、被拒绝的凭据（`auth_failure`）、密码重置与修改、电子邮件地址变更。 |
| 访问 | 被拒绝的权限检查（`access_denied`）、通过 API 查看资源、搜索、项目成员与用户组变更、匿名访问设置。 |
| 智能体 | 仅由智能体通过 MCP 执行的操作：读取和列出工作项（Issue）、维基页面、项目、成员和版本；创建或修改工作项、评论、维基页面、版本、关联、重复模板、工时记录和 AI 订阅。 |
| 工作项和维基 | 附件、维基页面的删除与恢复、冲刺（Sprint）的开始与完成、标签、丢弃的计时。由智能体写入的记录会标记为 **MCP**。 |
| 管理 | 项目标识重命名、仓库集成切换到其他提供商、元数据架构变更、停用账户、货币的启用或停用、报告货币变更、汇率的添加、编辑、删除或导入、汇率设置及汇率抓取的开启或关闭、AI 模型价格的添加、修改或删除、内置价格的加载、管理员对他人 AI 订阅的添加、修改或删除，以及 AI 订阅自助服务的开启或关闭。管理员对用户的操作（创建、锁定、重置密码、API 密钥）归入“访问”类别，事件类型为 `member_change`。 |
| 单点登录 | 单点登录的登录及失败、账户的创建、关联或解除关联、角色同步、提供商配置。 |
| 预配 | SCIM 用户与用户组变更、被拒绝的 SCIM 请求、SCIM 令牌变更。 |
| 会话 | 被撤销的会话、单点登录登出（包括 back-channel 登出）、身份提供商（IdP）拒绝续期的会话。 |
| AI 使用 | 以他人名义记录的 AI 使用、由非所有者修改的条目、从报告中排除或重新纳入的条目、删除的条目、价格变更后重新计算的费用，以及智能体通过 MCP 记录或修改的 AI 使用。 |

每一次 MCP 工具调用都会记录，详情中带有 `source: mcp`，并包含该次调用的客户端地址、请求 ID 和 user agent。

## 不记录的内容 {#what-is-not-recorded}

- **人工对工作项和维基页面的编辑。** 在 Web 界面或通过 REST API 编辑工作项、发表评论或修改维基页面不属于审计事件。这些变更保存在工作项的历史和维基页面的版本中，包含修改人和时间。智能体通过 MCP 做的同类编辑则两处都有记录。
- **通过的权限检查。** 打开一个你有权查看的页面不会写入任何记录；被拒绝的检查才会（`access_denied`）。
- **没有凭据的请求。** 未登录的浏览器标签页、爬虫或端口扫描器调用 API 不会写入记录，刚好过期的浏览器会话也不会。
- **重复的拒绝。** 被提交并被拒绝的凭据（错误的令牌，或无效、停用、过期的 API 密钥），按客户端地址和原因每分钟只记录一次 `auth_failure`，而不是每次请求都记。API 与 MCP 的拒绝分开计数，来自 MCP 端点的记录标记为 **MCP**。
- 对公开项目的 **匿名读取**。

## 查看方式 {#reading-it}

**管理 → 审计日志**（`/admin/audit-log/`）按时间倒序列出事件。可按类别、事件类型、用户登录名、项目标识、日期范围和客户端地址筛选；日期按你所在时区的自然日计算，页面默认显示最近 30 天。时间按你的时区显示。点击某一行的 **显示** 可查看该行的详情、请求 ID 和 user agent。此后已被删除的用户或项目显示为 `#` 加其 ID。

同样的数据也可通过 `GET /api/v1/admin/audit-logs/` 获取，支持筛选参数 `event_type`、`event_types`（可重复传入多个）、`user_id`、`login`、`project_id`、`ip_address`、`after`、`before`、`offset` 和 `limit`。每条记录带有 `user_login` 和 `project_key`。传入未知的事件类型或登录名会返回 `422`。

只有实例管理员可以读取日志。管理员的 API 密钥可用于该 API；限定在部分项目的密钥则不行。

## 保留期限 {#retention}

默认保留 **12 个月**，按完整的自然月计算：在 2026 年 11 月 4 日，保留的最早一天是 2025 年 11 月 1 日。每天运行一次的任务（worker 启动时也会运行）会删除更早的月份。通过环境变量修改，然后重启整个服务栈：

| 变量 | 含义 |
|---|---|
| `AUDIT_LOG_RETENTION_MONTHS` | 当前月份之前保留的完整月数。默认 `12`。`0` 表示全部保留。 |

这个设置刻意没有放进管理页面：安全证据的保留时长不应该能从管理员的会话或 API 密钥中修改。

日志按月分区存储在 `security_audit_logs` 表中，因此删除一个月的数据很快。如果任务在五秒内拿不到该表的锁，就会放弃并在次日重试。日志是数据库的一部分，所以[备份](backup-restore.md)中会包含它。

## 详情中保存的内容 {#what-the-details-hold}

对象的 ID、键、slug、名称和标题（文件名、页面标题、冲刺名称）、代码、计数、账户登录名、用户组名称，以及截断到 500 个字符的搜索文本。绝不保存令牌、密钥或任何以密码形式输入的内容，也绝不保存工作项、评论、描述或维基页面的正文。

登录失败时，只有登录名对应现有账户才会记录该登录名；否则记录中写 `login_known: false`，这样误输入登录名栏的密码不会被保存。密码重置请求对邮箱地址的处理相同：只有匹配到账户时才保存，否则记录中写 `email_known: false`。

### 电子邮件地址变更 {#email-address-changes}

| 事件 | 账户 | 详情 |
|---|---|---|
| `email_change_requested` | 本人 | `new_email`、`address_in_use`（该地址已被另一账户使用，未发送链接）、`resend` |
| `email_change_failed` | 本人 | `reason`：密码错误、未在身份提供商处重新登录，或确认链接已使用、已被替换、已取消、已过期、其地址在此期间被另一账户占用 |
| `email_changed` | 本人或管理员 | `by`（`user` 或 `admin`）、`target_user_id`、`target_login`、`old_email`、`new_email`；本人修改时还有 `revoked_sessions` |

与任何请求都不匹配的确认链接不会写入记录。参见[个人资料](../getting-started/your-profile.md#changing-your-email-address)。

### 仓库提供商变更 {#repository-provider-changes}

| 事件 | 账户 | 详情 |
|---|---|---|
| `repository_provider_changed` | 保存变更的人 | `old_provider`、`new_provider`、`old_repository`、`new_repository`、`secret_rotated`；记录中注明项目和集成（`repository_integration` 及其 ID） |

更换集成的提供商会生成新的 webhook URL 和新的密钥；密钥本身绝不会记入日志。仓库集成的其他编辑不属于审计事件。参见[仓库集成](../integrations/repositories.md#change-the-provider)。

### 货币 {#currencies}

| 事件 | 账户 | 详情 |
|---|---|---|
| `currency_toggled` | 管理员 | `code`、`is_active` |
| `reporting_currency_changed` | 管理员或项目经理 | `scope`（`instance` 或 `project`）、项目级别时有 `project_key`、`old`、`new`；项目的 `null` 表示跟随实例设置 |

保存与当前相同的值不会写入记录。参见[货币与报告货币](currencies.md)。

### 开发链接 {#development-links}

| 事件 | 账户 | 详情 |
|---|---|---|
| `development_link_detached` | 解除链接的人 | `issue`、`item_type`（`commit` 或 `pull_request`）、`item_id`、`item`（commit SHA，或 `#N` / `!N`）、`repository`；记录中注明项目和工作项 |
| `development_link_moved` | 移动链接的人 | 同上，另加 `target_issue` 和 `target_project_id` |

指通过 API 将 commit 或 pull request 从工作项上解除，或移动到另一个工作项。commit 消息和 pull request 标题绝不会记入日志。参见[仓库集成](../integrations/repositories.md#correcting-links-detach-and-move)。

### 汇率 {#exchange-rates}

| 事件 | 账户 | 详情 |
|---|---|---|
| `exchange_rate_created` | 管理员 | `rate_id`、`rate_date`、`base`、`quote`、`rate`、`source`（`manual`） |
| `exchange_rate_updated` | 管理员 | 同上，另加 `changes`：每个变更的字段各带 `old` 和 `new` |
| `exchange_rate_deleted` | 管理员 | 被删除的汇率：`rate_id`、`rate_date`、`base`、`quote`、`rate`、`source` |
| `exchange_rates_imported` | 管理员 | `rows`、`created`、`updated`、`unchanged`、`date_from`、`date_to` |
| `exchange_rates_fetch_toggled` | 管理员 | `enabled`、`url` |
| `exchange_rate_settings_changed` | 管理员 | `changes`：`pivot`、`max_age_days` 或 `fetch_url`，各带 `old` 和 `new` |

CSV 预览、被拒绝的导入以及保存未变更的值都不会写入记录。每日抓取本身不是审计事件，其最近一次结果显示在管理页面上。参见[汇率](exchange-rates.md)。

### AI 模型价格 {#ai-model-prices}

| 事件 | 账户 | 详情 |
|---|---|---|
| `ai_model_price_created` | 管理员 | `price_id`、`provider`、`model`、`currency`、`effective_from`、`effective_to`、`source`（`admin`）、`is_active`、`prices`（每百万 token 的各项设定价格）；若它结束了该模型上一个价格的有效期，则还有 `closed_price_id` |
| `ai_model_price_updated` | 管理员 | 同上，另加 `changes`：每个变更的字段（`effective_to`、`is_active`、`aliases`、`note`）各带 `old` 和 `new` |
| `ai_model_price_deleted` | 管理员 | 被删除的价格，字段同 `ai_model_price_created` |
| `ai_model_prices_seeded` | 管理员；若由 worker 在启动时加载则无账户 | `as_of`（内置价格表的日期）、`inserted`、`closed`、`history_inserted`、`aliases_updated`、`unchanged`、`skipped`（均为计数） |

保存未变更的值，以及加载一份不带来任何变化的内置价格表，都不会写入记录。安装后的首次加载在数据库迁移中完成，不记入审计。参见[AI 模型价格](ai-model-prices.md)。

### AI 订阅 {#ai-subscriptions}

| 事件 | 账户 | 详情 |
|---|---|---|
| `ai_subscription_created` | 管理员 | `subscription_id`、`target_user_id`、`target_login`（订阅持有人）、`provider`、`plan_key`、`plan_name`、`tool_slugs`、`amount`、`currency`、`billing_period`、`starts_on`、`ends_on` |
| `ai_subscription_updated` | 管理员 | 同上，另加 `changes`：每个变更的字段各带 `old` 和 `new`；备注的变更只显示为 `notes: {"changed": true}` |
| `ai_subscription_deleted` | 管理员 | 被删除的订阅，字段同 `ai_subscription_created` |
| `ai_subscriptions_self_service_toggled` | 管理员 | `enabled` |
| `ai_subscriptions_changed` | 智能体所用密钥对应的账户，通过 MCP（`specivo_ai_subscriptions` 添加、更新、结束或删除） | `tool: "ai_subscriptions"`、`source: "mcp"`、`op`、`subscription_ids`、`target_login`（订阅属于谁），修改时还有变更的 `fields` |

只有管理员对 **他人** 订阅的修改才记录为 `ai_subscription_*`。用户修改自己的订阅，以及管理员修改自己的订阅，都不会记录；但与所有智能体工具调用一样，通过 MCP 的每一次修改都会记录为 `ai_subscriptions_changed`。备注绝不会复制到日志中。参见[AI 订阅](ai-subscriptions.md)。


### AI 使用 {#ai-usage}

| 事件 | 操作者 | 详情 |
|---|---|---|
| `ai_usage_logged_for_other` | 记录该使用量的账户 | `entry_id`、`target_user_id`、`target_login`（使用量属于谁）、`source`、`tool`、`provider`、`model`、`usage_date`、`project_key`、`issue`、`reporter_api_key_id`；当上报方为已替该用户记录过的条目发送新的总量时，还有 `status: "updated"` 和 `revision`（重复发送且无新内容时不写入记录） |
| `ai_usage_updated_by_other` | 修改他人条目的人 | 同上，另加 `changes`：每个变更的字段各带 `old` 和 `new`；评论的变更只显示为 `comments: {"changed": true}` |
| `ai_usage_excluded`、`ai_usage_included` | 将条目从报告中排除或重新纳入的人 | 该条目，字段同上 |
| `ai_usage_deleted` | 删除条目的人 | 该条目，另加其 `tokens` 和 `cost_type` |
| `ai_usage_logged`、`ai_usage_changed` | 智能体账户，通过 MCP（`specivo_log_ai_usage`；`specivo_ai_usage` 更新、排除或重新纳入条目） | `tool: "log_ai_usage"` 或 `"ai_usage"`、`source: "mcp"` 和 `entry_id`；记录新条目时还有 `target_login`、`tool`、`model`、`usage_date`、`project_key` 和 `issue`，修改时则有 `op` 和变更的 `fields` |
| `ai_usage_costs_recomputed` | 发起重算的管理员 | `provider`、`models`、`from`、`to`、`entries`（读取数）、`changed`，以及边际成本和 API 等价价值按货币汇总的 `before` / `after` 总额 |

用户通过 Web 或 REST API 为自己记录的使用量不在此记录：他们随时可以查看自己的条目。通过 MCP 记录的则会记录（`ai_usage_logged`），与所有智能体工具调用一样。评论是自由文本，绝不会复制到日志中；AI 使用记录中也绝不会保存任何提示词、代码或文件名。
## 个人数据 {#personal-data}

审计日志包含个人数据，所有实例管理员都可读取：

- 所记录的人员和智能体的 **客户端地址（IP）** 和 **user agent**。
- 执行操作或被操作的账户的 **登录名**。
- 邮箱变更事件中的 **电子邮件地址**：旧地址、新地址，以及申请变更时填写的地址。
- 人员和智能体输入的 **搜索文本**（最多 500 个字符）。

保留期限不要超过实际需要，在隐私声明中说明这份日志，并在回应个人数据请求时把它包括在内。如果部署在反向代理之后，请设置 [`TRUSTED_PROXIES`](configuration.md#client-addresses-behind-a-proxy)，让日志记录客户端地址而不是代理地址。
