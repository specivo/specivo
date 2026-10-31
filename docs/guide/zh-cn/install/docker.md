---
description: 用 Docker 安装 Specivo：克隆仓库、运行 docker compose up、创建第一个管理员账户，然后通过 9933 端口登录。
---

# Docker 快速上手 {#quick-start-with-docker}

这是运行 Specivo 的推荐方式。你需要克隆仓库（取 compose 文件和 nginx 配置）、启动整个服务栈，再创建第一个管理员用户。应用镜像从 Docker Hub（或 GitHub Container Registry，见下文）拉取，无需自行构建。

## 1. 克隆并启动 {#1-clone-and-start}

```bash
git clone https://github.com/specivo/specivo.git
cd specivo
docker compose up -d
```

`docker compose up -d` 会拉取 `specivo/specivo:latest`（以及 Postgres、Redis 和 nginx 镜像），然后在后台启动所有服务。首次运行需要等待镜像下载，稍等片刻即可。

!!! tip "Docker Hub 被屏蔽或速度很慢？"
    每个发布版本也会同步发布到 GitHub Container Registry，地址为 `ghcr.io/specivo/specivo`，标签和镜像摘要与 Docker Hub 完全一致。要从那里拉取，在运行 `docker compose up -d` 之前，把下面这行写进 `.env`（与 `docker-compose.yml` 放在同一目录）：

    ```bash
    SPECIVO_IMAGE=ghcr.io/specivo/specivo
    ```

    这不需要 GitHub 账号。Postgres、Redis 和 nginx 镜像仍然来自 Docker Hub；如果 Docker Hub 无法访问，请为它们配置镜像加速源。

可以这样查看启动过程：

```bash
docker compose ps
docker compose logs -f api
```

!!! note "迁移和种子数据会自动完成"
    API 容器的入口脚本会等待数据库就绪，应用全部迁移，并写入默认的追踪器（Tracker）、状态、优先级和元数据预设。走 Docker 路线时，你**不需要**手动运行 `migrate` 或 `seed`：等 `api` 服务进入健康状态，数据库就已经准备好了。

## 2. 创建第一个管理员用户 {#2-create-the-first-admin-user}

第一个账户没有公开注册入口，需要在命令行创建：

```bash
docker compose exec api python -m specivo.cli.admin create \
  --login admin --email you@example.com --password 'choose-a-strong-one'
```

请使用你能收到邮件的真实地址，并设置一个强密码。之后这个账户就可以在界面里创建项目、邀请其他用户，并管理一切。

## 3. 登录 {#3-sign-in}

打开 **http://localhost:9933**（或 `http://your-specivo-host:9933`），用刚设置的登录名和密码登录。接下来，按照 [上手前 10 分钟](../getting-started/first-10-minutes.md) 创建一个项目和第一个工作项（Issue）。

## 上生产环境之前 {#before-you-go-to-production}

默认配置是为快速本地体验调优的，不适合对外部署。有两处必须修改：

!!! warning "设置真正的 SECRET_KEY"
    内置的默认密钥不安全，只用于本地试用。在把 Specivo 开放给任何人之前，先在 `.env.local` 中设置一个强度足够且唯一的 `SECRET_KEY`。参见 [配置](configuration.md#secrets-envlocal)。

!!! tip "锁定版本"
    `latest` 方便试用，但正式部署应锁定到具体版本，让每次升级都是有意为之。在 `.env` 中设置 `SPECIVO_VERSION`（例如 `SPECIVO_VERSION=0.1.10`），然后重新运行 `docker compose up -d`。

准备加固安装时，请通读 [配置](configuration.md)，其中涵盖密钥、注册模式、CORS、SMTP，以及如何接入外部数据库。

## 停止与重启 {#stopping-and-restarting}

```bash
docker compose stop      # stop the stack, keep data
docker compose up -d     # start it again
docker compose down      # stop and remove containers (data in SPECIVO_DATA_DIR is kept)
```

数据保存在 `SPECIVO_DATA_DIR`（默认 `./specivo-data`）中，所以 `down` 不会清除数据。在正式依赖这套安装之前，请先阅读 [备份与恢复](backup-restore.md)。
