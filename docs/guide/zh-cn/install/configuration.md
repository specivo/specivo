---
description: "了解如何通过环境变量配置 Specivo：.env 与 .env.local 的区别、关键环境变量、外部数据库和 Redis、数据目录、SMTP 以及语义搜索的嵌入模型。"
---

# 配置 {#configuration}

Specivo 通过环境变量配置。Docker 部署栈会从仓库根目录读取两个文件，两者都不是启动所必需的：在你覆盖之前，一直使用合理的默认值。

## `.env` 与 `.env.local` {#env-vs-envlocal}

- **`.env`** 存放设置：端口、镜像版本、数据目录、注册模式。由 `configure.py` 生成的 `.env` 还包含数据库 URL 和密码，所以生成的 `.env` 同样要当作机密对待。
- **`.env.local`** 存放机密信息，最重要的是 `SECRET_KEY`。不要把它提交到版本控制。

从示例文件开始：

```bash
cp .env.example .env
```

也可以让辅助脚本生成这两个文件（包括一个强随机的 `SECRET_KEY`）：

```bash
make configure          # or: python scripts/configure.py
```

!!! note "用同一个用户运行 `configure.py` 和 Docker Compose"
    `configure.py` 创建的 `.env` 和 `.env.local` 只有文件所有者可读写（权限 `0600`）：`.env` 里有数据库密码，`.env.local` 里有 `SECRET_KEY`。Docker Compose 以运行它的用户身份读取这两个文件，所以请用同一个用户运行 `configure.py` 和 `docker compose`（或 `make up`），或者两者都用 `sudo`。如果你以一个用户创建了文件，却以另一个用户启动部署栈，`docker compose` 将无法读取它们；此时用 `sudo chown <user> .env .env.local` 更改文件所有者，而不要放宽权限。

修改任一文件后，运行以下命令使其生效：

```bash
docker compose up -d
```

## 关键变量 {#key-variables}

| 变量 | 用途 / 默认值 |
|---|---|
| `COMPOSE_PROJECT_NAME` | 隔离一个实例（容器、网络、数据卷），以便在同一台主机上运行多个实例。默认 `specivo`。 |
| `SPECIVO_PORT` | 对外的 nginx 端口。默认 `9933`。 |
| `SPECIVO_DATA_DIR` | 持久化数据目录：数据库文件、日志、附件、主题。默认 `./specivo-data`。 |
| `SPECIVO_VERSION` | 镜像标签：`latest`，或固定到某个发布版本，例如 `0.12.0`。 |
| `SPECIVO_IMAGE` | 镜像拉取来源。默认 `specivo/specivo`（Docker Hub）。设为 `ghcr.io/specivo/specivo` 可从 GitHub Container Registry 拉取同一镜像，例如在 Docker Hub 被屏蔽的环境中。 |
| `DATABASE_URL` | `postgresql+asyncpg://specivo:specivo@db:5432/specivo`。指向外部数据库即可使用你自己的数据库。 |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | 内置 Postgres 的凭据。 |
| `REDIS_URL` | `redis://redis:6379/0`。如需使用外部 Redis，指向它即可。 |
| `APP_URL` | Specivo 的公开地址，例如 `https://specivo.example.com`。邮件中的链接、代码仓库 Webhook URL 和[单点登录](single-sign-on/index.md)（SSO）地址都由它生成。参见[公开地址（`APP_URL`）](#public-address-app_url)。 |
| `SECRET_KEY` | 用于签名会话和令牌。生产环境**必须**设为强随机的唯一值。放在 `.env.local` 中。 |
| `DEBUG` | 生产环境为 `false`。 |
| `REGISTRATION_MODE` | `open` 允许任何人自行注册。要求邀请制时使用封闭模式。 |
| `CAPTCHA_ENABLED` | 在注册表单上启用验证码。 |
| `SEARCH_ENGINE_INDEXING` | 允许搜索引擎索引无需账户即可阅读的页面。默认 `false`。**管理 → 设置 → 匿名访问** 中的复选框会覆盖它。参见[搜索引擎](#search-engines)。 |
| `ROBOTS_TXT` | 固定的 `robots.txt`，替换 Specivo 自动生成的那份。默认未设置。管理设置 `robots_txt` 会覆盖它。参见[搜索引擎](#search-engines)。 |
| `CORS_ORIGINS` | 允许的浏览器来源，例如 `["https://specivo.example.com"]`。 |
| `AUDIT_LOG_RETENTION_MONTHS` | [安全审计日志](audit-log.md)在当前月份之前保留的整月数；更早的月份每天清理一次。默认 `12`。`0` 表示全部保留。 |
| `TRUSTED_PROXIES` | 允许在 `X-Forwarded-For` 中指明客户端的反向代理地址，以 JSON 列表给出地址或网段。默认 `[]`（连接的来源地址即客户端）；内置部署栈设为内置 nginx 的地址 `["10.213.47.2/32"]`。参见[代理后的客户端地址](#client-addresses-behind-a-proxy)。 |
| `TRUSTED_PROXY_HOPS` | Specivo 前面串联了几层代理。默认 `1`。 |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_TLS` / `SMTP_USER` / `SMTP_PASSWORD` | 外发邮件（可选）。 |
| `SMTP_MESSAGE_ID_DOMAIN` | 外发邮件 `Message-ID` 和 `List-Id` 中的域名：必须是纯主机名，例如 `mail.example.com`；任何其他值都会导致 Specivo 无法启动。默认：`SMTP_FROM` 的域名。 |
| `MARKDOWN_RENDER_DEADLINE_SECONDS` | 单个 Markdown 字段（描述、评论、维基页面）渲染时可用的 CPU 秒数，超出后改为按纯文本显示。实际耗时也不得超过该值的 4 倍。这是一道兜底防线：渲染会很慢的文本在渲染完成前就会被截住。计量的是 CPU 时间，所以主机繁忙不会触发它。默认 `2`，并按 `MARKDOWN_RENDER_SPEED_FACTOR` 缩放；实际中最大的页面在快速机器上也用不到半秒。参见[大小和复杂度限制](../reference/markdown.md#size-and-complexity-limits)。 |
| `MARKDOWN_RENDER_REQUEST_BUDGET_SECONDS` | 一个页面或一次 API 响应中所有 Markdown 字段（描述、其评论和历史记录）合计可用的 CPU 秒数。用完之后，剩余字段按纯文本显示。默认 `5`，并按 `MARKDOWN_RENDER_SPEED_FACTOR` 缩放。 |
| `MARKDOWN_RENDER_SPEED_FACTOR` | 上述两项设置以一台当前的快速机器为基准。Specivo 会测量本机渲染 Markdown 比它慢多少（每个进程在首次使用时测一次，以 INFO 级别记录为 “Markdown render speed factor”），并把两个上限乘以该系数（1 到 20 之间），这样大页面在慢速或低功耗硬件上也能渲染出来。设一个固定值可覆盖测量结果，例如 `1` 表示关闭缩放。默认：自动测量。 |

## 机密信息（`.env.local`） {#secrets-envlocal}

!!! warning "设置强 SECRET_KEY 并检查注册模式"
    默认的 `SECRET_KEY` 并不安全，它只是为了让部署栈能在首次运行时启动。在把 Specivo 开放给任何人之前，先在 `.env.local` 中设置一个强随机的唯一值：

    ```bash
    # .env.local
    SECRET_KEY=a-long-random-string-you-generated
    ```

    同时检查 `REGISTRATION_MODE`。设为 `open` 时，任何能访问该 URL 的人都能创建账户。私有部署请切换到封闭模式，改为邀请成员加入。

## 外部数据库和 Redis {#external-database-and-redis}

内置的 `db` 和 `redis` 服务用起来方便，但并非必需。要改用托管服务，把 URL 指向它们即可：

```bash
# .env
DATABASE_URL=postgresql+asyncpg://user:pass@db.example.com:5432/specivo
REDIS_URL=redis://cache.example.com:6379/0
```

你的 PostgreSQL 必须提供 **pgvector** 扩展，因为搜索依赖它。请使用 pgvector **0.8 或更高版本**：语义搜索只有借助 0.8 的迭代式索引扫描才会使用向量索引，在更早的版本上则会逐一比较每个向量嵌入（内置的 `pgvector/pgvector:pg18` 镜像自带 0.8）。把 URL 指向别处之后，就可以从 `docker-compose.yml` 中删除 `db` 和 `redis` 服务：它们被声明为可选依赖，没有它们应用也能启动。

它还需要 **pg_trgm** 扩展和 **btree_gist** 扩展：前者用于相似工作项（Issue）建议，后者用于防止 [AI 模型价格](ai-model-prices.md)的有效期互相重叠。两者都随 PostgreSQL 本身提供。数据库迁移会像创建 pgvector 一样创建它们，创建不了就会中止：

- 两者都是受信任扩展（PostgreSQL 13 及更高版本），因此对数据库拥有 `CREATE` 权限的角色就能安装，不需要超级用户。
- AWS RDS 的受支持扩展列表中包含这两个扩展。
- 在 Google Cloud SQL 上，角色需要 `cloudsqlsuperuser`。
- 在 Azure Database for PostgreSQL 灵活服务器上，必须先把扩展加入 `azure.extensions` 服务器参数；迁移前请检查该列表中是否有 `pg_trgm` 和 `btree_gist`。

## 数据目录 {#the-data-directory}

所有需要在容器重启后保留的数据都位于 `SPECIVO_DATA_DIR`（默认 `./specivo-data`）下：

- `postgresql/`：内置数据库的文件
- 上传到工作项、维基（Wiki）页面和评论的附件
- `logs/`：应用和 nginx 日志
- 主题，以及嵌入模型（如果你下载了它）

请把这个目录和数据库转储一起备份。参见[备份与恢复](backup-restore.md)。

## 邮件（SMTP） {#email-smtp}

配置 SMTP 后，Specivo 可以发送通知（提及、关注者更新、密码重置）。不配置邮件也完全可以运行，这些通知只是留在应用内。

```bash
# .env
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_TLS=true
SMTP_USER=notifications@example.com
SMTP_PASSWORD=...      # put the password in .env.local
```

关于同一个工作项的所有邮件主题相同（`[KEY-123] issue subject`，即工作项编号加主题），并归属同一个会话线程，所以邮件客户端会把它们归入一个对话；每封邮件的第一行说明发生了什么（“由……指派给你”“……发表了评论”）。每封邮件都带有 `Message-ID`、`Date` 和 `Auto-Submitted: auto-generated`，因此不会收到自动回复的外出邮件。关于工作项的邮件还带有 `X-Specivo-Issue`、`X-Specivo-Project` 和 `X-Specivo-Event`（`assignment`、`updated`、`comment` 或 `mention`），以及每个项目各自的 `List-Id`，方便设置过滤规则。

每个人收到的邮件都使用自己的语言，也就是 **偏好设置** 中选定的语言；如果该语言不可用，则使用实例的默认语言。邮件中的时间按收件人的时区显示。邮件末尾附有指向工作项或评论的链接，由 [`APP_URL`](#public-address-app_url) 生成。如果 `APP_URL` 为空，则不附链接。如果它仍指向 `localhost`，链接也会指向 `localhost`，并且 Specivo 会在启动时记录一条警告。

邮件只发给能打开该工作项的人，所以私有工作项的主题不会泄露给其他任何人。被 `@username` 提及的人会收到一封提及邮件；如果此人同时关注该工作项或是其负责人，那么关于这条评论他只会收到这一封邮件。

`Message-ID` 以 `SMTP_FROM` 的域名结尾。要改用别的域名（例如发件地址位于共享邮件服务商的域名下时），设置：

```bash
# .env
SMTP_MESSAGE_ID_DOMAIN=specivo.example.com
```

该值必须是纯主机名（字母、数字、连字符和点）；否则 Specivo 会拒绝启动。如果未设置，且 `SMTP_FROM` 的域名不是纯主机名，则使用 `APP_URL` 的主机名。

## 公开地址（APP_URL） {#public-address-app_url}

Specivo 对外发出的每个地址都由 `APP_URL` 生成：邮件中的链接、[代码仓库集成](../integrations/repositories.md)的 Webhook URL、[单点登录](single-sign-on/index.md)提供商的重定向 URI 和后端通道登出 URI，以及其 [SCIM](single-sign-on/scim.md) 基础 URL。它从不使用浏览器访问时所用的地址，因为客户端可以伪造该地址。把它设为用户和其他系统访问 Specivo 时使用的协议、主机名和端口（非默认端口时），不包含 `STEALTH_PREFIX`，然后重启：

```bash
# .env
APP_URL=https://specivo.example.com
```

默认值 `http://localhost:8030` 只在运行 Specivo 的那台机器上可用。只要 `APP_URL` 为空或指向本机（`localhost` 或 `*.localhost` 名称、`127.0.0.1` 或其他回环地址、`::1`、`0.0.0.0`）：

- 展示这些 URL 供复制的页面（项目的 **设置 → 代码仓库**，以及单点登录提供商的 **连接** 和 **预配（SCIM）** 区域）会显示警告并给出当前值；
- 在 `DEBUG=false` 时，Web 应用和后台 worker 启动时各记录一条警告。

当 `APP_URL` 是真实地址，但你通过另一个主机名打开了页面时，这些页面同样会给出提示：你复制的 URL 使用的是 `APP_URL` 的主机名，它未必就是能访问本实例的那个。

## 搜索引擎 {#search-engines}

默认情况下搜索引擎不会索引任何内容。每个响应都带有 `X-Robots-Tag: noindex, nofollow, noarchive`，`/robots.txt` 的内容是 `Disallow: /`，`/sitemap.xml` 返回 404。

索引需要主动开启，而且只覆盖无账户访客本来就能阅读的页面。页面在同时满足以下条件时才可被索引：

1. **搜索引擎索引已开启**：勾选 **管理 → 设置 → 匿名访问** 中的 **允许搜索引擎索引公开页面** 复选框，或设置 `SEARCH_ENGINE_INDEXING=true`。复选框优先于环境变量。
2. **匿名访问已开启**（同一卡片中的开关）。
3. **项目是公开的、活跃的，并且已针对工作项、维基或两者启用匿名访问**。
4. **页面是在无账户状态下访问的**：项目列表、项目页面、其工作项列表、工作项页面和维基页面。

无论设置如何都保持 `noindex` 的内容：私有项目、未启用匿名访问的项目、已归档项目、私有工作项和评论、已登录访客看到的任何内容、登录和密码页面、错误页面和重定向、管理页面、JSON API 和 MCP。搜索结果页，以及任何带查询字符串（筛选、排序、分页）打开的公开页面，返回 `noindex, follow`：爬虫可以跟随其中的链接，但不索引这些页面本身。可索引的页面仍保留 `noarchive` 和 `Cache-Control: no-store`，并带有由 [`APP_URL`](#public-address-app_url) 生成的 `<link rel="canonical">`；工作项和维基页面还会从正文生成一段简短的 `<meta name="description">`。

在索引生效且至少有一个项目可读时：

- **`/robots.txt`** 允许 `/projects/`、可读项目的页面（`/projects/KEY/`）和工作项（`/issue/KEY-`），禁止其他所有路径，并给出站点地图地址。
- **`/sitemap.xml`** 是一个站点地图索引。它指向 `/sitemaps/projects.xml`（项目列表、项目页面和工作项列表），以及按项目划分的工作项和维基页面站点地图，每个最多 5,000 个 URL（`/sitemaps/KEY/issues-N.xml`、`/sitemaps/KEY/wiki-N.xml`）。其中只列出无账户访客可以打开的工作项和维基页面，并附最后修改时间。链接由 `APP_URL` 生成，所以请先把它设为公开地址。

更改在下一次请求时生效：关闭匿名访问或索引，或者把某个项目从匿名访问中移除，都会立即将其从 `robots.txt` 和站点地图中去掉，并恢复 `noindex`。搜索引擎则会按自己的节奏移除已索引的页面。

设置了 `STEALTH_PREFIX` 的实例永远不会被索引：`robots.txt` 和站点地图在根路径提供，会暴露该前缀。

**让某个公开项目不被搜索引擎收录**。索引是实例级的。要在保持某个项目无需账户即可阅读的同时将其排除在外，可在管理设置 `robots_txt`（或 `ROBOTS_TXT`）中写入自己的 `robots.txt`，替换自动生成的那份，例如：

```text
User-agent: *
Allow: /projects/$
Allow: /projects/ACME/
Allow: /issue/ACME-
Disallow: /

Sitemap: https://specivo.example.com/sitemap.xml
```

站点地图仍会列出所有可读项目；如果在意这一点，就去掉 `Sitemap:` 那一行。

**代码仓库动态**。公开项目的工作项页面可以显示代码仓库动态：提交信息、分支名和拉取请求标题。项目可被索引后，搜索引擎也能抓取这些文本。请在把项目设为公开并开启索引之前想清楚。

## 搜索速率限制 {#search-rate-limits}

每次搜索都会在数据库中执行查询，混合搜索或语义搜索还会在服务器 CPU 上运行[嵌入模型](#semantic-search-model)。因此搜索按客户端地址限速，搜索页面和 REST 搜索端点（`GET /api/v1/search/`）都受限，每种模式、有无账户都一样。无账户的人使用的搜索与其他人相同（默认为混合搜索），但只覆盖无需登录即可阅读的内容。

| 变量 | 用途 / 默认值 |
|---|---|
| `SEARCH_RATE_LIMIT_ANONYMOUS_PER_MINUTE` | 一个客户端地址未登录时每分钟可执行的搜索次数。默认 `10`。 |
| `SEARCH_RATE_LIMIT_PER_MINUTE` | 一个客户端地址登录后每分钟可执行的搜索次数。默认 `60`。设得更高是因为同一地址后面可能是整个办公室在共用。 |
| `SEARCH_QUERY_EMBEDDING_CACHE_SECONDS` | 查询的向量嵌入在 Redis 中保留的秒数，这样重复的查询或结果的下一页就不必再运行模型。默认 `300`；`0` 关闭缓存。 |

这两个配额相互独立，搜索页面和 REST 端点共用同一配额：在两者之间切换不会重新计数。IPv6 客户端按其 /64 计数，与其他按地址的限制一致。只打开搜索页面而不搜索不计数。

超出限制时，搜索页面会显示一个简短页面，说明搜索已暂停以及何时可以重试，状态码为 `429` 并带 `Retry-After` 头（以秒计）。REST API 返回 `429` 及其常规 JSON 错误（代码 `rate_limit_exceeded`）和同一响应头。其他所有受速率限制的网页也以同样方式响应，只是措辞更笼统。

在反向代理后面，请设置 [`TRUSTED_PROXIES`](#client-addresses-behind-a-proxy)，否则所有访客共用代理的配额。如果 Redis 不可用，搜索不受限制，其他速率限制同理；`RATE_LIMIT_ENABLED=false` 则关闭所有速率限制。

## AI 报告速率限制 {#ai-report-rate-limits}

AI 使用图表和支出报告会汇总一个人或一个项目的全部 AI 使用记录并换算金额，因此每个人每分钟都有一个配额。该配额由以下几处共用：我的 AI 使用（其图表）、项目和实例的 AI 支出报告页面、报告 API（`GET /api/v1/projects/{key}/ai-usage/report/` 和 `GET /api/v1/ai-usage/report/`，JSON 和 CSV）以及 MCP 报告工具。

| 变量 | 用途 / 默认值 |
|---|---|
| `AI_REPORT_RATE_LIMIT_PER_MINUTE` | 一个人每分钟可加载的图表和报告数量。默认 `30`。 |
| `AI_REPORT_RATE_LIMIT_ADMIN_PER_MINUTE` | 同上，适用于管理员，他们还会读取实例报告。默认 `120`。 |
| `AI_USAGE_CHART_CACHE_SECONDS` | 同一个人、同一组筛选条件下，我的 AI 使用图表数据在 Redis 中保留的秒数。新增、修改或删除的记录立即可见；其间更改的汇率、价格和套餐会在此时间内体现。默认 `60`；`0` 关闭缓存。 |

超出限制时，页面显示限速页面，API 返回 `429` 和代码 `rate_limit_exceeded`，两者都带 `Retry-After`。无论请求什么范围，图表最多 120 个点（按所示记录的范围，以天、周、月、季或年为单位）；跨度很长的按日或按周分组的支出报告会改为按周或按月分组。

## 代理后的客户端地址 {#client-addresses-behind-a-proxy}

Specivo 在以下场景使用客户端 IP 地址：按地址的速率限制（登录、单点登录、匿名读取、[搜索](#search-rate-limits)）、每个会话上显示的地址、API 密钥的 IP 白名单，以及安全审计日志。在反向代理后面，连接来自代理。每层代理都会把它收到请求的来源地址追加到 `X-Forwarded-For` 头，所以客户端地址就在这个头里。两个设置决定 Specivo 相信什么：

- **`TRUSTED_PROXIES`**：连接到 Specivo 的代理的地址。只有来自这些地址的连接才能指明客户端是谁。来自其他任何地方的连接，无论带什么头，其来源地址本身就是客户端。默认 `[]`：不信任任何代理。
- **`TRUSTED_PROXY_HOPS`**：Specivo 前面串联了几层代理。客户端就是 `X-Forwarded-For` 中从右数第这么多个条目。它左边的所有内容都是客户端写入的，一律忽略，所以没人能通过发送该头来自选地址。默认 `1`。

请列出代理的精确地址，而不是整个私有网段：受信任网段内的任何主机都可以冒充任意客户端地址。

### 内置部署栈 {#the-bundled-stack}

`docker-compose.yml` 把 nginx 和 api 放在独立的 `proxy` 网络（`10.213.47.0/29`）上，nginx 使用固定地址 `10.213.47.2`，并设置 `TRUSTED_PROXIES=["10.213.47.2/32"]`。Docker 只从 `10.213.47.4/30` 自动分配地址（例如给 api），因此永远不会与 nginx 的地址冲突。该网络上没有其他服务，api 不对外发布端口，nginx 会追加它看到的地址，所以无需任何改动即可工作。如果这个网段在你的主机上已被占用（`docker compose up` 报错 “Pool overlaps”），在 `.env` 中换一个。三项要一起改，并保持 nginx 地址在子网内、但在自动分配范围之外：

```bash
# .env
SPECIVO_PROXY_SUBNET=10.99.0.0/29
SPECIVO_PROXY_IP_RANGE=10.99.0.4/30
SPECIVO_NGINX_IP=10.99.0.2
```

除非你自己设置了 `TRUSTED_PROXIES`，否则它会跟随 `SPECIVO_NGINX_IP`。这些变量要放在 `.env` 而不是 `.env.local` 中：compose 文件只从 `.env` 读取它们。

### nginx 前面还有代理 {#another-proxy-in-front-of-nginx}

当 HTTPS 终结器或 CDN 把请求转发给内置 nginx 时，就有了两层串联的代理。外层代理写入客户端地址，nginx 再追加外层代理的地址，所以设置：

```bash
# .env
TRUSTED_PROXY_HOPS=2
```

- **Caddy**：`reverse_proxy` 把 `X-Forwarded-For` 设为客户端地址，不会传递客户端自己发送的头，除非你配置了 Caddy 自己的 `trusted_proxies`。
- **Traefik**：追加客户端地址。不要在入口点上开启 `forwardedHeaders.insecure`，以免客户端发送的头被当作 Traefik 写入的内容原样传递。
- **Cloudflare**：追加访客地址。此时 nginx 必须只接受来自 Cloudflare 的连接（用防火墙把发布的端口限制在 Cloudflare 的地址段内），因为绕过 Cloudflare 的请求可以在多出来的那个条目里填任何内容。

如果设 `TRUSTED_PROXY_HOPS=1` 却有外层代理，所有客户端都会显示为外层代理的地址：安全，但所有人共用一个速率限制桶。如果跳数设得比实际代理层数多，客户端就能自选地址。请准确计数。

### 不使用内置 nginx {#without-the-bundled-nginx}

如果你自己的代理（nginx、Caddy、Traefik）直接连接到 api，把 `TRUSTED_PROXIES` 设为该代理的地址，并保持 `TRUSTED_PROXY_HOPS=1`。对于 nginx，常用的 `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;` 就是正确的：它会追加客户端地址。

```bash
TRUSTED_PROXIES=["192.0.2.10/32"]
```

## MCP 会话 {#mcp-sessions}

通过 MCP（Streamable HTTP，路径 `/mcp/`）连接的 AI 智能体（AI agent）持有一个会话，并在每个请求的 `Mcp-Session-Id` 头中标明。Specivo 把每个会话保存在 Redis 中，因此智能体的电脑休眠、网络连接中断或 Specivo 重启后，会话依然存在：智能体带着会话 ID 回来时，会话照常工作。

| 变量 | 用途 |
|---|---|
| `MCP_SESSION_TTL_SECONDS` | 会话闲置多久后过期。每个请求都会重新计时，所以使用中的会话永远不会过期。默认 `604800`（7 天）。过期后，智能体的下一个请求会得到 HTTP 404，MCP 客户端随即自行打开新会话。 |
| `MCP_SESSION_IDLE_TIMEOUT_SECONDS` | 闲置会话在内存中保留多久。只用于释放内存：下一个请求会从 Redis 重建会话。默认 `3600`（1 小时）。 |

只有当 Redis 把数据持久化到磁盘时，会话才能在 Redis 重启后保留。内置 Redis 会这样做（追加日志文件，AOF）；外部 Redis 若没有持久化，Specivo 会在启动时记录警告。没有持久化时，Redis 重启那一刻已连接的智能体会重新开始新会话。

旧的 SSE 传输（`/mcp/sse/`）不保存会话：事件流断开的智能体重连后可以继续，但在流中断期间发出的请求会失败一次。在支持的情况下，请把智能体配置为 Streamable HTTP（`"type": "http"`，URL 以 `/mcp/` 结尾）。

### Specivo 前面的代理 {#proxies-in-front-of-specivo}

MCP 响应是流式的。内置 nginx 已为此做好配置。如果 Specivo 前面还有其他代理（Caddy、Traefik、负载均衡器、Cloudflare），请确保它对 `/mcp/`：

- **不缓冲响应**。nginx：`proxy_buffering off;`。Specivo 还会发送 `X-Accel-Buffering: no`，基于 nginx 的代理会遵守它。
- **允许长时间的响应**。智能体在连接期间会一直保持一个流打开，Specivo 每 15 秒在流上发送一次保活。读取超时至少设为一分钟即可；内置 nginx 使用 `proxy_read_timeout 24h;`。
- **以 HTTP/1.1 或更高版本连接 Specivo**，并启用 keep-alive。nginx：`proxy_http_version 1.1;` 和 `proxy_set_header Connection "";`。

代理切断流不会结束会话：智能体重连后继续工作。

## 代码仓库集成与 Webhook {#repository-integrations-and-webhooks}

[代码仓库集成](../integrations/repositories.md)在 `APP_URL` 加 `/hooks/...`（若设置了 `STEALTH_PREFIX`，则在其之后）接收来自 GitHub、GitLab、Gitea 和 Forgejo 的 Webhook，所以 `APP_URL` 必须是代码托管平台能访问到的地址。它们和[出站 Webhook](../integrations/webhooks.md) 的工作都在后台容器中完成（`celery-worker`，由 `celery-beat` 补上遗漏的任务）：这些容器停止期间，入站投递停留在 *Accepted*（已接受）状态，出站投递停留在 *Pending*（待处理）状态，容器恢复运行后两者都会被处理。

| 变量 | 用途 / 默认值 |
|---|---|
| `GIT_HOOK_MAX_BODY_BYTES` | 代码仓库集成接受的最大 Webhook 请求体；更大的返回 413。默认 `5242880`（5 MB），最小 `1024`。 |
| `GIT_HOOK_RATE_LIMIT_PER_MINUTE` | 一个代码仓库集成每分钟可接收的 Webhook 请求数，超出后 Specivo 返回 429。默认 `120`。 |
| `GIT_HOOK_UNKNOWN_RATE_LIMIT_PER_MINUTE` | 一个客户端地址每分钟可向不匹配任何集成、或签名错误的 hook URL 发送的请求数。默认 `30`。 |
| `AI_USAGE_INGEST_RATE_LIMIT_PER_MINUTE` | 一个账户每分钟可向 [AI 使用记录上报 API](../ai-agents/reporting-ai-usage.md) 发送的批次数，超出后 Specivo 返回 429。默认 `60`。 |
| `WEBHOOK_TIMEOUT` | 出站 Webhook 请求每个阶段（连接、发送、读取响应）允许的秒数。默认 `30`；一次完整尝试最多 60 秒后一律中断。 |

## 单点登录：确保能重新登录 {#single-sign-on-getting-back-in}

身份提供商（IdP）在 **管理 → 单点登录** 中配置，登录页设置在 **管理 → 设置 → 认证** 中。这些设置可以隐藏密码表单、让登录页直接跳转到某个提供商，以及把密码登录限制为仅管理员可用。它们都不会把管理员锁在外面：

- 密码表单被隐藏时，登录页仍显示一个小的 **使用本地账户登录** 链接。它打开 `/login/?local=1`，该页面始终显示表单，从不重定向。
- 身份提供商宕机时，登录页上的错误信息会链接到同一个表单。
- 如果 SSO 设置本身有误（提供商配置损坏、所有管理员都被拒绝），开启下面的环境开关。它会覆盖上述三项设置，直到你再次关闭它：

  ```bash
  # .env
  SSO_FORCE_LOCAL_LOGIN=true
  ```

  ```bash
  docker compose up -d   # recreates the containers with the new value
  ```

  开启期间 Specivo 会在启动时记录警告。修好设置后，删掉这一行并再次重建容器。
- 如果没有任何管理员知道密码，在主机上用 `make reset-password login=... password=...` 设置一个，或用 `make create-admin login=... email=... password=...` 创建新管理员。

| 变量 | 用途 / 默认值 |
|---|---|
| `SSO_FORCE_LOCAL_LOGIN` | 应急开关：始终显示密码表单，登录页从不重定向到提供商，并允许所有用户用密码登录。默认 `false`。 |
| `SSO_IDP_RECHECK_MINUTES` | 续期 SSO 会话时，最多每隔多少分钟向身份提供商确认一次此人是否仍可登录（按会话计）。默认：访问令牌的有效期 `ACCESS_TOKEN_EXPIRE_MINUTES`（15）。`0` 表示每次续期都确认。参见[会话检查](single-sign-on/index.md#the-session-check)。 |

其他 SSO 变量列在[单点登录](single-sign-on/index.md#settings-in-the-environment)中。

## 导入的密码 {#imported-passwords}

这些设置只在你从其他系统导入账户并让他们继续使用原密码时才有意义。导入包可以携带旧的密码哈希（参见[从其他项目管理系统导入](importing.md)）。另见[编写密码哈希插件](password-hasher-plugins.md)。

| 变量 | 用途 / 默认值 |
|---|---|
| `LEGACY_PASSWORD_ALGORITHMS` | 允许用于登录的导入密码格式，例如 `["redmine_sha1"]`。默认 `[]`：不允许任何格式。 |
| `LEGACY_PASSWORD_HASHES_EXPIRE_AT` | 此时刻之后所有导入的密码失效，相关用户需通过密码找回重设，例如 `2027-03-31T00:00:00Z`。默认未设置。 |
| `PASSWORD_HASHERS` | 按点分路径指定的额外哈希器类，例如 `["acme_hashers.AcmeTrackerMd5"]`。 |
| `INSTALLED_PLUGINS` | 按点分路径指定的插件；插件可以提供密码哈希器。 |

## 汇率源 {#exchange-rate-feed}

| 变量 | 含义 |
|---|---|
| `EXCHANGE_RATE_FEED_ALLOW_PRIVATE_NETWORKS` | 允许可选的每日汇率抓取访问回环地址和私有地址，用于部署在内网的自托管 Frankfurter。默认 `false`：只允许公网地址。链路本地地址和云元数据地址无论如何都会被拒绝。参见[汇率](exchange-rates.md#fetch-the-ecb-rates-automatically-optional)。 |
| `EXCHANGE_RATE_FEED_ALLOW_UNRESOLVED_VIA_PROXY` | 在代理后面时，允许抓取使用一个在本服务器上无法解析的汇率源主机名（域名只能通过代理解析）。此时本机无法检查其地址，所以**你的代理必须对私有地址段和链路本地地址段实施出站限制**。默认 `false`：拒绝这样的主机名。 |

抓取本身由管理员在 **管理 → 汇率** 中开启；它遵守 `HTTPS_PROXY` 和 `NO_PROXY`。

## 语义搜索模型 {#semantic-search-model}

混合搜索和语义搜索按语义匹配，这需要一个嵌入模型。Specivo 在你自己的 CPU 上运行 `multilingual-e5-small`（约 487 MB，384 维向量，约 100 种语言）。它不包含在 Docker 镜像中，也不会自动下载。在部署栈运行时手动下载一次即可：

```bash
docker compose exec api python -m specivo.cli.download_model   # or: make download-model
```

它会保存到 `SPECIVO_DATA_DIR/models`，并把每个文件与公布的校验和比对。参见[语义搜索与向量嵌入](semantic-search.md#turn-it-on)。

没有专门的开关：只要模型文件在 `SPECIVO_DATA_DIR/models` 中，语义搜索就是开启的。没有模型文件时，关键词搜索和其他一切照常工作，混合搜索表现为关键词搜索。只支持这个内置的本地模型：没有托管嵌入服务商，没有自带密钥（BYOK）选项，也无法切换到其他模型（参见[更换模型](semantic-search.md#changing-the-model)）。

| 变量 | 用途 / 默认值 |
|---|---|
| `EMBEDDING_ONNX_THREADS` | 模型可使用的 CPU 线程数。默认 `2`；`0` 使用全部核心。 |
| `EMBEDDING_WORKER_CONCURRENCY` | `celery-embeddings` 容器中的进程数。默认 `1`；每个进程各自加载一份模型。 |
| `EMBEDDING_QUEUE` | 嵌入任务使用的 Celery 队列。默认 `embeddings`，由 `celery-embeddings` 消费；设为 `celery` 则改由 `celery-worker` 执行。 |
| `SEARCH_INDEX_COMMENTS` | 为搜索索引评论。默认 `true`。 |
| `SEARCH_MIN_COMMENT_LENGTH` | 短于此字符数的评论不被索引。默认 `20`。 |
| `SEARCH_EXCLUDE_BOT_COMMENTS` | 跳过通过 API 密钥或由服务账户发表的评论。默认 `true`。 |
| `SEARCH_HNSW_EF_SEARCH` | 每次向量索引扫描时 pgvector 候选列表的大小（`hnsw.ef_search`，10 到 1000）。越大越能可靠地找到真正最近的匹配，速度略慢。默认 `100`。 |
| `SEARCH_SEMANTIC_OVERFETCH` | 每个所需结果从向量索引中读取的最近片段数，在移除读者无权查看的结果之前计算（1 到 50）。默认 `4`。参见[向量索引](semantic-search.md#vector-index)。 |
| `SEARCH_SEMANTIC_MIN_SIMILARITY` | 片段被视为语义匹配所需的最低余弦相似度（-1 到 1）；在混合搜索中只作用于纯按语义找到的匹配。默认未设置，即使用模型自身的最低值（`multilingual-e5-small` 为 `0.82`）；`-1` 关闭此限制。参见[最低相似度](semantic-search.md#minimum-similarity)。 |

语义搜索的用途、现有内容如何索引、如何验证它正常工作以及故障排查，参见[语义搜索与向量嵌入](semantic-search.md)。离线主机请参见[离线部署](air-gapped.md#semantic-search-offline)。
