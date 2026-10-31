---
description: "了解 Specivo 语义搜索的工作原理和运维方法：向量嵌入的用途、默认模型 multilingual-e5-small 的安装与回填、离线部署、更换模型以及故障排查。"
---

# 语义搜索与向量嵌入 {#semantic-search-and-embeddings}

Specivo 有两种查找内容的方式。**关键词搜索**匹配文字，**语义搜索**匹配含义，后者需要在你的服务器上运行一个*嵌入模型*。本页说明这个模型的作用、如何启用、如何保持正常运行，以及出问题时该怎么办。

这里的一切都是可选的。没有嵌入模型，Specivo 照常工作，关键词搜索仍然按文字查找内容。

## 向量嵌入的用途 {#what-embeddings-are-for}

向量嵌入（embedding）是一组数字（默认模型为 384 个），用来表示一段文本的含义。含义相近的文本会得到相近的数字，即使它们没有任何共同的词。Specivo 为每个内容*分块*（chunk）存储一个向量嵌入，搜索时把你的搜索词与它们进行比较。

向量嵌入**只用于搜索**。Specivo 的其他功能（通知、“相关工作项”、看板等）都不使用它。

### 关键词、语义与混合搜索 {#keyword-semantic-and-hybrid}

| 模式 | 查找方式 | 适用场景 | 是否需要模型 |
|---|---|---|---|
| **关键词** | PostgreSQL 全文搜索：按项目的搜索语言把你的搜索词还原为词干后匹配 | 精确名称、错误信息、ID | 否 |
| **语义** | 按含义与搜索词的接近程度匹配 | 搜“auth flow”能找到关于“login sequence”的工作项 | 是 |
| **混合** | 同时运行两种搜索并合并两份排名，两者都认可的结果会排到最前 | 所有场景；面向人类用户的默认模式 | 语义部分需要 |

工作项编号（如 `ACME-42`）在所有模式下都直接匹配并排在最前，不管有没有模型。

各模式的提供位置：

- **网页搜索页面**提供*混合*（默认）和*关键词*两种模式。没有账户的访客只能用关键词搜索，因为嵌入查询要消耗 CPU。
- **REST API**（`GET /api/v1/search/`）接受 `mode=keyword`（默认）、`semantic` 或 `hybrid`。
- 通过 MCP 接入的 **AI 智能体**默认使用 `hybrid`，除非明确请求其他模式。参见[智能体能做什么](../ai-agents/capabilities.md)。

### 没有模型会怎样 {#what-you-lose-without-a-model}

不会出故障，也不会报错。混合搜索会悄悄退化为关键词搜索，语义搜索则只返回你输入了编号的工作项。大家仍能按文字找到工作内容，但找不到含义相近的内容：搜索“auth flow”不会找出标题为“login sequence”的工作项。

### 哪些内容会被索引 {#what-gets-indexed}

内容会被切成分块，每个分块各有一个向量嵌入：

| 内容 | 分块 |
|---|---|
| 工作项 | 一个分块：主题加描述 |
| 维基页面 | 每个章节一个分块（按 `#`、`##`、`###` 标题切分，代码块保持完整），每块前面加上页面标题。过小的章节会合并，超过约 1,500 字符的章节会拆分 |
| 评论 | 每条评论一个分块，前提是长度至少 `SEARCH_MIN_COMMENT_LENGTH` 个字符（默认 20）。通过 API 密钥或服务账户写的评论默认跳过 |
| 附件 | 一个分块，包含文件名和描述；如果附件带有提取出的文本和 AI 描述，也一并包含 |

模型最多读取分块的前 512 个 token（大约前 2,000 个字符），长分块的剩余部分不影响其向量嵌入。

相关设置（都在 `.env` 中）：

| 变量 | 默认值 | 作用 |
|---|---|---|
| `SEARCH_INDEX_COMMENTS` | `true` | 设为 `false` 后不再索引评论 |
| `SEARCH_MIN_COMMENT_LENGTH` | `20` | 短于此长度的评论不索引 |
| `SEARCH_EXCLUDE_BOT_COMMENTS` | `true` | 通过 API 密钥或服务账户写的评论不索引 |

### 向量索引 {#vector-index}

向量嵌入用 pgvector 存在 PostgreSQL 中，并建有 **HNSW 索引**（由迁移 0038 创建）。有了它，语义搜索和混合搜索无需把查询与每个已存向量逐一比较，就能找到最近的分块。该索引覆盖 384 维向量，正是 `multilingual-e5-small` 输出的维度。

搜索先从索引读取最近的分块，然后才剔除读者无权查看的内容：其他项目、私有工作项、回收站。如果可见的分块太少，就从索引读取更大范围，最终会比较读者可见项目中的每一个向量。可见的匹配结果绝不会因为有读者看不到的内容排在它前面而被丢掉。代价是：在大型实例上只能看到一小部分内容的人，语义查询会比管理员慢。

该索引需要 pgvector **0.8 或更高版本**（用到其迭代索引扫描）；自带的数据库镜像已满足要求。在更老的 pgvector 上，搜索会比较每一个向量，并在每个进程中记录一次警告。

| 变量 | 默认值 | 作用 |
|---|---|---|
| `SEARCH_HNSW_EF_SEARCH` | `100` | 每次索引扫描中 pgvector 保留的候选数（`hnsw.ef_search`）。调高能更可靠地找到真正最近的分块，但速度会稍慢 |
| `SEARCH_SEMANTIC_OVERFETCH` | `4` | 在剔除不可见分块之前，每个期望结果读取的最近分块数。如果很多读者只能看到少数项目，可以调高 |

索引查找是近似的：在大型实例上，一个接近的匹配可能比精确比较的排名前后差一两位。调高 `SEARCH_HNSW_EF_SEARCH` 可以缩小这种差异。

## 默认模型 {#the-default-model}

| | |
|---|---|
| 名称 | `multilingual-e5-small`（来自 Hugging Face 上的 `intfloat/multilingual-e5-small` 仓库，MIT 许可），固定在修订版 `fd1525a9fd15` |
| 格式 | ONNX，通过 ONNX Runtime 在 CPU 上运行。不需要 GPU、外部服务或 API 密钥 |
| 大小 | 下载约 487 MB：模型文件约 470 MB，分词器约 17 MB |
| 向量维度 | 每个分块 384 个数字 |
| 语言 | 约 100 种 |
| 相似度 | 余弦相似度，向量经长度归一化 |

这个模型是一个“文本进、数字出”的网络。该模型家族要求在内容和搜索文本前分别加上 `passage: ` 和 `query: ` 前缀，Specivo 会自动添加，你不用操心。

### 文件位置 {#where-it-lives}

模型**不在** Docker 镜像里，**不在**发布包里，也**不会**在首次使用时自动下载。你需要把模型文件放进数据目录下的 `models` 文件夹：

```
<SPECIVO_DATA_DIR>/models/multilingual-e5-small.onnx
<SPECIVO_DATA_DIR>/models/tokenizer.json
```

`SPECIVO_DATA_DIR` 默认为 `./specivo-data`，Docker Compose 会把它挂载到 `api`、`celery-worker` 和 `celery-embeddings` 容器的 `/app/data`，因此文件位于 `/app/data/models/`。它们也会随数据目录一起被你的常规[备份](backup-restore.md)覆盖。

Specivo 只读这两个文件，下载命令也只获取这两个文件。

**没有开关设置**。文件存在即为开启。Specivo 每次需要向量嵌入时都会查找这些文件，所以添加后无需重启。全新安装的数据库中已经注册了该模型（由入口脚本完成），因此只要放好文件就行。

### CPU 与内存 {#cpu-and-memory}

- 模型在进程首次使用时加载到内存，之后一直驻留。每个会做嵌入的进程至少要预留模型文件大小的内存：嵌入搜索查询的 `api` 容器，以及嵌入内容的 `celery-embeddings` 容器。嵌入任务进入专用队列（`embeddings`），只有 `celery-embeddings` 读取，而它默认只运行**一个进程**，所以无论主机有多少核，内容嵌入在内存中只保留一份模型。通用的 `celery-worker` 从不加载模型。请在自己的主机上实测；小团队按[概览](index.md)中 2 核 CPU / 2 到 4 GB 内存的建议即可。
- 想在大主机上加快嵌入速度，在 `.env` 中设置 `EMBEDDING_WORKER_CONCURRENCY`（默认 `1`）；每多一个进程就多占一份模型内存。想改为在通用 worker 中做嵌入而不用单独容器，设置 `EMBEDDING_QUEUE=celery` 并停掉 `celery-embeddings`。改动任一项后都要重启整个服务栈。
- 推理默认使用 **2 个 CPU 线程**。在 `.env` 中设置 `EMBEDDING_ONNX_THREADS` 可以更改：数值越大索引越快，占用 CPU 也越多；`0` 表示使用全部核心。之后重启服务栈。
- 嵌入一条搜索查询只是一段短文本，开销很小。真正重的是给大型实例建索引（见[索引已有内容](#index-existing-content)）。

## 启用语义搜索 {#turn-it-on}

1. **下载模型**到数据目录，在运行中的 `api` 容器内执行：

    ```bash
    docker compose exec api python -m specivo.cli.download_model
    ```

    `make download-model` 运行的是同一条命令，可在代码检出目录或发布包中使用。它要求 `api` 容器能访问 `huggingface.co`（如果走代理，为容器设置 `HTTPS_PROXY`），并写入 `/app/data/models`，即主机上的 `<SPECIVO_DATA_DIR>/models`。每个文件都来自固定的修订版，替换前会校验 SHA-256；校验失败的文件会被丢弃，命令以错误退出。已存在且校验值正确的文件会跳过，所以可以放心重复运行。`--force` 会强制重新下载。主机离线时，命令会停止并列出所有文件及其 URL 和校验值。

    `scripts/download-model.sh`（在仓库和发布包中）可在没有 Docker 的主机上完成同样的事，例如为物理隔离（air-gapped）的服务器准备文件：`bash scripts/download-model.sh /path/to/models`。它需要 `curl`，以及 `sha256sum` 或 `shasum`。

2. **索引已有内容**（见下文）。新内容会按[内容如何被索引](#how-content-is-indexed)中所述自动处理。

3. **验证是否正常工作**（见下文）。

没有互联网的主机，请在联网机器上下载后把 `models` 文件夹复制过去，步骤见[离线部署](air-gapped.md#semantic-search-offline)。

## 内容如何被索引 {#how-content-is-indexed}

索引分两步，只有第二步需要模型。

1. **分块，即时完成**。创建、编辑或删除工作项、维基页面、评论或附件时，其分块会在同一个数据库事务中写入、替换或删除。关键词搜索立刻就能读到。这一步不需要模型，也不需要 worker，开销小到不会拖慢保存。
2. **嵌入，几秒之后**。改动保存后，Specivo 会向 `celery-embeddings` worker 排一个任务，由它为新分块生成向量嵌入。任务跑完后（通常几秒内），语义搜索和混合搜索就能找到这些内容。连续的快速编辑通常只按最新文本嵌入一次。

这覆盖了内容变化的所有途径：网页界面、REST API、通过 MCP 接入的智能体、重复任务、把工作项移到另一个项目（其分块、评论和附件一同迁移），以及引用了工作项的提交信息。导入则在运行结束时一次性索引所有导入的内容。

| 事件 | 关键词搜索 | 语义搜索 |
|---|---|---|
| 创建或编辑工作项、维基页面、评论或附件 | 立即 | worker 完成嵌入后 |
| 删除上述内容，或把维基页面移到回收站 | 立即消失 | 立即消失 |
| 从回收站恢复维基页面 | 立即 | worker 完成嵌入后 |
| 评论被设为私有 | 对无权查看私有备注的人立即隐藏 | 同样 |

须知：

- **语义索引需要 `celery-embeddings` worker 处于运行状态**。标准 Compose 文件会启动它。没有它，关键词搜索仍能找到一切，但新内容和编辑过的内容要等 worker（或下文的回填）处理后才能被语义搜索找到。排队的任务保存在 Redis 中，worker 恢复后会继续执行。
- **没有模型文件就不会排队任何任务**。分块仍会写入，所以关键词搜索是完整的。之后补上文件时，运行一次回填，把这期间写入的分块嵌入。嵌入 worker 启动时会在日志中记录语义索引是否开启。
- 如果保存改动时无法连接 Redis，改动仍会保存并可被关键词搜索到；`api` 会记录一条警告，稍后由回填来嵌入这些内容。
- 索引问题以警告级别记录，带有内容类型和 ID，绝不会导致保存本身失败。
- 上述评论设置在评论写入和编辑时生效。日后更改设置只影响之后写入或编辑的评论；要应用到旧评论，请运行回填。

## 索引已有内容 {#index-existing-content}

回填会遍历每个工作项、维基页面、评论和附件，使其分块与内容和当前设置保持一致，并为尚无向量嵌入的分块生成嵌入。它会索引每个附件，无论有没有描述，也无论挂在工作项、评论还是维基页面下。它会删除被当前评论设置排除的评论的分块，以及已不存在条目的分块。随时重复运行都是安全的：文本未变的分块保留原有嵌入。请在添加模型文件后以及更改评论设置后运行它。日常无需运行，因为新内容和编辑过的内容会在保存时索引。

```bash
docker compose exec api python -m specivo.cli.backfill_embeddings
```

`make backfill-embeddings` 运行的是同一条命令（`make backfill-embeddings ARGS=--reembed` 可传递选项）。它在前台运行，打印进度，结束时汇报结果：

```
Chunked 1840 sources into 2312 chunks; removed 3 stale sources.
Embedded 2312 chunks with multilingual-e5-small; 2312 of 2312 chunks now have an embedding.
```

工作按小批次保存，用 Ctrl+C 中断几乎不会丢失进度，之后可以重新启动。耗时随分块数量和 CPU 性能而变。首次大规模回填请选在服务器空闲时进行，如果有空余核心，可以在回填期间临时调高 `EMBEDDING_ONNX_THREADS`。

| 选项 | 作用 |
|---|---|
| `--reembed` | 重新计算每个分块的嵌入，而不只是没有嵌入的分块。替换模型文件后使用。旧嵌入会逐个替换，期间语义搜索照常工作 |
| `--require-model` | 没有可运行的模型时，不做任何事，直接以退出状态 2 停止。适合在预期语义搜索已开启的脚本中使用 |

运行成功时退出状态为 0（不管有没有模型）；有条目无法索引时为 1（日志会逐条列出）。

!!! note "没有模型时只写入关键词分块"
    如果模型文件缺失，回填仍会写入所有分块（这正是关键词搜索所需的全部），但不做任何嵌入。结束时会用一条警告说明：*Only keyword-search
    chunks were written; 0 chunks were embedded and semantic search stays off.*（只写入了关键词搜索分块；嵌入了 0 个分块，语义搜索保持关闭。）补上文件后再运行一次，即可启用语义搜索。

管理员也可以通过 `POST /api/v1/admin/embedding-models/{id}/backfill/` 在后台为某个已注册模型启动回填。它在 `celery-embeddings` worker 上运行，为该模型嵌入尚无嵌入的分块；加上 `?reembed=true` 会重新计算全部嵌入，例如在替换模型文件之后。它不会重建分块文本（分块文本在内容保存时就保持最新，上面的命令会重建它）。当模型无法在此运行（例如文件缺失）时，返回 `409`。它需要管理员账户；网页界面中没有对应按钮。

## 验证是否正常工作 {#check-that-it-works}

1. **文件在不在**？

    ```bash
    docker compose exec api ls -l /app/data/models
    ```

    应该能看到 `multilingual-e5-small.onnx` 和 `tokenizer.json`。

2. **日志是否一致**？嵌入 worker 启动时会说明语义索引是否开启，每个进程首次加载模型时会记录下面的日志行。模型缺失时，`api` 每次需要嵌入搜索查询都会记录一条警告。

    ```bash
    docker compose logs celery-embeddings api | grep -iE "semantic indexing|embedding model|ONNX model"
    ```

    ```
    Semantic indexing is on: embedding with multilingual-e5-small
    Loaded ONNX model multilingual-e5-small from /app/data/models/multilingual-e5-small.onnx (threads=2)
    Local embedding model 'multilingual-e5-small' is not available (missing ONNX/tokenizer files)
    ```

3. **有没有索引到内容**？比较分块数和嵌入数。worker 跟上进度后，两者应该接近相等。

    ```bash
    docker compose exec db psql -U specivo specivo -c \
      "SELECT (SELECT count(*) FROM search_chunks) AS chunks,
              (SELECT count(*) FROM chunk_embeddings) AS embeddings;"
    ```

    （如果你改过 `POSTGRES_USER` 和 `POSTGRES_DB`，请换成自己的值。）分块很多而嵌入为零，说明分块是在模型文件缺失时建立的，或者 `celery-embeddings` worker 没在运行。检查 worker，补上文件，再运行一次回填。

4. **按含义搜索是否有效**？用 [API 密钥](../ai-agents/api-keys.md)搜索你确定存在于某个工作项中的内容的另一种说法：

    ```bash
    curl -H "Authorization: Bearer spv_your_api_key_here" \
      "https://your-specivo-host/api/v1/search/?q=sign-in+flow&mode=semantic"
    ```

    返回的结果中不包含你的搜索词，就说明语义搜索在工作。

## 更换模型 {#changing-the-model}

**只支持自带的本地模型 `multilingual-e5-small`**。不提供托管嵌入服务商，不支持自带密钥（BYOK），也不支持切换到其他模型。没有相关设置，也没有相关界面。

- 模型是 `embedding_models` 表中的行，搜索使用标记为默认的那一行。种子命令会注册本地模型（每次 `api` 容器启动时运行）。`/api/v1/admin/embedding-models/` 下的管理员 REST API 可以列出模型、删除模型和启动[回填](#index-existing-content)，但不能注册模型。
- 数据库最多允许一个默认模型。升级到 0.9.0（迁移 0039）时，如果旧版本留下了多个默认模型，会清除多余的，只保留本地模型。
- 如果默认行指向其他提供商（例如旧版本注册的），Specivo 会把它当作文件缺失的模型处理：记录一条警告，不做嵌入，混合搜索返回关键词结果。删除该行并重启 `api` 容器，让种子命令重新注册本地模型，然后运行回填。
- 不同模型的向量无法互相比较，而[向量索引](#vector-index)只覆盖 `multilingual-e5-small` 的 384 维。迁移到其他模型需要完整重建索引；一旦支持，本页会补充说明。

## 故障排查 {#troubleshooting}

**语义搜索或混合搜索只找到关键词结果**。
按[验证是否正常工作](#check-that-it-works)中的清单逐项排查：`/app/data/models` 中的文件、日志行、分块数和嵌入数。常见原因有：没有模型文件；文件放错了位置（必须是挂载为 `/app/data` 的目录*里面*的 `models` 文件夹，且 `.env` 中的 `SPECIVO_DATA_DIR` 对所有容器指向同一位置）；`celery-embeddings` worker 已停止；或者添加文件后还没运行过回填。

**我知道存在的结果没出现，或者看起来过时了**。
关键词模式下，先确认你能打开该条目：搜索只显示你有权查看的内容，而短于 `SEARCH_MIN_COMMENT_LENGTH` 或由机器人写的评论不会被索引。语义模式下，`celery-embeddings` worker 可能还没嵌入最新文本；确认它在运行，并查看其日志中的警告。0.9.0 之前的内容如果从未回填过，可能没有嵌入；运行回填。参见[内容如何被索引](#how-content-is-indexed)。

**模型下载失败**。
*Cannot reach huggingface.co* 表示 `api` 容器无法连接 Hugging Face。有代理时，为容器设置 `HTTPS_PROXY`；有防火墙时，放行该主机；否则在另一台机器上用 `scripts/download-model.sh` 下载并把文件复制过来（见[离线部署](air-gapped.md#semantic-search-offline)）。错误信息会列出两个文件及其 URL 和校验值。*Checksum mismatch* 表示收到的文件不是固定修订版的那个，例如被代理改动过；该文件已丢弃，没有替换任何东西。下载失败不会留下残缺文件，再次运行命令会跳过已完整的文件。

**索引很慢**。
嵌入是在 CPU 上逐块进行的。回填期间调高 `EMBEDDING_ONNX_THREADS`，并给 `api` 和 `celery-embeddings` 容器分配更多核心；如果内存够再放一份模型，也可以调高 `EMBEDDING_WORKER_CONCURRENCY`。保存不会因此变慢：内容在保存后由 worker 嵌入，所以一波集中的改动只意味着语义搜索稍晚一点跟上。

**大型实例上搜索很慢**。
语义搜索从[向量索引](#vector-index)读取最近的分块，所以开销随分块数量增长得很慢。检查索引是否存在且有效：

    docker compose exec db psql -U specivo -d specivo -c \
      "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass('ix_chunk_embeddings_hnsw_384')"

期望结果是 `t`。没有返回行表示索引未建立，原因是 pgvector 扩展低于 0.5（升级日志会有说明）。`f` 表示索引已损坏；在同一个 `psql` 中执行 `REINDEX INDEX CONCURRENTLY ix_chunk_embeddings_hnsw_384` 重建，不会阻塞搜索。也检查一下 pgvector 版本（`SELECT extversion FROM pg_extension WHERE extname = 'vector'`）：0.8 之前不会使用索引。索引就位后，慢的情况是大型实例上只能看到少数项目的读者，因为搜索会不断扩大候选集，直到凑够他们可见的结果；调高 `SEARCH_SEMANTIC_OVERFETCH` 可以用更少的步数达到目标。混合搜索还会运行一次关键词搜索，这往往是耗时的大头。

**`api` 或 `celery-embeddings` 容器占用大量内存**。
模型在每个进程首次嵌入时加载，并一直驻留。这是预期行为：仅模型文件就约 470 MB。`celery-embeddings` 默认运行一个进程；如果你调高了 `EMBEDDING_WORKER_CONCURRENCY`，请调低。内存仍然紧张的话，给容器分配更多内存，或者不放模型文件、只用关键词搜索。通用的 `celery-worker` 不加载模型；如果它加载了，检查 `EMBEDDING_QUEUE` 是否被设为了 `celery`。
