---
description: "升级 Specivo：修改 SPECIVO_VERSION、拉取镜像并重启，数据库迁移自动执行；升级前务必备份，回滚时固定回旧版本标签即可。"
---

# 升级 {#upgrading}

升级 Specivo 就是切换到更新的 `specivo/specivo` 镜像。容器入口脚本会在启动时自动应用新的数据库迁移，所以升级通常只需两条命令。

!!! warning "每次升级前先备份"
    迁移可能修改数据库结构，而且不一定可逆。拉取新镜像 **之前**，先导出数据库并备份 `SPECIVO_DATA_DIR`，参见
    [备份与恢复](backup-restore.md)。一旦出问题，这份备份就是你的退路。

## 1. 选择目标版本 {#1-choose-the-target-version}

在 `.env` 中固定要升级到的版本，让升级成为一次明确的操作：

```bash
# .env
SPECIVO_VERSION=0.1.11
```

`SPECIVO_VERSION=latest` 会始终拉取最新发布的镜像。试用时这样没问题，但对于你真正依赖的部署，请固定一个明确的标签，这样你才清楚运行的到底是哪个版本。

## 2. 拉取并重启 {#2-pull-and-restart}

```bash
docker compose pull
docker compose stop api celery-worker celery-embeddings celery-beat
docker compose up -d
```

`pull` 拉取新镜像；`stop` 停掉应用容器，确保一次性的 `init-data` 服务准备数据目录时没有应用容器在运行；`up -d` 重新创建容器。启动时，入口脚本会等待数据库就绪并自动应用新的迁移，你不需要自己执行迁移步骤。

查看 API 日志，确认启动正常：

```bash
docker compose logs -f api
```

当 `api` 服务报告健康状态后，升级就完成了。

## 回滚 {#rolling-back}

如果新版本运行异常，固定回旧标签并重启即可回滚：

```bash
# .env
SPECIVO_VERSION=0.1.10      # the version you were on before

docker compose pull
docker compose up -d
```

!!! danger "数据库结构迁移后可能需要恢复备份"
    如果失败的升级已经应用了迁移，数据库结构可能已领先于旧镜像，仅靠回滚还不够。这时需要恢复升级前导出的数据库和
    `SPECIVO_DATA_DIR`，再启动旧镜像。这正是升级前必须备份的原因。

## 建议 {#tips}

- 生产环境每次只升级一个小版本，不要一次跨越多个版本。
- 拉取前先阅读目标版本的发布说明。
- 升级后做一次快速冒烟测试：登录、打开一个工作项、执行一次搜索。

## 升级到 0.13.0 {#upgrading-to-0130}

### pg_trgm 与 btree_gist 扩展 {#the-pg_trgm-and-btree_gist-extensions}

迁移会创建两个 PostgreSQL 扩展：`pg_trgm` 用于
[相似工作项建议](../issues/creating.md#similar-issues)，`btree_gist` 用于 [AI 模型价格](ai-model-prices.md)。自带的数据库不需要任何操作。使用外部数据库时，执行迁移的角色需要对该数据库有 `CREATE` 权限（两者都是受信任扩展，不需要超级用户）；在
Azure 灵活服务器上，先把 `pg_trgm` 和 `btree_gist` 加入 `azure.extensions`。参见
[配置](configuration.md)。

### AI 模型价格取代智能体成本表 {#ai-model-prices-replace-the-agent-cost-tables}

未使用的 `agent_token_logs` 和 `model_cost_configs` 表连同其未挂载的 API 路由一起被删除。新的 [AI 模型价格](ai-model-prices.md)表会用自带的价目表填充；每个模型的第一条自带价格从 2000 年 1 月 1 日起生效，因此更早的用量也能计价。

### 填充链接图 {#fill-in-the-link-graph}

工作项上的“提及于”和维基页面上的“链接来源”来自存储的链接图。新写和修改的文本在保存时就会建立链接；升级后运行一次下面的命令，为已有内容建立链接：

```bash
docker compose exec api python -m specivo.cli.rebuild_links
```

项目管理者也可以在
[项目设置 → 搜索](../wiki/history-linking.md#the-project-link-graph)中用 **重建链接图** 按项目执行同样的操作。

### 元数据值会被校验 {#metadata-values-are-validated}

元数据架构现在会拒绝不符合架构的值，无论来自工作项页面、REST API 还是 AI 智能体（AI agent）。只检查本次保存改动的内容：已经持有不合架构值的工作项仍然可以编辑，但对该字段的写入会被拒绝，直到值符合架构为止（对于 `commits` 这样的列表，追加也算写入）。详情见[哪些值会被拒绝](../metadata/values.md#what-is-refused)。之前写入过短或格式错误的值的智能体，现在会收到指明字段名的错误。

升级前不需要修复任何内容。升级后，列出持有不合架构值的工作项并修复，这样人和智能体才能继续写入这些字段：

```bash
docker compose exec api python -m specivo.cli.admin metadata-check
docker compose exec api python -m specivo.cli.admin metadata-check --project ACME
```

它会按项目和架构输出失败的工作项数量、涉及的字段以及工作项编号。它从不输出值，也不做任何修改。收紧架构后可以再运行一次。

### 工作流字段规则生效 {#workflow-field-rules-apply}

通过管理 API 创建的字段规则（按状态设置必填或只读字段）现在会在每次保存工作项时强制执行，只判断本次保存改动的内容。没有规则的实例不会有变化。引用未知字段的规则会被拒绝。参见
[字段规则](../issues/statuses-workflow.md#field-rules)。

## 升级到 0.11.0 {#upgrading-to-0110}

0.11.0 版本把 Redmine 导入从 Specivo 中移出。Specivo 现在只加载导入包，导入包由迁移工具生成。从未使用过直接导入的实例无需任何改动。

### 直接 Redmine 导入已移除 {#the-direct-redmine-import-is-removed}

`import_data --source redmine`、`make import-redmine` 和 `importers` 附加依赖都已移除，导入源插件钩子也一并移除。要从其他系统迁移，参见[从其他项目管理系统导入](importing.md)；Redmine 请使用
[Redmine 迁移工具](https://github.com/specivo/redmine-migration-tool/blob/main/docs/migrating-from-redmine.md)，它生成的导入包可以用
`python -m specivo.cli.import_data --source bundle` 加载。请更新使用旧选项的脚本和计划任务。

### 加载导入包前先重新设置早期直接导入的键 {#re-key-an-earlier-direct-import-before-loading-a-bundle}

如果你曾用旧的直接导入功能导入过数据，这些记录以脱敏后的数据库 URL 为键，而导入包以清单中指定的实例为键。先把它们迁移到导入包的键下，这样导入包才会在早期导入的基础上补充，而不是产生重复：

```bash
# Move the scope (leave out --yes first for a dry run that checks and counts)
python -m specivo.cli.admin import-map rename --system redmine \
    --from 'postgresql://redmine:***@db.internal/redmine' --to tracker.example.com --yes
# Then load the tool's bundle as a continuation of that import
python -m specivo.cli.import_data --source bundle --path /data/exports/redmine --continue-import
```

运行 `import-map list` 可以查看当前的键。
[导入包格式](https://github.com/specivo/specivo/blob/main/docs/import-bundle/v1.md)中描述了
`source.system` 和 `source.instance`。

## 升级到 0.10.0 {#upgrading-to-0100}

0.10.0 版本新增单点登录（SSO）和三个迁移（0041 到 0043），入口脚本会在启动时应用。要保持原有行为，不需要配置任何东西：在你添加提供商之前，单点登录处于关闭状态，已登录的用户保持登录。请检查以下几点。

### 自带技术栈新增 `proxy` 网络 {#the-bundled-stack-gets-a-proxy-network}

使用新的 `docker-compose.yml`。它把 `nginx` 和 `api` 放到新的 `proxy` 网络上，nginx 使用固定地址
`10.213.47.2`，这样 Specivo 从 `X-Forwarded-For` 读取客户端地址时可以只信任这一个地址（参见
[代理后的客户端地址](configuration.md#client-addresses-behind-a-proxy)）。

- 如果 `docker compose up -d` 因 “Pool overlaps” 而停止，说明这个网段在你的主机上已被占用。在 `.env` 中把
  `SPECIVO_PROXY_SUBNET` 和 `SPECIVO_NGINX_IP` 设为空闲的网段和地址，然后重新运行。
- 如果你使用自己的 Compose 文件，请用同样的方式添加该网络，或者把 `TRUSTED_PROXIES` 设为你的代理地址。`TRUSTED_PROXIES`
  为空时，连接的地址就被当作客户端地址：在代理后面，所有人会共用同一个限流桶，审计日志里也只有同一个地址。
- 如果自带的 nginx 前面还有另一个代理（Caddy、Traefik、CDN），设置
  `TRUSTED_PROXY_HOPS=2`。只有列在 `TRUSTED_PROXIES` 中的代理才能指定客户端地址，`TRUSTED_PROXY_HOPS`
  说明串联的代理有几层。
- 如果你用自己的文件替换了 `nginx/default.conf`，请保留
  `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`。你也可以从自带文件中引入新的
  `specivo_no_query` 访问日志格式：访问日志不再记录查询字符串，因为其中可能包含单点登录时的 code 和 state，以及链接中的一次性令牌。如果想清除这些内容，请删除或轮换旧的访问日志。

### 退出登录的变化 {#signing-out-changed}

- **退出登录** 按钮现在是一个向 `/logout/` 提交的表单。指向 `/logout/` 的书签或链接只会打开“退出登录？”页面，不会让任何人退出登录。
- `POST /api/v1/auth/logout/` 现在返回 `200` 和 `{"end_session_url": ...}`，而不是 `204`。除非会话来自支持在提供商端退出登录的单点登录提供商，否则该值为 `null`。精确检查 `204` 的脚本必须改为接受 `200`。
- 退出登录现在会立即结束服务器端会话并吊销其访问令牌。以前两者会一直有效直到过期。
- 重置密码（通过邮件、由管理员操作或在命令行执行）会结束该账户的所有会话。

### 管理员 API 密钥的权限缩减 {#administrator-api-keys-do-less}

管理员的 API 密钥在以下操作上会被拒绝，返回 `403` 和错误码 `api_key_not_allowed`：重置密码、解锁或重新激活用户、创建用户或管理员（服务账户仍然允许）、为其他用户创建密钥，以及更改单点登录设置。此前用密钥做这些事的自动化脚本，必须改为以已登录的管理员身份运行。参见
[管理员密钥不能做什么](../ai-agents/api-keys.md#what-an-administrators-key-cannot-do)。

### 停用用户 {#deactivated-users}

管理员可以停用和重新激活用户（**管理 → 用户**，在该用户的页面上）。停用会结束所有会话并关闭该用户的 API 密钥；重新激活不会重新开启这些密钥。

### 如果你启用单点登录 {#if-you-turn-on-single-sign-on}

- 先设置 `APP_URL`：重定向和退出登录地址都由它生成。
- Specivo 中存储的客户端密钥使用由 `SECRET_KEY` 派生的密钥加密。如果你轮换了
  `SECRET_KEY`，需要打开每个提供商并重新输入其客户端密钥。请把 `.env.local` 和数据库导出一起备份；参见[备份与恢复](backup-restore.md#what-depends-on-secret_key)。
- 在身份提供商（IdP）处注册重定向 URI；如果使用后端通道退出登录，也注册其 URI。参见[单点登录](single-sign-on/index.md)。
- 保留一条恢复登录的途径：[重新登录](configuration.md#single-sign-on-getting-back-in)。

迁移还会删除用户表中未使用的 `github_id` 和 `google_id` 列。

## 升级到 0.9.0 {#upgrading-to-090}

0.9.0 版本新增八个迁移（0033 到 0040），入口脚本会像其他迁移一样在启动时应用。大部分内容无需操作，但有几件事值得在升级后做一下或检查一下。

### 容器不再以 root 运行 {#the-containers-no-longer-run-as-root}

`api` 和 Celery 容器，以及你通过 `docker compose exec api ...` 运行的命令，现在都以非特权用户 `specivo`（UID/GID `10001`）运行。请使用新的 `docker-compose.yml`：其中的
`init-data` 服务会在 `api` 启动前以 root 运行一次，把数据目录下的 `attachments/`、`avatars/`、`models/`、`state/` 和 `logs/app/` 子目录交给应用，包括旧版本以 root 身份写入的文件，并把 `.bootstrap.json`（如果还在原位）移入 `state/`。附件目录很大时，首次启动可能需要一点时间；之后的启动会跳过已属于应用的目录。

- 按照[拉取并重启](#2-pull-and-restart)的做法，在 `docker compose up -d` 之前先停掉应用容器：`init-data` 运行时不能有旧的 `celery-worker`、
  `celery-embeddings` 或 `celery-beat` 容器仍在运行。
- 它不会进入应用已拥有的目录。如果它报告某个目录中有 root 的文件，停掉所有应用容器并运行 `docker compose run --rm init-data fix-permissions --all`。

- 数据目录本身仍归 root（或你自己）所有，`logs/`、`postgresql/`、`redis/`
  和 `logs/nginx/` 也一样：应用无法修改数据目录顶层的条目。
- 应用日志文件从 `logs/specivo.log` 移到 `logs/app/specivo.log`。旧文件原地保留，不再需要时删掉即可。
- 如果某个应用目录是符号链接、位于另一个文件系统或属于其他用户，`init-data` 会跳过它并记录警告。请把目标直接挂载到该路径而不是使用符号链接，或者自己对它运行 `sudo chown -R 10001:10001`。
- 如果你使用自己的 Compose 文件，请添加 `init-data` 服务并让 `api` 依赖它，或者在启动新镜像前自己准备好目录：
  `cd <SPECIVO_DATA_DIR> && sudo mkdir -p attachments avatars models state logs/app`，然后
  `sudo chown -R 10001:10001 attachments avatars models state logs/app`。
  否则 api 启动时会记录警告，上传也会失败。
- 挂载到应用容器中的其他主机目录（自定义主题或错误页面目录、存放导入文件的目录）必须对 UID `10001` 可读。
- 要完全不经过 root 步骤，在 `docker-compose.override.yml` 中为 `init-data`、`api` 和 Celery 服务设置 `user: "10001:10001"`；这样 `init-data` 就不会做任何修改。镜像也支持只读根文件系统（`read_only: true` 加上挂载到 `/tmp` 的 `tmpfs`）。
- 镜像现在自带 `HEALTHCHECK`，`docker ps` 会显示每个 Specivo 容器的健康状态。

### 重建搜索索引 {#rebuild-the-search-index}

现在在所有搜索模式下都能搜索 `ACME-42` 这样的工作项编号。旧文本必须重新索引后，其中对编号的*提及*才能被搜到，因此迁移会把索引标记为需要重建。在你运行重建之前，工作项仍然可以通过自身编号找到。重建一次即可：

- 在 Web 界面中，进入 **管理 → 设置**，点击 **立即重建索引**；或者
- 在命令行中：

  ```bash
  docker compose exec api python -m specivo.cli.reindex_fts
  ```

重建索引分小批次进行，期间 Specivo 仍可正常使用。升级后编辑的文本在保存时就会建立索引。按钮旁边的提示文字为“搜索语言已更改”，它与语言设置使用的是同一个标记；重建完成后会自动清除。

### 评论可被搜索，索引跟随每次更改 {#comments-become-searchable-and-indexing-follows-every-change}

0.9.0 之前，评论在有人运行向量嵌入回填之前无法被任何搜索找到，而编辑过的工作项和维基页面在语义搜索中仍保留旧文本。现在每次更改在保存时就会建立索引：关键词搜索立即可见，新的 `celery-embeddings` 容器几秒后为语义搜索添加向量嵌入。参见
[内容如何建立索引](semantic-search.md#how-content-is-indexed)。

- 迁移 0037 让已有评论可以通过关键词搜索到。它遵循你的
  `SEARCH_INDEX_COMMENTS`、`SEARCH_MIN_COMMENT_LENGTH` 和 `SEARCH_EXCLUDE_BOT_COMMENTS` 设置。无需操作。
- 使用新的 `docker-compose.yml`：它增加了 `celery-embeddings` 服务，在单个进程中运行嵌入任务，模型只需加载一次（`EMBEDDING_WORKER_CONCURRENCY`，默认
  `1`）。如果你使用自己的 Compose 文件，请添加该服务，或者设置 `EMBEDDING_QUEUE=celery` 让
  `celery-worker` 运行这些任务。它挂载同一个数据目录，所以模型文件不需要移动。
- 如果你使用语义搜索，升级后运行一次回填，让升级前的评论和编辑也获得向量嵌入。它还会索引所有附件（包括没有描述的附件和维基页面上的附件），并删除你的设置所排除的评论的分块。没有模型文件时，它只写入关键词分块并会提示这一点，这没有坏处：

  ```bash
  docker compose exec api python -m specivo.cli.backfill_embeddings
  ```

  如果你之前设置了定时回填，现在不再需要。
- `make download-model`、`make backfill-embeddings`、`make reindex-fts` 和 `make import-data` 现在在当前目录所用技术栈的 `api` 容器中运行，因此在发布包中也能使用。模型下载命令改为 `python -m specivo.cli.download_model`，会校验校验和；发布包中的 `configure.py` 移到了 `scripts/configure.py`。

### 语义搜索新增向量索引 {#semantic-search-gets-a-vector-index}

迁移 0038 在存储的向量嵌入上构建 HNSW 索引，语义搜索和混合搜索不再需要把查询与每个分块逐一比较（参见[向量索引](semantic-search.md#vector-index)）。

- 索引通过 `CREATE INDEX CONCURRENTLY` 构建：构建期间 Specivo 仍可使用，但迁移（以及 `api` 容器的启动）会等到构建完成。几万个分块需要几秒（笔记本上 100,000 个约 30 秒），几百万个则需要几分钟。先用
  `docker compose exec db psql -U specivo -d specivo -c "SELECT count(*) FROM chunk_embeddings"` 统计你的分块数量。
- 索引能放进 PostgreSQL 的 `maintenance_work_mem` 时，构建会快得多（默认 64 MB；按每个分块约 2 KB 估算）。放不下时 PostgreSQL 会记录一条提示。大型部署可以在升级期间临时调高，例如
  `ALTER SYSTEM SET maintenance_work_mem = '1GB'; SELECT pg_reload_conf();`，升级后再改回去。
- 使用该索引需要 pgvector 0.8 或更高版本，自带的 `pgvector/pgvector:pg18` 镜像已满足。外部数据库可用 `SELECT extversion FROM pg_extension WHERE extname = 'vector'` 检查。
- 如果构建被中断，下次启动会删除构建到一半的索引并重新构建。

### 只支持自带的嵌入模型 {#only-the-bundled-embedding-model}

管理员 API 不再能注册嵌入模型：只支持自带的本地模型
`multilingual-e5-small`，不支持托管（自带密钥）的提供商。迁移
0039 只保留一个默认模型；如果旧版本留下了多个，它会把本地模型保留为默认。如果默认模型属于其他提供商，语义搜索会保持关闭，日志中会提示一次；参见[更换模型](semantic-search.md#changing-the-model)把本地模型改回来。

### 检查工时记录权限 {#check-time-tracking-permissions}

现在读取工时记录需要项目中的 **查看工时记录** 权限，记录工时需要 **记录工时** 权限。0.9.0 之前，任何能打开项目的人都能看到它的工时记录。升级后：

- **Reporter**（报告人）和内置的 **Non member**（非成员）角色不再能看到任何工时。如果这些角色的成员需要查看，请给角色添加 **查看工时记录** 权限。
- 迁移会给 **Agent**（代理）授予 **查看工时记录**，这样服务账户才能读回自己记录的工时。这只适用于仍然叫 *Agent* 的角色。如果你重命名了它，或者为智能体创建了自己的角色，请自行授权。
- 拥有全部权限的 **Manager**（管理者）和 **Developer**（开发者）保持不变。
- 任何拥有 **记录工时** 但没有 **查看工时记录** 的自定义角色可以记录工时，但之后看不到。请在 **管理 → 角色** 中检查你的自定义角色。

参见[工时记录](../projects/time-tracking.md#who-can-do-what)。

### 新的角色和追踪器设置 {#new-role-and-tracker-settings}

**管理 → 角色** 和 **管理 → 追踪器** 是新增页面。角色现在有*用户可见性*和*工时记录可见性*设置，追踪器可以设为 **默认私有**。所有现有角色和追踪器的初始值都保持现有行为（所有人可见、所有工时记录可见、不私有），所以在你修改之前不会有任何变化。参见
[角色能看到什么](../projects/index.md#what-a-role-can-see)和
[默认私有](../issues/creating.md#private-by-default)。

### 使用浏览器 Cookie 的脚本需要 CSRF 令牌 {#scripts-that-use-browser-cookies-need-a-csrf-token}

通过登录 Cookie 认证的调用，在修改密码、退出登录、全部退出登录、吊销会话以及
`/api/v1/auth/` 下任何其他改变状态的请求上，现在必须发送 `X-CSRF-Token` 头；例外是设计上无需会话的四个接口：登录、刷新令牌、忘记密码和重置密码。头的值是 Specivo 在任何 `GET` 请求上设置的 `csrf_token` Cookie。缺少该头的请求会被拒绝，返回 `403 CSRF validation failed`。Web 界面会自动发送这个头。

使用 API 密钥或 `Authorization: Bearer` 头的客户端不受影响，脚本和智能体本来就应该这样认证。

### 过长的 Markdown 会被拒绝 {#longer-markdown-is-refused}

工作项描述、评论、维基页面、版本描述或重复工作项描述现在最多可包含 **262,144 个字符**。保存更长的内容会被拒绝，并提示上限。已存储的更长内容（例如导入的内容）原样保留，以纯文本显示。格式化耗时过长的文本也会以纯文本显示。参见
[大小与复杂度限制](../reference/markdown.md#size-and-complexity-limits)。

### 搜索分页设有上限 {#search-paging-is-capped}

REST 搜索接口对超过 1000 的 `offset` 返回 `422` 并拒绝，MCP 搜索工具的 `limit` 接受 1 到 50。需要翻得更深的客户端应改为缩小查询范围。Web 搜索页面会把 offset 限制在上限内，而不是报错。

### 导入命令已重命名 {#import-command-renamed}

`python -m specivo.cli.import_redmine` 改为 `python -m specivo.cli.import_data`。请更新按旧模块名调用的脚本或计划任务。从 0.11.0 起直接 Redmine 导入已移除；参见[升级到 0.11.0](#upgrading-to-0110)。

Redmine 导入还会带过来匿名和非成员访问、角色的可见性设置以及追踪器的默认私有设置。它从不会打开实例级的匿名访问开关。

### 其他你可能注意到的变化 {#other-changes-you-may-notice}

- 附在你无法查看的工作项上的文件现在返回“未找到”而不是下载，项目的共享版本对无法打开该项目的人隐藏。
- 在 Markdown 中，`![chart](chart.png)` 这样的裸文件名会链接到同名附件。参见[按文件名链接附件](../issues/attachments.md#link-to-an-attachment-by-filename)。
- 导入的账户可以继续使用旧密码。在 `LEGACY_PASSWORD_ALGORITHMS` 中列出算法之前此功能关闭；参见[配置](configuration.md#imported-passwords)。
