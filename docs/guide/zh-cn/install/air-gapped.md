---
description: "介绍如何在无法访问互联网的主机上安装 Specivo：先在联网机器上拉取并保存容器镜像，传输到离线主机后加载，再用 docker compose up 启动整套服务。"
---

# 离线部署 {#air-gapped-install}

Specivo 可以运行在没有出站互联网的主机上。思路很简单：在联网机器上完成所有下载，把文件搬到物理隔离（air-gapped）的离线主机上加载即可。运行期间，Specivo 完全不需要联网。

## 需要传输的内容 {#what-you-need-to-transfer}

从联网机器搬到离线主机的东西有两样：

1. **容器镜像**，保存为一个 tar 文件。
2. **克隆下来的仓库**，其中包含 `docker-compose.yml`、nginx 配置和 `.env.example`。

## 1. 在联网机器上拉取并保存镜像 {#1-on-a-connected-machine-pull-and-save-the-images}

拉取 Specivo 用到的四个镜像。`specivo/specivo` 请固定为你打算运行的发布版本，不要用 `latest`，这样离线主机拿到的是一个确定的版本。

```bash
TAG=0.1.10   # the Specivo release you want

docker pull specivo/specivo:$TAG
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

同时克隆仓库，以便把 compose 文件和 nginx 配置一起带过去：

```bash
git clone https://github.com/specivo/specivo.git
```

## 2. 传输到离线主机 {#2-transfer-to-the-offline-host}

用你平时跨越隔离网络的任何方式（移动存储介质、内部文件共享等），把 `images.tar` 和克隆得到的 `specivo/` 目录复制到离线主机。

## 3. 在离线主机上加载并启动 {#3-on-the-offline-host-load-and-start}

```bash
docker load -i images.tar
```

在仓库目录中，把版本固定为你传输过来的那个，这样 Compose 会直接使用本地镜像，而不会尝试拉取：

```bash
# .env
SPECIVO_VERSION=0.1.10
```

然后启动整套服务：

```bash
docker compose up -d
```

像常规安装一样创建第一个管理员用户并登录：

```bash
docker compose exec api python -m specivo.cli.admin create \
  --login admin --email you@example.com --password 'choose-a-strong-one'
```

打开 `http://your-specivo-host:9933` 并登录。

!!! note "数据库迁移仍会自动执行"
    入口脚本会从已加载的镜像内部执行数据库迁移并写入默认数据。这一步不需要下载任何内容。

## 离线使用语义搜索 {#semantic-search-offline}

嵌入模型不包含在 Docker 镜像和发布包中，需要从 Hugging Face 单独下载，大小约 487 MB。要在物理隔离的主机上使用语义搜索，先在联网机器上获取模型，再复制到离线主机的 `SPECIVO_DATA_DIR`（默认为 `./specivo-data`）。

1. 在联网机器上运行 `bash scripts/download-model.sh ./models` 下载模型（脚本在仓库和发布包中都有，需要 `curl`，以及 `sha256sum` 或 `shasum`）。它会从固定的修订版本获取 `multilingual-e5-small.onnx` 和 `tokenizer.json`，并校验它们的 SHA-256。如果在离线主机上运行 `python -m specivo.cli.download_model` 时报告 *cannot reach huggingface.co*，这条消息中也会列出同样的文件和校验值。
2. 把得到的 `models` 文件夹复制到离线主机的 `<SPECIVO_DATA_DIR>/models/`，确保 `<SPECIVO_DATA_DIR>/models/multilingual-e5-small.onnx` 存在。
3. 为已有内容建立索引：

    ```bash
    docker compose exec api python -m specivo.cli.backfill_embeddings
    ```

Specivo 会在需要时查找这些文件，因此无需重启。它在本地加载模型并在 CPU 上运行，不会发起任何外部调用。

没有模型时，关键词搜索和 Specivo 的其他功能都照常可用，只有按语义搜索不可用。

!!! tip "运行时无需联网"
    镜像加载完成、可选的模型也就位之后，Specivo 不再需要任何出站互联网连接。唯一需要再次联网的时候，是为[升级](upgrading.md)获取新镜像——用新标签把拉取、保存、传输、加载的流程再做一遍即可。
