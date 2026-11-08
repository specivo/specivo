---
description: "本页介绍 Specivo 语义搜索的原理与运行方式：向量嵌入的用途、默认模型 multilingual-e5-small 的安装与回填、离线部署、更换模型以及常见问题排查。"
---

# 语义搜索与向量嵌入 {#semantic-search-and-embeddings}

Specivo 提供两种查找内容的方式。**关键词搜索** 匹配字面词语；**语义搜索** 匹配含义，需要在你的服务器上运行一个*嵌入模型*。本页介绍这个模型的作用、如何启用、如何保持正常运行，以及出问题时怎么办。

这里的一切都是可选的。没有嵌入模型，Specivo 照常工作，关键词搜索依旧按词语查找内容。

## 向量嵌入的用途 {#what-embeddings-are-for}

向量嵌入（embedding）是一组代表一段文本含义的数字（默认模型为 384 个）。含义相近的文本会得到相近的数字，即使它们没有任何共同的词。Specivo 为每个内容*分块*（chunk）存储一个向量嵌入，搜索时拿你的搜索词与之比较。

向量嵌入 **只用于搜索，别无他用**。Specivo 的其他功能，例如通知、“相关工作项”或看板，都不使用它。

### 关键词、语义与混合 {#keyword-semantic-and-hybrid}

| 模式 | 查找方式 | 适用场景 | 是否需要模型 |
|---|---|---|---|
| **关键词** | PostgreSQL 全文搜索：把你的搜索词按项目的搜索语言还原为词干后匹配 | 精确名称、错误信息、ID | 否 |
| **语义** | 按与搜索词的含义接近程度匹配 | 用“auth flow”找到关于“login sequence”的工作项 | 是 |
| **混合** | 同时运行两种搜索并合并两份排名，两者都认可的结果会排到最前 | 各种场景；面向人的默认模式 | 是（语义部分需要） |

像 `ACME-42` 这样的工作项编号会被直接匹配，并在所有模式下排在最前，有没有模型都一样。

各模式在哪些地方可用：

- **网页搜索页面** 提供 *混合*（默认）和 *关键词* 两种模式，没有账户的访客也能在自己可读的公开项目中使用。由于嵌入查询会消耗 CPU，搜索改为按客户端地址限流；见[搜索限流](configuration.md#search-rate-limits)。
- **REST API**（`GET /api/v1/search/`）接受 `mode=keyword`（默认）、`semantic` 或 `hybrid`。
- **AI 智能体**（AI agent）通过 MCP 搜索时默认使用 `hybrid`，除非明确指定其他模式。见[智能体能做什么](../ai-agents/capabilities.md)。

### 没有模型会失去什么 {#what-you-lose-without-a-model}

不会出故障，也不会报错。混合搜索会悄悄退化为关键词搜索，语义搜索只返回你输入了编号的那些工作项。大家仍能按词语找到内容，但无法按含义查找：搜索“auth flow”不会找出标题为“login sequence”的工作项。

### 哪些内容会被索引 {#what-gets-indexed}

内容会被切成分块，每个分块各有一个向量嵌入：

| 内容 | 分块方式 |
|---|---|
| 工作项 | 一个分块：主题加描述 |
| 维基页面 | 每个小节一个分块（在 `#`、`##` 和 `###` 标题处切分，代码块保持完整），每块前面带上页面标题。过短的小节会合并，超过约 1,500 个字符的小节会再拆分 |
| 评论 | 每条评论一个分块，前提是长度不少于 `SEARCH_MIN_COMMENT_LENGTH` 个字符（默认 20）。通过 API 密钥或由服务账户写的评论默认跳过 |
| 附件 | 文件名和描述合为一个分块；附件带有提取文本和 AI 描述时也一并纳入 |

模型最多读取分块的前 512 个 token（大约前 2,000 个字符）；长分块余下的部分不影响其向量嵌入。

相关设置（都在 `.env` 中）：

| 变量 | 默认值 | 作用 |
|---|---|---|
| `SEARCH_INDEX_COMMENTS` | `true` | 设为 `false` 后不再索引评论 |
| `SEARCH_MIN_COMMENT_LENGTH` | `20` | 短于此长度的评论不索引 |
| `SEARCH_EXCLUDE_BOT_COMMENTS` | `true` | 通过 API 密钥或由服务账户写的评论不索引 |

### 向量索引 {#vector-index}

向量嵌入通过 pgvector 存储在 PostgreSQL 中，其上的 **HNSW 索引**（由迁移 0038 创建）让语义搜索和混合搜索不必把查询与每个已存向量逐一比较就能找到最近的分块。该索引覆盖 384 维向量，也就是 `multilingual-e5-small` 输出的维度。

搜索先从索引中读取最近的分块，然后才剔除读者无权查看的内容：其他项目、私有工作项、回收站。如果可见的分块太少，就从索引中读取更大的范围，最终会把读者可见项目中的每个向量都比较一遍。可见的匹配结果不会因为排在它前面的是读者看不到的内容而被丢弃。代价是：在大型实例上只能看到一小部分内容的人，语义查询会比管理员慢。

该索引依赖 pgvector 的迭代索引扫描，需要 **0.8 或更高版本**；自带的数据库镜像已满足。在更旧的 pgvector 上，搜索会比较每一个向量，并在每个进程中记录一次警告。

| 变量 | 默认值 | 作用 |
|---|---|---|
| `SEARCH_HNSW_EF_SEARCH` | `100` | pgvector 每次索引扫描保留的候选数（`hnsw.ef_search`）。调高后更能可靠找到真正最近的分块，但会牺牲一些速度 |
| `SEARCH_SEMANTIC_OVERFETCH` | `4` | 每个所需结果在剔除不可见项之前读取的最近分块数。如果很多读者只能看到少数项目，可调高 |

索引查找是近似的：在大型实例上，某个相近的匹配结果可能比精确比较的位置前后相差一两位。调高 `SEARCH_HNSW_EF_SEARCH` 可缩小这种差异。

### 最低相似度 {#minimum-similarity}

任何查询都有“最近的分块”，哪怕查询内容和你的数据毫无关系。因此语义搜索只在分块与查询的相似度达到最低值时才算命中。低于该值的分块既不出现在结果中，也不计入结果数；没有真正匹配的查询会显示正常的“无结果”页面。

相似度指两个向量的余弦相似度，取值从 -1 到 1；它也是 REST API 在 `semantic` 模式下为每条结果返回的 `score`。相关文本和无关文本各能得多少分取决于模型，所以除非你自行设置，最低值由模型决定：

| 模型 | 默认最低值 |
|---|---|
| `multilingual-e5-small` 及 E5 系列其他模型 | `0.82` |
| 其他任何模型 | 无：每个最近的分块都算命中 |

E5 模型给任意两段文本打出的分数都偏高，大约在 0.7 到 0.9 之间。在工作项和维基文本上实测：搜索某个分块确实描述的内容，得分为 0.81 到 0.89（通常为 0.87）；而与内容无关的搜索（菜谱、体育、随机字母，或像“trash”这样的孤立单词）最高只到 0.81。默认值刚好略高于这个数。

在 **混合** 搜索中，最低值只作用于语义部分。所有关键词匹配都会保留；仅凭含义找到的结果则必须达到最低值。

| 变量 | 默认值 | 作用 |
|---|---|---|
| `SEARCH_SEMANTIC_MIN_SIMILARITY` | 未设置（使用模型默认值） | 最低余弦相似度，取值 `-1` 到 `1`。如果语义搜索漏掉措辞宽松的匹配，就调低；如果仍出现无关结果，就调高。`-1` 关闭此阈值 |

要针对自己的内容调优，先暂时把它设为 `-1` 并重启 `api` 容器。然后通过 REST API（`GET /api/v1/search/?q=...&mode=semantic`）搜索几项你确知存在的内容和几项本该一无所获的内容，比较最靠前结果的 `score`。把变量设在两组分数之间，再重启一次。

## 默认模型 {#the-default-model}

| | |
|---|---|
| 名称 | `multilingual-e5-small`（来自 Hugging Face 上的 `intfloat/multilingual-e5-small` 仓库，MIT 许可证），固定在修订版本 `fd1525a9fd15` |
| 格式 | ONNX，通过 ONNX Runtime 在 CPU 上运行。不需要 GPU，不需要外部服务，也不需要 API 密钥 |
| 大小 | 下载约 487 MB：模型文件约 470 MB，分词器约 17 MB |
| 向量维度 | 每个分块 384 个数字 |
| 语言 | 约 100 种 |
| 相似度 | 余弦相似度，基于长度归一化后的向量 |

这个模型是一个输入文本、输出数字的网络。该模型系列要求在内容前加 `passage: ` 前缀、在搜索文本前加 `query: ` 前缀，Specivo 会自动添加，你不必操心。

### 模型文件存放位置 {#where-it-lives}

模型 **不在** Docker 镜像里，**不在** 发布包里，也 **不会** 在首次使用时自动下载。你需要把模型文件放到数据目录下的 `models` 文件夹中：

```
<SPECIVO_DATA_DIR>/models/multilingual-e5-small.onnx
<SPECIVO_DATA_DIR>/models/tokenizer.json
```

`SPECIVO_DATA_DIR` 默认为 `./specivo-data`，Docker Compose 会把它挂载到 `api`、`celery-worker` 和 `celery-embeddings` 容器的 `/app/data`，因此容器内的文件路径为 `/app/data/models/`。这些文件也包含在你对数据目录的常规[备份](backup-restore.md)中。

Specivo 只读取这两个文件，下载命令也只获取这两个文件。

**没有开关设置**。文件是否存在就是开关。Specivo 每次需要计算向量嵌入时都会查找这两个文件，所以添加后无需重启。全新安装已在数据库中注册了该模型（由入口脚本完成），所以放好文件即可。

### CPU 与内存 {#cpu-and-memory}

- 模型会在进程首次使用时加载到内存，并一直驻留。每个需要嵌入的进程至少要预留模型文件大小的内存：`api` 容器负责嵌入搜索查询，`celery-embeddings` 容器负责嵌入内容。嵌入任务进入专用队列（`embeddings`），只有 `celery-embeddings` 消费，而它默认只运行 **一个进程**，所以无论主机有多少核，内容嵌入在内存中只保留一份模型。通用的 `celery-worker` 从不加载模型。请在自己的主机上实测；小团队符合[概览](index.md)中 2 CPU / 2 到 4 GB 内存的建议。
- 要在大型主机上加快嵌入速度，在 `.env` 中设置 `EMBEDDING_WORKER_CONCURRENCY`（默认 `1`）；每多一个进程就多占一份模型内存。如果想改在通用 worker 中运行嵌入而不用单独的容器，设置 `EMBEDDING_QUEUE=celery` 并停止 `celery-embeddings`。改动其中任一项后都要重启整套服务。
- 推理默认使用 **2 个 CPU 线程**。在 `.env` 中设置 `EMBEDDING_ONNX_THREADS` 可以更改：数值越大，索引越快，占用的 CPU 也越多；`0` 表示让模型使用所有核心。修改后重启整套服务。
- 嵌入一条搜索查询只是一小段文本，开销很小。真正的重活是为大型实例建立索引（见[索引已有内容](#index-existing-content)）。

## 启用语义搜索 {#turn-it-on}

1. **下载模型** 到数据目录，在运行中的 `api` 容器内执行：

    ```bash
    docker compose exec api python -m specivo.cli.download_model
    ```

    `make download-model` 运行的是同一条命令，可在代码检出目录或发布包中执行。它需要 `api` 容器能访问 `huggingface.co`（如果使用代理，为容器设置 `HTTPS_PROXY`），文件写入 `/app/data/models`，即主机上的 `<SPECIVO_DATA_DIR>/models`。每个文件都来自固定的修订版本，并在替换任何内容前先校验 SHA-256；校验失败的文件会被丢弃，命令以错误退出。已存在且校验和正确的文件会跳过，所以可以放心重复运行。`--force` 会强制重新下载。主机离线时，命令会停止并列出文件清单、URL 和校验和。

    `scripts/download-model.sh`（在代码仓库和发布包中）可在没有 Docker 的主机上完成同样的事，例如为物理隔离（air-gapped）的服务器准备文件：`bash scripts/download-model.sh /path/to/models`。它需要 `curl` 以及 `sha256sum` 或 `shasum`。

2. **索引已有内容**（见下文）。新内容会按[内容如何被索引](#how-content-is-indexed)中的说明自动处理。

3. **验证是否生效**（见下文）。

主机无法联网时，在能联网的机器上下载后把 `models` 文件夹复制过去，具体见[离线部署](air-gapped.md#semantic-search-offline)。

## 内容如何被索引 {#how-content-is-indexed}

索引分两步进行，只有第二步需要模型。

1. **分块，即时完成**。工作项、维基页面、评论或附件在创建、编辑或删除时，其分块会在同一个数据库事务中写入、替换或移除。关键词搜索立刻就能读到。这一步不需要模型也不需要 worker，开销很小，不会拖慢保存。
2. **向量嵌入，几秒之后**。改动保存后，Specivo 会向 `celery-embeddings` worker 发送一个任务来嵌入新分块。该任务运行完成后（通常在几秒内），语义搜索和混合搜索就能找到这些内容。连续的快速编辑通常只会按最新文本嵌入一次。

内容的每一种变更途径都包含在内：网页界面、REST API、通过 MCP 操作的智能体、重复工作项、把工作项移到另一个项目（其分块、评论和附件一同移动），以及引用了工作项的提交信息。导入则会在运行结束时索引所有导入的内容。

| 事件 | 关键词搜索 | 语义搜索 |
|---|---|---|
| 创建或编辑工作项、维基页面、评论或附件 | 立即可搜 | worker 完成嵌入后可搜 |
| 删除上述内容，或把维基页面移入回收站 | 立即消失 | 立即消失 |
| 从回收站恢复维基页面 | 立即可搜 | worker 完成嵌入后可搜 |
| 评论被设为私有 | 对无权查看私有备注的人立即隐藏 | 同样 |

需要注意：

- **语义索引要求 `celery-embeddings` worker 处于运行状态**。标准 Compose 文件会启动它。没有它，关键词搜索仍能找到所有内容，但新建和编辑的内容要等 worker（或下文的回填）处理后语义搜索才能找到。排队的任务保存在 Redis 中，worker 恢复后继续执行。
- **没有模型文件时不会排队任何任务**。分块照常写入，所以关键词搜索是完整的。之后添加了文件，运行一次回填即可嵌入这期间写入的内容。嵌入 worker 启动时会在日志中说明语义索引是否开启。
- 如果保存改动时无法连接 Redis，改动仍会保存并可通过关键词搜索到；`api` 会记录一条警告，回填会在之后嵌入这些内容。
- 索引问题以警告级别记录日志，附带内容类型和 ID，绝不会导致保存本身失败。
- 上述评论设置在评论写入和编辑时生效。之后修改设置只影响此后写入或编辑的评论；要让旧评论也适用，请运行回填。

## 索引已有内容 {#index-existing-content}

回填会遍历每个工作项、维基页面、评论和附件，让它们的分块与内容及当前设置保持一致，并为尚无向量嵌入的分块计算嵌入。所有附件都会被索引，不论有没有描述，也不论挂在工作项、评论还是维基页面下。当前评论设置排除的评论以及已不存在的条目，其分块会被移除。随时可以重复运行：文本未变的分块会保留原有嵌入。添加模型文件后和修改评论设置后运行它即可。日常无需运行，因为新建和编辑的内容在保存时就会被索引。

```bash
docker compose exec api python -m specivo.cli.backfill_embeddings
```

`make backfill-embeddings` 运行的是同一条命令（`make backfill-embeddings ARGS=--reembed` 可传入选项）。它在前台运行，打印进度，结束时汇总所做的工作：

```
Chunked 1840 sources into 2312 chunks; removed 3 stale sources.
Embedded 2312 chunks with multilingual-e5-small; 2312 of 2312 chunks now have an embedding.
```

工作以小批量保存，用 Ctrl+C 中止几乎不会丢失进度，可以随时重新开始。耗时随分块数量和 CPU 性能而变。规模较大的首次回填请在服务器空闲时运行；如果有空余核心，可在回填期间调高 `EMBEDDING_ONNX_THREADS`。

| 选项 | 作用 |
|---|---|
| `--reembed` | 重新计算每个分块的向量嵌入，而不只是没有嵌入的分块。替换模型文件后使用。旧嵌入逐个替换，语义搜索在此期间照常可用 |
| `--require-model` | 没有可运行的模型时不做任何事，直接以退出状态 2 停止。适用于要求语义搜索必须开启的脚本 |

运行成功（无论有无模型）时退出状态为 0；有条目无法索引时为 1（日志会逐一列出）。

!!! note "没有模型时只写入关键词分块"
    如果模型文件缺失，回填仍会写入所有分块（关键词搜索只需要这些），但不计算任何嵌入。结束时会用一条警告说明：*Only keyword-search
    chunks were written; 0 chunks were embedded and semantic search stays off.*（只写入了关键词搜索分块；0 个分块被嵌入，语义搜索保持关闭。）添加文件后再运行一次，语义搜索即可使用。

管理员也可以通过 `POST /api/v1/admin/embedding-models/{id}/backfill/` 在后台为某个已注册模型启动回填。它在 `celery-embeddings` worker 上运行，为该模型嵌入尚无嵌入的分块；加上 `?reembed=true` 可重新计算所有嵌入，例如替换模型文件之后。它不会重建分块文本（分块文本在内容保存时已保持最新，上面的命令则会重建它）。模型无法在此运行时（例如文件缺失），它返回 `409`。这需要管理员账户；网页界面中没有对应按钮。

## 验证是否生效 {#check-that-it-works}

1. **文件在吗？**

    ```bash
    docker compose exec api ls -l /app/data/models
    ```

    应能看到 `multilingual-e5-small.onnx` 和 `tokenizer.json`。

2. **日志怎么说？** 嵌入 worker 启动时会说明语义索引是否开启，每个进程首次加载模型时会记录下面的日志行。模型缺失时，`api` 每次需要嵌入搜索查询都会记录一条警告。

    ```bash
    docker compose logs celery-embeddings api | grep -iE "semantic indexing|embedding model|ONNX model"
    ```

    ```
    Semantic indexing is on: embedding with multilingual-e5-small
    Loaded ONNX model multilingual-e5-small from /app/data/models/multilingual-e5-small.onnx (threads=2)
    Local embedding model 'multilingual-e5-small' is not available (missing ONNX/tokenizer files)
    ```

3. **有内容被索引了吗？** 比较分块数和嵌入数。worker 跟上进度后，两者应该接近相等。

    ```bash
    docker compose exec db psql -U specivo specivo -c \
      "SELECT (SELECT count(*) FROM search_chunks) AS chunks,
              (SELECT count(*) FROM chunk_embeddings) AS embeddings;"
    ```

    （如果修改过 `POSTGRES_USER` 和 `POSTGRES_DB`，请替换为你自己的值。）分块很多而嵌入为零，说明分块是在模型文件缺失时建立的，或者 `celery-embeddings` worker 没有运行。检查 worker，添加文件，再运行一次回填。

4. **按含义搜索有效吗？** 用 [API 密钥](../ai-agents/api-keys.md)搜索某个你确知存在于工作项中的内容的同义表述：

    ```bash
    curl -H "Authorization: Bearer spv_your_api_key_here" \
      "https://your-specivo-host/api/v1/search/?q=sign-in+flow&mode=semantic"
    ```

    结果中不含你的搜索词，说明语义搜索正常工作。

## 更换模型 {#changing-the-model}

**只支持内置的本地模型 `multilingual-e5-small`**。托管的嵌入服务提供商和自带密钥（BYOK）方案均不可用，也不支持切换到其他模型。没有相应的设置项，也没有相应的界面。

- 模型是 `embedding_models` 表中的行，搜索使用标记为默认的那一个。seed 命令会注册本地模型（每次 `api` 容器启动时运行）。`/api/v1/admin/embedding-models/` 下的管理员 REST API 可以列出模型、删除模型和启动[回填](#index-existing-content)，但不能注册模型。
- 数据库最多允许一个默认模型。升级到 0.9.0（迁移 0039）时，如果旧版本留下了多个默认模型，会清除多余的默认标记，保留本地模型。
- 如果默认行指向其他提供商（例如旧版本注册的），Specivo 会把它当作文件缺失的模型处理：记录一条警告，不计算任何嵌入，混合搜索返回关键词结果。删除该行并重启 `api` 容器，让 seed 重新注册本地模型，然后运行回填。
- 不同模型的向量无法比较，而[向量索引](#vector-index)只覆盖 `multilingual-e5-small` 的 384 维。迁移到其他模型需要完全重建索引；如果将来支持，本页会补充说明。

## 故障排查 {#troubleshooting}

**语义搜索或混合搜索找不到关键词结果以外的任何内容**。
按[验证是否生效](#check-that-it-works)中的清单逐项排查：`/app/data/models` 中的文件、日志行、分块数和嵌入数。常见原因有：没有模型文件；文件放错了文件夹（必须是挂载为 `/app/data` 的那个文件夹*内部*的 `models` 文件夹，并且 `.env` 中的 `SPECIVO_DATA_DIR` 对所有容器都必须指向同一位置）；`celery-embeddings` worker 已停止；或者添加文件后还没运行过回填。如果这些都没问题，可能是语义匹配结果的得分低于[最低相似度](#minimum-similarity)：在 REST API 中查看它们的 `score`，必要时调低 `SEARCH_SEMANTIC_MIN_SIMILARITY`。

**语义搜索返回与查询毫不相关的结果**。
低于[最低相似度](#minimum-similarity)的结果会被排除，但过短或过于笼统的查询仍可能达到阈值。把 `SEARCH_SEMANTIC_MIN_SIMILARITY` 稍微调高（例如调到 `0.84`）并重启 `api` 容器。

**我确知存在的某条结果不见了，或看起来是过时的**。
关键词模式下，先确认你能打开该条目：搜索只显示你有权查看的内容，而且短于 `SEARCH_MIN_COMMENT_LENGTH` 或由机器人写的评论不会被索引。语义模式下，`celery-embeddings` worker 可能还没嵌入最新文本；确认它在运行，并检查其日志中的警告。0.9.0 之前的内容如果从未回填过，可能没有嵌入；运行回填。见[内容如何被索引](#how-content-is-indexed)。

**模型下载失败**。
*Cannot reach huggingface.co* 表示 `api` 容器没有通往 Hugging Face 的网络路径。使用代理时，为容器设置 `HTTPS_PROXY`；有防火墙时，放行该主机；否则在另一台机器上用 `scripts/download-model.sh` 下载后把文件复制过来（见[离线部署](air-gapped.md#semantic-search-offline)）。错误信息会列出两个文件的 URL 和校验和。*Checksum mismatch* 表示收到的文件不是固定修订版本的那个，例如被代理篡改过；该文件已被丢弃，没有替换任何内容。下载失败不会留下不完整的文件，再次运行命令会跳过已完整的文件。

**索引很慢**。
嵌入在 CPU 上逐块进行。回填期间调高 `EMBEDDING_ONNX_THREADS`，给 `api` 和 `celery-embeddings` 容器分配更多核心；如果内存够放下另一份模型，也可以调高 `EMBEDDING_WORKER_CONCURRENCY`。保存不会因此变慢：内容在保存后由 worker 嵌入，所以集中的大量改动只意味着语义搜索稍晚一点跟上。

**大型实例上搜索很慢**。
语义搜索从[向量索引](#vector-index)读取最近的分块，所以其开销随分块数增长得很慢。检查索引是否存在且有效：

    docker compose exec db psql -U specivo -d specivo -c \
      "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass('ix_chunk_embeddings_hnsw_384')"

`t` 是期望的结果。没有返回行说明索引没有建立，原因是 pgvector 扩展低于 0.5（升级日志中会有说明）。`f` 说明索引已损坏；在同一个 `psql` 中执行 `REINDEX INDEX CONCURRENTLY ix_chunk_embeddings_hnsw_384` 可在不阻塞搜索的情况下重建。也检查一下 pgvector 版本（`SELECT extversion FROM pg_extension WHERE extname = 'vector'`）：0.8 之前索引不会被使用。索引就位后，慢的情形是大型实例上只能看到少数项目的读者，因为搜索会不断扩大候选集，直到凑够他们可见的结果；调高 `SEARCH_SEMANTIC_OVERFETCH` 可以用更少的步骤达到目的。混合搜索还会运行一次关键词搜索，这往往是耗时的大头。

**`api` 或 `celery-embeddings` 容器占用大量内存**。
模型在每个进程首次嵌入时加载并一直驻留。这是正常的：仅模型文件就约 470 MB。`celery-embeddings` 默认运行一个进程；如果你调高了 `EMBEDDING_WORKER_CONCURRENCY`，请调低。内存仍然紧张的话，给容器分配更多内存，或者不放模型文件、只用关键词搜索。通用的 `celery-worker` 不会加载模型；如果它加载了，检查 `EMBEDDING_QUEUE` 是否被设成了 `celery`。
