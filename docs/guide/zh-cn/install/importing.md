---
description: 本页介绍如何用迁移工具生成的导入包把其他项目管理系统的数据导入 Specivo：校验导入包、演练、正式导入、后续追加新数据，以及查看或清理一次导入。
---

# 从其他项目管理系统导入数据 {#importing-from-another-tracker}

Specivo 本身不会直接读取其他项目管理系统。由单独的 **迁移工具** 读取旧系统，并写出一个 **导入包**：一个由纯文件组成的文件夹（或 `.zip`），包含项目、工作项（Issue）、历史记录、维基（Wiki）页面和附件。Specivo 再检查并加载这个导入包。两者分开有两个好处：迁移工具可以独立修复和发布，不必等新版 Specivo；导入包一旦生成，Specivo 就不再需要访问旧系统。

本页介绍 Specivo 这一侧的操作，对所有迁移工具都一样。导出步骤见各工具自己的指南。

## 迁移工具 {#migration-tools}

| 迁移来源 | 工具 | 指南 |
|---|---|---|
| Jira Cloud 或 Jira Data Center | `specivo-jira-migration` | [Jira 迁移工具文档](https://specivo.io/docs/jira-migration-tool/) |
| Redmine | `specivo-redmine-migration` | [Redmine 迁移工具](https://github.com/specivo/redmine-migration-tool/blob/main/docs/migrating-from-redmine.md) |
| Request Tracker（RT） | `specivo-rt-migration` | [RT 迁移工具文档](https://specivo.io/docs/rt-migration-tool/) |

其他来源需要一个能写出导入包格式的工具。该格式在 Specivo 仓库的
[`docs/import-bundle/v1.md`](https://github.com/specivo/specivo/blob/main/docs/import-bundle/v1.md) 中有说明，迁移 SDK 可以替你生成。

!!! note "需要 Specivo 0.11.0 或更高版本"
    导入包加载器在 0.11.0 中引入，取代了早期版本的直接导入；参见
    [升级到 0.11.0](upgrading.md#upgrading-to-0110)。

## 导入包可以包含什么 {#what-a-bundle-can-carry}

导入包可以包含用户和用户组、项目及其成员、版本、类别、冲刺（Sprint）、自定义字段、带评论和变更历史的工作项、关联和父工作项、关注者、附件、工时记录，以及带全部修订版本的维基页面。迁移工具会填入其来源系统拥有的内容。下面是 Specivo 加载这些内容时的几个细节：

- **自定义字段** 成为 [工作项元数据](../metadata/index.md)，并沿用旧系统中的字段名显示。存放人员或版本的字段显示对应人员或版本的名称。
- **旧系统的标签（Label）** 可以作为 Specivo 的 [标签](../projects/tags.md) 导入，在每个工作项所属的项目中创建。
- **冲刺** 会带着原有状态（计划中、进行中或已完成）导入。一个项目只能有一个进行中的冲刺，因此第二个进行中的冲刺会按计划中导入，并在报告中列出。已完成冲刺的速率根据其工作项记录。
- **工作项的剩余预计工时** 会在预计工时旁边显示并保存。Specivo 按给定值保留，不会根据工时记录推算。
- **历史记录** 保留原始作者和时间。状态、负责人、版本等的变更和其他变更一样按名称显示。
- **文本** 为 Markdown。待所有记录都创建完毕后，对工作项、人员和附件的引用会用工作项新的 Specivo 编号改写为链接。

- **自定义字段值按来源原样保留**，即使某个值不符合这里字段的架构（例如来源系统后来才加上校验，或后来才把某个字段设为必填）。报告会把每个这样的工作项和字段列为“Custom field value does not match its schema; kept”（自定义字段值不符合其架构；已保留）。该工作项仍可编辑，但之后再修改该字段时必须符合架构。参见
  [哪些值会被拒绝](../metadata/values.md#what-is-refused)。

迁移工具无法迁移的内容都会列在导入报告中，不会有任何东西悄悄消失。

## 第 1 步：把导入包放到 Specivo 能读取的位置 {#step-1-put-the-bundle-where-specivo-can-read-it}

使用 Docker 时，`api` 容器必须能看到该文件夹。在 `docker-compose.override.yml` 中挂载：

```yaml
services:
  api:
    volumes:
      - ./imports:/import
```

然后重启服务（`docker compose up -d`），把导入包复制到 `./imports`。确保该文件夹对用户 ID `10001`（应用运行所用的用户）可写：导入过程会在导入包旁边保存一个小的 **链接文件**（见 [下文](#exporting-in-parts)）。
附件很大的导入包可以把文件放在单独的文件夹中，通过 `--files-path` 指定。

## 第 2 步：校验、演练、导入 {#step-2-check-rehearse-import}

按顺序运行以下命令。前两条不会改动任何内容。

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

继续之前先阅读演练的报告。报告会列出 Specivo 选定的项目标识、类别需要猜测的状态、需要邮箱地址的账户，以及所有无法迁移的内容。**先备份**，再导入。参见 [备份与恢复](backup-restore.md)。

### 值得了解的选项 {#options-worth-knowing}

| 选项 | 作用 |
|---|---|
| `--project IDS` | 只导入这些来源项目（逗号分隔的来源 ID）。子项目随父项目一起导入。 |
| `--project-key-map ID=KEY,...` | 为来源项目指定 Specivo 项目标识，不使用自动推导的标识。 |
| `--merge-duplicate-statuses` | 复用同名的现有状态、追踪器或优先级，而不是再建一个。 |
| `--continue-import` | 在同一来源的早先导入基础上追加（见下文）。 |
| `--resume RUN_ID` | 继续一次中断的运行。每个阶段单独提交，已完成的部分会保留。 |
| `--links-file PATH` | 链接文件的保存位置。 |
| `--files-path DIR` | 从此文件夹读取附件文件，而不是导入包自带的 `files/`。 |
| `--import-passwords` | 让导入的账户在首次登录前继续使用旧密码。需要匹配的 [密码哈希插件](password-hasher-plugins.md)。管理员始终需要设置新密码。 |
| `--import-admins` | 让来源系统的管理员在这里也成为管理员。否则他们按普通账户导入。 |
| `--flatten-excess-depth` | 把嵌套深度超过 Specivo 限制的项目重新挂到允许的层级。 |
| `--report-json PATH` | 同时把报告写入文件。 |
| `--strict` | 把每条警告都视为失败。用于测试迁移工具，不用于正式导入。 |

`python -m specivo.cli.import_data --source bundle --help` 会列出全部选项，包括不受信任导入包的大小限制。

!!! tip "没有邮箱地址的账户"
    有些来源系统不会提供每个人的邮箱地址。Specivo 会给这类账户一个永远收不到邮件的占位地址，并在报告中列出，方便你填入真实地址。

## 再次运行，以及后续追加 {#running-it-again-and-adding-to-it-later}

导入包中的每条记录都带有来源系统的稳定标识符。Specivo 会记住哪条来源记录变成了哪一行 Specivo 数据，并按 **作用域（scope）** 归档：作用域即来源系统加上导入包清单（manifest）中声明的实例。

- 再次加载同一个导入包不会创建任何新内容。
- 用 `--continue-import` 加载 **同一套安装的后续导出**，会追加其间新建的记录。你可以借此先早早导入，在切换前再补齐差额。已导入的记录会被跳过，不会更新。
- 不带 `--continue-import` 时，如果某个作用域已有数据行，Specivo 会拒绝该导入包，所以一个导入包无法通过声明来源把自己挂到另一次导入上。

## 分批导出 {#exporting-in-parts}

大型安装可以一次导出一个（或几个）项目。不同批次中的工作项仍然可以相互关联：评论中提到另一个工作项、两个工作项之间的关联、父工作项在另一批中的子工作项。

按任意顺序逐批加载，从第二批起加 `--continue-import`，并且每次使用同一个 `--links-file`。凡是指向尚未到达的工作项的内容，都会在链接文件中等待：

- 提及先写成纯文本，待另一批加载后变成链接；
- 两个工作项都存在后，才创建关联、设置父工作项。

没有待处理内容时，该文件会被删除。导入后有人编辑过的文本不会被改动。

## 旧工单号继续有效 {#old-ticket-numbers-keep-working}

导入后工作项会有新的编号，但它原来的编号会一并保留，因此邮件、维基页面和提交信息中的旧编号仍然能指向正确位置：

- 工作项页面在日期旁边显示它，例如 **导入自：RT #123**。API 中的工作项详情（`imported_from`）和 MCP 工具 `specivo_show_issue` 也会显示。
- [搜索](../issues/finding.md#search) 可以通过 `RT #123`、`#123` 或 `PROJ-7` 这样的编号找到该工作项。
- `/imported/{system}/{number}/` 会跳转到该工作项，例如 `https://specivo.example.com/imported/rt/123/`。
  其中 system 就是 `import-map list` 显示的系统名。如果两个导入的工作项有同一个编号（同一种系统的两套安装都可能有 123 号工单），该链接会改为打开针对它的搜索。

能看到该工作项的人都能看到它的旧编号。对于无权查看的人，编号不会指向任何位置，就像它不存在一样。这些编号不依赖导入的作用域：`purge` 之后它们依然保留。

要让旧链接本身继续有效，把旧主机名指向一台负责转发的 Web 服务器。这只能由你的 Web 服务器完成，因为每种系统的 URL 形式各不相同。以 nginx 为例：

```nginx
# RT: /Ticket/Display.html?id=123
location = /Ticket/Display.html {
    if ($arg_id !~ "^[0-9]+$") {
        return 404;
    }
    return 301 https://specivo.example.com/imported/rt/$arg_id/;
}

# Redmine: /issues/45
location ~ ^/issues/(\d+)$ {
    return 301 https://specivo.example.com/imported/redmine/$1/;
}

# Jira: /browse/PROJ-7
location ~ ^/browse/([A-Za-z0-9_-]+)$ {
    return 301 https://specivo.example.com/imported/jira/$1/;
}
```

在此功能推出之前完成的导入，会在升级时根据导入记住的信息补上编号。这些是来源系统自己的 ID，对于另有独立键的系统可能不正确。用当前版本的导入工具重新生成同一个导入包，再加 `--continue-import` 运行一次即可纠正。工作项的其他内容不会改变。

## 查看和清理导入 {#inspect-and-tidy-up-an-import}

`admin` 命令可以显示和管理各次导入记住的信息：

```bash
docker compose exec api python -m specivo.cli.admin import-map list
docker compose exec api python -m specivo.cli.admin import-map pending --links-file /import/specivo-links.SYSTEM.INSTANCE.sqlite
docker compose exec api python -m specivo.cli.admin import-map resolve-pending --links-file /import/... --yes
```

| 命令 | 作用 |
|---|---|
| `import-map list` | 列出每个作用域及其数据行数和最后写入时间。 |
| `import-map pending` | 统计链接文件中仍在等待的文本、关联和父工作项。只读。 |
| `import-map resolve-pending` | 把现在能关联的内容关联起来。不带 `--yes` 时只报告。 |
| `import-map rename` | 把作用域改为新的系统或实例，让下次导入能找到其数据行。工作项的旧编号会跟随新的系统名，因此 `/imported/{system}/...` 也随之改变。 |
| `import-map purge` | 切换完成后删除作用域的数据行。已导入的记录及其旧编号会保留，但 **之后再导入该来源会把所有内容重新创建一遍**。 |

`rename` 和 `purge` 默认只是演练，加 `--yes` 才会真正执行。

!!! warning "只有在旧系统停用后才执行 purge"
    作用域的数据行是 Specivo 识别哪些内容已经导入的依据。`purge` 之后，就无法再追加该来源的后续导出。同时也要删除该作用域的链接文件。

## 导入之后 {#after-the-import}

- 以管理员身份登录，抽查几个项目：工作项数量、一个历史记录很长的工作项、一个维基页面、一个附件。
- 为获得占位地址的账户填入真实邮箱地址，并通知大家设置新密码（或通过 [单点登录](single-sign-on/index.md) 登录）。
- 检查每个项目中导入成员的角色。
- 导入结束时会为导入的记录建立搜索索引。参见 [语义搜索](semantic-search.md)。
