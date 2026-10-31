---
description: 在自己的服务器上运行 Specivo 需要什么：一台装有 Docker 的主机、随附的 Postgres、Redis、nginx 与 Celery，以及两种受支持的安装方式。
---

# 概览与系统要求 {#overview-requirements}

Specivo 是自托管（私有化部署）软件：你把它运行在自己的服务器上，数据完全归你所有。本页介绍软件运行所需的条件，以及两种受支持的安装方式。

## 你需要准备什么 {#what-you-need}

- 一台 Linux 主机（小型云服务器或自有服务器都可以），安装好 **Docker** 和 **Docker
  Compose** 插件。
- 小团队大约需要 **2 核 CPU** 和 **2–4 GB 内存**。语义搜索和嵌入模型是最吃资源的部分；不启用它们时，Specivo 在配置下限也能轻松运行。
- 几 GB 磁盘空间，用于数据库、附件和日志。存储占用随附件增多而增长。

就这么多。Specivo 运行时所需的一切都在容器内运行，不需要先搭建外部搜索服务、消息队列设备或托管数据库。

## 各组件如何协作 {#how-the-pieces-fit-together}

默认安装会运行几个容器，全部定义在克隆仓库后得到的 `docker-compose.yml` 中：

| 服务 | 镜像 | 作用 |
|---|---|---|
| nginx | `nginx:1-alpine` | 入口。监听 `SPECIVO_PORT`（默认 **9933**），把请求代理到 API。 |
| api | `specivo/specivo` | 应用本体。在内部网络中监听 **8000** 端口，位于 nginx 之后。 |
| db | `pgvector/pgvector:pg18` | **PostgreSQL 18**，带 pgvector，用于全文搜索和语义搜索。 |
| redis | `redis:7-alpine` | 缓存和 Celery 消息代理。 |
| celery-worker / celery-beat | `specivo/specivo` | 后台任务和定时任务。 |
| celery-embeddings | `specivo/specivo` | 为语义搜索生成内容的向量嵌入，单独一个进程，模型只需加载一次。 |

通过 `http://your-specivo-host:9933` 访问 Specivo。默认不对外暴露其他任何端口。

!!! note "搜索在 PostgreSQL 内部运行"
    关键词搜索和语义搜索都基于 PostgreSQL（tsvector + pgvector）。不需要运维
    Elasticsearch 或任何外部搜索服务。

## 选择安装方式 {#pick-an-install-path}

**使用官方镜像的 Docker 安装——推荐。**
对绝大多数人来说这是最快的方式。克隆仓库以获取 compose 文件和 nginx 配置，然后从 Docker Hub 拉取 `specivo/specivo` 镜像；如果 Docker Hub 无法访问，也可以从 GitHub Container
Registry（`ghcr.io/specivo/specivo`）拉取同一镜像。参见
[Docker 快速上手](docker.md)。

**物理隔离（air-gapped）/ 离线部署。**
适用于无法访问外网的主机。在一台联网的机器上拉取并保存镜像，传输到离线主机后再加载。参见 [离线部署](air-gapped.md)。

## 从源码运行：用于评估或参与开发 {#from-source-for-evaluation-or-contributing}

如果你想阅读代码、修改 Specivo 或在本地评估它，可以使用从源码启动的开发环境。它需要 [uv](https://docs.astral.sh/uv/)：

```bash
make install    # install dependencies
make dev-up     # run the dev server with hot-reload
```

这种方式只适合评估和参与开发。任何需要真正依赖的场景，请使用上面的 Docker 方式。

## 语义搜索是可选的 {#semantic-search-is-optional}

关键词搜索和 Specivo 的其他功能开箱即用。语义搜索（按语义搜索）需要一个嵌入模型。Specivo 会在你自己的 CPU 上运行一个小型模型，不发起任何外部调用，但模型文件不包含在镜像中，需要你下载一次。它的作用和启用方法参见
[语义搜索与向量嵌入](semantic-search.md)。

## 后续步骤 {#next-steps}

- [Docker 快速上手](docker.md)：几分钟内搭建一个可用实例。
- [配置](configuration.md)：环境变量、外部数据库、SMTP、密钥。
- [单点登录](single-sign-on/index.md)：让用户通过 Keycloak、authentik、Microsoft Entra ID 及其他 OpenID Connect 提供商登录。
- [备份与恢复](backup-restore.md)：在正式依赖 Specivo 之前保护好数据。
- [从其他系统导入](importing.md)：用迁移工具和导入包，把 Jira 等其他系统的项目、工作项、历史、维基和附件导入进来。
