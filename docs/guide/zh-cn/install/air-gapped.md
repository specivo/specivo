---
description: 在没有外网的主机上安装 Specivo：在联网机器上拉取并保存镜像，拷贝到离线主机后加载，再运行 docker compose up。
---

# 离线部署 {#air-gapped-install}

Specivo 可以运行在没有出站互联网连接的主机上。思路很简单：在一台联网机器上完成下载，把所有东西拷贝过去，再在离线主机上加载。运行时 Specivo 完全不需要联网。

## 需要传输的内容 {#what-you-need-to-transfer}

从联网机器到离线主机，要传两样东西：

1. **容器镜像**，保存为一个 tar 文件。
2. **克隆下来的代码仓库**，其中包含 `docker-compose.yml`、nginx 配置和 `.env.example`。

## 1. 在联网机器上拉取并保存镜像 {#1-on-a-connected-machine-pull-and-save-the-images}

拉取 Specivo 用到的四个镜像。`specivo/specivo` 要固定到你打算运行的发布版本，而不是 `latest`，这样离线主机拿到的是一个确定的版本。

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

同时克隆代码仓库，以便把 compose 文件和 nginx 配置一起带过去：

```bash
git clone https://github.com/specivo/specivo.git
```

## 2. 传输到离线主机 {#2-transfer-to-the-offline-host}

用你跨越物理隔离（air-gapped）网络的任何方式（移动存储介质、内部文件共享等），把 `images.tar` 和克隆得到的 `specivo/` 目录都拷贝到离线主机上。

## 3. 在离线主机上加载并启动 {#3-on-the-offline-host-load-and-start}

```bash
docker load -i images.tar
```

在仓库目录中固定你传过来的版本，让 Compose 使用本地镜像而不是尝试拉取：

```bash
# .env
SPECIVO_VERSION=0.1.10
```

然后启动整个服务栈：

```bash
docker compose up -d
```

创建第一个管理员用户并登录，和普通安装完全一样：

```bash
docker compose exec api python -m specivo.cli.admin create \
  --login admin --email you@example.com --password 'choose-a-strong-one'
```

打开 `http://your-specivo-host:9933` 并登录。

!!! note "数据库迁移仍会自动执行"
    入口脚本会从已加载的镜像内部执行数据库迁移并写入默认数据。这一步不需要下载任何内容。

## 离线使用语义搜索 {#semantic-search-offline}

嵌入模型不在 Docker 镜像和发布包里，而是需要从 Hugging Face 单独下载，大小约 487 MB。要在离线主机上使用语义搜索，先在联网机器上下载模型，再拷贝到离线主机的 `SPECIVO_DATA_DIR` 中（默认为 `./specivo-data`）。

1. 在联网机器上运行 `bash scripts/download-model.sh ./models` 下载模型（脚本位于代码仓库和发布包中，需要 `curl` 以及 `sha256sum` 或 `shasum`）。它会从固定的修订版本获取 `multilingual-e5-small.onnx` 和 `tokenizer.json`，并校验它们的 SHA-256。如果在离线主机上运行 `python -m specivo.cli.download_model` 时提示*无法访问 huggingface.co*，该提示也会列出同样的文件和校验和。
2. 把得到的 `models` 文件夹拷贝到离线主机的 `<SPECIVO_DATA_DIR>/models/`，确保 `<SPECIVO_DATA_DIR>/models/multilingual-e5-small.onnx` 这个文件存在。
3. 为已有内容建立索引：

    ```bash
    docker compose exec api python -m specivo.cli.backfill_embeddings
    ```

Specivo 在需要时才会查找这些文件，因此无需重启。模型在本地加载并在 CPU 上运行，不会发起任何外部请求。

没有模型时，关键词搜索和 Specivo 的其他功能一切正常，只有按语义搜索不可用。

!!! tip "运行时无需联网"
    镜像加载完成、可选的模型就位之后，Specivo 就不再需要出站互联网连接。唯一需要再次联网的时候，是为 [升级](upgrading.md) 获取更新的镜像：用新的标签重复一遍拉取、保存、传输、加载的流程即可。

## 离线使用汇率 {#exchange-rates-offline}

财务报表按每日汇率换算金额。每日自动获取欧洲央行（ECB）汇率的功能默认关闭，因此离线主机不会发出相关请求。你可以手动录入汇率，或者导入随其他文件一起传过来的 `date,base,quote,rate` 格式 CSV 文件；参见 [汇率](exchange-rates.md#air-gapped-installs)。

## 离线使用 AI 模型价格 {#ai-model-prices-offline}

用于核算 AI 使用费用的 AI 模型价格随镜像一起发布（截至发布日期的数据），加载时无需任何下载。更新的或缺失的价格可手动录入；参见 [AI 模型价格](ai-model-prices.md)。
