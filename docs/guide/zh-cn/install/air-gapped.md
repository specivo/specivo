---
description: "在没有外网的主机上安装 Specivo：先在联网机器上拉取并保存镜像，传输到离线主机后加载，再运行 docker compose up 启动，运行时完全不需要联网。"
---

# 离线部署 {#air-gapped-install}

Specivo 可以运行在没有出站网络的主机上。思路很简单：在一台联网机器上完成所有下载，把文件带到离线主机，再加载进去。运行时 Specivo 完全不需要联网。

## 需要传输的内容 {#what-you-need-to-transfer}

从联网机器到离线主机，需要搬两样东西：

1. **容器镜像**，保存为一个 tar 文件。
2. **克隆下来的代码仓库**，其中包含 `docker-compose.yml`、nginx 配置和 `.env.example`。

## 1. 在联网机器上拉取并保存镜像 {#1-on-a-connected-machine-pull-and-save-the-images}

拉取 Specivo 用到的四个镜像。`specivo/specivo` 请固定为你准备运行的发行版本，不要用 `latest`，这样离线主机拿到的就是一个确定的版本。

```bash
TAG=0.1.10   # the Specivo release you want

docker pull specivo/specivo:$TAG        # or ghcr.io/specivo/specivo:$TAG, the same image
docker pull pgvector/pgvector:pg18
docker pull redis:7-alpine
docker pull nginx:1-alpine
```

把它们全部保存到一个归档文件中：

```bash
docker save \
  specivo/specivo:$TAG \
  pgvector/pgvector:pg18 \
  redis:7-alpine \
  nginx:1-alpine \
  -o images.tar
```

同时克隆代码仓库，拿到需要带过去的 compose 文件和 nginx 配置：

```bash
git clone https://github.com/specivo/specivo.git
```

## 2. 传输到离线主机 {#2-transfer-to-the-offline-host}

把 `images.tar` 和克隆得到的 `specivo/` 目录一起复制到离线主机，用什么方式跨越物理隔离都可以（可移动存储介质、内网文件共享等）。

## 3. 在离线主机上加载并启动 {#3-on-the-offline-host-load-and-start}

```bash
docker load -i images.tar
```

在仓库目录中固定你传过来的版本，让 Compose 使用本地镜像，而不是尝试拉取：

```bash
# .env
SPECIVO_VERSION=0.1.10
```

然后启动整个服务栈：

```bash
docker compose up -d
```

创建第一个管理员用户并登录，步骤与常规安装完全相同：

```bash
docker compose exec api python -m specivo.cli.admin create \
  --login admin --email you@example.com --password 'choose-a-strong-one'
```

打开 `http://your-specivo-host:9933` 并登录。

!!! note "数据库迁移仍会自动执行"
    入口脚本会从已加载的镜像内部执行数据库迁移并写入默认数据，这一步不需要下载任何东西。

## 离线使用语义搜索 {#semantic-search-offline}

嵌入模型不在 Docker 镜像和发布包里，需要单独从 Hugging Face 下载，约 487 MB。要在物理隔离（air-gapped）的主机上使用语义搜索，先在联网机器上下载模型，再复制到离线主机的 `SPECIVO_DATA_DIR`（默认 `./specivo-data`）。

1. 在联网机器上运行 `bash scripts/download-model.sh ./models` 下载模型（脚本在代码仓库和发布包中都有，需要 `curl` 以及 `sha256sum` 或 `shasum`）。它会从固定的 revision 获取 `multilingual-e5-small.onnx` 和 `tokenizer.json`，并校验它们的 SHA-256。如果离线主机在运行 `python -m specivo.cli.download_model` 时提示 *cannot reach huggingface.co*，那条消息里也列出了相同的文件和校验和。
2. 把生成的 `models` 文件夹复制到离线主机的 `<SPECIVO_DATA_DIR>/models/`，确保 `<SPECIVO_DATA_DIR>/models/multilingual-e5-small.onnx` 存在。
3. 为已有内容建立索引：

    ```bash
    docker compose exec api python -m specivo.cli.backfill_embeddings
    ```

Specivo 会在需要时自动查找这些文件，无需重启。模型在本地加载、在 CPU 上运行，不会发出任何外部请求。

没有模型时，关键词搜索和 Specivo 的其他功能都正常可用，只有按语义搜索不可用。

!!! tip "运行时不需要联网"
    镜像加载完成、可选的模型就位之后，Specivo 就不再需要出站网络。唯一需要再联网的时候，是为 [升级](upgrading.md) 获取新镜像：用新的 tag 重复一遍拉取、保存、传输、加载的流程。
