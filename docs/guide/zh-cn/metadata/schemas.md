---
description: "了解 Specivo 元数据架构的工作方式：它如何把带类型的字段编成一组并挂载到项目或追踪器级别，以及五个内置预设各自包含哪些字段。"
---

# 元数据架构 {#metadata-schemas}

**元数据架构** 定义了一组带类型的[元数据](index.md)字段，并决定这些字段出现在哪些工作项（Issue）上。你不必逐个工作项地临时添加字段，只需启用一次架构，所有符合条件的工作项就会得到同一套一致的字段。

## 架构如何挂载 {#how-a-schema-attaches}

架构有两种应用方式：

- **项目级别**：字段出现在项目中的每一个工作项上，与追踪器（Tracker）无关。
- **按追踪器**：字段只出现在指定追踪器的工作项上，例如只出现在 **Bug**（缺陷）工作项上。

**一个工作项可以同时应用多个架构。** 项目级别的架构和特定追踪器的架构会同时挂载，所以一个 **Bug** 工作项可能既显示团队的标准字段，*又*显示缺陷专用字段。每个架构只贡献自己的字段，彼此互不干扰。

## 字段名称 {#field-names}

架构中的每个字段都可以带一个 **标题**，也就是大家实际看到的名称：它出现在工作项页面的元数据面板、工作项的历史记录、搜索筛选条件、REST API 和 MCP 中。没有标题时，Specivo 会把字段的键转换成易读的形式显示。标题可以使用任意文字。字段还可以标记为存放人员或版本，此时其值会以对应人员或版本的名称显示。

## 谁来管理架构 {#who-manages-schemas}

架构由 **Manager**（管理者）在 **管理 → 元数据预设**（`/admin/metadata-presets/`）中定义和启用。其他人只需在自己的工作项上填写字段即可，使用元数据不需要进入管理区域。

!!! note "仅限 Manager 操作"
    创建、启用或编辑架构需要 **Manager** 角色。如果你看不到 **管理 → 元数据预设**，请项目的 Manager 帮你配置所需字段。

![管理区域中的元数据预设](../../assets/img/admin-metadata-presets.webp)

## 项目派生（计算）字段 {#project-derived-computed-fields}

大多数元数据是按工作项逐个填写的。**计算** 字段则不同：它的值由 **项目** 唯一决定，因此该项目中的每个工作项都会自动获得相同的值，无需手动操作。典型用途是把项目按领域归组，比如用一个“领域”或“归属部门”字段标明项目属于组织的哪个部分。

计算字段 **按项目配置一次**，位置在 **项目 → 设置 → 元数据 → 计算字段**，需要项目管理权限。每个字段添加一行：字段名，以及项目中每个工作项应得到的值。同一份映射也可以通过 REST API 以 `computed_metadata` 设置，可以在创建项目时设置，也可以之后更新；有权管理该项目的人都可以通过 `GET /api/v1/projects/{key}/` 获取项目当前的配置。

配置好计算字段后：

- 它会 **自动填充到项目中的每个工作项** 上，覆盖所有创建途径——Web 界面、REST API 和 [AI 智能体](../ai-agents/index.md)，任何人都无需手动输入。
- 它 **从不存储在工作项上**，用户也 **无法编辑或覆盖**，因此永远不会与项目脱节。
- 工作项[移动到另一个项目](../issues/updating.md#move-an-issue-to-another-project)时，它会 **自动重新计算**，直接取新项目的值，无需额外操作。

由于值来自项目而非工作项，凡是应始终与所属项目保持一致的信息，计算字段都是合适的选择。

## 内置预设 {#built-in-presets}

Specivo 自带五个现成的预设。你可以直接启用，也可以把它们当作自定义架构的起点。每个预设都是一个架构，包含以下字段。

### 软件开发 {#software-development}

追踪工作项背后的代码。

| 字段 | 类型 |
|---|---|
| `component` | 字符串 |
| `commits` | 数组 |
| `branches` | 数组 |
| `pull_requests` | 数组 |

配置了[代码仓库集成](../integrations/repositories.md)后，提交和 Pull Request 关联到工作项时，`commits`、`branches` 和 `pull_requests` 会自动填充。

### 缺陷分类 {#bug-triage}

记录让缺陷可复现、可排优先级的细节。

| 字段 | 类型 |
|---|---|
| `severity` | 枚举：`critical`、`major`、`minor`、`trivial` |
| `environment` | 枚举：`production`、`staging`、`development`、`local` |
| `browser` | 字符串 |
| `steps_to_reproduce` | 字符串 |

### 内容与市场营销 {#content-marketing}

规划和追踪内容生产。

| 字段 | 类型 |
|---|---|
| `content_type` | 枚举：`blog`、`social`、`email`、`landing_page`、`video`、`whitepaper`、`case_study` |
| `target_audience` | 字符串 |
| `publish_date` | 日期 |
| `content_status` | 枚举：`idea`、`draft`、`in_review`、`approved`、`published` |

### 冲刺规划 {#sprint-planning}

为工作项添加敏捷估算字段。

| 字段 | 类型 |
|---|---|
| `story_points` | 整数（1–100） |
| `sprint` | 字符串 |
| `epic` | 字符串 |

### 研究与文档 {#research-documentation}

记录研究工作的来源和评审状态。

| 字段 | 类型 |
|---|---|
| `source_url` | URL |
| `reviewed_by` | 字符串 |
| `confidence` | 枚举：`high`、`medium`、`low` |
| `tags` | 数组 |

## 下一步 {#next-steps}

- [什么是元数据？](index.md)：概念介绍和字段类型。
- [使用元数据值](values.md)：填写字段、编辑数组、筛选工作项列表。
