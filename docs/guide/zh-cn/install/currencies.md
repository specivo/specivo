---
description: "介绍 Specivo 的货币设置：ISO 4217 货币列表、为选择器启用或停用货币、实例报表货币与项目级覆盖，以及金额的存储与舍入方式。"
---

# 货币与报表货币 {#currencies-and-the-reporting-currency}

Specivo 把每一笔金额都存为精确的十进制数，并记录其货币。货币来自 ISO 4217 标准，由管理员决定哪些货币可供选择、哪一种用于金额报表。

## 货币列表 {#the-currency-list}

打开 **管理 → 货币**。表格列出当前 ISO 4217 的全部代码，以及按你的语言显示的名称和符号、小数位数、种类和是否已启用。

| 种类 | 含义 | 初始是否启用 |
|---|---|---|
| 货币 | 国家或地区使用的货币，例如 USD、EUR、THB 或 JPY | 是 |
| 基金 | 基金代码，例如 CLF（Unidad de Fomento）或 USN（美元，次日结算） | 否 |
| 贵金属 | 黄金、白银、铂金和钯金（XAU、XAG、XPT、XPD） | 否 |
| 特殊代码 | 记账单位（例如 XDR）以及 XXX（“无货币”） | 否 |
| 测试代码 | XTS，保留用于测试 | 否 |

可以按代码或名称搜索，也可以用表格上方的筛选只显示已启用或只显示已停用的货币。**停用** 会把该货币从所有货币选择器中隐藏；**启用** 则重新显示。

停用货币不会影响任何已经在使用它的地方。已有金额保持不变，以它作为报表货币的项目继续照常，已经包含它的表单也仍然可以保存。只是不能再新选择它。

## 报表货币 {#reporting-currency}

金额报表会把各项金额汇总为同一种货币，即 **报表货币**。新实例默认为 **USD**。要修改，在 **管理 → 货币** 顶部的 **报表货币** 卡片中选择另一种货币，然后点击 **保存**。只能选择已启用的货币。

项目可以单独使用另一种货币。在项目的 **设置 → 常规** 标签页中，项目经理在 **报表货币** 下选择一种货币，或保留 **实例默认** 以跟随实例设置（见 [项目](../projects/index.md#reporting-currency)）。

修改报表货币不会改变任何已存储的金额，只决定报表换算到哪种货币。报表按管理员录入、导入或让 Specivo 自动获取的每日 [汇率](exchange-rates.md) 进行换算。

## 金额如何存储和舍入 {#how-amounts-are-stored-and-rounded}

- 金额是精确的十进制数，绝不使用浮点数。
- 用户输入的金额（例如价格或费用）小数位数不能超过该货币允许的位数：USD 或 EUR 为两位，JPY 为零位，BHD 为三位。`10.50` USD 会被接受，`10.505` USD 会被拒绝。
- Specivo 计算得出的金额（例如若干 AI token 的费用）以完整精度存储（最多八位小数），只在显示或汇总时舍入。舍入方式为四舍五入：`10.005` USD 显示为 `$10.01`。
- API 以字符串（`"12.50"`）收发金额，绝不使用 JSON 数字。

## API {#the-api}

| 端点 | 谁可用 | 作用 |
|---|---|---|
| `GET /api/v1/currencies/` | 任何已登录用户 | 已启用的货币：代码、按读者语言显示的名称和符号、小数位数。`?include=EUR` 会额外返回该代码，即使它已停用。 |
| `GET /api/v1/admin/currencies/` | 管理员 | 全部货币及其 ISO 名称、数字代码、种类和 `is_active`。可用 `?active=true` 或 `false` 筛选。 |
| `PATCH /api/v1/admin/currencies/{code}/` | 管理员 | `{"is_active": false}` 停用货币，`true` 启用。 |
| `GET` / `PATCH /api/v1/admin/settings/reporting-currency/` | 管理员 | 读取或设置实例报表货币：`{"code": "EUR"}`。 |
| `PATCH /api/v1/projects/{key}/` | 项目经理 | `{"reporting_currency_code": "THB"}` 设置项目自己的报表货币；`null` 清除。 |

传入的代码若不是已启用的货币，会被拒绝并返回 `422`，错误码为 `currency_unknown` 或 `currency_inactive`。通用设置端点（`PATCH /api/v1/admin/settings/`）不接受 `reporting_currency` 键；请使用上面的端点。

启用或停用货币，以及每一次报表货币的变更，都会写入 [安全审计日志](audit-log.md#currencies)。

## 列表来源 {#where-the-list-comes-from}

货币列表随 Specivo 一起发布，位于 `specivo/data/iso4217.csv`。ISO 4217 每年会有几次变动，Specivo 的新版本会带上这些变动。你对启用货币的选择在升级后保留；从标准中撤销的代码只会被停用，不会被删除。
