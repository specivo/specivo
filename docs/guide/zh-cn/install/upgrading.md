---
description: 升级 Specivo 只需修改 SPECIVO_VERSION、拉取镜像并重启，数据库迁移会自动执行；升级前务必先备份，出问题时把版本固定回上一个标签即可回滚。
---

# 升级 {#upgrading}

升级 Specivo 就是换用更新的 `specivo/specivo` 镜像。容器入口脚本会在启动时应用所有新的数据库迁移，所以升级通常只需要两条命令。

!!! warning "每次升级前先备份"
    迁移可能修改数据库结构，而且不一定可逆。拉取新镜像 **之前**，先导出数据库并备份 `SPECIVO_DATA_DIR`。参见[备份与恢复](backup-restore.md)。一旦出问题，这份备份就是你的退路。

## 1. 选择目标版本 {#1-choose-the-target-version}

在 `.env` 中固定要升级到的版本，让每次升级都是有意为之：

```bash
# .env
SPECIVO_VERSION=0.1.11
```

`SPECIVO_VERSION=latest` 总是拉取最新发布的镜像。试用时这样没问题，但如果是你依赖的正式部署，请固定一个明确的标签，这样才能确切知道当前运行的是哪个版本。

## 2. 拉取并重启 {#2-pull-and-restart}

```bash
docker compose pull
docker compose stop api celery-worker celery-embeddings celery-beat
docker compose up -d
```

`pull` 拉取新镜像；`stop` 停止应用容器，确保一次性的 `init-data` 服务准备数据目录时没有应用容器在运行；`up -d` 重新创建容器。启动时，入口脚本会等待数据库就绪，并自动应用所有新的迁移，不需要你手动执行迁移步骤。

查看 API 日志，确认启动正常：

```bash
docker compose logs -f api
```

`api` 服务报告健康（healthy）后，升级就完成了。

## 回滚 {#rolling-back}

如果新版本运行异常，把版本固定回上一个标签并重启即可回滚：

```bash
# .env
SPECIVO_VERSION=0.1.10      # the version you were on before

docker compose pull
docker compose up -d
```

!!! danger "数据库结构迁移后可能需要恢复备份"
    如果失败的升级已经应用了迁移，数据库结构可能比旧镜像更新，只回滚镜像是不够的。这种情况下，先恢复升级前导出的数据库和备份的 `SPECIVO_DATA_DIR`，再启动旧镜像。这正是升级前必须备份的原因。

## 建议 {#tips}

- 在生产环境中每次只升级一个次版本，不要一次跨越多个版本。
- 拉取镜像之前，先阅读目标版本的发布说明。
- 升级后做一次简单的冒烟测试：登录、打开一个工作项（Issue）、执行一次搜索。

## 升级到 0.13.0 {#upgrading-to-0130}

### pg_trgm 和 btree_gist 扩展 {#the-pg_trgm-and-btree_gist-extensions}

迁移会创建两个 PostgreSQL 扩展：`pg_trgm` 用于[相似工作项建议](../issues/creating.md#similar-issues)，`btree_gist` 用于 [AI 模型价格](ai-model-prices.md)。使用内置数据库时无需任何操作。使用外部数据库时，执行迁移的角色需要拥有该数据库的 `CREATE` 权限（两者都是受信任扩展，不需要超级用户）；在 Azure flexible server（灵活服务器）上，需要先把 `pg_trgm` 和 `btree_gist` 加入 `azure.extensions`。参见[配置](configuration.md)。

### AI 模型价格取代智能体成本表 {#ai-model-prices-replace-the-agent-cost-tables}

未使用的 `agent_token_logs` 和 `model_cost_configs` 表已删除，对应的未挂载 API 路由也一并移除。新的 [AI 模型价格](ai-model-prices.md)表会用内置价格表填充；每个模型的第一条内置价格自 2000 年 1 月 1 日起生效，因此更早的用量也能计价。

### 补全链接图 {#fill-in-the-link-graph}

工作项上的“提及于”和维基（Wiki）页面上的“链接来源”都来自已存储的链接图。新建和编辑的文本在保存时就会建立链接。升级之后，或者恢复了较旧的数据库备份之后，凡是从未完整索引过的项目都会自动重建：有一个每小时运行的后台任务负责把重建加入队列，所以只要 worker 和调度器容器在运行，你什么都不用做。项目设置中的 **链接图** 卡片会显示 **上次重建：**，重建尚未执行时则显示 **尚未索引，已排队等待重建**。

如果没有运行 worker 和调度器，执行一次下面的命令，为已有内容建立链接：

```bash
docker compose exec api python -m specivo.cli.rebuild_links
```

项目管理者也可以在[项目设置 → 搜索](../wiki/history-linking.md#the-project-link-graph)中用 **重建链接图** 对单个项目执行同样的操作。

### 元数据值会被校验 {#metadata-values-are-validated}

现在，不符合元数据架构的值会被拒绝，无论是来自工作项页面、REST API 还是 AI 智能体（AI agent）。只校验本次保存所修改的内容：如果工作项中已有的值不符合架构，这个工作项仍然可以编辑，但对该字段的写入会被拒绝，直到值符合架构为止（对于 `commits` 这样的列表，追加元素也算写入）。详见[哪些值会被拒绝](../metadata/values.md#what-is-refused)。以前写入过短或格式错误的值的智能体，现在会收到一条指明字段名的错误。

升级前不需要修复任何东西。升级后，列出值与架构不符的工作项并修复，这样用户和智能体才能继续写入这些字段：

```bash
docker compose exec api python -m specivo.cli.admin metadata-check
docker compose exec api python -m specivo.cli.admin metadata-check --project ACME
```

命令会按项目和架构输出有多少个工作项未通过校验、涉及哪些字段，以及这些工作项的编号。它从不输出具体的值，也不做任何修改。收紧架构之后，再运行一次。

### 工作流字段规则开始生效 {#workflow-field-rules-apply}

通过管理 API 创建的字段规则（按状态设置的必填或只读字段）现在会强制执行：每次保存工作项时都会检查，但只判断本次保存所修改的内容。没有设置规则的实例不受影响。引用未知字段的规则会被拒绝。参见[字段规则](../issues/statuses-workflow.md#field-rules)。

## 升级到 0.11.0 {#upgrading-to-0110}

0.11.0 版本把 Redmine 导入功能移出了 Specivo。Specivo 现在只负责加载导入包，导入包由迁移工具生成。如果实例从未使用过直接导入，则没有任何变化。

### 直接导入 Redmine 的功能已移除 {#the-direct-redmine-import-is-removed}

`import_data --source redmine`、`make import-redmine` 和 `importers` extra 已经移除，导入源插件钩子也一并移除。要从其他项目管理系统迁移，参见[从其他项目管理系统导入](importing.md)；迁移 Redmine 请使用 [Redmine 迁移工具](https://github.com/specivo/redmine-migration-tool/blob/main/docs/migrating-from-redmine.md)，它生成的导入包可以用 `python -m specivo.cli.import_data --source bundle` 加载。请更新使用旧选项的脚本和定时任务。

### 加载导入包之前，先为早期的直接导入更换键 {#re-key-an-earlier-direct-import-before-loading-a-bundle}

如果你用过旧的直接导入，那些数据行的键是脱敏后的数据库 URL，而导入包的键是其清单（manifest）中指定的实例。先把这些数据行迁到导入包的键下，这样导入包会在早期导入的基础上追加数据，而不是产生重复：

```bash
# Move the scope (leave out --yes first for a dry run that checks and counts)
python -m specivo.cli.admin import-map rename --system redmine \
    --from 'postgresql://redmine:***@db.internal/redmine' --to tracker.example.com --yes
# Then load the tool's bundle as a continuation of that import
python -m specivo.cli.import_data --source bundle --path /data/exports/redmine --continue-import
```

运行 `import-map list` 可以查看当前的键。`source.system` 和 `source.instance` 的含义见[导入包格式](https://github.com/specivo/specivo/blob/main/docs/import-bundle/v1.md)。

## 升级到 0.10.0 {#upgrading-to-0100}

0.10.0 版本新增了单点登录（SSO）和三个迁移（0041 至 0043），入口脚本会在启动时应用这些迁移。不做任何配置也能照常使用：添加提供商之前，单点登录处于关闭状态，已登录的用户也会保持登录。请检查以下各项。

### 内置的服务栈新增 `proxy` 网络 {#the-bundled-stack-gets-a-proxy-network}

请换用新的 `docker-compose.yml`。它把 `nginx` 和 `api` 放进新的 `proxy` 网络，并让 nginx 使用固定地址 `10.213.47.2`。这样 Specivo 从 `X-Forwarded-For` 读取客户端地址时，可以只信任这一个地址（参见[代理之后的客户端地址](configuration.md#client-addresses-behind-a-proxy)）。

- 如果 `docker compose up -d` 报 “Pool overlaps” 并中止，说明这个网段在你的主机上已被占用。在 `.env` 中把 `SPECIVO_PROXY_SUBNET` 和 `SPECIVO_NGINX_IP` 设为空闲的网段和地址，然后重新运行。
- 如果你维护自己的 Compose 文件，请按同样的方式添加这个网络，或者把 `TRUSTED_PROXIES` 设为你的代理地址。`TRUSTED_PROXIES` 为空时，连接的来源地址就被当作客户端地址：这样在代理之后，所有人会共用同一个限流桶，审计日志中记录的也是同一个地址。
- 如果内置的 nginx 前面还有另一层代理（Caddy、Traefik 或 CDN），设置 `TRUSTED_PROXY_HOPS=2`。只有 `TRUSTED_PROXIES` 中列出的代理才能指定客户端地址，`TRUSTED_PROXY_HOPS` 则说明连续有几层这样的代理。
- 如果你用自己的文件替换了 `nginx/default.conf`，请保留 `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`。你也可以沿用内置文件中新的 `specivo_no_query` 访问日志格式：访问日志不再包含查询字符串，因为其中可能带有单点登录过程中的 code 和 state，以及链接中的一次性令牌。如果不想保留这些信息，请删除或轮转旧的访问日志。

### 退出登录的行为有变化 {#signing-out-changed}

- **退出登录** 按钮现在是一个向 `/logout/` 发送 POST 请求的表单。通过书签或链接访问 `/logout/` 会打开“退出登录？”页面，并不会让任何人退出登录。
- `POST /api/v1/auth/logout/` 现在返回 `200` 和 `{"end_session_url": ...}`，不再返回 `204`。只有当会话来自支持在提供商一侧退出登录的单点登录提供商时，这个值才不是 `null`。只认 `204` 的脚本必须改为接受 `200`。
- 现在退出登录会立即在服务器端结束会话，并吊销其访问令牌。此前两者在过期之前一直有效。
- 重置密码会结束该账户的所有会话，无论是通过邮件、由管理员操作，还是通过命令行重置。

### 管理员 API 密钥的权限缩小 {#administrator-api-keys-do-less}

用管理员的 API 密钥执行以下操作时，会被以 `403` 和错误码 `api_key_not_allowed` 拒绝：重置密码、解锁或重新启用用户、创建普通用户或管理员（仍然可以创建服务账户）、为其他用户创建密钥，以及修改单点登录设置。以前用密钥做这些事的自动化脚本，必须改为以已登录的管理员身份运行。参见[管理员密钥不能做什么](../ai-agents/api-keys.md#what-an-administrators-key-cannot-do)。

### 停用用户 {#deactivated-users}

管理员可以停用和重新启用用户（**管理 → 用户**，在用户页面上操作）。停用会结束该用户的所有会话，并关闭其 API 密钥；重新启用不会把密钥重新打开。

### 如果你要启用单点登录 {#if-you-turn-on-single-sign-on}

- 先设置 `APP_URL`：重定向地址和退出登录地址都由它生成。
- 保存在 Specivo 中的客户端密钥使用由 `SECRET_KEY` 派生的密钥加密。如果轮换了 `SECRET_KEY`，需要打开每个提供商，重新输入它的客户端密钥。请把 `.env.local` 和数据库导出文件一起备份；参见[备份与恢复](backup-restore.md#what-depends-on-secret_key)。
- 在身份提供商（IdP）处注册重定向 URI；如果使用后端通道注销（back-channel logout），也要注册对应的 URI。参见[单点登录](single-sign-on/index.md)。
- 给自己留一条后路：[重新进入系统](configuration.md#single-sign-on-getting-back-in)。

这些迁移还会删除用户表中未使用的 `github_id` 和 `google_id` 列。

## 升级到 0.9.0 {#upgrading-to-090}

0.9.0 版本新增了八个迁移（0033 至 0040），入口脚本会像往常一样在启动时应用。大部分变更不需要你做任何事，但有几项值得在升级后处理或检查。

### 容器不再以 root 运行 {#the-containers-no-longer-run-as-root}

`api` 和 Celery 容器，以及你用 `docker compose exec api ...` 执行的命令，现在都以非特权用户 `specivo`（UID/GID `10001`）运行。请换用新的 `docker-compose.yml`：其中的 `init-data` 服务会在 `api` 启动前以 root 身份运行一次。它把数据目录下应用专用的子目录 `attachments/`、`avatars/`、`models/`、`state/` 和 `logs/app/` 交给应用用户，旧版本以 root 身份写入其中的文件也一并移交；如果 `.bootstrap.json` 还在原处，会把它移到 `state/` 中。首次启动时，如果附件目录很大，这一步可能需要一点时间；之后的启动会跳过已经属于应用用户的目录。

- 执行 `docker compose up -d` 之前先停止应用容器，做法见[拉取并重启](#2-pull-and-restart)：旧的 `celery-worker`、`celery-embeddings` 或 `celery-beat` 容器还在运行时，不能运行 `init-data`。
- 它不会进入已经属于应用用户的目录。如果它报告某个这样的目录中存在属于 root 的文件，请停止所有应用容器，然后运行 `docker compose run --rm init-data fix-permissions --all`。

- 数据目录本身仍属于 root（或属于你），`logs/`、`postgresql/`、`redis/` 和 `logs/nginx/` 也是如此：应用无法修改数据目录顶层的条目。
- 应用日志文件从 `logs/specivo.log` 移到了 `logs/app/specivo.log`。旧文件保留在原处，不再需要时自行删除。
- 如果某个应用目录是符号链接、位于另一个文件系统，或者属于其他用户，`init-data` 不会动它，只会记录一条警告。请把目标目录直接挂载到该路径，不要用符号链接；或者自己对它执行 `sudo chown -R 10001:10001`。
- 如果你维护自己的 Compose 文件，请添加 `init-data` 服务并让 `api` 依赖它，或者在启动新镜像之前自己准备好这些目录：
  `cd <SPECIVO_DATA_DIR> && sudo mkdir -p attachments avatars models state logs/app` 和
  `sudo chown -R 10001:10001 attachments avatars models state logs/app`。
  否则 api 会在启动时记录一条警告，上传也会失败。
- 挂载到应用容器中的其他主机目录（自定义主题或错误页面目录、存放导入文件的目录）必须能被 UID `10001` 读取。
- 如果完全不想有 root 步骤，在 `docker-compose.override.yml` 中为 `init-data`、`api` 和各个 Celery 服务设置 `user: "10001:10001"`；这样 `init-data` 不会做任何修改。镜像也支持只读根文件系统（`read_only: true`，并为 `/tmp` 配置 `tmpfs`）。
- 镜像现在自带 `HEALTHCHECK`，所以 `docker ps` 会显示每个 Specivo 容器的健康状态。

### 重建搜索索引 {#rebuild-the-search-index}

现在在所有搜索模式下都可以搜索 `ACME-42` 这样的工作项编号。旧文本需要重建索引之后，其中 *提到* 的编号才能被搜到，因此迁移会把索引标记为需要重建。在你重建之前，仍然可以用工作项自己的编号找到它。重建一次即可：

- 在 Web 界面的 **管理 → 设置** 中点击 **立即重建索引**；或者
- 通过命令行：

  ```bash
  docker compose exec api python -m specivo.cli.reindex_fts
  ```

重建索引分小批执行，期间 Specivo 可以照常使用。升级后编辑的文本会在保存时建立索引。按钮旁边的提示显示为“搜索语言已更改”，它和语言设置用的是同一个标记；重建完成后提示会消失。

### 评论可以被搜索，索引随每次修改更新 {#comments-become-searchable-and-indexing-follows-every-change}

0.9.0 之前，在有人运行向量嵌入回填之前，任何搜索方式都搜不到评论；编辑过的工作项和维基页面在语义搜索中也仍是旧文本。现在每次修改都会在保存时建立索引：关键词搜索立即可见，新增的 `celery-embeddings` 容器会在几秒后为语义搜索补上向量嵌入。参见[内容如何建立索引](semantic-search.md#how-content-is-indexed)。

- 迁移 0037 让已有的评论可以用关键词搜索。它遵循你的 `SEARCH_INDEX_COMMENTS`、`SEARCH_MIN_COMMENT_LENGTH` 和 `SEARCH_EXCLUDE_BOT_COMMENTS` 设置。无需任何操作。
- 请换用新的 `docker-compose.yml`：它新增了 `celery-embeddings` 服务，在单个进程中运行嵌入任务，模型只需加载一次（`EMBEDDING_WORKER_CONCURRENCY`，默认为 `1`）。如果你维护自己的 Compose 文件，请添加这个服务，或者设置 `EMBEDDING_QUEUE=celery`，让 `celery-worker` 来运行这些任务。它挂载的是同一个数据目录，所以模型文件不需要移动。
- 如果你使用语义搜索，升级后运行一次回填，为评论和升级前所做的修改也生成向量嵌入。回填还会为所有附件建立索引（包括没有描述的附件和维基页面上的附件），并删除被你的设置排除的评论的分块。没有模型文件时，它只写入关键词分块并给出提示，这不会造成任何问题：

  ```bash
  docker compose exec api python -m specivo.cli.backfill_embeddings
  ```

  如果你以前设置过定时回填，现在不再需要了。
- `make download-model`、`make backfill-embeddings`、`make reindex-fts` 和 `make import-data` 现在在当前目录所属服务栈的 `api` 容器中运行，因此在发布包中也能使用。模型下载命令改为 `python -m specivo.cli.download_model`，它会校验校验和；发布包中的 `configure.py` 移到了 `scripts/configure.py`。

### 语义搜索新增向量索引 {#semantic-search-gets-a-vector-index}

迁移 0038 在已存储的向量嵌入上构建 HNSW 索引，语义搜索和混合搜索不再需要把查询与每个分块逐一比较（参见[向量索引](semantic-search.md#vector-index)）。

- 索引使用 `CREATE INDEX CONCURRENTLY` 构建：构建期间 Specivo 可以继续工作，但迁移要等它完成，`api` 容器的启动也因此要等它完成。几万个分块需要几秒（在笔记本电脑上，100,000 个分块约 30 秒），数百万个分块需要几分钟。可以先用 `docker compose exec db psql -U specivo -d specivo -c "SELECT count(*) FROM chunk_embeddings"` 统计分块数量。
- 如果索引能放进 PostgreSQL 的 `maintenance_work_mem`（默认 64 MB；按每个分块约 2 KB 估算），构建会快得多。放不下时 PostgreSQL 会在日志中给出提示。大型部署可以在升级期间调高这个值，例如执行 `ALTER SYSTEM SET maintenance_work_mem = '1GB'; SELECT pg_reload_conf();`，升级后再改回去。
- 使用这个索引需要 pgvector 0.8 或更高版本，内置的 `pgvector/pgvector:pg18` 镜像满足这一要求。使用外部数据库时，用 `SELECT extversion FROM pg_extension WHERE extname = 'vector'` 检查版本。
- 如果构建被中断，下次启动时会删除构建到一半的索引并重新构建。

### 只支持内置的嵌入模型 {#only-the-bundled-embedding-model}

管理员 API 不能再注册嵌入模型：只支持内置的本地模型 `multilingual-e5-small`，不支持托管服务商（自带密钥）。迁移 0039 只保留一个默认模型；如果旧版本留下了多个模型，它会把本地模型保留为默认模型。如果默认模型来自其他提供商，语义搜索会保持关闭，日志中会提示一次；要换回本地模型，参见[更换模型](semantic-search.md#changing-the-model)。

### 检查工时记录权限 {#check-time-tracking-permissions}

现在读取工时记录需要项目中的 **查看工时记录** 权限，记录工时需要 **记录工时** 权限。0.9.0 之前，任何能打开项目的人都可以读取其中的工时记录。升级之后：

- **Reporter**（报告者）和内置的 **Non member**（非成员）角色不再能看到任何工时。如果某个角色的成员需要查看工时，为该角色添加 **查看工时记录** 权限。
- 迁移会为 **Agent**（智能体）角色授予 **查看工时记录** 权限，这样服务账户可以读回自己记录的工时。这只适用于名称仍为 *Agent* 的角色。如果你给它改了名，或者为智能体另建了角色，需要自己授予这项权限。
- 拥有全部权限的 **Manager**（管理者）和 **Developer**（开发者）没有变化。
- 任何有 **记录工时** 但没有 **查看工时记录** 权限的自定义角色，可以记录工时，但之后看不到这些记录。请在 **管理 → 角色** 中检查你的自定义角色。

参见[工时记录](../projects/time-tracking.md#who-can-do-what)。

### 新的角色和追踪器设置 {#new-role-and-tracker-settings}

新增了 **管理 → 角色** 和 **管理 → 追踪器** 两个页面。角色现在有 *用户可见性* 和 *工时记录可见性* 两项设置，追踪器（Tracker，即工作项类型）可以设为 **默认私有**。所有已有的角色和追踪器都从保持现有行为的取值开始（所有人可见、所有工时记录可见、非私有），所以在你编辑之前不会有任何变化。参见[角色能看到什么](../projects/index.md#what-a-role-can-see)和[默认私有](../issues/creating.md#private-by-default)。

### 使用浏览器 Cookie 的脚本需要 CSRF 令牌 {#scripts-that-use-browser-cookies-need-a-csrf-token}

通过登录 Cookie 认证的调用，现在必须在修改密码、退出登录、退出所有会话、吊销会话，以及 `/api/v1/auth/` 下其他任何会改变状态的请求中发送 `X-CSRF-Token` 请求头。只有四个本来就不需要会话的请求例外：登录、刷新令牌、忘记密码和重置密码。请求头的值是 Specivo 在任意 `GET` 请求中设置的 `csrf_token` Cookie。不带这个请求头的请求会被以 `403 CSRF validation failed` 拒绝。Web 界面会自动发送这个请求头。

使用 API 密钥或 `Authorization: Bearer` 请求头的客户端不受影响，脚本和智能体本来也应该用这种方式认证。

### 过长的 Markdown 会被拒绝 {#longer-markdown-is-refused}

工作项描述、评论、维基页面、版本描述和重复任务的描述现在最多可以包含 **262,144 个字符**。超出时保存会被拒绝，并提示这一上限。已经存储的超长内容（例如导入的内容）保持原样，以纯文本显示。格式化耗时过长的文本也会以纯文本显示。参见[大小和复杂度限制](../reference/markdown.md#size-and-complexity-limits)。

### 搜索分页有上限 {#search-paging-is-capped}

REST 搜索端点会以 `422` 拒绝大于 1000 的 `offset`，MCP 搜索工具接受的 `limit` 范围是 1 到 50。需要翻到更深页的客户端应该改为缩小查询范围。Web 搜索页面遇到这种情况不会报错，而是把 offset 限制在上限以内。

### 导入命令改名 {#import-command-renamed}

`python -m specivo.cli.import_redmine` 改为 `python -m specivo.cli.import_data`。请更新所有按名称调用旧模块的脚本和定时任务。从 0.11.0 起，直接导入 Redmine 的功能已经移除；参见[升级到 0.11.0](#upgrading-to-0110)。

Redmine 导入还会带上匿名访问和非成员访问、角色的可见性设置，以及追踪器的默认私有设置。它绝不会打开实例级的匿名访问开关。

### 你可能注意到的其他变化 {#other-changes-you-may-notice}

- 附在你无权查看的工作项上的文件，现在会返回“未找到”，不再提供下载；无权打开某个项目的人，也看不到该项目共享的版本。
- 在 Markdown 中，`![chart](chart.png)` 这样只写文件名的写法会链接到同名附件。参见[按文件名链接附件](../issues/attachments.md#link-to-an-attachment-by-filename)。
- 导入的账户可以保留原来的密码。这项功能默认关闭，只有在 `LEGACY_PASSWORD_ALGORITHMS` 中列出算法后才会启用；参见[配置](configuration.md#imported-passwords)。
