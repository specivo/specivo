---
description: 介绍如何通过导入包把 Jira、Redmine 等其他系统的数据导入 Specivo，包括校验、演练、正式导入、分批导入、后续补充，以及查看和清理导入记录。
---

# 从其他系统导入数据 {#importing-from-another-tracker}

Specivo 本身不直接读取其他系统的数据。由独立的 **迁移工具** 读取旧系统，并生成一个 **导入包**：一个由普通文件组成的文件夹（或 `.zip` 压缩包），其中包含项目、工作项（Issue）、历史记录、维基（Wiki）页面和附件。随后 Specivo 校验这个导入包并加载它。两者分开的好处是：迁移工具可以独立修复和发布，不必等新版 Specivo；而导入包一旦生成，Specivo 就不再需要访问旧系统。

本页只介绍 Specivo 这一侧的操作，对所有迁移工具都一样。各工具的导出步骤见其各自的文档。

## 迁移工具 {#migration-tools}

| 原系统 | 工具 | 文档 |
|---|---|---|
| Jira Cloud 或 Jira Data Center | `specivo-jira-migration` | [Jira 迁移工具文档](https://specivo.io/docs/jira-migration-tool/) |
| Redmine | `specivo-redmine-migration` | [Redmine 迁移工具](https://github.com/specivo/redmine-migration-tool/blob/main/docs/migrating-from-redmine.md) |

其他来源的系统需要一个能生成该导入包格式的工具。格式说明见 Specivo 代码仓库中的
[`docs/import-bundle/v1.md`](https://github.com/specivo/specivo/blob/main/docs/import-bundle/v1.md)，迁移 SDK 会帮你生成这种格式。

!!! note "需要 Specivo 0.11.0 或更高版本"
    导入包加载功能在 0.11.0 版本引入，取代了早期版本的直接导入方式，参见
    [升级到 0.11.0](upgrading.md#upgrading-to-0110)。

## 导入包可以包含的内容 {#what-a-bundle-can-carry}

导入包可以包含用户和用户组、项目及其成员、版本、类别、冲刺（Sprint）、自定义字段、带评论和变更历史的工作项、关联和父工作项、关注者、附件、工时记录，以及维基页面的所有修订版本。迁移工具会把来源系统中有的内容都填进去。Specivo 加载这些内容时有几点细节：

- **自定义字段** 会变成 [工作项元数据](../metadata/index.md)，按它们在旧系统中的名称显示。存放人员或版本的字段会显示该人员或版本的名称。
- **Labels** 可以作为 Specivo 的 [标签](../projects/tags.md) 导入，在每个工作项所属的项目中创建。
- **冲刺** 保留原有状态（计划中、进行中或已完成）。一个项目只能有一个进行中的冲刺，因此第二个进行中的冲刺会按计划中导入，并在报告中列出。已完成冲刺的速率根据其工作项记录。
- **工作项的剩余预估** 与预计工时一同保存。Specivo 按原值保留，不会根据工时记录推算。
- **历史记录** 保留原作者和原时间。状态、负责人、版本等的变更和其他变更一样，按名称显示。
- **文本** 为 Markdown 格式。所有记录创建完成后，对工作项、人员和附件的引用会用工作项的新 Specivo 编号重写为链接。

迁移工具无法迁移的内容都会列在导入报告中，不会悄无声息地丢失。

## 第一步：把导入包放到 Specivo 能读取的位置 {#step-1-put-the-bundle-where-specivo-can-read-it}

使用 Docker 时，`api` 容器必须能访问该文件夹。在 `docker-compose.override.yml` 中挂载：

```yaml
services:
  api:
    volumes:
      - ./imports:/import
```

然后重启服务（`docker compose up -d`），并把导入包复制到 `./imports`。让该文件夹对用户 ID `10001`（应用运行所用的用户）可写：导入过程会在导入包旁边保存一个小的 **链接文件**（见 [下文](#exporting-in-parts)）。附件较大的导入包可以把文件放在单独的文件夹中，通过 `--files-path` 指定。

## 第二步：校验、演练、导入 {#step-2-check-rehearse-import}

按顺序运行以下命令。前两条不会改动任何数据。

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

继续之前先阅读演练的报告。报告会列出 Specivo 选用的项目标识、需要猜测分类的状态、缺少邮箱地址的账户，以及所有无法迁移的内容。**先备份**，再导入。参见 [备份与恢复](backup-restore.md)。

### 值得了解的选项 {#options-worth-knowing}

| 选项 | 作用 |
|---|---|
| `--project IDS` | 只导入这些来源项目（逗号分隔的来源 ID）。子项目随父项目一起导入。 |
| `--project-key-map ID=KEY,...` | 为来源项目指定 Specivo 项目标识，而不使用自动推导的标识。 |
| `--merge-duplicate-statuses` | 复用已有的同名状态、追踪器或优先级，而不是再创建一个。 |
| `--continue-import` | 在同一来源的早先导入基础上追加（见下文）。 |
| `--resume RUN_ID` | 继续一次中断的运行。每个阶段单独提交，已完成的部分会保留。 |
| `--links-file PATH` | 链接文件的保存位置。 |
| `--files-path DIR` | 从该文件夹读取附件文件，而不是导入包自带的 `files/`。 |
| `--import-passwords` | 让导入的账户在首次登录前继续使用旧密码。需要配套的 [密码哈希插件](password-hasher-plugins.md)。管理员一律要设置新密码。 |
| `--import-admins` | 让来源系统的管理员在这里也成为管理员。否则他们会作为普通账户导入。 |
| `--flatten-excess-depth` | 把嵌套层级超出 Specivo 限制的项目重新挂到允许的层级下。 |
| `--report-json PATH` | 同时把报告写入文件。 |
| `--strict` | 把所有警告视为失败。用于测试迁移工具，不要用于正式导入。 |

`python -m specivo.cli.import_data --source bundle --help` 会列出全部选项，包括针对不可信导入包的大小限制。

!!! tip "没有邮箱地址的账户"
    有些来源系统不会提供所有人的邮箱地址。Specivo 会给这类账户分配一个永不收信的占位地址，
    并在报告中列出，方便你补填真实地址。

## 重复运行与后续补充 {#running-it-again-and-adding-to-it-later}

导入包中的每条记录都带有来自来源系统的稳定标识。Specivo 会记住哪条来源记录对应哪一行 Specivo 数据，并归在一个 **作用域（scope）** 下：即导入包清单（manifest）中注明的来源系统和实例。

- 再次加载同一个导入包不会创建任何新内容。
- 用 `--continue-import` 加载 **同一安装的较新导出**，会补充自上次之后新建的记录。利用这一点可以提前导入，在切换前再补齐。已导入的记录会跳过，不会更新。
- 没有 `--continue-import` 时，如果导入包的作用域已有数据，Specivo 会拒绝加载，因此一个导入包无法仅凭声明来源就挂到另一次导入上。

## 分批导出 {#exporting-in-parts}

大型安装可以一次导出一个（或几个）项目。不同部分的工作项之间仍然可以互相关联：评论中提到另一个工作项、两个工作项之间的关联、父工作项在另一部分的子工作项。

按任意顺序逐个加载各部分，从第二个开始加上 `--continue-import`，并且每次使用同一个 `--links-file`。凡是指向尚未导入的工作项的内容，都会先在链接文件中等待：

- 提及先以纯文本写入，另一部分加载后再变成链接；
- 关联的创建和父工作项的设置，会在两个工作项都存在后完成。

没有待处理内容时，链接文件会被删除。导入后被人编辑过的文本不会被改动。

## 查看和清理导入记录 {#inspect-and-tidy-up-an-import}

`admin` 命令可以查看和管理导入记录：

```bash
docker compose exec api python -m specivo.cli.admin import-map list
docker compose exec api python -m specivo.cli.admin import-map pending --links-file /import/specivo-links.SYSTEM.INSTANCE.sqlite
docker compose exec api python -m specivo.cli.admin import-map resolve-pending --links-file /import/... --yes
```

| 命令 | 作用 |
|---|---|
| `import-map list` | 列出每个作用域及其行数和最后写入时间。 |
| `import-map pending` | 统计链接文件中仍在等待的文本、关联和父工作项数量。只读。 |
| `import-map resolve-pending` | 把当前能链接的全部链接起来。不加 `--yes` 时只输出报告。 |
| `import-map rename` | 把一个作用域改为新的系统或实例名，让下一次导入能找到它的数据行。 |
| `import-map purge` | 在切换完成后删除一个作用域的数据行。已导入的记录保留，但 **之后再导入该来源会把所有内容重新创建一遍**。 |

不加 `--yes` 时，`rename` 和 `purge` 只是演练。

!!! warning "仅在旧系统彻底停用后再清除"
    作用域的数据行是 Specivo 识别哪些内容已导入的依据。执行 `purge` 后，就无法再补充该来源的
    后续导出。同时也要删除该作用域的链接文件。

## 导入之后 {#after-the-import}

- 以管理员身份登录，抽查几个项目：工作项数量、一个历史记录较长的工作项、一个维基页面、一个附件。
- 为分配了占位邮箱的账户补填真实邮箱地址，并通知大家设置新密码（或通过 [单点登录](single-sign-on/index.md) 登录）。
- 检查每个项目中导入成员的角色。
- 导入结束时会对导入的记录运行搜索索引。参见 [语义搜索](semantic-search.md)。
