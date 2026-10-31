---
description: "配置 Specivo：.env 与 .env.local 的分工、关键环境变量、外部数据库和 Redis、数据目录、SMTP 邮件，以及语义搜索所用的嵌入模型。"
---

# 配置 {#configuration}

Specivo 通过环境变量配置。Docker 栈从仓库根目录读取两个文件，两者都不是启动的必要条件——在你覆盖之前，默认值已经够用。

## `.env` 与 `.env.local` {#env-vs-envlocal}

- **`.env`** 存放设置：端口、镜像版本、数据目录、注册模式。由 `configure.py` 生成的 `.env` 还包含数据库 URL 和密码，所以生成的 `.env` 也要当作机密对待。
- **`.env.local`** 存放机密，最重要的是 `SECRET_KEY`。不要把它提交到版本控制。

从示例文件开始：

```bash
cp .env.example .env
```

或者让辅助脚本生成这两个文件（包括一个强随机的 `SECRET_KEY`）：

```bash
make configure          # or: python scripts/configure.py
```

!!! note "用同一个用户运行 `configure.py` 和 Docker Compose"
    `configure.py` 创建的 `.env` 和 `.env.local` 只有文件所有者可读写（权限 `0600`）：`.env` 里有数据库密码，`.env.local` 里有 `SECRET_KEY`。Docker Compose 以运行它的用户身份读取这两个文件，所以要用同一个用户运行 `configure.py` 和 `docker compose`（或 `make up`），或者两者都用 `sudo`。如果用一个用户创建了文件，再用另一个用户启动栈，`docker compose` 会读不到文件；此时用 `sudo chown <user> .env .env.local` 更改所有者，不要放宽权限。

修改任一文件后，运行以下命令使之生效：

```bash
docker compose up -d
```

## 关键变量 {#key-variables}

| 变量 | 用途 / 默认值 |
|---|---|
| `COMPOSE_PROJECT_NAME` | 隔离一个实例（容器、网络、数据卷），便于在同一台主机上运行多个实例。默认 `specivo`。 |
| `SPECIVO_PORT` | nginx 对外端口。默认 `9933`。 |
| `SPECIVO_DATA_DIR` | 持久化数据目录：数据库文件、日志、附件、主题。默认 `./specivo-data`。 |
| `SPECIVO_VERSION` | 镜像标签：`latest`，或固定到某个发布版本，例如 `0.12.0`。 |
| `SPECIVO_IMAGE` | 镜像拉取来源。默认 `specivo/specivo`（Docker Hub）。设为 `ghcr.io/specivo/specivo` 可从 GitHub Container Registry 拉取同一镜像，例如在 Docker Hub 被屏蔽的环境中。 |
| `DATABASE_URL` | `postgresql+asyncpg://specivo:specivo@db:5432/specivo`。指向外部数据库即可使用你自己的数据库。 |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | 内置 Postgres 的凭据。 |
| `REDIS_URL` | `redis://redis:6379/0`。也可以指向外部 Redis。 |
| `APP_URL` | Specivo 的公开地址，例如 `https://specivo.example.com`。邮件中的链接和[单点登录](single-sign-on/index.md)的地址都由它生成。 |
| `SECRET_KEY` | 用于签名会话和令牌。生产环境 **必须** 设为强随机的唯一值。放在 `.env.local` 中。 |
| `DEBUG` | 生产环境设为 `false`。 |
| `REGISTRATION_MODE` | `open` 允许任何人自行注册。要求邀请才能注册时，使用封闭模式。 |
| `CAPTCHA_ENABLED` | 注册表单上的验证码。 |
| `CORS_ORIGINS` | 允许的浏览器来源，例如 `["https://specivo.example.com"]`。 |
| `TRUSTED_PROXIES` | 允许在 `X-Forwarded-For` 中指明客户端地址的反向代理，以 JSON 列表形式给出地址或网段。默认 `[]`（连接来源地址即客户端）；内置栈将其设为内置 nginx 的地址 `["10.213.47.2/32"]`。见[代理后面的客户端地址](#client-addresses-behind-a-proxy)。 |
| `TRUSTED_PROXY_HOPS` | Specivo 前面串联了几层代理。默认 `1`。 |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_TLS` / `SMTP_USER` / `SMTP_PASSWORD` | 外发邮件（可选）。 |
| `SMTP_MESSAGE_ID_DOMAIN` | 外发邮件 `Message-ID` 和 `List-Id` 中的域名：必须是纯主机名，例如 `mail.example.com`；其他任何值都会导致 Specivo 无法启动。默认：`SMTP_FROM` 的域名。 |
| `MARKDOWN_RENDER_DEADLINE_SECONDS` | 单个 Markdown 字段（描述、评论、维基页面）渲染可用的 CPU 秒数，超出后改为按纯文本显示。实际耗时也不得超过该值的 4 倍。这是一道兜底防线：渲染会很慢的文本通常在渲染之前就已被拦下。因为计的是 CPU 时间，主机繁忙不会触发它。默认 `2`，并按 `MARKDOWN_RENDER_SPEED_FACTOR` 缩放；现实中最大的页面在快速机器上也不到半秒。见[大小和复杂度限制](../reference/markdown.md#size-and-complexity-limits)。 |
| `MARKDOWN_RENDER_REQUEST_BUDGET_SECONDS` | 一个页面或一次 API 响应中所有 Markdown 字段合计可用的 CPU 秒数（描述、它的评论及其历史记录）。用完后，剩余字段按纯文本显示。默认 `5`，并按 `MARKDOWN_RENDER_SPEED_FACTOR` 缩放。 |
| `MARKDOWN_RENDER_SPEED_FACTOR` | 上面两个设置以当前的快速机器为基准。Specivo 会在每个进程首次使用时测量本机渲染 Markdown 慢了多少（以 INFO 级别记录为 “Markdown render speed factor”），并把两个限制都乘以该系数（1 到 20 之间），这样大页面在慢速或低功耗硬件上也能正常渲染。设置固定值可覆盖测量结果，例如 `1` 关闭缩放。默认：自动测量。 |

## 机密（`.env.local`） {#secrets-envlocal}

!!! warning "设置强 SECRET_KEY 并检查注册模式"
    默认的 `SECRET_KEY` 并不安全，它只是为了让栈能在首次运行时启动。在把 Specivo 开放给任何人之前，先在 `.env.local` 中设置一个强随机的唯一值：

    ```bash
    # .env.local
    SECRET_KEY=a-long-random-string-you-generated
    ```

    同时检查 `REGISTRATION_MODE`。设为 `open` 时，任何能访问该 URL 的人都可以创建账户。私有部署请改为封闭模式，通过邀请添加用户。

## 外部数据库和 Redis {#external-database-and-redis}

内置的 `db` 和 `redis` 服务用起来方便，但不是必需的。要改用托管服务，把 URL 指向它们即可：

```bash
# .env
DATABASE_URL=postgresql+asyncpg://user:pass@db.example.com:5432/specivo
REDIS_URL=redis://cache.example.com:6379/0
```

你的 PostgreSQL 必须能启用 **pgvector** 扩展，因为搜索依赖它。请使用 pgvector **0.8 或更高版本**：语义搜索只在 0.8 的迭代索引扫描下才使用向量索引，在更早版本上会逐一比较每个向量嵌入（内置的 `pgvector/pgvector:pg18` 镜像自带 0.8）。URL 指向别处之后，可以从 `docker-compose.yml` 中删除 `db` 和 `redis` 服务——它们被声明为可选依赖，没有它们应用也能启动。

## 数据目录 {#the-data-directory}

所有需要在容器重启后保留的数据都放在 `SPECIVO_DATA_DIR`（默认 `./specivo-data`）下：

- `postgresql/`：内置数据库的文件
- 上传到工作项（Issue）、维基页面和评论的附件
- `logs/`：应用和 nginx 日志
- 主题，以及嵌入模型（如果你下载了）

备份这个目录时，连同数据库转储一起备份。见[备份与恢复](backup-restore.md)。

## 邮件（SMTP） {#email-smtp}

配置 SMTP 后，Specivo 可以发送通知（提及、关注者更新、密码重置）。不配邮件也完全可以用，这些通知只是只在应用内显示。

```bash
# .env
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_TLS=true
SMTP_USER=notifications@example.com
SMTP_PASSWORD=...      # put the password in .env.local
```

同一个工作项的所有邮件主题相同，形如 `[KEY-123] issue subject`，并归属同一个会话线程，因此邮件客户端会把它们归为一组；每封邮件的第一行说明发生了什么（“由……指派给你”、“……发表了评论”）。每封邮件都带有 `Message-ID`、`Date` 和 `Auto-Submitted: auto-generated`，所以不会收到外出自动回复。与工作项相关的邮件还带有 `X-Specivo-Issue`、`X-Specivo-Project` 和 `X-Specivo-Event`（`assignment`、`updated`、`comment` 或 `mention`），以及每个项目各自的 `List-Id`，便于编写过滤规则。

`Message-ID` 以 `SMTP_FROM` 的域名结尾。要改用其他域名（例如发件地址属于共享邮件服务商的域名时），设置：

```bash
# .env
SMTP_MESSAGE_ID_DOMAIN=specivo.example.com
```

值必须是纯主机名（字母、数字、连字符和点）；设为其他任何值，Specivo 都会拒绝启动。如果未设置，而 `SMTP_FROM` 的域名又不是纯主机名，则使用 `APP_URL` 的主机名。

## 代理后面的客户端地址 {#client-addresses-behind-a-proxy}

Specivo 在以下场景使用客户端 IP 地址：按地址限流（登录、单点登录、匿名读取）、每个会话上显示的地址、API 密钥的 IP 白名单，以及安全审计日志。在反向代理后面，连接来自代理。每层代理都会把它收到请求的来源地址追加到 `X-Forwarded-For` 头，所以客户端地址就在这个头里。两个设置决定 Specivo 相信什么：

- **`TRUSTED_PROXIES`**：连接到 Specivo 的代理地址。只有来自这些地址的连接才能指明客户端。来自其他任何地方的连接本身就是客户端，无论它发送什么头。默认 `[]`：不信任任何代理。
- **`TRUSTED_PROXY_HOPS`**：Specivo 前面串联了几层代理。客户端就是 `X-Forwarded-For` 中从右往左数第这么多个的条目。它左边的内容全是客户端写的，会被忽略，所以没人能通过发送这个头来伪造自己的地址。默认 `1`。

请填写代理的确切地址，而不是整个私有网段：受信任网段内的任何机器都能指明任意客户端地址。

### 内置栈 {#the-bundled-stack}

`docker-compose.yml` 把 nginx 和 api 放在独立的 `proxy` 网络（`10.213.47.0/29`）中，nginx 使用固定地址 `10.213.47.2`，并设置 `TRUSTED_PROXIES=["10.213.47.2/32"]`。Docker 只从 `10.213.47.4/30` 分配自动地址（例如 api 的地址），所以永远不会与 nginx 的地址冲突。这个网络上没有其他东西，api 不发布任何端口，nginx 会追加它看到的地址，因此无需改动即可正常工作。如果该网段在你的主机上已被占用（`docker compose up` 报错 “Pool overlaps”），在 `.env` 中换一个网段。三个值要一起改，并保证 nginx 地址在子网内、但在自动分配范围之外：

```bash
# .env
SPECIVO_PROXY_SUBNET=10.99.0.0/29
SPECIVO_PROXY_IP_RANGE=10.99.0.4/30
SPECIVO_NGINX_IP=10.99.0.2
```

除非你自己设置，`TRUSTED_PROXIES` 会跟随 `SPECIVO_NGINX_IP`。这些变量要放在 `.env` 而不是 `.env.local`：compose 文件从 `.env` 读取它们。

### nginx 前面还有一层代理 {#another-proxy-in-front-of-nginx}

当 HTTPS 终结器或 CDN 把请求转发给内置 nginx 时，就有两层串联的代理。外层写入客户端地址，nginx 再追加外层代理的地址，因此设置：

```bash
# .env
TRUSTED_PROXY_HOPS=2
```

- **Caddy**：`reverse_proxy` 会把 `X-Forwarded-For` 设为客户端地址，并且不会传递客户端自己发送的头，除非你配置了 Caddy 自己的 `trusted_proxies`。
- **Traefik**：追加客户端地址。入口点上的 `forwardedHeaders.insecure` 保持关闭，这样客户端发送的头不会被当作 Traefik 写入的内容原样传递。
- **Cloudflare**：追加访客地址。此时 nginx 必须只接受来自 Cloudflare 的连接（用防火墙把发布的端口限制在 Cloudflare 的地址段），因为绕过 Cloudflare 的请求可以在多出来的那个条目里填任何内容。

如果设 `TRUSTED_PROXY_HOPS=1` 却有外层代理，所有客户端都会显示为外层代理的地址：安全，但所有人共用一个限流桶。如果跳数设得比实际代理层数多，客户端就能伪造自己的地址。请准确计数。

### 不使用内置 nginx {#without-the-bundled-nginx}

如果你自己的代理（nginx、Caddy、Traefik）直接连接 api，把 `TRUSTED_PROXIES` 设为该代理的地址，并保持 `TRUSTED_PROXY_HOPS=1`。对 nginx 而言，常用的 `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;` 就是正确的：它会追加客户端地址。

```bash
TRUSTED_PROXIES=["192.0.2.10/32"]
```

## 代码仓库集成与 Webhook {#repository-integrations-and-webhooks}

[代码仓库集成](../integrations/repositories.md)在 `APP_URL` 后接 `/hooks/...`（如果设置了 `STEALTH_PREFIX`，则在其之后）的地址上接收来自 GitHub、GitLab、Gitea 和 Forgejo 的 webhook，所以 `APP_URL` 必须是代码托管平台能访问到的地址。它们和[出站 Webhook](../integrations/webhooks.md)都在后台容器中工作（`celery-worker`，并由 `celery-beat` 补上遗漏的任务）：这些容器停止期间，入站投递以 *已接受* 状态等待，出站投递以 *待处理* 状态等待，容器恢复运行后都会被处理。

| 变量 | 用途 / 默认值 |
|---|---|
| `GIT_HOOK_MAX_BODY_BYTES` | 代码仓库集成接受的最大 webhook 请求体；更大的请求返回 413。默认 `5242880`（5 MB），最小 `1024`。 |
| `GIT_HOOK_RATE_LIMIT_PER_MINUTE` | 单个代码仓库集成每分钟可接收的 webhook 请求数，超出后 Specivo 返回 429。默认 `120`。 |
| `GIT_HOOK_UNKNOWN_RATE_LIMIT_PER_MINUTE` | 单个客户端地址每分钟可向不匹配任何集成、或签名错误的 hook URL 发送的请求数。默认 `30`。 |
| `WEBHOOK_TIMEOUT` | 出站 webhook 请求每个阶段（连接、发送、读取响应）允许的秒数。默认 `30`；一次完整尝试始终在 60 秒后被中止。 |

## 单点登录：恢复登录 {#single-sign-on-getting-back-in}

身份提供商（IdP）在 **管理 → 单点登录** 中配置，登录页设置在 **管理 → 设置 → 认证** 中。这些设置可以隐藏密码表单、让登录页直接跳转到某个提供商，以及限制只有管理员能用密码登录。但没有任何一项会把管理员锁在外面：

- 隐藏密码表单后，登录页仍会显示一个小小的 **使用本地账户登录** 链接。它打开 `/login/?local=1`，该页面始终显示表单且不会跳转。
- 如果身份提供商不可用，登录页上的错误信息会链接到同一个表单。
- 如果 SSO 设置本身出了问题（提供商配置损坏、所有管理员都被拒绝），打开下面的环境开关。它会覆盖上述三项设置，直到你再把它关掉：

  ```bash
  # .env
  SSO_FORCE_LOCAL_LOGIN=true
  ```

  ```bash
  docker compose up -d   # recreates the containers with the new value
  ```

  开关打开期间，Specivo 启动时会记录一条警告。修好设置后，删掉这一行并再次重建容器。
- 如果没有管理员知道密码，在主机上用 `make reset-password login=... password=...` 设置一个，或用 `make create-admin login=... email=... password=...` 创建新管理员。

| 变量 | 用途 / 默认值 |
|---|---|
| `SSO_FORCE_LOCAL_LOGIN` | 应急开关：始终显示密码表单，登录页从不跳转到提供商，并允许所有用户用密码登录。默认 `false`。 |
| `SSO_IDP_RECHECK_MINUTES` | 续期 SSO 会话时，最多每隔多少分钟向身份提供商确认一次此人是否仍可登录（按会话计）。默认：访问令牌有效期，即 `ACCESS_TOKEN_EXPIRE_MINUTES`（15）。`0` 表示每次续期都确认。见[会话检查](single-sign-on/index.md#the-session-check)。 |

其他 SSO 变量见[单点登录](single-sign-on/index.md#settings-in-the-environment)。

## 导入的密码 {#imported-passwords}

只有当你从其他系统导入账户并让他们继续使用原密码时，这些设置才有意义。导入包可以携带旧的密码哈希（见[从其他系统导入](importing.md)）。另见[编写密码哈希插件](password-hasher-plugins.md)。

| 变量 | 用途 / 默认值 |
|---|---|
| `LEGACY_PASSWORD_ALGORITHMS` | 允许登录的导入密码格式，例如 `["redmine_sha1"]`。默认 `[]`：不允许任何格式。 |
| `LEGACY_PASSWORD_HASHES_EXPIRE_AT` | 过了这个时刻，所有导入的密码都失效，相关用户需通过密码找回重设密码，例如 `2027-03-31T00:00:00Z`。默认未设置。 |
| `PASSWORD_HASHERS` | 额外的哈希器类，以点分路径给出，例如 `["acme_hashers.AcmeTrackerMd5"]`。 |
| `INSTALLED_PLUGINS` | 以点分路径列出的插件；插件可以提供密码哈希器。 |

## 语义搜索模型 {#semantic-search-model}

混合搜索和语义搜索按语义匹配，这需要一个嵌入模型。Specivo 在你自己的 CPU 上运行 `multilingual-e5-small`（约 487 MB，384 维向量，支持约 100 种语言）。它不包含在 Docker 镜像中，也不会自动下载。在栈运行时下载一次即可：

```bash
docker compose exec api python -m specivo.cli.download_model   # or: make download-model
```

模型保存到 `SPECIVO_DATA_DIR/models`，每个文件都会与公布的校验和核对。见[语义搜索与向量嵌入](semantic-search.md#turn-it-on)。

没有专门的开关：模型文件在 `SPECIVO_DATA_DIR/models` 里，功能就是开启的。没有模型文件时，关键词搜索和其他一切功能照常工作，混合搜索的行为与关键词搜索相同。只支持这个内置的本地模型：没有托管的嵌入服务商，没有自带密钥（BYOK）选项，也不能切换到其他模型（见[更换模型](semantic-search.md#changing-the-model)）。

| 变量 | 用途 / 默认值 |
|---|---|
| `EMBEDDING_ONNX_THREADS` | 模型可使用的 CPU 线程数。默认 `2`；`0` 使用全部核心。 |
| `EMBEDDING_WORKER_CONCURRENCY` | `celery-embeddings` 容器中的进程数。默认 `1`；每个进程各自加载一份模型。 |
| `EMBEDDING_QUEUE` | 嵌入任务使用的 Celery 队列。默认 `embeddings`，由 `celery-embeddings` 消费；设为 `celery` 则改由 `celery-worker` 执行。 |
| `SEARCH_INDEX_COMMENTS` | 为评论建立搜索索引。默认 `true`。 |
| `SEARCH_MIN_COMMENT_LENGTH` | 短于这么多字符的评论不建索引。默认 `20`。 |
| `SEARCH_EXCLUDE_BOT_COMMENTS` | 跳过通过 API 密钥或由服务账户发表的评论。默认 `true`。 |
| `SEARCH_HNSW_EF_SEARCH` | pgvector 每次向量索引扫描的候选列表大小（`hnsw.ef_search`，10 到 1000）。值越大，越能可靠地找到真正最近的匹配，但稍慢。默认 `100`。 |
| `SEARCH_SEMANTIC_OVERFETCH` | 每个期望结果从向量索引读取的最近分块数，在剔除读者无权查看的结果之前计算（1 到 50）。默认 `4`。见[向量索引](semantic-search.md#vector-index)。 |

关于语义搜索的用途、现有内容如何建立索引、如何确认它正常工作以及故障排查，见[语义搜索与向量嵌入](semantic-search.md)。离线主机见[离线部署](air-gapped.md#semantic-search-offline)。
