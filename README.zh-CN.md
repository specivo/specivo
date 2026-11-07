<p align="center">
  <a href="https://specivo.io"><img src="https://specivo.io/static/img/og/cover-zh-cn-v2.jpg" alt="Specivo：让团队的知识，终于触手可及" width="820"></a>
</p>

# Specivo

[English](README.md) | **简体中文**

**看清 AI 预算究竟花在了哪里。** Specivo 是私有化部署的项目管理工具，内置维基（Wiki）和知识库，还能告诉你团队的 AI 智能体（AI agent）花了多少钱：按人、按功能，一直细到每一次智能体会话。

开源，不按人头收费，为与 AI 智能体协作的团队而生。

## AI 用量与支出追踪

<p align="center">
  <a href="https://specivo.io/zh-cn/ai-usage/"><img src="docs/assets/readme/ai-spend-ledger.gif" alt="AI 支出从每个人流向他们参与的功能，并显示公司总额（示意数据）" width="640"></a>
</p>

你的团队每天都在用 Claude Code、Codex、Cursor 等 AI 工具，有的用个人订阅，有的用公司席位，有的用 API 密钥。账单告诉你花了多少；Specivo 告诉你是谁花的、花在了哪个功能上。

- **自动采集。** 开源的 [`ai-usage`](https://github.com/specivo/ai-usage-reporter) 上报工具读取 Claude Code、Codex、Gemini CLI 和 OpenCode 的本地日志，只发送用量数字，从不发送提示词、代码或文件内容。
- **每次会话都对应到工作项。** 根据智能体自己的 Specivo 调用、提交、分支名或代码仓库，把用量归到它处理的工作项；长时间会话按工作项拆分。
- **逐层下钻。** 从公司总额到项目、个人、功能，再到背后的每次会话；并提供按人、工具、模型和项目的图表。
- **各类成本一览。** 个人订阅、团队席位和按用量付费的 API 密钥放在同一视图，支持任意币种，基于内置的 300 多个模型价格计算。
- **每个工作项上都能看到。** 打开工作项即可看到它（含子工作项）在 AI 上花了多少：

<p align="center">
  <img src="docs/assets/readme/ai-usage-issue-tab.png" alt="工作项的 AI 用量标签页：支出、API 等值、Token 数和人数，按人、按工具、按模型的支出，以及每周的价值（示意数据）" width="720">
</p>

更多内容见 [specivo.io/zh-cn/ai-usage](https://specivo.io/zh-cn/ai-usage/)，以及用户指南：[上报 AI 用量](https://specivo.io/docs/specivo/zh-cn/ai-agents/reporting-ai-usage/) · [AI 支出报表](https://specivo.io/docs/specivo/zh-cn/projects/ai-spend-reports/)。

## 问题所在

团队的知识一直在流失：决策在聊天里敲定，随后不见踪影；架构文档散落在五个工具里。AI 编程智能体让情况更糟——它们在黑盒里产出代码，会话一结束，做了什么、为什么做、和其他工作有什么联系，就再也无从追溯。

Specivo 的解法是让智能体在项目管理系统内部工作，而不是游离在外。用户在智能体协助下规划冲刺（Sprint），把工作项（Issue）指派给人或智能体，再在熟悉的工作项追踪界面里审查完成情况。智能体通过 MCP（Model Context Protocol，模型上下文协议）工具创建工作项、更新维基页面、记录工时、管理版本——只需一行配置，不用写胶水代码。每个动作都有记录、可搜索、相互关联。把任务分给不同的模型，追踪结果，就能看清哪些智能体交付的是高质量成果，哪些只会制造需要人来收拾的烂摊子。

1 月你写的是“登录流程”，3 月写的是“认证时序”，关键词搜索无法把两者关联起来。Specivo 的混合搜索（全文 + 语义）按语义查找，而不只是匹配字面。

## 快速开始

```bash
git clone https://github.com/specivo/specivo.git
cd specivo
docker compose up -d
```

创建第一个管理员用户：

```bash
docker compose exec api python -m specivo.cli.admin create \
  --login admin --email admin@localhost --password changeme
```

打开 http://localhost:9933 并登录。

生产环境请把 `.env.example` 复制为 `.env`，并设置一个真正的 `SECRET_KEY`。

如需追踪 AI 用量，请在每位开发者的机器上安装上报工具（[ai-usage-reporter](https://github.com/specivo/ai-usage-reporter)），用个人 API 密钥连接到你的 Specivo，并安装钩子：

```bash
ai-usage init
ai-usage hooks install --tool all
```

分步部署指南：[用 Docker 自托管 Specivo](https://specivo.io/self-host-specivo-docker/)。

## 功能一览

**AI 用量与支出**——每个人、每个项目、每个功能在 AI 上的花费，数据来自 Claude Code、Codex、Gemini CLI 和 OpenCode，涵盖订阅、团队席位和 API 密钥；从公司到会话逐层下钻，附图表，并可通过 CSV、REST API 和 MCP 获取。详见[上文](#ai-用量与支出追踪)。

**工作项**——层级最多 16 层（嵌套集合），工作流引擎，工时记录，版本，9 种关联类型，关注者，批量操作，自定义元数据架构。

**维基**——Markdown 编写，完整版本历史，页面层级，一键回退。每次编辑都有记录，并与工作项交叉关联。

**混合搜索**——全文搜索基于 PostgreSQL tsvector，语义搜索基于 pgvector 向量嵌入，再用 Reciprocal Rank Fusion 融合。覆盖工作项、维基、评论和附件。附件描述也可搜索：按图里画的内容找到那张架构图，而不用记文件名。

**MCP 服务器**——提供 Model Context Protocol 工具，支持 Claude Code、Codex 以及任何会说 MCP 的客户端。工作项、维基页面、评论和附件描述都成为智能体可检索、可更新的上下文——可以自动进行，也可以等用户确认后再执行。一行配置即可接入。面向 Claude Code、Codex 和 Cursor 的现成智能体指令见 [**specivo-agent-skills**](https://github.com/specivo/specivo-agent-skills)。

**冲刺管理**——待办列表、冲刺规划、开始/完成周期。工作项在冲刺之间移动时保留完整历史。

**自定义元数据架构**——按项目或工作项类型定义结构化字段，并为常见工作流提供预设架构，比静态自定义字段灵活得多。

**AI 是可选的**。不启用也能正常使用。需要时再开启语义搜索，使用 multilingual-e5-small 模型（在 CPU 上运行，支持约 100 种语言，用 `make download-model` 下载一次即可）。

**单点登录**（SSO）——基于 OpenID Connect，支持 Keycloak、authentik、Microsoft Entra ID 或任何 OIDC 提供商。可同时配置多个提供商，首次登录自动创建账户，支持用户组和管理员映射、在提供商处退出登录以及后端通道登出（back-channel logout），并保留一条应急登录通道（break-glass）。

**国际化**（i18n）——内置英语、俄语、中文、法语、西班牙语和泰语。支持按用户设置语言，也可设置工作区默认语言。通过 `.po` 文件扩展。

## 从其他系统迁移

项目、工作项、历史记录、维基页面和附件都可以一起迁移过来。由独立的迁移工具读取旧系统并生成导入包；Specivo
校验导入包，支持先演练导入，然后正式加载。

| 原系统 | 工具 |
|---|---|
| Jira Cloud 或 Jira Data Center | [jira-migration-tool](https://github.com/specivo/jira-migration-tool)（[文档](https://specivo.io/docs/jira-migration-tool/)） |
| Redmine | [redmine-migration-tool](https://github.com/specivo/redmine-migration-tool) |
| Request Tracker（RT） | [rt-migration-tool](https://github.com/specivo/rt-migration-tool)（[文档](https://specivo.io/docs/rt-migration-tool/)） |

其他系统可以用 [migration-sdk](https://github.com/specivo/migration-sdk) 生成[导入包格式](docs/import-bundle/v1.md)。
Specivo 这一侧的导入步骤见[从其他系统导入数据](https://specivo.io/docs/specivo/zh-cn/install/importing/)。

## 技术栈

- Python 3.12、FastAPI、SQLAlchemy 2.0 async、Pydantic V2
- PostgreSQL 18（含 ltree 和 pgvector）、Redis
- Celery 处理后台任务
- 前端：Jinja2、Alpine.js、htmx

没有 Elasticsearch，也没有外部搜索服务。搜索完全在 PostgreSQL 内部完成。

## 开发

需要 [uv](https://docs.astral.sh/uv/) 和 Docker。Node 可选：前端产物由构建生成（不提交到仓库），
没有安装 `npm` 时会在容器中构建。

```bash
make install          # dependencies
make dev-up           # dev server with hot-reload, rebuilds frontend bundles on change
make frontend-build   # one-off frontend build (needed for tests that load bundles)
make test-db-up       # test database
make test             # run tests
make lint             # ruff + mypy
make download-model   # embedding model (~487 MB), with the stack running
```

## 文档

[specivo.io/docs/specivo/zh-cn](https://specivo.io/docs/specivo/zh-cn/)（[English](https://specivo.io/docs/specivo/)）

**面向 AI 智能体**：[specivo-agent-skills](https://github.com/specivo/specivo-agent-skills)——开箱即用的约定，教 Claude Code、Codex、Cursor 和任何 MCP 客户端用好 Specivo 的工具。

## 许可证

[AGPL-3.0-or-later](LICENSE)
