---
description: 在没有互联网连接的主机上安装 Specivo，先在联网机器上拉取并保存镜像，传输到离线主机后加载，再运行 docker compose up 启动。
---

# 离线部署 {#air-gapped-install}

Specivo 可以运行在无法访问外网的主机上。思路很简单：在一台联网机器上完成所有下载，把文件带到离线主机，再在离线主机上加载。Specivo 运行时完全不需要互联网。

## 需要传输的内容 {#what-you-need-to-transfer}

从联网机器带到离线主机的内容有两项：

1. **容器镜像**，保存为单个 tar 文件。
2. **克隆下来的仓库**，其中包含 `docker-compose.yml`、nginx 配置和 `.env.example`。

## 1. 在联网机器上：拉取并保存镜像 {#1-on-a-connected-machine-pull-and-save-the-images}

拉取 Specivo 用到的 4 个镜像。`specivo/specivo` 不要用 `latest`，而要固定为你打算运行的版本，这样离线主机拿到的版本是确定的。

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

同时克隆仓库，以便把 compose 文件和 nginx 配置一并带过去：

```bash
git clone https://github.com/specivo/specivo.git
```

## 2. 传输到离线主机 {#2-transfer-to-the-offline-host}

把 `images.tar` 和克隆下来的 `specivo/` 目录都复制到离线主机。用你平时跨越物理隔离（air-gapped）边界的方式即可，例如可移动存储介质、内部文件共享等。

## 3. 在离线主机上：加载并启动 {#3-on-the-offline-host-load-and-start}

```bash
docker load -i images.tar
```

在仓库目录中固定你传输过来的版本，让 Compose 使用本地镜像，而不是尝试拉取：

```bash
# .env
SPECIVO_VERSION=0.1.10
```

然后启动整套服务：

```bash
docker compose up -d
```

创建第一个管理员用户并登录，步骤与普通安装完全相同：

```bash
docker compose exec api python -m specivo.cli.admin create \
  --login admin --email you@example.com --password 'choose-a-strong-one'
```

打开 `http://your-specivo-host:9933` 并登录。

!!! note "数据库迁移仍会自动执行"
    入口脚本会在已加载的镜像内部执行数据库迁移并写入默认数据。这一步不需要下载任何内容。

## 离线环境下的语义搜索 {#semantic-search-offline}

嵌入模型不在 Docker 镜像中，也不在发布包中，需要单独从 Hugging Face 下载，大小约 487 MB。要在物理隔离的主机上使用语义搜索，先在联网机器上下载模型，再把它复制到离线主机的 `SPECIVO_DATA_DIR` 目录（默认为 `./specivo-data`）。

1. 在联网机器上，用 `bash scripts/download-model.sh ./models` 下载模型（该脚本在仓库和发布包中都有，需要 `curl`，以及 `sha256sum` 或 `shasum`）。脚本会从固定的修订版本获取 `multilingual-e5-small.onnx` 和 `tokenizer.json`，并校验它们的 SHA-256。如果在离线主机上运行 `python -m specivo.cli.download_model` 时提示 *cannot reach huggingface.co*，这条提示信息中列出的也是同样的文件和校验和。
2. 把得到的 `models` 文件夹复制到离线主机的 `<SPECIVO_DATA_DIR>/models/`，确保 `<SPECIVO_DATA_DIR>/models/multilingual-e5-small.onnx` 存在。
3. 为已有内容建立索引：

    ```bash
    docker compose exec api python -m specivo.cli.backfill_embeddings
    ```

Specivo 每次需要这些文件时都会去查找，因此无需重启。它从本地加载文件，在 CPU 上运行模型，不发起任何外部调用。

没有这个模型时，关键词搜索和 Specivo 的其他功能都能正常使用，只是无法按语义搜索。

!!! tip "运行时无需联网"
    镜像加载完成、可选的模型也放置到位后，Specivo 就不再需要访问外网。只有在[升级](upgrading.md)时才需要再次联网获取新镜像：用新的标签重复一遍拉取、保存、传输、加载的流程。

## 离线环境下的汇率 {#exchange-rates-offline}

金额报表按每日汇率换算金额。每日获取欧洲中央银行（ECB）汇率的功能是可选的，默认关闭，因此离线主机不会发出这类请求。你可以手动录入汇率，也可以导入一个格式为 `date,base,quote,rate` 的 CSV 文件，随其他文件一起带到离线主机；详见[汇率](exchange-rates.md#air-gapped-installs)。

## 离线环境下的 AI 模型价格 {#ai-model-prices-offline}

用于为 AI 使用计价的 AI 模型价格内置在镜像中，数据截至该版本的发布日期，加载时无需下载任何内容。更新的价格或缺失的价格可以手动录入；详见 [AI 模型价格](ai-model-prices.md)。

## 离线环境下的概念提取 {#concept-extraction-offline}

[概念提取](../projects/index.md#concept-extraction-ai)默认关闭，在项目配置 AI 提供商之前不会调用任何服务。把提供商指向你自己网络内的模型即可：任何提供 OpenAI 兼容 API 的服务器都可以，例如 Ollama、vLLM 或 LM Studio（例如 `http://ollama:11434/v1`，无需 API 密钥）。这样项目内容始终不会离开你的网络。
