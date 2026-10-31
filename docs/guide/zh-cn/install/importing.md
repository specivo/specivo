---
description: "本页介绍如何通过导入包把其他项目管理系统的数据迁入 Specivo：校验、预演、正式导入，之后追加增量导入，并查看或清理导入记录。"
---

# 从其他项目管理系统导入 {#importing-from-another-tracker}

Specivo 本身不会读取其他系统的数据。读取旧系统的是独立的 **迁移工具**，它把数据写成一个 **导入包**：一个文件夹（或 `.zip`），由纯文本文件组成，包含项目、工作项（Issue）、历史记录、维基（Wiki）页面和附件。Specivo 随后校验并加载这个导入包。两者分开有两个好处：迁移工具可以独立修复和发布，无需等待 Specivo 新版本；导入包生成后，Specivo 也不再依赖旧系统的任何东西。

本页介绍 Specivo 这一侧的操作，对所有迁移工具都相同。导出步骤请参阅各工具自己的指南。

## 迁移工具 {#migration-tools}

| 来源 | 工具 | 指南 |
|---|---|---|
| Jira Cloud 或 Jira Data Center | `specivo-jira-migration` | [Jira 迁移工具文档](https://specivo.io/docs/jira-migration-tool/) |
| Redmine | `specivo-redmine-migration` | [Redmine 迁移工具](https://github.com/specivo/redmine-migration-tool/blob/main/docs/migrating-from-redmine.md) |
| Request Tracker（RT） | `specivo-rt-migration` | [RT 迁移工具文档](https://specivo.io/docs/rt-migration-tool/) |

其他来源需要一个能写出导入包格式的工具。格式说明见 Specivo 仓库中的
[`docs/import-bundle/v1.md`](https://github.com/specivo/specivo/blob/main/docs/import-bundle/v1.md)，迁移 SDK 可以替你生成这种格式。

!!! note "需要 Specivo 0.11.0 或更高版本"
    导入包加载器在 0.11.0 中引入，取代了早期版本的直接导入方式；参阅
    [升级到 0.11.0](upgrading.md#upgrading-to-0110)。

## 导入包能携带什么 {#what-a-bundle-can-carry}

导入包可以包含用户和用户组、项目及其成员、版本、类别、冲刺（Sprint）、自定义字段、带评论和变更历史的工作项、关联和父工作项、关注者、附件、工时记录，以及带全部修订版本的维基页面。工具会填入来源系统拥有的内容。Specivo 加载时有几点细节需要了解：

- **自定义字段** 会变成[工作项元数据](../metadata/index.md)，名称沿用旧系统中的字段名。存放人员或版本的字段，显示的是该人员或版本的名称。
- **标签（Labels）** 可以作为[标签](../projects/tags.md)导入，在每个工作项所属的项目中创建。
- **冲刺** 会保留原有状态（计划中、进行中或已完成）。一个项目只能有一个进行中的冲刺，因此第二个进行中的冲刺会按计划中导入，并在报告中列出。已完成冲刺的速率根据其工作项记录。
- **工作项的剩余预计工时** 与预计工时一起存储。Specivo 按给定值保存，不会从工时记录推算。
- **历史记录** 保留原始作者和时间。状态、负责人、版本等的变更按名称显示，与其他变更一样。
- **文本** 为 Markdown。待所有记录都存在后，对工作项、人员和附件的引用会用工作项的新 Specivo 编号改写成链接。

工具无法携带的内容都会列在导入报告中，不会悄悄丢失。

## 第 1 步：把导入包放到 Specivo 能读到的位置 {#step-1-put-the-bundle-where-specivo-can-read-it}

使用 Docker 时，`api` 容器必须能看到这个文件夹。在 `docker-compose.override.yml` 中挂载：

```yaml
services:
  api:
    volumes:
      - ./imports:/import
```

然后重启服务（`docker compose up -d`），把导入包复制到 `./imports`。让该文件夹对用户 ID `10001`（应用运行所用的用户）可写：导入过程会在导入包旁边保存一个小的 **链接文件**（见[下文](#exporting-in-parts)）。
附件很大的导入包可以把文件放在单独的文件夹中，通过 `--files-path` 指定。

## 第 2 步：校验、预演、导入 {#step-2-check-rehearse-import}

按顺序执行以下命令。前两条不会改变任何数据。

```bash
# 1. Check the bundle itself: schema, references, file hashes. No database is touched.
docker compose exec api python -m specivo.cli.import_data \
    --source bundle --path /import/jira-export --validate

# 2. Rehearse: the whole import runs inside a transaction that is rolled back.
docker compose exec api python -m specivo.cli.import_data \
    --source bundle --path /import/jira-export --dry-run

# 3. Import for real.
docker compose exec api python -m specivo.cli.import_data \
    --source bundle --path /import/jira-export
```

继续之前先读预演的报告。报告列出 Specivo 选定的项目标识、类别只能靠猜测的状态、需要补填邮箱地址的账户，以及所有无法迁移的内容。**先备份**，再导入。参阅[备份与恢复](backup-restore.md)。

### 值得了解的选项 {#options-worth-knowing}

| 选项 | 作用 |
|---|---|
| `--project IDS` | 只导入这些来源项目（逗号分隔的来源 ID）。子项目随父项目一起导入。 |
| `--project-key-map ID=KEY,...` | 为来源项目指定 Specivo 项目标识，代替自动推导的标识。 |
| `--merge-duplicate-statuses` | 复用同名的现有状态、追踪器（Tracker）或优先级，而不是再创建一个。 |
| `--continue-import` | 在同一来源的早先导入基础上追加（见下文）。 |
| `--resume RUN_ID` | 继续一次中断的运行。每个阶段独立提交，已完成的部分会保留。 |
| `--links-file PATH` | 链接文件的存放位置。 |
| `--files-path DIR` | 从这个文件夹读取附件文件，而不是导入包自带的 `files/`。 |
| `--import-passwords` | 让导入的账户在首次登录前继续使用旧密码。需要匹配的[密码哈希插件](password-hasher-plugins.md)。管理员账户始终需要设置新密码。 |
| `--import-admins` | 把来源系统的管理员在这里也设为管理员。否则他们以普通账户导入。 |
| `--flatten-excess-depth` | 把嵌套层级超出 Specivo 限制的项目重新挂到允许的父级下。 |
| `--report-json PATH` | 同时把报告写入文件。 |
| `--strict` | 把每个警告都视为失败。用于测试迁移工具，不用于正式导入。 |

`python -m specivo.cli.import_data --source bundle --help` 列出全部选项，包括针对不可信导入包的大小限制。

!!! tip "没有邮箱地址的账户"
    有些来源系统不会公开所有人的邮箱地址。Specivo 会给这类账户一个永不收信的占位地址，并在报告中列出，方便你补填真实地址。

## 重复运行与后续追加 {#running-it-again-and-adding-to-it-later}

导入包中的每条记录都带有来自来源系统的稳定标识符。Specivo 会记住哪条来源记录对应哪一行 Specivo 数据，归在一个 **作用域（scope）** 之下：即导入包清单中指明的来源系统和实例。

- 再次加载同一个导入包不会创建任何新数据。
- **同一套旧系统的后续导出**，加上 `--continue-import` 加载时，会补入此后新增的记录。可以利用这一点提前导入，然后在切换前补齐增量。已导入的记录会跳过，不会更新。
- 没有 `--continue-import` 时，如果某个作用域已有数据，Specivo 会拒绝加载该作用域的导入包，因此导入包无法通过冒用来源名称挂到别的导入上。

## 分批导出 {#exporting-in-parts}

大型系统可以按项目（一个或几个）分批导出。不同批次中的工作项仍然可以互相关联：评论中提到的工作项、两个工作项之间的关联、父工作项在另一批中的子工作项。

按任意顺序逐个加载各批次，从第二批起加上 `--continue-import`，并且每次使用同一个 `--links-file`。凡是指向尚未到达的工作项的内容，都会在链接文件中等待：

- 提及先以纯文本写入，另一批加载后变成链接；
- 关联的创建和父工作项的设置，在两个工作项都存在后完成。

没有待处理项时，该文件会被删除。导入后被人编辑过的文本不会再被改动。

## 查看和清理导入记录 {#inspect-and-tidy-up-an-import}

`admin` 命令可以显示和管理导入所记住的内容：

```bash
docker compose exec api python -m specivo.cli.admin import-map list
docker compose exec api python -m specivo.cli.admin import-map pending --links-file /import/specivo-links.SYSTEM.INSTANCE.sqlite
docker compose exec api python -m specivo.cli.admin import-map resolve-pending --links-file /import/... --yes
```

| 命令 | 作用 |
|---|---|
| `import-map list` | 列出每个作用域的行数及最近写入时间。 |
| `import-map pending` | 统计链接文件中仍在等待的文本、关联和父工作项。只读。 |
| `import-map resolve-pending` | 把现在可以链接的内容都链接起来。不加 `--yes` 时只报告。 |
| `import-map rename` | 把作用域改到新的系统或实例名下，让下次导入能找到它的数据。 |
| `import-map purge` | 在你完成切换后删除作用域的数据。已导入的记录保留，但 **之后再导入该来源会把所有内容重新创建一遍**。 |

`rename` 和 `purge` 默认只是预演，加上 `--yes` 才会实际执行。

!!! warning "只在旧系统已经停用时执行 purge"
    作用域的数据是 Specivo 识别哪些内容已导入的依据。执行 `purge` 之后，就无法再追加该来源的后续导出。同时也要删除该作用域的链接文件。

## 导入之后 {#after-the-import}

- 以管理员身份登录，抽查几个项目：工作项数量、一个历史记录很长的工作项、一个维基页面、一个附件。
- 为分配了占位地址的账户补填真实邮箱，并通知大家设置新密码（或使用[单点登录](single-sign-on/index.md)登录）。
- 检查每个项目中导入成员的角色。
- 导入结束时会对导入的记录运行搜索索引。参阅[语义搜索](semantic-search.md)。
