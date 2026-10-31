---
description: "本文介绍如何用 Docker 快速部署 Specivo：克隆仓库、执行 docker compose up 启动整套服务、在命令行创建首个管理员账户，然后通过 9933 端口登录。"
---

# 用 Docker 快速开始 {#quick-start-with-docker}

这是运行 Specivo 的推荐方式。克隆仓库（获取 compose 文件和 nginx 配置），启动整套服务，再创建第一个管理员用户即可。应用镜像直接从 Docker Hub 拉取，无需自行构建。

## 1. 克隆并启动 {#1-clone-and-start}

```bash
git clone https://github.com/specivo/specivo.git
cd specivo
docker compose up -d
```

`docker compose up -d` 会拉取 `specivo/specivo:latest`（以及 Postgres、Redis 和 nginx 镜像），然后在后台启动所有服务。首次运行需要等待镜像下载，耗时稍长。

可以这样查看启动进度：

```bash
docker compose ps
docker compose logs -f api
```

!!! note "迁移和初始数据会自动完成"
    API 容器的入口脚本会等待数据库就绪，应用全部迁移，并写入默认的追踪器、状态、优先级和元数据预设。走 Docker 这条路线时，你**不需要**手动运行 `migrate` 或 `seed`——当 `api` 服务进入健康状态时，数据库已经准备好了。

## 2. 创建第一个管理员用户 {#2-create-the-first-admin-user}

第一个账户不能通过公开注册创建，需要在命令行中创建：

```bash
docker compose exec api python -m specivo.cli.admin create \
  --login admin --email you@example.com --password 'choose-a-strong-one'
```

请使用你能收到邮件的真实地址和足够强的密码。之后就可以用这个账户在界面中创建项目、邀请其他用户并管理一切。

## 3. 登录 {#3-sign-in}

打开 **http://localhost:9933**（或 `http://your-specivo-host:9933`），用刚才设置的登录名和密码登录。接下来可以按照 [上手前 10 分钟](../getting-started/first-10-minutes.md) 创建项目和第一个工作项（Issue）。

## 上线前须知 {#before-you-go-to-production}

默认配置是为快速本地试用而设的，并不适合公开部署。有两处需要修改：

!!! warning "设置真正的 SECRET_KEY"
    自带的默认密钥并不安全，仅供本地试用。在把 Specivo 开放给任何人之前，务必在 `.env.local` 中设置一个强度足够且唯一的 `SECRET_KEY`。参见 [配置](configuration.md#secrets-envlocal)。

!!! tip "固定版本号"
    `latest` 用来试用很方便，但正式部署应固定到具体的发布版本，这样升级才是有意为之。在 `.env` 中设置 `SPECIVO_VERSION`（例如 `SPECIVO_VERSION=0.1.10`），然后重新运行 `docker compose up -d`。

准备加固安装环境时，请通读 [配置](configuration.md)，其中涵盖密钥、注册模式、CORS、SMTP，以及如何接入外部数据库。

## 停止与重启 {#stopping-and-restarting}

```bash
docker compose stop      # stop the stack, keep data
docker compose up -d     # start it again
docker compose down      # stop and remove containers (data in SPECIVO_DATA_DIR is kept)
```

数据保存在 `SPECIVO_DATA_DIR`（默认为 `./specivo-data`）中，因此 `down` 不会删除数据。在正式依赖这套安装之前，请先阅读 [备份与恢复](backup-restore.md)。
