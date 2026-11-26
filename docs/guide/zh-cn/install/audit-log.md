---
description: 了解 Specivo 安全审计日志会记录哪些事件、不记录哪些内容，日志默认保留多久、如何调整，管理员如何查看和筛选，以及其中包含哪些个人数据。
---

# 安全审计日志 {#security-audit-log}

Specivo 会把登录、访问权限变更、管理操作、单点登录（SSO）与预配，以及 AI 智能体（AI agent）通过 MCP 执行的所有操作记入安全审计日志。所有安装实例都会记录，无需手动开启。管理员可以在 **管理 → 审计日志** 中查看，也可以通过 API 读取。

## 记录的内容 {#what-is-recorded}

每条记录包含时间、**事件类型**（一个代码，例如 `login_failure` 或 `scim_user_created`）、执行操作的账户，以及请求来源：客户端地址、请求 ID 和浏览器或客户端名称（用户代理，user agent）。涉及某个项目或对象的记录还会写明该项目或对象。**详情** 中保存 ID、标识（key）、计数和简短名称，见[详情中保存的内容](#what-the-details-hold)。

查看页面把事件类型分为以下类别：

| 类别 | 包含的内容 |
|---|---|
| 登录 | 密码登录及登录失败、被拒绝的凭据（`auth_failure`）、密码重置与修改、电子邮件地址变更。 |
| 访问 | 被拒绝的权限检查（`access_denied`）；通过 API 查看资源；搜索；项目成员与用户组变更；角色的创建、修改或删除（见[角色与权限](roles.md#audit-log)）；匿名访问设置。 |
| 智能体 | 仅由智能体通过 MCP 执行的操作：读取和列出工作项（Issue）、维基（Wiki）页面、项目、成员和版本，以及创建或修改工作项、评论、维基页面、版本、关联、重复模板、工时记录和 AI 订阅。 |
| 工作项和维基 | 附件、维基页面的删除与恢复、冲刺（Sprint）的开始与完成、标签、被丢弃的计时器。由智能体写入的记录带有 **MCP** 标记。 |
| 管理 | 项目标识重命名；代码仓库集成迁移到其他提供商；元数据架构变更；账户停用；货币的启用或停用；报表货币变更；汇率的添加、编辑、删除或导入；汇率设置的修改以及汇率自动获取的开启或关闭；AI 模型价格的添加、修改或删除；内置价格的加载；管理员添加、修改或删除他人的 AI 订阅；AI 订阅自助服务的开启或关闭。管理员对用户的操作（创建、锁定、重置密码、API 密钥）归在“访问”类别下，事件类型为 `member_change`。 |
| 单点登录 | 单点登录的登录及失败；账户的创建、关联或取消关联；角色同步；提供商配置。 |
| 预配 | SCIM 用户和用户组变更、被拒绝的 SCIM 请求、SCIM 令牌变更。 |
| 会话 | 被撤销的会话、单点登录登出（包括后端通道登出）、身份提供商（IdP）拒绝续期的会话。 |
| AI 使用 | 以他人名义记录的 AI 使用、由非所有者修改的条目、从报表中排除或重新纳入的条目、被删除的条目、价格变更后重新计算的成本，以及智能体通过 MCP 记录或修改的 AI 使用。 |

每次 MCP 工具调用都会被记录，详情中带有 `source: mcp`，并包含该次调用的客户端地址、请求 ID 和用户代理。

## 不记录的内容 {#what-is-not-recorded}

- **人对工作项和维基页面的编辑。** 在 Web 界面或通过 REST API 编辑工作项、发表评论或修改维基页面，不算审计事件。这些变更保存在工作项的历史记录和维基页面的版本中，并带有修改人和修改时间。智能体通过 MCP 做同样的编辑时，两处都会记录。
- **通过的权限检查。** 打开一个你有权查看的页面不会写入任何记录；被拒绝的检查才会写入（`access_denied`）。
- **不带凭据的请求。** 已退出登录的浏览器标签页、爬虫或端口扫描器调用 API 时不会写入任何记录，浏览器会话只是过期时也一样。
- **重复的拒绝。** 提交后被拒绝的凭据（错误的令牌，或者无效、已停用、已过期的 API 密钥）记为 `auth_failure`，按客户端地址和原因每分钟记录一次，而不是每个请求记录一次。API 和 MCP 的拒绝分开计数，来自 MCP 端点的拒绝带有 **MCP** 标记。
- 对公开项目的 **匿名读取**。

## 查看日志 {#reading-it}

**管理 → 审计日志**（`/admin/audit-log/`）按时间从新到旧列出事件。可以按类别、事件类型、用户登录名、项目标识、日期范围和客户端地址筛选；日期按你所在时区的自然日计算，页面默认显示最近 30 天。时间按你的时区显示。点击某一行的 **显示**，可以查看该行的详情、请求 ID 和用户代理。事件发生后已被删除的用户或项目，显示为 `#` 加其 ID。

同样的数据可以通过 `GET /api/v1/admin/audit-logs/` 获取，支持的筛选参数有 `event_type`、`event_types`（要筛选多个时重复该参数）、`user_id`、`login`、`project_id`、`ip_address`、`after`、`before`、`offset` 和 `limit`。每个条目都带有 `user_login` 和 `project_key`。事件类型或登录名不存在时返回 `422`。

只有实例管理员可以读取日志。管理员的 API 密钥可以用于该 API；仅限部分项目的密钥不行。

## 保留期限 {#retention}

记录默认保留 **12 个月**，按完整的自然月计算：在 2026 年 11 月 4 日，保留的最早一天是 2025 年 11 月 1 日。每日任务（worker 启动时也会运行一次）会删除更早的月份。要修改保留期限，设置下面的环境变量，然后重启整个服务栈：

| 变量 | 含义 |
|---|---|
| `AUDIT_LOG_RETENTION_MONTHS` | 当前月份之前要保留的审计日志完整月数。默认为 `12`。设为 `0` 则保留全部记录。 |

这项设置有意不放在管理页面中：安全证据保留多久，不应该能从管理员的会话或 API 密钥中修改。

日志存储在 `security_audit_logs` 表的按月分区中，所以删除一个月的数据很快。如果任务在 5 秒内拿不到该表的锁，就会放弃，第二天再试。日志是数据库的一部分，因此[备份](backup-restore.md)会包含它。

## 详情中保存的内容 {#what-the-details-hold}

对象的 ID、标识、slug、名称和标题（文件名、页面标题、冲刺名称），代码，计数，账户登录名，用户组名称，以及截断到 500 个字符的搜索文本。绝不包含令牌、密钥或任何作为密码输入的内容，也绝不包含工作项、评论、描述或维基页面的正文。

登录失败时，只有当登录名对应一个已存在的账户才会记录该登录名；否则记录中写的是 `login_known: false`，这样误把密码输入到登录名字段时，密码不会被保存。密码重置请求对邮箱地址的处理方式相同：只有地址与某个账户匹配时才保存，否则记录中写的是 `email_known: false`。

### 电子邮件地址变更 {#email-address-changes}

| 事件 | 账户 | 详情 |
|---|---|---|
| `email_change_requested` | 本人 | `new_email`、`address_in_use`（该地址已被其他账户使用：没有发送链接）、`resend` |
| `email_change_failed` | 本人 | `reason`：密码错误；没有在身份提供商处重新登录；或者确认链接已被使用、已被替换、已取消、已过期，或其地址在此期间被其他账户占用 |
| `email_changed` | 本人或管理员 | `by`（`user` 或 `admin`）、`target_user_id`、`target_login`、`old_email`、`new_email`；本人修改时还有 `revoked_sessions` |

与任何请求都不匹配的确认链接不会写入记录。见[个人资料](../getting-started/your-profile.md#changing-your-email-address)。

### 代码仓库提供商变更 {#repository-provider-changes}

| 事件 | 账户 | 详情 |
|---|---|---|
| `repository_provider_changed` | 保存该变更的人 | `old_provider`、`new_provider`、`old_repository`、`new_repository`、`secret_rotated`；记录中写明项目和集成（`repository_integration` 及其 ID） |

更换集成的提供商后，该集成会得到新的 webhook URL 和新的密钥；密钥本身绝不会写入日志。对代码仓库集成的其他编辑不算审计事件。见[代码仓库集成](../integrations/repositories.md#change-the-provider)。

### 货币 {#currencies}

| 事件 | 账户 | 详情 |
|---|---|---|
| `currency_toggled` | 管理员 | `code`、`is_active` |
| `reporting_currency_changed` | 管理员或项目经理 | `scope`（`instance` 或 `project`）、项目级变更时的 `project_key`、`old`、`new`；项目的值为 `null` 表示跟随实例设置 |

保存与当前值相同的值不会写入记录。见[货币与报表货币](currencies.md)。

### 开发链接 {#development-links}

| 事件 | 账户 | 详情 |
|---|---|---|
| `development_link_detached` | 解除链接的人 | `issue`、`item_type`（`commit` 或 `pull_request`）、`item_id`、`item`（提交的 SHA，或 `#N` / `!N`）、`repository`；记录中写明项目和工作项 |
| `development_link_moved` | 移动链接的人 | 同上，另有 `target_issue` 和 `target_project_id` |

指通过 API 把某个提交或拉取请求从工作项上解除，或移到另一个工作项。提交信息和拉取请求标题绝不会写入日志。见[代码仓库集成](../integrations/repositories.md#correcting-links-detach-and-move)。

### 汇率 {#exchange-rates}

| 事件 | 账户 | 详情 |
|---|---|---|
| `exchange_rate_created` | 管理员 | `rate_id`、`rate_date`、`base`、`quote`、`rate`、`source`（`manual`） |
| `exchange_rate_updated` | 管理员 | 同上，另有 `changes`：每个被修改的字段及其 `old` 和 `new` |
| `exchange_rate_deleted` | 管理员 | 被删除的汇率：`rate_id`、`rate_date`、`base`、`quote`、`rate`、`source` |
| `exchange_rates_imported` | 管理员 | `rows`、`created`、`updated`、`unchanged`、`date_from`、`date_to` |
| `exchange_rates_fetch_toggled` | 管理员 | `enabled`、`url` |
| `exchange_rate_settings_changed` | 管理员 | `changes`：`pivot`、`max_age_days` 或 `fetch_url`，各带 `old` 和 `new` |

CSV 预览、被拒绝的导入以及保存未改动的值都不会写入记录。每日的汇率获取本身不算审计事件；最近一次的结果显示在管理页面上。见[汇率](exchange-rates.md)。

### AI 模型价格 {#ai-model-prices}

| 事件 | 账户 | 详情 |
|---|---|---|
| `ai_model_price_created` | 管理员 | `price_id`、`provider`、`model`、`currency`、`effective_from`、`effective_to`、`source`（`admin`）、`is_active`、`prices`（已设置的各项价格，按每 100 万 token 计）；如果它结束了该模型的上一条价格，还有 `closed_price_id` |
| `ai_model_price_updated` | 管理员 | 同上，另有 `changes`：每个被修改的字段（`effective_to`、`is_active`、`aliases`、`note`）及其 `old` 和 `new` |
| `ai_model_price_deleted` | 管理员 | 被删除的价格，内容同 `ai_model_price_created` |
| `ai_model_prices_seeded` | 管理员；由 worker 在启动时加载时为空 | `as_of`（内置价格表的日期）、`inserted`、`closed`、`history_inserted`、`aliases_updated`、`unchanged`、`skipped`（均为计数） |

保存未改动的值，或加载一份不带来任何变化的内置价格表，都不会写入记录。安装时的首次加载在数据库迁移中执行，不做审计。见 [AI 模型价格](ai-model-prices.md)。

### AI 订阅 {#ai-subscriptions}

| 事件 | 账户 | 详情 |
|---|---|---|
| `ai_subscription_created` | 管理员 | `subscription_id`、`target_user_id`、`target_login`（订阅的持有人）、`provider`、`plan_key`、`plan_name`、`tool_slugs`、`amount`、`currency`、`billing_period`、`starts_on`、`ends_on` |
| `ai_subscription_updated` | 管理员 | 同上，另有 `changes`：每个被修改的字段及其 `old` 和 `new`；备注被修改时只显示为 `notes: {"changed": true}` |
| `ai_subscription_deleted` | 管理员 | 被删除的订阅，内容同 `ai_subscription_created` |
| `ai_subscription_member_added` | 管理员 | 公司席位套餐中的一个席位：`subscription_id`、`plan_name`、`member_id`、`target_user_id`、`target_login`（该成员）、`starts_on`、`ends_on` |
| `ai_subscription_member_updated` | 管理员 | 同上，另有 `changes`：`starts_on` 或 `ends_on` 及其 `old` 和 `new` |
| `ai_subscription_member_removed` | 管理员 | 被移除的席位，内容同 `ai_subscription_member_added` |
| `ai_subscriptions_self_service_toggled` | 管理员 | `enabled` |
| `ai_subscriptions_changed` | 智能体通过 MCP 操作时所用密钥所属的账户（`specivo_ai_subscriptions` 的添加、更新、结束或删除，以及席位操作） | `tool: "ai_subscriptions"`、`source: "mcp"`、`op`、`subscription_ids`、`target_login`（订阅属于谁；公司套餐没有此项），修改时还有被修改的 `fields`，席位操作时还有 `member_id` |

只有管理员对 **他人** 订阅或公司席位套餐的修改才记为 `ai_subscription_*`。公司套餐的事件没有 `target_login`，另有 `seat_count`；对其席位的每次修改都会记录。用户修改自己的订阅、管理员修改自己的订阅都不记录；但通过 MCP 做的每次修改都记为 `ai_subscriptions_changed`，与智能体的每次工具调用一样。备注绝不会复制到日志中。见 [AI 订阅](ai-subscriptions.md)。


### AI 计费账户 {#ai-billing-accounts}

| 事件 | 账户 | 详情 |
|---|---|---|
| `ai_billing_account_created` | 管理员 | `account_id`、`slug`、`name`、`kind`、`owner`（`instance`，或所有者的登录名）、`owner_user_id`、`payer`、`reimburse_share`、`route_patterns`、`tool_slugs`、`default_cost_type`、`is_active` |
| `ai_billing_account_updated` | 管理员 | 同上，另有 `changes`：每个被修改的字段及其 `old` 和 `new` |
| `ai_billing_account_deactivated` | 管理员 | 内容同 `ai_billing_account_updated`，在该修改停用了账户时写入 |
| `ai_usage_payer_default_changed` | 修改该项目设置的人 | `project_key`、`old`、`new`（`employer`、`employee` 或 `none`）、`entries` 和 `changed`（检查过的和被修改的使用条目数） |
| `ai_billing_accounts_changed` | 智能体通过 MCP 操作时所用密钥所属的账户（`specivo_ai_billing_accounts` 的创建、更新或停用） | `tool: "ai_billing_accounts"`、`source: "mcp"`、`op`、`account_ids`、`owner`，更新时还有被修改的 `fields` |

对公司账户的每次修改都会记录，管理员对 **他人** 个人账户的修改也会记录。用户修改自己的账户不记录；但通过 MCP 做的每次修改都记为 `ai_billing_accounts_changed`。如果某次修改导致使用记录重新计价，还会写入 `ai_usage_costs_recomputed`。见 [AI 计费账户](ai-billing-accounts.md)。

### 项目 AI 提供商 {#project-ai-providers}

| 事件 | 账户 | 详情 |
|---|---|---|
| `project_ai_provider_changed` | 管理员 | `op`（`set` 或 `removed`）、`project_key`；`set` 时还有 `provider`、`model`、`host`（基础 URL 的主机名）和 `api_key`（`set`、`kept` 或 `removed`；密钥本身绝不会被记录） |

见[概念提取](../projects/index.md#concept-extraction-ai)。

### AI 报销申请 {#ai-reimbursement-claims}

| 事件 | 账户 | 详情 |
|---|---|---|
| `ai_reimbursement_claim_submitted` | 提出申请的人 | `claim_id`、`holder_user_id`、`holder_login`、`month`（`YYYY-MM`）、`currency`、`total`、`entries`、`status_from`、`status_to` |
| `ai_reimbursement_claim_withdrawn` | 提出申请的人 | 同上 |
| `ai_reimbursement_claim_approved` | 管理员 | 同上，另有 `changed_entries`：申请提出后被编辑、重新计价或移除的条目 |
| `ai_reimbursement_claim_rejected` | 管理员 | 同上，另有 `reason_given`（拒绝原因本身绝不会复制到日志中） |
| `ai_reimbursement_claim_paid` | 管理员 | 同上 |
| `ai_reimbursement_claim_exported` | 下载该 CSV 的人 | 同上，另有 `format` |
| `ai_reimbursement_claims_changed` | 智能体通过 MCP 操作时所用密钥所属的账户（`specivo_ai_reimbursement_claims`） | `tool: "ai_reimbursement_claims"`、`source: "mcp"`、`op`、`claim_ids`、`holder` |

这些事件属于 **AI 使用** 类别，只有 `ai_reimbursement_claims_changed` 属于 **智能体** 类别。创建、刷新和删除草稿不会被记录。见 [AI 计费账户](ai-billing-accounts.md#reimbursement-claims)。

### AI 支出预算 {#ai-spend-budgets}

| 事件 | 账户 | 详情 |
|---|---|---|
| `ai_spend_budget_created` | 设置预算的人 | `budget_id`、`target`（`billing_account` 或 `payer`）、`account_id`、`account_slug`、`owner`（`instance` 或所有者的登录名）、`payer`、`amount`、`currency`、`thresholds`、`is_active` |
| `ai_spend_budget_updated` | 修改预算的人 | 同上，另有 `changes`：每个被修改的字段及其 `old` 和 `new` |
| `ai_spend_budget_deleted` | 删除预算的人 | 内容同 `ai_spend_budget_created` |
| `ai_spend_budget_alert_sent` | 无（每小时一次的检查） | 同上，另有 `month`（`YYYY-MM`）、已达到的 `thresholds`、`spend` 和 `recipients`（收到邮件的人数） |
| `ai_spend_budgets_changed` | 智能体通过 MCP 操作时所用密钥所属的账户（`specivo_ai_spend_budgets`） | `tool: "ai_spend_budgets"`、`source: "mcp"`、`op`、`budget_ids`、`target` |

公司账户和付款方上的预算始终会记录，管理员对 **他人** 预算的修改也会记录；用户修改自己的预算不记录，通过 MCP 修改时除外。前三个事件属于 **管理** 类别，`ai_spend_budget_alert_sent` 属于 **AI 使用** 类别，`ai_spend_budgets_changed` 属于 **智能体** 类别。见 [AI 计费账户](ai-billing-accounts.md#budgets-and-alerts)。

### AI 使用 {#ai-usage}

| 事件 | 操作者 | 详情 |
|---|---|---|
| `ai_usage_logged_for_other` | 记录该使用的账户 | `entry_id`、`target_user_id`、`target_login`（使用属于谁）、`source`、`tool`、`provider`、`model`、`usage_date`、`project_key`、`issue`、`reporter_api_key_id`；如果上报方为它已替该用户记录过的条目发送了新的合计值，还有 `status: "updated"` 和 `revision`（重新发送但没有新内容时不写入记录） |
| `ai_usage_updated_by_other` | 修改他人条目的人 | 同上，另有 `changes`：每个被修改的字段及其 `old` 和 `new`；评论被修改时只显示为 `comments: {"changed": true}` |
| `ai_usage_billing_overridden` | 手动设置或清除条目计费账户的人（条目可以是自己的，也可以是他人的） | 条目信息同上，另有 `override`（`set` 或 `cleared`）、`changes`（`billing_method`、`billing_account_id`、`payer` 和 `cost_type` 中发生变化的各项，带 `old` 和 `new`）和 `billing_account`（`old` 和 `new` 两个账户的 slug）。如果同时还修改了其他字段，仍会为那些字段写入 `ai_usage_updated_by_other` |
| `ai_usage_excluded`、`ai_usage_included` | 把条目从报表中排除或重新纳入的人 | 条目信息同上 |
| `ai_usage_deleted` | 删除条目的人 | 条目信息，另有其 `tokens` 和 `cost_type` |
| `ai_usage_logged`、`ai_usage_changed` | 智能体账户，通过 MCP（`specivo_log_ai_usage`；`specivo_ai_usage` 更新、排除或纳入条目） | `tool: "log_ai_usage"` 或 `"ai_usage"`、`source: "mcp"` 以及 `entry_id`；记录条目时另有 `target_login`、`tool`、`model`、`usage_date`、`project_key` 和 `issue`，修改时另有 `op` 和被修改的 `fields` |
| `ai_usage_costs_recomputed` | 发起重新计算的管理员 | `provider`、`models`、`from`、`to`、`entries`（读取的条目数）、`changed`，以及边际成本和 API 等价价值按货币汇总的 `before` / `after` 合计 |

用户通过 Web 界面或 REST API 为自己记录的使用不会记在这里：他们随时可以看到自己的条目。通过 MCP 记录时则会写入（`ai_usage_logged`），与智能体的每次工具调用一样。评论是自由文本，绝不会复制到日志中；AI 使用记录也绝不会保存任何提示词、代码或文件名。
### 智能体会话与 Hermes 接收器 {#agent-sessions-and-hermes-receivers}

| 事件 | 操作者 | 详情 |
|---|---|---|
| `agent_session_linked` | 智能体账户，通过 MCP（`specivo_agent_session` 设置客户端会话 ID 或正在处理的工作项） | 会话，以及工作项（记在该工作项所属的项目下）；工作项被清除（`issue_ref=""`）时，`issue_cleared` 保存原来的编号。清除一个没有工作项的会话不会写入记录 |
| 带有 `action: "agent_works_for_changed"` 的 `member_change` | 管理员 | `target_user_id`、`target_login`（该智能体）、`works_for_id` 和 `previous_works_for_id` |
| `hermes_receiver_created`、`hermes_receiver_updated`、`hermes_receiver_deleted` | 管理该接收器的人 | 接收器 |
| `hermes_event_rejected` | 无（发送方未登录） | 事件被拒绝的原因 |
| `agent_session_started`、`agent_session_ended` | 接收器的账户 | 会话，来自 Hermes 事件 |
| `agent_tool_called`、`agent_approval_requested`、`agent_approval_resolved`、`agent_subagent_started`、`agent_subagent_stopped` | 接收器的账户 | 该步骤：工具名称、状态以及它提到的标识，绝不包含发送给工具的内容或工具返回的内容 |

见 [Hermes 事件](../ai-agents/hermes-events.md)和[智能体会话](../ai-agents/capabilities.md#agent-sessions)。

## 个人数据 {#personal-data}

审计日志中保存着个人数据，所有实例管理员都可以读取：

- 被记录的人和智能体的 **客户端地址（IP）** 和 **用户代理**。
- 执行操作或被操作的账户的 **登录名**。
- 电子邮件地址变更事件中的 **电子邮件地址**：旧地址、新地址，以及请求变更的目标地址。
- 人和智能体输入的 **搜索文本**（最多 500 个字符）。

保留期限不要超过实际需要；在隐私声明中说明这份日志；答复个人数据查询请求时，把日志中的相关数据一并提供。如果部署在反向代理之后，请设置 [`TRUSTED_PROXIES`](configuration.md#client-addresses-behind-a-proxy)，让日志记录的是客户端地址，而不是代理的地址。
