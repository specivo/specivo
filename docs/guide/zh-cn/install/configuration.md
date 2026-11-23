---
description: 介绍如何配置 Specivo，包括 .env 与 .env.local 的区别、关键环境变量、外部数据库与 Redis、数据目录、SMTP 邮件和嵌入模型。
---

# 配置 {#configuration}

Specivo 通过环境变量配置。Docker 栈会读取仓库根目录下的两个文件，但两者都不是启动所必需的：在你覆盖之前，系统会使用合理的默认值。

## `.env` 与 `.env.local` {#env-vs-envlocal}

- **`.env`** 存放各项设置：端口、镜像版本、数据目录、注册模式。由 `configure.py` 生成的 `.env` 还包含数据库 URL 和密码，因此生成的 `.env` 也要当作机密文件对待。
- **`.env.local`** 存放机密信息，其中最重要的是 `SECRET_KEY`。不要把它纳入版本控制。

可以从示例文件开始：

```bash
cp .env.example .env
```

也可以让辅助脚本生成这两个文件（包括一个高强度的 `SECRET_KEY`）：

```bash
make configure          # or: python scripts/configure.py
```

!!! note "用同一个用户运行 `configure.py` 和 Docker Compose"
    `configure.py` 创建的 `.env` 和 `.env.local` 只有属主可读写（权限 `0600`）：`.env` 中写入数据库密码，`.env.local` 中写入 `SECRET_KEY`。Docker Compose 以运行它的用户身份读取这两个文件，所以要用同一个用户运行 `configure.py` 和 `docker compose`（或 `make up`），或者两者都用 `sudo` 运行。如果用一个用户创建了文件，却用另一个用户启动栈，`docker compose` 将无法读取它们；这时用 `sudo chown <user> .env .env.local` 修改属主，不要放宽文件权限。

修改任一文件后，运行以下命令使其生效：

```bash
docker compose up -d
```

## 关键变量 {#key-variables}

| 变量 | 用途 / 默认值 |
|---|---|
| `COMPOSE_PROJECT_NAME` | 隔离一个实例（容器、网络、数据卷），便于在同一台主机上运行多个实例。默认 `specivo`。 |
| `SPECIVO_PORT` | nginx 对外端口。默认 `9933`。 |
| `SPECIVO_DATA_DIR` | 持久化数据目录：数据库文件、日志、附件、主题。默认 `./specivo-data`。 |
| `SPECIVO_VERSION` | 镜像标签：`latest`，或固定的发布版本，如 `0.12.0`。 |
| `SPECIVO_IMAGE` | 镜像的拉取来源。默认 `specivo/specivo`（Docker Hub）。设为 `ghcr.io/specivo/specivo` 可从 GitHub Container Registry 拉取同一镜像，例如在 Docker Hub 被屏蔽的环境中。 |
| `DATABASE_URL` | `postgresql+asyncpg://specivo:specivo@db:5432/specivo`。要使用自己的数据库，把它指向外部数据库即可。 |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | 内置 Postgres 的凭据。 |
| `REDIS_URL` | `redis://redis:6379/0`。如有需要，可指向外部 Redis。 |
| `APP_URL` | Specivo 的公开地址，例如 `https://specivo.example.com`。邮件中的链接、仓库 Webhook URL 和[单点登录（SSO）](single-sign-on/index.md)地址都由它生成。参见[公开地址（`APP_URL`）](#public-address-app_url)。 |
| `STEALTH_PREFIX` | 一个保密路径，整个实例都在该路径下提供服务，例如 `/x7k2-tracker`。默认不设置。参见[路径前缀（`STEALTH_PREFIX`）](#path-prefix-stealth_prefix)。 |
| `SECRET_KEY` | 用于签名会话和令牌。生产环境中**必须**使用高强度且唯一的值。请放在 `.env.local` 中。 |
| `DEBUG` | 生产环境中为 `false`。 |
| `REGISTRATION_MODE` | `open` 允许任何人自行注册。使用封闭模式则必须凭邀请加入。 |
| `CAPTCHA_ENABLED` | 在注册表单上启用验证码。 |
| `SEARCH_ENGINE_INDEXING` | 允许搜索引擎索引无需账户即可阅读的页面。默认 `false`。**管理 → 设置 → 匿名访问** 中的复选框优先于此变量。参见[搜索引擎](#search-engines)。 |
| `ROBOTS_TXT` | 固定的 `robots.txt` 内容，用来替换 Specivo 自己提供的那份。默认不设置。管理设置 `robots_txt` 优先于此变量。参见[搜索引擎](#search-engines)。 |
| `CORS_ORIGINS` | 允许携带已登录用户的 Cookie 调用 API 的其他浏览器来源，例如 `["https://tools.example.com"]`。默认为空：Specivo 自身的页面无需添加条目。只列出你信任的来源。 |
| `AUDIT_LOG_RETENTION_MONTHS` | [安全审计日志](audit-log.md)在当前月份之前保留的整月数；更早的月份每天清理一次。默认 `12`。`0` 表示全部保留。 |
| `TRUSTED_PROXIES` | 允许在 `X-Forwarded-For` 中指明客户端地址的反向代理地址，格式为由地址或网段组成的 JSON 列表。默认 `[]`（连接的来源地址即客户端地址）；内置栈将其设为内置 nginx 的地址 `["10.213.47.2/32"]`。参见[代理之后的客户端地址](#client-addresses-behind-a-proxy)。 |
| `TRUSTED_PROXY_HOPS` | Specivo 前面串联了几层代理。默认 `1`。 |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_TLS` / `SMTP_USER` / `SMTP_PASSWORD` | 外发邮件（可选）。 |
| `SMTP_MESSAGE_ID_DOMAIN` | 外发邮件 `Message-ID` 和 `List-Id` 中使用的域名：必须是普通主机名，如 `mail.example.com`；其他任何值都会导致 Specivo 无法启动。默认：`SMTP_FROM` 的域名。 |
| `MARKDOWN_RENDER_DEADLINE_SECONDS` | 单个 Markdown 字段（描述、评论、维基（Wiki）页面）渲染时可使用的 CPU 秒数，超出后改为以纯文本显示。实际耗时也不得超过该值的 4 倍。这只是一道兜底措施：渲染会很慢的文本在渲染之前就会被拦下。它统计的是 CPU 时间，所以主机繁忙不会触发它。默认 `2`，并按 `MARKDOWN_RENDER_SPEED_FACTOR` 缩放；在快速的机器上，现实中最大的页面也用不了半秒。参见[大小与复杂度限制](../reference/markdown.md#size-and-complexity-limits)。 |
| `MARKDOWN_RENDER_REQUEST_BUDGET_SECONDS` | 一个页面或一次 API 响应中所有 Markdown 字段（描述及其评论和历史记录）合计可使用的 CPU 秒数。用完后，其余字段以纯文本显示。默认 `5`，并按 `MARKDOWN_RENDER_SPEED_FACTOR` 缩放。 |
| `MARKDOWN_RENDER_SPEED_FACTOR` | 上面两项设置是按当前的快速机器设定的。Specivo 会测量本机渲染 Markdown 慢多少，每个进程在首次使用时测量一次（以 INFO 级别记录为 “Markdown render speed factor”），并把两项限制都乘以该系数（介于 1 到 20 之间），这样大页面在较慢或低功耗的硬件上仍能渲染。设置固定值可覆盖测量结果，例如 `1` 表示关闭缩放。默认：实测值。 |

## 机密信息（`.env.local`） {#secrets-envlocal}

!!! warning "设置高强度的 SECRET_KEY 并检查注册模式"
    默认的 `SECRET_KEY` 并不安全，它的存在只是为了让栈在首次运行时能够启动。在向任何人开放 Specivo 之前，先在 `.env.local` 中填入一个高强度且唯一的值：

    ```bash
    # .env.local
    SECRET_KEY=a-long-random-string-you-generated
    ```

    同时检查 `REGISTRATION_MODE`。设为 `open` 时，任何能访问该 URL 的人都可以创建账户。私有部署应改用封闭模式，通过邀请添加成员。

## 外部数据库与 Redis {#external-database-and-redis}

内置的 `db` 和 `redis` 服务用起来方便，但并非必需。要改用托管服务，把 URL 指向它们即可：

```bash
# .env
DATABASE_URL=postgresql+asyncpg://user:pass@db.example.com:5432/specivo
REDIS_URL=redis://cache.example.com:6379/0
```

你的 PostgreSQL 必须提供 **pgvector** 扩展，因为搜索依赖它。请使用 pgvector **0.8 或更高版本**：语义搜索只有借助 0.8 的迭代索引扫描才会用上向量索引，在更早的版本上则会逐一比对每条向量嵌入（内置的 `pgvector/pgvector:pg18` 镜像自带 0.8）。把 URL 指向别处之后，可以从 `docker-compose.yml` 中移除 `db` 和 `redis` 服务：它们被声明为可选依赖，没有它们应用也能启动。

数据库还需要 **pg_trgm** 扩展（相似工作项（Issue）建议功能用到它）和 **btree_gist** 扩展（用来防止同一 [AI 模型价格](ai-model-prices.md)的各个时段相互重叠）。两者都随 PostgreSQL 本身提供。数据库迁移会像创建 pgvector 一样创建它们，创建不了就会中止：

- 两者都是受信任扩展（PostgreSQL 13 及以上），所以对数据库拥有 `CREATE` 权限的数据库角色就能安装，不需要超级用户。
- AWS RDS 的受支持扩展列表中包含这两个扩展。
- 在 Google Cloud SQL 上，角色需要具备 `cloudsqlsuperuser`。
- 在 Azure Database for PostgreSQL 灵活服务器上，扩展必须先加入服务器参数 `azure.extensions`；迁移之前，先确认该列表中有 `pg_trgm` 和 `btree_gist`。

## 数据目录 {#the-data-directory}

所有需要在容器重启后保留的内容都位于 `SPECIVO_DATA_DIR`（默认 `./specivo-data`）之下：

- `postgresql/`：内置数据库的文件
- 上传到工作项、维基页面和评论的附件
- `logs/`：应用日志和 nginx 日志
- 主题，以及嵌入模型（如果你下载了它）

备份时，除了数据库转储，还要一并备份这个目录。参见[备份与恢复](backup-restore.md)。

## 邮件（SMTP） {#email-smtp}

配置 SMTP 后，Specivo 可以发送通知（提及、关注者更新、密码重置）。不配置邮件也能正常使用，这些通知只会留在应用内。

```bash
# .env
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_TLS=true
SMTP_USER=notifications@example.com
SMTP_PASSWORD=...      # put the password in .env.local
```

同一个工作项的所有邮件主题相同，均为 `[KEY-123] issue subject`，并指向同一个会话线索，因此邮件客户端会把它们归为一个会话；每封邮件的第一行说明发生了什么（“… 已将其指派给你”“… 发表了评论”）。每封邮件都带有 `Message-ID`、`Date` 和 `Auto-Submitted: auto-generated`，所以外出自动回复不会被发回来。关于工作项的邮件还带有 `X-Specivo-Issue`、`X-Specivo-Project` 和 `X-Specivo-Event`（取值为 `assignment`、`updated`、`comment` 或 `mention`），以及按项目区分的 `List-Id`，方便设置过滤规则。

每个人收到的邮件都使用其本人的语言，即 **偏好设置** 中选择的语言；如果该语言不可用，则使用实例的默认语言。邮件中的时间按收件人的时区显示。邮件末尾附有指向工作项或评论的链接，该链接由 [`APP_URL`](#public-address-app_url) 生成。如果 `APP_URL` 为空，则不附链接。如果它仍指向 `localhost`，链接也会指向 `localhost`，并且 Specivo 会在启动时记录一条警告。

邮件只发给能打开该工作项的人，所以私有工作项的主题绝不会被其他人看到。被 `@username` 提及的人会收到一封说明此事的邮件；如果此人同时关注了该工作项或是其负责人，那么关于这条评论，他只会收到这一封邮件。

`Message-ID` 以 `SMTP_FROM` 的域名结尾。要改用其他域名（例如发件地址位于共享邮件服务商的域名下时），可以这样设置：

```bash
# .env
SMTP_MESSAGE_ID_DOMAIN=specivo.example.com
```

该值必须是普通主机名（字母、数字、连字符和点）；其他任何值都会让 Specivo 拒绝启动。如果未设置该值，而 `SMTP_FROM` 的域名又不是普通主机名，则使用 `APP_URL` 中的主机名。

## 公开地址（APP_URL） {#public-address-app_url}

所有离开实例的地址，Specivo 都用 `APP_URL` 生成：邮件中的链接、[仓库集成](../integrations/repositories.md)的 Webhook URL、[单点登录](single-sign-on/index.md)提供商的重定向 URI 和后端通道登出（back-channel logout）URI，以及它的 [SCIM](single-sign-on/scim.md) 基础 URL。Specivo 从不采用浏览器访问时使用的地址，因为客户端可以伪造它。把 `APP_URL` 设为用户和其他系统访问 Specivo 时使用的协议、主机和端口（非默认端口时才需要写），不要带 `STEALTH_PREFIX`，然后重启：

```bash
# .env
APP_URL=https://specivo.example.com
```

默认值 `http://localhost:8030` 只在运行 Specivo 的那台机器上有效。只要 `APP_URL` 为空或指向本机（`localhost` 或 `*.localhost` 形式的名称、`127.0.0.1` 或其他回环地址、`::1`、`0.0.0.0`）：

- 显示这些 URL 供复制的页面（项目的 **设置 → 仓库**，以及单点登录提供商的 **连接** 和 **预配（SCIM）** 区域）会显示一条警告，并给出当前值；
- 在 `DEBUG=false` 时，Web 应用和后台 worker 启动时各记录一条警告。

如果 `APP_URL` 是真实地址，而你是通过另一个主机名打开页面的，上述页面同样会给出提示：你复制的 URL 使用的是 `APP_URL` 中的主机，而它未必能访问到这个实例。

## 路径前缀（STEALTH_PREFIX） {#path-prefix-stealth_prefix}

`STEALTH_PREFIX` 让整个实例在你自选的路径下提供服务，这样探测常见地址（`/login/`、`/api/v1/`、`/static/`）的扫描器将一无所获：

```bash
# .env
STEALTH_PREFIX=/x7k2-tracker
```

它覆盖所有内容：网页及其样式表、脚本、字体和图片，API，[MCP](../ai-agents/connecting.md) 端点，仓库 Webhook，单点登录和 SCIM，健康检查，可安装应用（其起始页和作用范围）以及 API 文档。按上面的设置，用户在 `https://specivo.example.com/x7k2-tracker/` 登录，AI 智能体（AI agent）连接到 `https://specivo.example.com/x7k2-tracker/mcp/`。页面发出的每个链接、表单、重定向和请求都保持在该前缀之下，渲染后的 Markdown 中的链接（工作项引用、`[[wiki links]]` 和附件）也是如此。你在 Markdown 中自己写了路径的链接（`[spec](/issue/ACME-12/)`）同样会加上前缀；从地址栏复制的链接已经带有前缀，则保持原样。

前缀之外的请求只会得到一个光秃秃的 `404 Not Found`，其中既不出现 Specivo，也不出现前缀。只有 `robots.txt` 和站点地图留在根路径下，并且设置了前缀的实例永远不会被索引（参见[搜索引擎](#search-engines)）。

- **格式**。由一个或多个路径段组成，可使用字母、数字、`.`、`_`、`~` 和 `-`。开头的斜杠会自动补上，结尾的斜杠会被去掉。它不能以 Specivo 自身使用的路径开头（`/api`、`/static`、`/projects`、`/login` 等）；设成这样的值，Specivo 不会启动。`APP_URL` 中不要带前缀：Specivo 会自动加上。
- **反向代理**。原样透传路径。代理把 `https://specivo.example.com/x7k2-tracker/...` 以 `/x7k2-tracker/...` 的形式转发给 Specivo；不得去掉或改写前缀。内置的 nginx 已经这样处理，并且从 `.env` 中读取同一个 `STEALTH_PREFIX`，用于它的缓存、健康检查和 MCP 流式传输规则。在 `.env` 中填写时不要带结尾的斜杠。
- **修改前缀**。修改后需要重启。书签、已安装的应用、MCP 客户端设置、仓库 Webhook 以及在身份提供商（IdP）处登记的地址都还保留着旧路径，必须逐一更新。存储在 Markdown 中的链接仍然有效：Specivo 在渲染时才加上前缀。

## 搜索引擎 {#search-engines}

默认情况下，搜索引擎不会索引任何内容。每个响应都带有 `X-Robots-Tag: noindex, nofollow, noarchive`，`/robots.txt` 的内容为 `Disallow: /`，`/sitemap.xml` 返回 404。

索引需要主动开启，并且只涵盖无账户访客本来就能阅读的页面。一个页面要同时满足以下所有条件才可被索引：

1. **已开启搜索引擎索引**：勾选 **管理 → 设置 → 匿名访问** 中的复选框 **允许搜索引擎索引公开页面**，或设置 `SEARCH_ENGINE_INDEXING=true`。复选框优先于环境变量。
2. **已开启匿名访问**（同一卡片中的开关）。
3. **项目是公开的、处于活动状态，并且已选择开放**工作项、维基或两者的匿名访问。
4. **该页面无需账户即可访问**：项目列表、项目页面、项目的工作项列表、工作项页面和维基页面。

无论怎样设置，以下内容始终保持 `noindex`：私有项目、未选择开放的项目、已归档的项目、私有工作项和评论、已登录访客看到的任何内容、登录和密码页面、错误页面和重定向、管理页面、JSON API 和 MCP。搜索结果页面，以及任何带查询字符串（筛选、排序、分页）打开的公开页面，都以 `noindex, follow` 发送：爬虫可以跟踪其中的链接，但不索引这些页面。可索引的页面仍保留 `noarchive` 和 `Cache-Control: no-store`，并带有根据 [`APP_URL`](#public-address-app_url) 生成的 `<link rel="canonical">`；工作项页面和维基页面还会带上一段取自正文的简短 `<meta name="description">`。

当索引生效且至少有一个项目可读时：

- **`/robots.txt`** 允许抓取 `/projects/`、可读项目的页面（`/projects/KEY/`）和工作项（`/issue/KEY-`），禁止抓取其余一切，并给出站点地图的地址。
- **`/sitemap.xml`** 是一个站点地图索引。它指向 `/sitemaps/projects.xml`（项目列表、项目页面和工作项列表），以及按项目划分的工作项和维基页面站点地图，每个最多 5,000 个 URL（`/sitemaps/KEY/issues-N.xml`、`/sitemaps/KEY/wiki-N.xml`）。其中只列出无账户访客能打开的工作项和维基页面，并附上最后修改时间。链接根据 `APP_URL` 生成，所以要先把它设为公开地址。

更改在下一次请求时生效：关闭匿名访问或索引，或者让某个项目不再开放，都会立即把它从 `robots.txt` 和站点地图中移除，并恢复 `noindex`。至于已经索引的页面，搜索引擎会按自己的节奏清除。

设置了 `STEALTH_PREFIX` 的实例永远不会被索引：`robots.txt` 和站点地图在根路径下提供，会暴露前缀。

**让某个公开项目不被搜索引擎收录**。索引是实例级别的设置。要排除某个项目，同时保持它无需账户即可阅读，可以在管理设置 `robots_txt`（或 `ROBOTS_TXT`）中编写自己的 `robots.txt`，它会替换自动生成的那份，例如：

```text
User-agent: *
Allow: /projects/$
Allow: /projects/ACME/
Allow: /issue/ACME-
Disallow: /

Sitemap: https://specivo.example.com/sitemap.xml
```

站点地图仍会列出所有可读项目；如果介意这一点，就去掉 `Sitemap:` 那一行。

**仓库动态**。公开项目的工作项页面可以显示仓库动态：提交信息、分支名和拉取请求标题。项目一旦可被索引，搜索引擎也能抓取到这些文本。请在公开项目并开启索引之前想清楚。

## 搜索速率限制 {#search-rate-limits}

每次搜索都要在数据库中执行查询，混合搜索和语义搜索还要在服务器的 CPU 上运行[嵌入模型](#semantic-search-model)。因此，搜索按客户端地址限速，搜索页面和 REST 搜索端点（`GET /api/v1/search/`）都受限制，适用于所有搜索模式，有无账户都一样。没有账户的人使用的搜索与其他人相同，默认为混合搜索，但只能搜索无需登录即可阅读的内容。

| 变量 | 用途 / 默认值 |
|---|---|
| `SEARCH_RATE_LIMIT_ANONYMOUS_PER_MINUTE` | 同一客户端地址在未登录时每分钟可执行的搜索次数。默认 `10`。 |
| `SEARCH_RATE_LIMIT_PER_MINUTE` | 同一客户端地址在已登录时每分钟可执行的搜索次数。默认 `60`。这个值更高，是因为整个办公室可能共用一个地址。 |
| `SEARCH_QUERY_EMBEDDING_CACHE_SECONDS` | 查询的向量嵌入在 Redis 中保留的秒数，这样重复的查询或翻到下一页结果时不必再次运行模型。默认 `300`；`0` 表示关闭缓存。 |

这两项额度相互独立，而搜索页面和 REST 端点共用额度：从一处换到另一处不会重新计数。IPv6 客户端按其 /64 网段计数，与其他按地址的限制一致。只打开搜索页面而不搜索，不计入次数。

超出限制后，搜索页面会显示一个简短的页面，说明搜索已暂停以及访客何时可以重试，状态码为 `429`，并带有 `Retry-After` 响应头（单位为秒）。REST API 返回 `429` 和它惯用的 JSON 错误，错误码为 `rate_limit_exceeded`，并带有同样的响应头。其他受速率限制的网页也以同样的方式响应，只是措辞更笼统。

位于反向代理之后时，要设置 [`TRUSTED_PROXIES`](#client-addresses-behind-a-proxy)，否则所有访客都会共用代理的额度。如果 Redis 不可用，搜索不受限制，其他速率限制也是如此；`RATE_LIMIT_ENABLED=false` 会关闭所有速率限制。

## AI 报告速率限制 {#ai-report-rate-limits}

AI 使用图表和支出报告需要汇总某个人或某个项目的全部 AI 使用数据并换算金额，因此每个人每分钟有一定的额度。以下各处共用这一额度：“我的 AI 使用”（其中的图表）、项目和实例的 AI 支出报告页面、报告 API（`GET /api/v1/projects/{key}/ai-usage/report/` 和 `GET /api/v1/ai-usage/report/`，JSON 和 CSV）以及 MCP 报告工具。

| 变量 | 用途 / 默认值 |
|---|---|
| `AI_REPORT_RATE_LIMIT_PER_MINUTE` | 每人每分钟可加载的图表和报告数量。默认 `30`。 |
| `AI_REPORT_RATE_LIMIT_ADMIN_PER_MINUTE` | 针对管理员的同类限制，管理员还会查看实例报告。默认 `120`。 |
| `AI_USAGE_CHART_CACHE_SECONDS` | “我的 AI 使用”图表数据针对同一个人和同一组筛选条件在 Redis 中保留的秒数。新增、编辑或删除的记录会立即显示；期间变更的汇率、价格和套餐会在这段时间内显示出来。默认 `60`；`0` 表示关闭缓存。 |

超出限制后，页面会显示速率限制页面，API 则返回 `429` 和错误码 `rate_limit_exceeded`，两者都带有 `Retry-After`。无论请求的是什么，一张图表最多有 120 个数据点（按所显示记录的时间范围，以天、周、月、季度或年为单位）；时间范围较长时，按天或按周分组的支出报告会改为按周或按月分组。

## 代理之后的客户端地址 {#client-addresses-behind-a-proxy}

Specivo 会在以下场合用到客户端的 IP 地址：按地址的速率限制（登录、单点登录、匿名读取、[搜索](#search-rate-limits)）、每个会话上显示的地址、API 密钥的 IP 白名单以及安全审计日志。位于反向代理之后时，连接来自代理。每一层代理都会把它收到请求的来源地址追加到 `X-Forwarded-For` 请求头中，所以客户端的地址就在这个请求头里。Specivo 相信什么，由两项设置决定：

- **`TRUSTED_PROXIES`**：连接到 Specivo 的代理的地址。只有来自这些地址的连接才可以指明客户端地址。来自其他任何地方的连接，无论发送什么请求头，其来源地址本身就是客户端地址。默认 `[]`：不信任任何代理。
- **`TRUSTED_PROXY_HOPS`**：Specivo 前面串联了几层代理。在 `X-Forwarded-For` 中从右往左数到这个数目的那个条目，就是客户端地址。它左边的所有内容都是客户端写的，会被忽略，所以没有人能通过发送这个请求头来自选地址。默认 `1`。

要填写代理的确切地址，不要填整个私有网段：受信任网段内的任何主机都可以冒称任意客户端。

### 内置栈 {#the-bundled-stack}

`docker-compose.yml` 把 nginx 和 api 放在它们专用的网络 `proxy`（`10.213.47.0/29`）中，nginx 使用固定地址 `10.213.47.2`，并设置 `TRUSTED_PROXIES=["10.213.47.2/32"]`。Docker 只从 `10.213.47.4/30` 中自动分配地址（例如 api 的地址），所以它们绝不会与 nginx 的地址冲突。这个网络中没有别的东西，api 不对外发布端口，nginx 会追加它看到的地址，所以无需任何改动即可工作。如果这个网段在你的主机上已被占用（`docker compose up` 失败并提示 “Pool overlaps”），在 `.env` 中另选一个。三项要一起改，并且 nginx 的地址要在子网之内、自动分配范围之外：

```bash
# .env
SPECIVO_PROXY_SUBNET=10.99.0.0/29
SPECIVO_PROXY_IP_RANGE=10.99.0.4/30
SPECIVO_NGINX_IP=10.99.0.2
```

除非你自行设置，否则 `TRUSTED_PROXIES` 会跟随 `SPECIVO_NGINX_IP`。这些变量要写在 `.env` 中，不要写在 `.env.local` 中：compose 文件从 `.env` 读取它们。

### nginx 前面还有一层代理 {#another-proxy-in-front-of-nginx}

当 HTTPS 终结器或 CDN 把请求转发给内置 nginx 时，就有两层代理串联。外层代理写入客户端的地址，nginx 再追加外层代理的地址，所以要设置：

```bash
# .env
TRUSTED_PROXY_HOPS=2
```

- **Caddy**：`reverse_proxy` 会把 `X-Forwarded-For` 设为客户端的地址，并且不会透传客户端发来的请求头，除非你配置了 Caddy 自己的 `trusted_proxies`。
- **Traefik**：追加客户端的地址。入口点上的 `forwardedHeaders.insecure` 要保持关闭，以免客户端发来的请求头被当作 Traefik 写入的内容透传过去。
- **Cloudflare**：追加访客的地址。此时 nginx 必须只接受来自 Cloudflare 的连接（用防火墙把对外发布的端口限制为 Cloudflare 的地址段），因为绕过 Cloudflare 的请求可以在多出来的那个条目中填入任何内容。

如果有外层代理而 `TRUSTED_PROXY_HOPS=1`，每个客户端都会显示为外层代理的地址：这样是安全的，但所有人共用一个速率限制桶。如果跳数大于实际的代理层数，客户端就能自选地址。务必数准。

### 不使用内置 nginx {#without-the-bundled-nginx}

如果你自己的代理（nginx、Caddy、Traefik）直接连接到 api，把 `TRUSTED_PROXIES` 设为该代理的地址，并保持 `TRUSTED_PROXY_HOPS=1`。对 nginx 来说，常见的 `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;` 就是正确写法：它会追加客户端的地址。

```bash
TRUSTED_PROXIES=["192.0.2.10/32"]
```

## MCP 会话 {#mcp-sessions}

通过 MCP（Streamable HTTP，地址为 `/mcp/`）连接的 AI 智能体持有一个会话，并在每个请求的 `Mcp-Session-Id` 请求头中指明它。Specivo 把每个会话保存在 Redis 中，所以智能体所在的电脑休眠、网络连接中断或 Specivo 重启之后，会话依然存在：智能体带着会话 ID 回来时，会话照常可用。

| 变量 | 用途 |
|---|---|
| `MCP_SESSION_TTL_SECONDS` | 会话在多长时间不使用后过期。每个请求都会重新开始计时，所以使用中的会话永不过期。默认 `604800`（7 天）。过期后，智能体的下一个请求会得到 HTTP 404，MCP 客户端随后会自行开启新会话。 |
| `MCP_SESSION_IDLE_TIMEOUT_SECONDS` | 未使用的会话在内存中保留多久。它只用于释放内存：下一个请求会从 Redis 重建会话。默认 `3600`（1 小时）。 |

只有当 Redis 把数据保存到磁盘时，会话才能在 Redis 重启后保留。内置的 Redis 会这样做（使用 append-only 文件）；如果外部 Redis 没有这样做，Specivo 会在启动时记录一条警告。没有持久化时，Redis 重启时处于连接状态的智能体会开启新会话。

旧版 SSE 传输（`/mcp/sse/`）不保存会话：智能体的事件流中断后会重新连接并继续工作，但它在流中断期间发送的请求会失败一次。只要智能体支持，就把它配置为使用 Streamable HTTP（`"type": "http"`，URL 以 `/mcp/` 结尾）。

### Specivo 前面的代理 {#proxies-in-front-of-specivo}

MCP 响应以流的方式传输。内置的 nginx 已为此配置妥当。如果 Specivo 前面还有其他代理（Caddy、Traefik、负载均衡器、Cloudflare），要确保它对 `/mcp/` 做到：

- **不缓冲响应**。nginx：`proxy_buffering off;`。Specivo 还会发送 `X-Accel-Buffering: no`，基于 nginx 的代理会遵从它。
- **允许长时间存续的响应**。智能体只要保持连接，就会一直开着一个流，Specivo 每 15 秒在流上发送一次 keep-alive。读取超时至少设为一分钟就够了；内置的 nginx 使用 `proxy_read_timeout 24h;`。
- **以 HTTP/1.1 或更高版本连接 Specivo**，并启用 keep-alive。nginx：`proxy_http_version 1.1;` 和 `proxy_set_header Connection "";`。

代理切断流并不会结束会话：智能体会重新连接并继续工作。

## 仓库集成与 Webhook {#repository-integrations-and-webhooks}

[仓库集成](../integrations/repositories.md)在 `APP_URL` 后接 `/hooks/...` 的地址上（如果设置了 `STEALTH_PREFIX`，则位于前缀之后）接收来自 GitHub、GitLab、Gitea 和 Forgejo 的 Webhook，所以 `APP_URL` 必须是代码托管平台能访问到的地址。仓库集成和[外发 Webhook](../integrations/webhooks.md) 的工作都在后台容器中完成（`celery-worker`，另有 `celery-beat` 负责补上遗漏的任务）：这些容器停止期间，收到的投递以 *Accepted* 状态等待，外发的投递以 *Pending* 状态等待，容器恢复运行后两者都会得到处理。

| 变量 | 用途 / 默认值 |
|---|---|
| `GIT_HOOK_MAX_BODY_BYTES` | 仓库集成接受的最大 Webhook 请求体；超出的请求返回 413。默认 `5242880`（5 MB），最小 `1024`。 |
| `GIT_HOOK_RATE_LIMIT_PER_MINUTE` | 单个仓库集成每分钟可接收的 Webhook 请求数，超出后 Specivo 返回 429。默认 `120`。 |
| `GIT_HOOK_UNKNOWN_RATE_LIMIT_PER_MINUTE` | 同一客户端地址每分钟可向不匹配任何集成的 hook URL 发送的请求数，签名错误的请求也计入其中。默认 `30`。 |
| `AI_USAGE_INGEST_RATE_LIMIT_PER_MINUTE` | 单个账户每分钟可向 [AI 使用数据上报 API](../ai-agents/reporting-ai-usage.md) 发送的批次数，超出后 Specivo 返回 429。默认 `60`。 |
| `WEBHOOK_TIMEOUT` | 外发 Webhook 的请求在每个步骤（连接、发送、读取响应）上可花费的秒数。默认 `30`；一次完整的尝试到 60 秒总会被中止。 |

## 单点登录：找回登录入口 {#single-sign-on-getting-back-in}

身份提供商在 **管理 → 单点登录** 中配置，登录页面的设置在 **管理 → 设置 → 认证** 中。这些设置可以隐藏密码表单、让登录页面直接跳转到某个提供商，以及限定只有管理员可以用密码登录。但它们都不会把管理员锁在外面：

- 密码表单被隐藏时，登录页面仍会显示一个小小的 **使用本地账户登录** 链接。它打开 `/login/?local=1`，该地址始终显示表单，从不重定向。
- 如果身份提供商不可用，登录页面上的错误信息会链接到同一个表单。
- 如果 SSO 设置本身有误（提供商配置错误、所有管理员都被拒绝），打开下面的环境变量开关。在你再次关闭它之前，它会覆盖上述全部三项设置：

  ```bash
  # .env
  SSO_FORCE_LOCAL_LOGIN=true
  ```

  ```bash
  docker compose up -d   # recreates the containers with the new value
  ```

  开关打开期间，Specivo 会在启动时记录一条警告。修正设置后，删除这一行并再次重建容器。
- 如果没有任何管理员知道密码，可以在主机上用 `make reset-password login=... password=...` 设置一个密码，或用 `make create-admin login=... email=... password=...` 创建一个新管理员。

| 变量 | 用途 / 默认值 |
|---|---|
| `SSO_FORCE_LOCAL_LOGIN` | 应急开关：始终显示密码表单，登录页面从不重定向到提供商，并允许所有用户用密码登录。默认 `false`。 |
| `SSO_IDP_RECHECK_MINUTES` | 续期 SSO 会话时，最多每隔多久向身份提供商确认一次此人是否仍可登录（按会话计）。默认：访问令牌的有效期 `ACCESS_TOKEN_EXPIRE_MINUTES`（15）。`0` 表示每次续期都确认。参见[会话检查](single-sign-on/index.md#the-session-check)。 |

其余 SSO 变量列在[单点登录](single-sign-on/index.md#settings-in-the-environment)中。

## 导入的密码 {#imported-passwords}

只有从其他系统导入账户并让它们沿用原密码时，才需要关心这些设置。导入包可以携带旧的密码哈希（参见[从其他系统导入](importing.md)）。另见[编写密码哈希插件](password-hasher-plugins.md)。

| 变量 | 用途 / 默认值 |
|---|---|
| `LEGACY_PASSWORD_ALGORITHMS` | 允许用于登录的导入密码格式，例如 `["redmine_sha1"]`。默认 `[]`：不允许任何格式。 |
| `LEGACY_PASSWORD_HASHES_EXPIRE_AT` | 过了这个时刻，所有导入的密码都将失效，相关用户需要通过找回密码来登录，例如 `2027-03-31T00:00:00Z`。默认不设置。 |
| `PASSWORD_HASHERS` | 额外的哈希器类，以点分路径给出，例如 `["acme_hashers.AcmeTrackerMd5"]`。 |
| `INSTALLED_PLUGINS` | 以点分路径给出的插件；插件可以提供密码哈希器。 |

## 汇率数据源 {#exchange-rate-feed}

| 变量 | 含义 |
|---|---|
| `EXCHANGE_RATE_FEED_ALLOW_PRIVATE_NETWORKS` | 允许可选的每日汇率抓取访问回环地址和私有地址，用于内网中自托管的 Frankfurter。默认 `false`：只允许公网地址。链路本地地址和云元数据地址无论如何都会被拒绝。参见[汇率](exchange-rates.md#fetch-the-ecb-rates-automatically-optional)。 |
| `EXCHANGE_RATE_FEED_ALLOW_UNRESOLVED_VIA_PROXY` | 位于代理之后时，允许抓取使用在本服务器上无法解析的数据源主机（域名只能通过代理解析）。这时本机无法检查它的地址，所以**必须由你的代理强制执行出站规则**，禁止访问私有网段和链路本地网段。默认 `false`：拒绝这样的主机。 |

抓取功能本身由管理员在 **管理 → 汇率** 中开启；它遵从 `HTTPS_PROXY` 和 `NO_PROXY`。

## 语义搜索模型 {#semantic-search-model}

混合搜索和语义搜索按语义匹配，这需要一个嵌入模型。Specivo 在你自己的 CPU 上运行 `multilingual-e5-small`（约 487 MB，384 维向量，支持约 100 种语言）。它不包含在 Docker 镜像中，也不会自动下载。你需要在栈运行时下载一次：

```bash
docker compose exec api python -m specivo.cli.download_model   # or: make download-model
```

模型保存到 `SPECIVO_DATA_DIR/models`，每个文件都会按公布的校验和核对。参见[语义搜索与向量嵌入](semantic-search.md#turn-it-on)。

没有专门的开关来启用它：模型文件在 `SPECIVO_DATA_DIR/models` 中，它就处于启用状态。没有这些文件时，关键词搜索和其他一切功能正常工作，混合搜索的表现与关键词搜索相同。只支持这个内置的本地模型：没有托管的嵌入服务提供商，没有自带密钥（BYOK）选项，也无法切换到其他模型（参见[更换模型](semantic-search.md#changing-the-model)）。

| 变量 | 用途 / 默认值 |
|---|---|
| `EMBEDDING_ONNX_THREADS` | 模型可使用的 CPU 线程数。默认 `2`；`0` 表示使用所有核心。 |
| `EMBEDDING_WORKER_CONCURRENCY` | `celery-embeddings` 容器中的进程数。默认 `1`；每个进程都会各自加载一份模型。 |
| `EMBEDDING_QUEUE` | 嵌入任务使用的 Celery 队列。默认 `embeddings`，由 `celery-embeddings` 读取；设为 `celery` 则改由 `celery-worker` 执行这些任务。 |
| `SEARCH_INDEX_COMMENTS` | 为评论建立搜索索引。默认 `true`。 |
| `SEARCH_MIN_COMMENT_LENGTH` | 字符数少于此值的评论不建立索引。默认 `20`。 |
| `SEARCH_EXCLUDE_BOT_COMMENTS` | 跳过通过 API 密钥或由服务账户发表的评论。默认 `true`。 |
| `SEARCH_HNSW_EF_SEARCH` | 每次向量索引扫描时 pgvector 候选列表的大小（`hnsw.ef_search`，10 到 1000）。值越高，越能可靠地找到真正最接近的匹配，速度也略慢。默认 `100`。 |
| `SEARCH_SEMANTIC_OVERFETCH` | 每个所需结果对应从向量索引中读取多少个最近的文本块，之后再去掉读者无权查看的结果（1 到 50）。默认 `4`。参见[向量索引](semantic-search.md#vector-index)。 |
| `SEARCH_SEMANTIC_MIN_SIMILARITY` | 文本块要算作语义匹配所需的最低余弦相似度（-1 到 1）；在混合搜索中，它只作用于仅凭语义找到的匹配。默认不设置，此时使用模型自身的最低值（`multilingual-e5-small` 为 `0.82`）；`-1` 表示关闭该限制。参见[最低相似度](semantic-search.md#minimum-similarity)。 |

关于它的用途、已有内容如何建立索引、如何确认它正常工作以及故障排查，参见[语义搜索与向量嵌入](semantic-search.md)。离线主机的做法，参见[离线部署](air-gapped.md#semantic-search-offline)。
