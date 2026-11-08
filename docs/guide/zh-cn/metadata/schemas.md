---
description: 介绍 Specivo 的元数据架构，包括架构如何把类型化字段组合成一组、如何在项目或追踪器级别启用，以及五个内置预设各自包含的字段。
---

# 元数据架构 {#metadata-schemas}

**元数据架构** 定义一组带类型的 [元数据](index.md) 字段，并决定这些字段出现在哪些工作项（Issue）上。你不必一个工作项一个工作项地添加字段，只需启用一次架构，所有匹配的工作项就会得到同一套字段。

## 架构的生效范围 {#how-a-schema-attaches}

架构有两种生效方式：

- **项目级别**：字段出现在该项目的每个工作项上，不区分追踪器（Tracker）。
- **按追踪器**：字段只出现在指定追踪器的工作项上，例如只出现在 **Bug**（缺陷）工作项上。

**一个工作项可以同时应用多个架构**。项目级架构和追踪器专属架构会同时附加，因此一个 **Bug** 工作项可能既显示团队的标准字段，*又* 显示缺陷专属字段。每个架构只提供自己的字段，彼此互不干扰。

## 字段名称 {#field-names}

架构中的每个字段都可以设置 **标题**，也就是用户看到的名称：工作项页面的元数据面板、工作项的历史记录、搜索筛选条件、REST API 和 MCP 中显示的都是它。没有标题时，Specivo 会把字段的键转成易读形式显示。标题可以使用任何文字。字段还可以标记为保存人员或版本，这时它的值会显示为对应人员或版本的名称。

## 架构如何校验 {#how-schemas-are-enforced}

架构不仅决定显示哪些字段，还决定接受哪些值：无论从哪个客户端保存，违反架构的值都会被拒绝。Specivo 只检查本次保存改动的内容，所以之后收紧架构（新增必填字段、缩小选项范围、禁止额外的键）不会锁住已有工作项：旧值会一直保留，直到有人编辑那些字段。从那时起，新建的工作项必须填写必填字段。详情见 [哪些值会被拒绝](values.md#what-is-refused)，其中也说明了如何找出仍保留旧值的工作项。

## 谁来管理架构 {#who-manages-schemas}

架构由 **Manager**（管理者）在 **管理 → 元数据预设**（`/admin/metadata-presets/`）中定义和启用。其他人只需在自己的工作项上填写字段，使用元数据不需要进入管理区域。

!!! note "仅限 Manager"
    创建、启用或编辑架构需要 **Manager** 角色。如果你看不到 **管理 → 元数据预设**，请项目的 Manager 帮你配置所需字段。

![管理区域中的元数据预设](../../assets/img/admin-metadata-presets.webp)

## 项目派生（计算）字段 {#project-derived-computed-fields}

大多数元数据按工作项逐个填写。**计算** 字段则不同：它的值由 **项目** 固定决定，因此该项目的每个工作项都会自动获得同一个值，无需手动操作。典型用途是把项目归入某个领域，例如用一个“Area”或“cabinet”字段标明项目属于组织的哪个部分。

计算字段在 **项目 → 设置 → 元数据 → 计算字段** 中 **按项目配置一次**，需要项目管理权限。每行一个字段：填写字段名，以及该项目所有工作项应获得的值。同一份映射也可以通过 REST API 以 `computed_metadata` 设置，创建项目时或之后更新时都可以；有项目管理权限的人都能通过 `GET /api/v1/projects/{key}/` 读取项目当前的配置。

配置好计算字段后：

- 它会 **自动填充到项目中的每个工作项** 上，不论通过 Web 界面、REST API 还是 [AI 智能体（AI agent）](../ai-agents/index.md) 创建，都无需任何人手动输入。
- 它 **不会保存在工作项上**，用户也 **无法编辑或覆盖**，因此永远不会与项目脱节。
- 当工作项 [移动到其他项目](../issues/updating.md#move-an-issue-to-another-project) 时，它会 **自动重新计算**，直接取新项目的值，无需额外操作。

由于值来自项目而非工作项，凡是应当始终与所属项目保持一致的信息，都适合用计算字段。

## 内置预设 {#built-in-presets}

Specivo 自带五个现成的预设。你可以直接启用，也可以在它们的基础上创建自己的架构。每个预设都是一个架构，包含以下字段。

### Software Development（软件开发） {#software-development}

记录工作项背后的代码。

| 字段 | 类型 |
|---|---|
| `component` | 字符串 |
| `commits` | 数组 |
| `branches` | 数组 |
| `pull_requests` | 数组 |

接入 [代码仓库集成](../integrations/repositories.md) 后，提交和拉取请求关联到工作项时会自动填写 `commits`、`branches` 和 `pull_requests`。

### Bug Triage（缺陷分类） {#bug-triage}

记录让缺陷可复现、可排定优先级的细节。

| 字段 | 类型 |
|---|---|
| `severity` | 枚举：`critical`、`major`、`minor`、`trivial` |
| `environment` | 枚举：`production`、`staging`、`development`、`local` |
| `browser` | 字符串 |
| `steps_to_reproduce` | 字符串 |

### Content & Marketing（内容与营销） {#content-marketing}

规划并跟踪内容生产。

| 字段 | 类型 |
|---|---|
| `content_type` | 枚举：`blog`、`social`、`email`、`landing_page`、`video`、`whitepaper`、`case_study` |
| `target_audience` | 字符串 |
| `publish_date` | 日期 |
| `content_status` | 枚举：`idea`、`draft`、`in_review`、`approved`、`published` |

### Sprint Planning（冲刺规划） {#sprint-planning}

为工作项添加敏捷估算字段。

| 字段 | 类型 |
|---|---|
| `story_points` | 整数（1–100） |
| `sprint` | 字符串 |
| `epic` | 字符串 |

### Research & Documentation（研究与文档） {#research-documentation}

记录研究工作的来源和评审状态。

| 字段 | 类型 |
|---|---|
| `source_url` | URL |
| `reviewed_by` | 字符串 |
| `confidence` | 枚举：`high`、`medium`、`low` |
| `tags` | 数组 |

## 后续步骤 {#next-steps}

- [什么是元数据？](index.md)：概念和字段类型。
- [使用元数据值](values.md)：填写字段、编辑数组、筛选工作项列表。
