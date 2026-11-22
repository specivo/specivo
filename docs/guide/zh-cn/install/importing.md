---
description: 使用导入包把其他项目管理系统的数据导入 Specivo，按步骤完成校验、演练、正式导入和后续增量追加，并学会查看与清理导入记录。
---

# 从其他项目管理系统导入 {#importing-from-another-tracker}

Specivo 本身不直接读取其他项目管理系统。读取旧系统的是独立的 **迁移工具**，它会生成一个 **导入包**：一个由普通文件组成的文件夹（或 `.zip`），里面是项目、工作项（Issue）、历史记录、维基（Wiki）页面和附件。随后由 Specivo 校验并加载这个导入包。两者分开的好处是：迁移工具可以单独修复和发布，不必等 Specivo 出新版本；导入包一旦生成，Specivo 也不再需要访问旧系统。

本页介绍 Specivo 这一侧的操作，无论使用哪种迁移工具都一样。导出部分请参阅各工具自己的指南。

## 迁移工具 {#migration-tools}

| 迁移来源 | 工具 | 指南 |
|---|---|---|
| Jira Cloud 或 Jira Data Center | `specivo-jira-migration` | [Jira 迁移工具文档](https://specivo.io/docs/jira-migration-tool/) |
| Redmine | `specivo-redmine-migration` | [Redmine 迁移工具](https://github.com/specivo/redmine-migration-tool/blob/main/docs/migrating-from-redmine.md) |
| Request Tracker（RT） | `specivo-rt-migration` | [RT 迁移工具文档](https://specivo.io/docs/rt-migration-tool/) |

其他来源需要一个能生成导入包格式的工具。该格式的说明见 Specivo 仓库中的 [`docs/import-bundle/v1.md`](https://github.com/specivo/specivo/blob/main/docs/import-bundle/v1.md)，迁移 SDK 可以替你生成这种格式。

!!! note "需要 Specivo 0.11.0 或更高版本"
    导入包加载器自 0.11.0 起提供，取代了早期版本的直接导入功能，详见[升级到 0.11.0](upgrading.md#upgrading-to-0110)。

## 导入包可以包含哪些内容 {#what-a-bundle-can-carry}

导入包可以包含用户和用户组、项目及其成员、版本、类别、冲刺（Sprint）、自定义字段、工作项及其评论和变更历史、关联和父工作项、关注者、附件、已记录的工时，以及带全部修订版本的维基页面。源系统里有什么，迁移工具就填入什么。Specivo 加载这些内容时有几点细节：

- **自定义字段** 会变成[工作项元数据](../metadata/index.md)，以它在旧系统中的名称显示。值为人员或版本的字段，显示该人员或版本的名称。
- **标签（label）** 可以作为[标签](../projects/tags.md)导入，在各工作项所属的项目中创建。
- **冲刺** 保留原有状态（已计划、进行中或已完成）。一个项目只能有一个进行中的冲刺，因此第二个进行中的冲刺会按已计划导入，并写入报告。已完成冲刺的速率根据其中的工作项记录。
- **工作项的剩余预计工时** 显示并保存在预计工时旁边。Specivo 按给定的值原样保存，不会根据工时记录推算。
- **历史记录** 保留原作者和原时间。状态、负责人、版本等的变更按名称显示，与其他变更一样。
- **文本** 采用 Markdown 格式。所有记录都创建完成后，文本中对工作项、人员和附件的引用会改写为链接，并使用工作项在 Specivo 中的新编号。

- **自定义字段的值按源系统中的原样保留**，即使某个值不符合该字段在这里的架构（例如源系统增加校验之前就存在的值，或源系统后来才设为必填的字段）。报告会逐一列出这样的工作项和字段，提示为“Custom field value does not match its schema; kept”（自定义字段值与其架构不符，已保留）。这类工作项仍可编辑；之后再修改该字段时，新值必须符合架构。参见[哪些值会被拒绝](../metadata/values.md#what-is-refused)。

迁移工具未能带过来的内容都会列在导入报告中，不会有任何东西悄悄丢失。

## 第 1 步：把导入包放到 Specivo 能读取的位置 {#step-1-put-the-bundle-where-specivo-can-read-it}

使用 Docker 时，`api` 容器必须能访问这个文件夹。在 `docker-compose.override.yml` 中挂载它：

```yaml
services:
  api:
    volumes:
      - ./imports:/import
```

然后重启服务（`docker compose up -d`），把导入包复制到 `./imports`。让用户 ID `10001`（应用运行时使用的用户）对该文件夹有写权限：导入时会在导入包旁边保存一个很小的 **链接文件**（见[下文](#exporting-in-parts)）。
附件较大的导入包可以把文件放在单独的文件夹中，用 `--files-path` 指定。

## 第 2 步：校验、演练、导入 {#step-2-check-rehearse-import}

按顺序执行以下命令。前两条不会改动任何数据。

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

继续之前，先阅读演练生成的报告。报告会列出 Specivo 选定的项目标识、类别只能靠推测确定的状态、需要补充邮箱地址的账户，以及所有未能带过来的内容。**先备份**，再导入。参见[备份与恢复](backup-restore.md)。

### 值得了解的选项 {#options-worth-knowing}

| 选项 | 作用 |
|---|---|
| `--project IDS` | 只导入这些源项目（以逗号分隔的源 ID）。子项目随父项目一起导入。 |
| `--project-key-map ID=KEY,...` | 为源项目指定 Specivo 项目标识，不使用自动生成的标识。 |
| `--merge-duplicate-statuses` | 复用已有的同名状态、追踪器（Tracker）或优先级，不再创建第二个。 |
| `--continue-import` | 在同一来源的早先导入基础上追加（见下文）。 |
| `--resume RUN_ID` | 继续一次中断的运行。每个阶段单独提交；耗时较长的阶段（工作项、历史记录、附件、维基、工时）还会在每个项目之后以及每 `--commit-every` 条记录之后提交，所以已完成的部分会保留下来。 |
| `--commit-every N` | 耗时较长的阶段每写入多少条记录提交一次（默认 1000）。 |
| `--links-file PATH` | 链接文件的保存位置。 |
| `--files-path DIR` | 从该文件夹读取附件文件，不使用导入包自带的 `files/`。 |
| `--import-passwords` | 让导入的账户在首次登录前继续使用旧密码。需要匹配的[密码哈希插件](password-hasher-plugins.md)。管理员始终要设置新密码。 |
| `--import-admins` | 让源系统的管理员在这里也成为管理员。否则他们会作为普通账户导入。 |
| `--flatten-excess-depth` | 为嵌套层级超出 Specivo 上限的项目重新指定父项目。 |
| `--report-json PATH` | 同时把报告写入文件。 |
| `--strict` | 把每条警告都视为失败。用于测试迁移工具，不用于正式导入。 |

`python -m specivo.cli.import_data --source bundle --help` 会列出全部选项，包括针对不可信导入包的大小限制。

!!! tip "没有邮箱地址的账户"
    有些源系统不会提供所有人的邮箱地址。Specivo 会给这类账户分配一个永远收不到邮件的占位地址，并在报告中列出，方便你填入真实地址。

!!! note "登录名"
    Specivo 的登录名只能包含小写字母、数字、`_` 和 `-`。导入的登录名会按此规则调整：`John.Doe` 变为 `john-doe`；其他字母表中与拉丁字母外形相似的字母会被去掉，这样任何账户都无法冒充其他账户。每一处改动都会列在报告中。如果登录名在这里已被占用，会加上数字后缀。

## 重复运行与后续追加 {#running-it-again-and-adding-to-it-later}

导入包中的每条记录都带有来自源系统的稳定标识符。Specivo 会记住哪条源记录对应哪一行 Specivo 数据，并按 **范围**（scope）归类：范围由导入包清单中写明的源系统和实例确定。

- 再次加载同一个导入包，不会创建任何新内容。
- 用 `--continue-import` 加载 **同一套安装后来导出的导入包**，会追加上次之后新建的记录。可以借此提前导入，到切换前再补齐。已导入的记录会被跳过，不会更新。
- 不加 `--continue-import` 时，如果导入包的范围下已有数据行，Specivo 会拒绝导入。这样，导入包就无法通过冒用来源名称把自己挂到另一次导入上。

## 分批导出 {#exporting-in-parts}

大型安装可以每次导出一个（或几个）项目。不同批次中的工作项仍然可以互相引用：评论中提到某个工作项、两个工作项之间的关联、父工作项在另一批次中的子工作项。

各批次依次加载，顺序不限。从第二批起加上 `--continue-import`，并且每次使用同一个 `--links-file`。凡是指向尚未导入的工作项的内容，都会在链接文件中等待：

- 提及会先写成纯文本，等另一批次加载后再变成链接；
- 关联的创建和父工作项的设置，要等两个工作项都存在后才进行。

没有内容等待时，链接文件会被删除。导入后有人编辑过的文本不会被改动。

## 旧工单编号依然有效 {#old-ticket-numbers-keep-working}

导入之后，工作项有了新编号，但原来的编号仍然跟着它，所以邮件、维基页面和提交信息中的旧编号依然能找到对应的工作项：

- 工作项页面在日期旁边显示旧编号，例如 **导入自：RT #123**。API 的工作项详情（`imported_from`）和 MCP 工具 `specivo_show_issue` 中也会显示。
- 在[搜索](../issues/finding.md#search)中输入 `RT #123`、`#123` 或 `PROJ-7` 这样的编号，都能找到该工作项。
- `/imported/{system}/{number}/` 会跳转到该工作项，例如 `https://specivo.example.com/imported/rt/123/`。
  其中的 system 就是 `import-map list` 显示的系统名。如果有两个导入的工作项都是这个编号（同一种系统的两套安装可能都有工单 123），该链接会改为打开对这个编号的搜索。

能看到工作项的人都能看到它的旧编号。看不到该工作项的人用这个编号什么也打不开，就像它不存在一样。这些编号不依赖导入的范围：对范围执行 `purge` 之后，它们仍然保留。

要让旧链接本身继续可用，把旧主机名指向一台负责转发这些链接的 Web 服务器。这件事只能由你的 Web 服务器来做，因为每种系统的 URL 格式都不一样。nginx 的配置如下：

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

在此功能推出之前完成的导入，会在升级时根据当时记住的信息补上旧编号。这些信息是源系统自身的 ID，因此如果源系统另有一套独立的编号，补上的编号可能不对。用当前版本的导入工具重新生成同一个导入包，再加上 `--continue-import` 运行一次，即可纠正。工作项的其他内容不会变化。

## 查看和清理导入记录 {#inspect-and-tidy-up-an-import}

`admin` 命令可以查看和管理各次导入记住的信息：

```bash
docker compose exec api python -m specivo.cli.admin import-map list
docker compose exec api python -m specivo.cli.admin import-map pending --links-file /import/specivo-links.SYSTEM.INSTANCE.sqlite
docker compose exec api python -m specivo.cli.admin import-map resolve-pending --links-file /import/... --yes
```

| 命令 | 作用 |
|---|---|
| `import-map list` | 列出每个范围及其行数和最近一次写入时间。 |
| `import-map pending` | 统计链接文件中仍在等待的文本、关联和父工作项数量。只读。 |
| `import-map resolve-pending` | 把现在能建立的链接都建立起来。不加 `--yes` 时只输出报告。 |
| `import-map rename` | 把范围移到新的系统名或实例名下，让下一次导入能找到它的数据行。工作项的旧编号会跟随新的系统名，因此 `/imported/{system}/...` 也随之改变。 |
| `import-map purge` | 切换完成后删除某个范围的数据行。已导入的记录及其旧编号会保留，但 **之后再导入该来源，会把所有内容重新创建一遍**。 |

`rename` 和 `purge` 不加 `--yes` 时只是演练。

!!! warning "只有旧系统停用后才能执行 purge"
    Specivo 靠范围的数据行识别哪些内容已经导入。执行 `purge` 之后，就无法再追加该来源后来导出的内容。范围对应的链接文件也要一并删除。

## 导入之后 {#after-the-import}

- 以管理员身份登录，抽查几个项目：工作项数量、一个历史记录较长的工作项、一个维基页面、一个附件。
- 为分配了占位地址的账户填入真实邮箱地址，并通知大家设置新密码（或通过[单点登录](single-sign-on/index.md)（SSO）登录）。
- 检查每个项目中导入成员的角色。导入的角色不带任何权限：请在 **管理 → 角色** 中设置（参见[角色与权限](roles.md)）。
- 导入记录的搜索索引会在导入结束时运行。参见[语义搜索](semantic-search.md)。
