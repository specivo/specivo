---
description: "升级 Specivo 只需修改 SPECIVO_VERSION、拉取镜像并重启，数据库迁移会自动执行；本文还说明升级前如何备份，以及如何固定旧版本标签回滚。"
---

# 升级 {#upgrading}

升级 Specivo 就是切换到更新的 `specivo/specivo` 镜像。容器入口脚本会在启动时应用新的数据库迁移，所以一次升级通常只需两条命令。

!!! warning "每次升级前都要备份"
    迁移可能会修改数据库结构，而且并非总能逆转。拉取新镜像 **之前**，先做一次数据库转储，并备份 `SPECIVO_DATA_DIR`。参阅[备份与恢复](backup-restore.md)。一旦出问题，这份备份就是你的退路。

## 1. 选择目标版本 {#1-choose-the-target-version}

在 `.env` 中固定你要升级到的版本，让升级有明确的目标：

```bash
# .env
SPECIVO_VERSION=0.1.11
```

使用 `SPECIVO_VERSION=latest` 会始终拉取最新发布的镜像。试用时这没问题，但对于你依赖的正式部署，应固定一个明确的标签，这样你随时清楚运行的是哪个版本。

## 2. 拉取并重启 {#2-pull-and-restart}

```bash
docker compose pull
docker compose stop api celery-worker celery-embeddings celery-beat
docker compose up -d
```

`pull` 拉取新镜像；`stop` 停止应用容器，确保一次性运行的 `init-data` 服务准备数据目录时没有应用容器在运行；`up -d` 重新创建容器。启动时，入口脚本会等待数据库就绪并自动应用新的迁移，你不需要手动执行迁移步骤。

查看 API 日志，确认启动正常：

```bash
docker compose logs -f api
```

当 `api` 服务报告健康状态时，升级即完成。

## 回滚 {#rolling-back}

如果新版本运行异常，固定回之前的标签并重启即可回滚：

```bash
# .env
SPECIVO_VERSION=0.1.10      # the version you were on before

docker compose pull
docker compose up -d
```

!!! danger "数据库结构迁移可能需要恢复备份"
    如果失败的升级已经应用了迁移，数据库结构可能比旧镜像更新，仅回滚镜像并不够。这种情况下，先恢复升级前做的数据库转储和 `SPECIVO_DATA_DIR` 备份，再启动旧镜像。这正是升级前必须备份的原因。

## 提示 {#tips}

- 生产环境每次只升级一个次版本，不要一次跨越多个发布版本。
- 拉取镜像前，先阅读目标版本的发布说明。
- 升级完成后做一次快速冒烟测试：登录、打开一个工作项、执行一次搜索。

## 升级到 0.11.0 {#upgrading-to-0110}

0.11.0 版本把 Redmine 导入功能移出了 Specivo。现在 Specivo 只负责加载导入包，导入包由迁移工具生成。从未使用过直接导入功能的实例不受任何影响。

### 直接 Redmine 导入已移除 {#the-direct-redmine-import-is-removed}

`import_data --source redmine`、`make import-redmine` 和 `importers` 附加依赖已被移除，导入源插件钩子也一并移除。要从其他项目管理系统迁移，请参阅[从其他项目管理系统导入](importing.md)；Redmine 用户请使用 [Redmine 迁移工具](https://github.com/specivo/redmine-migration-tool/blob/main/docs/migrating-from-redmine.md)，它会生成一个导入包，再由 `python -m specivo.cli.import_data --source bundle` 加载。请更新用到旧选项的脚本和定时任务。

### 加载导入包前，先为早先的直接导入重设键 {#re-key-an-earlier-direct-import-before-loading-a-bundle}

如果你曾用旧的直接导入功能导入数据，那些记录以脱敏后的数据库 URL 作为键，而导入包以其清单（manifest）中指定的实例作为键。请先把它们迁移到导入包使用的键，这样导入包会在先前导入的基础上追加，而不是重复导入：

```bash
# Move the scope (leave out --yes first for a dry run that checks and counts)
python -m specivo.cli.admin import-map rename --system redmine \
    --from 'postgresql://redmine:***@db.internal/redmine' --to tracker.example.com --yes
# Then load the tool's bundle as a continuation of that import
python -m specivo.cli.import_data --source bundle --path /data/exports/redmine --continue-import
```

运行 `import-map list` 可查看当前的键。[导入包格式](https://github.com/specivo/specivo/blob/main/docs/import-bundle/v1.md)文档说明了 `source.system` 和 `source.instance`。

## 升级到 0.10.0 {#upgrading-to-0100}

0.10.0 版本新增了单点登录（SSO）和三个迁移（0041 到 0043），入口脚本会在启动时应用。要保持原有行为，无需任何配置：在你添加身份提供商（IdP）之前，单点登录处于关闭状态，已登录的用户也会保持登录。请检查以下几点。

### 内置部署栈新增 `proxy` 网络 {#the-bundled-stack-gets-a-proxy-network}

采用新的 `docker-compose.yml`。它把 `nginx` 和 `api` 放在新网络 `proxy` 上，nginx 使用固定地址 `10.213.47.2`，这样 Specivo 从 `X-Forwarded-For` 读取客户端地址时，可以只信任这一个地址（参阅[代理后的客户端地址](configuration.md#client-addresses-behind-a-proxy)）。

- 如果 `docker compose up -d` 因 “Pool overlaps” 中止，说明该网段在你的主机上已被占用。在 `.env` 中把 `SPECIVO_PROXY_SUBNET` 和 `SPECIVO_NGINX_IP` 设置为空闲的网段和地址，然后重新运行。
- 如果你使用自己的 Compose 文件，请以同样方式添加该网络，或把 `TRUSTED_PROXIES` 设置为代理的地址。`TRUSTED_PROXIES` 为空时，连接的来源地址即被视为客户端：在代理后面，所有人会共用同一个限流桶，审计日志中也只有同一个地址。
- 如果在内置 nginx 前面还有另一层代理（Caddy、Traefik 或 CDN），设置 `TRUSTED_PROXY_HOPS=2`。只有 `TRUSTED_PROXIES` 中列出的代理才能指定客户端地址，`TRUSTED_PROXY_HOPS` 则说明串联的代理有几层。
- 如果你用自己的文件替换了 `nginx/default.conf`，请保留 `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`。你也可以从内置文件中照搬新的 `specivo_no_query` 访问日志格式：访问日志不再记录查询字符串，因为其中可能包含单点登录过程中的 code 和 state，以及链接中的一次性令牌。若想清除这些内容，请删除或轮转旧的访问日志。

### 退出登录方式有变 {#signing-out-changed}

- **退出登录** 按钮现在是一个向 `/logout/` 发送 POST 请求的表单。指向 `/logout/` 的书签或链接只会打开“退出登录？”页面，不会让任何人退出登录。
- `POST /api/v1/auth/logout/` 现在返回 `200` 和 `{"end_session_url": ...}`，而不是 `204`。除非会话来自支持在提供商侧退出登录的单点登录提供商，否则该值为 `null`。只接受 `204` 的脚本必须改为接受 `200`。
- 退出登录现在会立即在服务端结束会话并吊销其访问令牌。此前两者都会一直有效，直到过期。
- 重置密码（无论通过邮件、由管理员操作还是在命令行执行）会结束该账户的所有会话。

### 管理员 API 密钥的权限收窄 {#administrator-api-keys-do-less}

管理员的 API 密钥在以下操作上会被拒绝，返回 `403` 和错误码 `api_key_not_allowed`：重置密码、解锁或重新激活用户、创建用户或管理员（服务账户仍然允许）、为其他用户创建密钥、修改单点登录设置。以前用密钥完成这些操作的自动化脚本，必须改为以已登录管理员的身份运行。参阅[管理员密钥不能做什么](../ai-agents/api-keys.md#what-an-administrators-key-cannot-do)。

### 停用用户 {#deactivated-users}

管理员可以停用和重新激活用户（**管理 → 用户**，在用户页面上操作）。停用会结束该用户的所有会话并关闭其 API 密钥；重新激活不会重新开启这些密钥。

### 如果你要开启单点登录 {#if-you-turn-on-single-sign-on}

- 先设置 `APP_URL`：重定向地址和退出登录地址都由它生成。
- 存储在 Specivo 中的客户端密钥使用由 `SECRET_KEY` 派生的密钥加密。如果你轮换了 `SECRET_KEY`，需要打开每个提供商并重新输入其客户端密钥。请把 `.env.local` 与数据库转储一起备份；参阅[备份与恢复](backup-restore.md#what-depends-on-secret_key)。
- 在身份提供商处注册重定向 URI；如果使用后端通道退出登录（back-channel logout），也要注册对应的 URI。参阅[单点登录](single-sign-on/index.md)。
- 保留一条备用登录途径：[重新获得访问权限](configuration.md#single-sign-on-getting-back-in)。

迁移还会删除用户表中未使用的 `github_id` 和 `google_id` 列。

## 升级到 0.9.0 {#upgrading-to-090}

0.9.0 版本新增了八个迁移（0033 到 0040），入口脚本会像其他迁移一样在启动时应用。大部分无需操作，但有几点值得在升级后处理或检查。

### 容器不再以 root 运行 {#the-containers-no-longer-run-as-root}

`api` 和 Celery 容器，以及你通过 `docker compose exec api ...` 运行的命令，现在都以非特权用户 `specivo`（UID/GID `10001`）运行。请采用新的 `docker-compose.yml`：其中的 `init-data` 服务会在 `api` 启动前以 root 身份运行一次，把数据目录下属于应用的子目录 `attachments/`、`avatars/`、`models/`、`state/` 和 `logs/app/` 的所有权交给应用（包括旧版本以 root 身份写入其中的文件），并把仍留在原处的 `.bootstrap.json` 移入 `state/`。附件目录很大时，首次启动可能需要一些时间；之后的启动会跳过已经归属应用的目录。

- 在 `docker compose up -d` 之前先停止应用容器，如[拉取并重启](#2-pull-and-restart)所述：`init-data` 运行时，不能有旧的 `celery-worker`、`celery-embeddings` 或 `celery-beat` 容器仍在运行。
- 它不会进入应用已拥有的目录。如果它报告某个目录中仍有属于 root 的文件，请停止所有应用容器并运行 `docker compose run --rm init-data fix-permissions --all`。

- 数据目录本身仍归 root（或你自己）所有，`logs/`、`postgresql/`、`redis/` 和 `logs/nginx/` 也一样：应用无法修改数据目录顶层的条目。
- 应用的日志文件从 `logs/specivo.log` 移到 `logs/app/specivo.log`。旧文件原地保留，不再需要时自行删除。
- 如果某个应用目录是符号链接、位于另一个文件系统上或属于其他用户，`init-data` 会跳过它并记录一条警告。请把目标直接挂载到该路径，而不是用符号链接，或者自己对它运行 `sudo chown -R 10001:10001`。
- 如果你使用自己的 Compose 文件，请添加 `init-data` 服务并让 `api` 依赖它，或者在启动新镜像前自行准备这些目录：先执行 `cd <SPECIVO_DATA_DIR> && sudo mkdir -p attachments avatars models state logs/app`，再执行 `sudo chown -R 10001:10001 attachments avatars models state logs/app`。否则 api 启动时会记录一条警告，上传也会失败。
- 挂载到应用容器中的其他主机目录（自定义主题或错误页目录、存放导入文件的目录）必须对 UID `10001` 可读。
- 如果想完全不经过 root 步骤，在 `docker-compose.override.yml` 中为 `init-data`、`api` 和各 Celery 服务设置 `user: "10001:10001"`；这样 `init-data` 就不会做任何更改。镜像也支持只读根文件系统（`read_only: true` 加上挂载到 `/tmp` 的 `tmpfs`）。
- 镜像现在自带 `HEALTHCHECK`，`docker ps` 会显示每个 Specivo 容器的健康状态。

### 重建搜索索引 {#rebuild-the-search-index}

现在在所有搜索模式下都可以搜索 `ACME-42` 这样的工作项编号。旧文本必须重新索引后，其中*提及*的编号才能被搜到，因此迁移会把索引标记为需要重建。在你执行重建之前，仍然可以通过工作项自身的编号找到它。重建一次即可：

- 在 Web 界面中，打开 **管理 → 设置**，点击 **立即重建索引**；或
- 在命令行中执行：

  ```bash
  docker compose exec api python -m specivo.cli.reindex_fts
  ```

重建以小批量进行，期间 Specivo 仍可正常使用。升级后编辑的文本在保存时即被索引。按钮旁边的提示文字是“搜索语言已更改”，这与语言设置使用的是同一个标记；重建完成后提示会消失。

### 评论可被搜索，索引随每次更改更新 {#comments-become-searchable-and-indexing-follows-every-change}

0.9.0 之前，评论在有人运行向量嵌入回填之前无法通过任何搜索找到，而编辑过的工作项和维基页面在语义搜索中仍保留旧文本。现在每次更改都会在保存时被索引：关键词搜索立即可见，新增的 `celery-embeddings` 容器会在几秒后为语义搜索添加向量嵌入。参阅[内容如何被索引](semantic-search.md#how-content-is-indexed)。

- 迁移 0037 让已有评论可以通过关键词搜索到。它遵循你的 `SEARCH_INDEX_COMMENTS`、`SEARCH_MIN_COMMENT_LENGTH` 和 `SEARCH_EXCLUDE_BOT_COMMENTS` 设置。无需操作。
- 采用新的 `docker-compose.yml`：它新增了 `celery-embeddings` 服务，在单个进程中运行嵌入任务，模型只需加载一次（`EMBEDDING_WORKER_CONCURRENCY`，默认 `1`）。如果你使用自己的 Compose 文件，请添加该服务，或设置 `EMBEDDING_QUEUE=celery` 让 `celery-worker` 来运行这些任务。它挂载同一个数据目录，模型文件无需移动。
- 如果你使用语义搜索，升级后运行一次回填，让升级前的评论和编辑也获得向量嵌入。回填还会索引所有附件（包括没有描述的附件和维基页面上的附件），并删除你的设置所排除的评论的分块。没有模型文件时，它只写入关键词分块并给出提示，这不会造成任何问题：

  ```bash
  docker compose exec api python -m specivo.cli.backfill_embeddings
  ```

  如果你此前设置了定时回填，现在不再需要。
- `make download-model`、`make backfill-embeddings`、`make reindex-fts` 和 `make import-data` 现在在当前目录所用部署栈的 `api` 容器中运行，因此在发布包中也能使用。模型下载命令现为 `python -m specivo.cli.download_model`，它会校验校验和；发布包中的 `configure.py` 移到了 `scripts/configure.py`。

### 语义搜索新增向量索引 {#semantic-search-gets-a-vector-index}

迁移 0038 会在已存储的向量嵌入上建立 HNSW 索引，语义搜索和混合搜索不再需要把查询与每个分块逐一比较（参阅[向量索引](semantic-search.md#vector-index)）。

- 索引通过 `CREATE INDEX CONCURRENTLY` 建立：建立期间 Specivo 可以继续工作，但迁移（也就是 `api` 容器的启动）会等到索引建完。几万个分块需要几秒（笔记本上 100,000 个约 30 秒），数百万个则需要几分钟。先用 `docker compose exec db psql -U specivo -d specivo -c "SELECT count(*) FROM chunk_embeddings"` 统计你的分块数量。
- 索引能放进 PostgreSQL 的 `maintenance_work_mem`（默认 64 MB；大约按每个分块 2 KB 预算）时，建立速度会快很多。放不下时 PostgreSQL 会记录一条提示。大型部署可在升级期间临时调高，例如 `ALTER SYSTEM SET maintenance_work_mem = '1GB'; SELECT pg_reload_conf();`，升级后再改回。
- 要用上该索引，需要 pgvector 0.8 或更高版本，内置的 `pgvector/pgvector:pg18` 镜像已满足。如果使用外部数据库，用 `SELECT extversion FROM pg_extension WHERE extname = 'vector'` 检查版本。
- 如果建立过程被中断，下次启动会删除半成品索引并重新建立。

### 仅支持内置嵌入模型 {#only-the-bundled-embedding-model}

管理员 API 不再能注册嵌入模型：只支持内置的本地模型 `multilingual-e5-small`，不再支持托管（自带密钥）的提供商。迁移 0039 只保留一个默认模型；如果旧版本留下了多个，则保留本地模型作为默认。如果默认模型使用其他提供商，语义搜索会保持关闭，并在日志中提示一次；参阅[更换模型](semantic-search.md#changing-the-model)恢复本地模型。

### 检查工时记录权限 {#check-time-tracking-permissions}

现在读取工时记录需要项目中的 **查看工时记录** 权限，记录工时需要 **记录工时** 权限。0.9.0 之前，任何能打开项目的人都能查看其工时记录。升级后：

- **Reporter**（报告者）和内置的 **Non member**（非成员）角色不再能看到任何工时。如果这些角色的成员需要查看，请为角色添加 **查看工时记录** 权限。
- 迁移会为 **Agent**（代理）授予 **查看工时记录** 权限，这样服务账户可以读回自己记录的工时。这只适用于名称仍为 *Agent* 的角色。如果你重命名了它，或者为智能体创建了自己的角色，请自行授予该权限。
- 拥有所有权限的 **Manager**（管理者）和 **Developer**（开发者）保持不变。
- 任何拥有 **记录工时** 但没有 **查看工时记录** 权限的自定义角色，可以记录工时，但之后看不到。请在 **管理 → 角色** 中检查你的自定义角色。

参阅[工时记录](../projects/time-tracking.md#who-can-do-what)。

### 新的角色和追踪器设置 {#new-role-and-tracker-settings}

**管理 → 角色** 和 **管理 → 追踪器** 是新增页面。角色现在有*用户可见性*和*工时记录可见性*两项设置，追踪器可以设为 **默认私有**。所有现有角色和追踪器的初始值都保持当前行为（所有人可见、所有工时记录可见、不私有），因此在你修改之前不会有任何变化。参阅[角色能看到什么](../projects/index.md#what-a-role-can-see)和[默认私有](../issues/creating.md#private-by-default)。

### 使用浏览器 Cookie 的脚本需要 CSRF 令牌 {#scripts-that-use-browser-cookies-need-a-csrf-token}

通过登录 Cookie 认证的请求现在必须携带 `X-CSRF-Token` 请求头，包括修改密码、退出登录、全部退出登录、吊销会话，以及 `/api/v1/auth/` 下所有其他会改变状态的请求；只有四个无需会话即可使用的接口例外：登录、刷新令牌、忘记密码和重置密码。请求头的值是 Specivo 在任何 `GET` 请求中设置的 `csrf_token` Cookie。缺少该请求头的请求会被拒绝，返回 `403 CSRF validation failed`。Web 界面会自动发送该请求头。

使用 API 密钥或 `Authorization: Bearer` 请求头的客户端不受影响，脚本和智能体本来也应该用这种方式认证。

### 过长的 Markdown 会被拒绝 {#longer-markdown-is-refused}

工作项描述、评论、维基页面、版本描述或重复任务描述现在最多可包含 **262,144 个字符**。保存更长的内容会被拒绝，并提示字符上限。已存储的超长内容（例如导入的内容）会原样保留，并以纯文本显示。格式化耗时过长的文本也会以纯文本显示。参阅[大小与复杂度限制](../reference/markdown.md#size-and-complexity-limits)。

### 搜索分页有上限 {#search-paging-is-capped}

REST 搜索接口对超过 1000 的 `offset` 返回 `422` 并拒绝请求，MCP 搜索工具的 `limit` 接受 1 到 50。需要翻到更深页数的客户端应改为缩小查询范围。Web 搜索页面会把 offset 截断到上限，而不是报错。

### 导入命令重命名 {#import-command-renamed}

`python -m specivo.cli.import_redmine` 已改为 `python -m specivo.cli.import_data`。请更新所有按旧模块名调用的脚本或定时任务。自 0.11.0 起，直接 Redmine 导入已被移除；参阅[升级到 0.11.0](#upgrading-to-0110)。

Redmine 导入还会带入匿名访问和非成员访问、角色的可见性设置以及追踪器的默认私有设置。它不会开启实例级的匿名访问开关。

### 其他可能注意到的变化 {#other-changes-you-may-notice}

- 附加在你无权查看的工作项上的文件现在返回“未找到”，而不是允许下载；项目的共享版本对无法打开该项目的人隐藏。
- 在 Markdown 中，`![chart](chart.png)` 这样的纯文件名会链接到同名附件。参阅[按文件名链接附件](../issues/attachments.md#link-to-an-attachment-by-filename)。
- 导入的账户可以继续使用旧密码。此功能默认关闭，直到你在 `LEGACY_PASSWORD_ALGORITHMS` 中列出算法；参阅[配置](configuration.md#imported-passwords)。
