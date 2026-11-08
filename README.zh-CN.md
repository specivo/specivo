# Specivo

[English](README.md) | **简体中文**

<p align="center">
  <a href="https://specivo.io"><img src="https://specivo.io/static/img/og/cover-zh-cn-v2.jpg" alt="Specivo：团队的知识，终于找得到了" width="820"></a>
</p>

**看清你的 AI 预算到底花在了哪里**。Specivo 是一个自托管（私有化部署）的项目追踪、维基与知识库平台，还能告诉你团队的 AI 智能体花了多少钱：按人、按功能，细到每一次智能体会话。

开源，不按人头收费，为与 AI 智能体协作的团队而生。

## AI 使用与支出追踪

<p align="center">
  <a href="https://specivo.io/zh-cn/ai-usage/"><img src="docs/assets/readme/ai-spend-ledger.png" alt="AI 支出从每个人流向他们处理过的功能，并汇总为公司总额（演示数据）" width="640"></a>
</p>

你的团队每天都在用 Claude Code、Codex、Cursor 和其他 AI 工具，有个人套餐、公司席位，也有 API 密钥。账单只告诉你花了多少，Specivo 告诉你是谁花的、花在了哪个功能上。

- **自动采集**。开源的 [`ai-usage`](https://github.com/specivo/ai-usage-reporter)
  上报工具读取 Claude Code、Codex、Gemini CLI 和 OpenCode 的本地日志，只上传用量数字，
  绝不上传提示词、代码或文件内容。
- **每次会话都归到对应的工作项**。用量会归属到智能体处理的那个工作项（Issue），依据是它自己的
  Specivo 调用、提交、分支名或代码仓库。长会话会按工作项拆分。
- **逐层下钻**。从公司总额到项目、到人、到功能，再到背后的每次会话；还有按人、按工具、
  按模型和按项目的图表。
- **覆盖各类成本**。个人套餐、团队席位和按量付费的 API 密钥统一呈现，支持任意币种，
  价格来自内置的 300 多个模型的价目表。
- **每个工作项都能看**。打开一个工作项，就能看到它（含子工作项）花了多少 AI 费用：

<p align="center">
  <img src="docs/assets/readme/ai-usage-issue-tab.png" alt="工作项的 AI 使用标签页：支出、API 等价价值、token 数和参与人员，按人、按工具、按模型的支出，以及每周价值（演示数据）" width="720">
</p>

更多内容见 [specivo.io/zh-cn/ai-usage](https://specivo.io/zh-cn/ai-usage/) 和用户指南：
[上报 AI 使用情况](https://specivo.io/docs/specivo/zh-cn/ai-agents/reporting-ai-usage/) · [项目、工作项和你自己的 AI 使用情况](https://specivo.io/docs/specivo/zh-cn/projects/ai-usage/) · [AI 支出报表](https://specivo.io/docs/specivo/zh-cn/projects/ai-spend-reports/)。

## 问题所在

团队无时无刻不在丢失知识。决策在聊天里做出，然后消失不见。架构文档散落在五个工具里。AI 编码智能体让情况更糟：它们在黑盒里交付代码。做了什么、为什么做、和其他工作有什么关系，会话一结束就再也说不清。而且现在每个人都用着自己的一套智能体、套餐和 API 密钥，没人能看清每个人到底是怎么用 AI 的，又花了多少钱。

Specivo 的解法是让智能体在项目管理系统内部工作，而不是在它之外。用户在智能体的协助下规划冲刺（Sprint），把工作项指派给人或智能体，并在熟悉的工作项追踪界面里审阅成果。智能体通过 MCP（Model Context Protocol，模型上下文协议）工具创建工作项、更新维基页面、记录工时、管理版本——只需一行配置，不用写胶水代码。每一步操作都有记录、可搜索、相互关联。对每个人、每个项目、每个功能，Specivo 都能显示用了哪些工具和模型、投入了多少 AI 费用和多少时间、哪些智能体会话干了活；每个版本也会显示记录在它上面的工时。把任务分配给不同模型，跟踪结果，看看哪些智能体交付的是质量，哪些产出的是要靠人返工的垃圾。

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

要追踪 AI 使用情况，在每台开发机上安装上报工具
（[ai-usage-reporter](https://github.com/specivo/ai-usage-reporter)），用个人 API 密钥把它指向
你的 Specivo，然后安装钩子：

```bash
ai-usage init
ai-usage hooks install --tool all
```

逐步的自托管指南：[用 Docker 自托管 Specivo](https://specivo.io/self-host-specivo-docker/)。

## 功能一览

**AI 使用与支出**：每个人、每个项目、每个功能的 AI 成本，来自 Claude Code、Codex、Gemini CLI 和 OpenCode，涵盖订阅、团队席位和 API 密钥，支持从公司到会话的逐层下钻，提供图表、CSV、REST API 和 MCP 访问。见[上文](#ai-usage-and-spend-tracking)。

**工作项**：最多 16 层层级（嵌套集合），工作流引擎，工时记录，版本，9 种关联类型，关注者，批量操作，自定义元数据架构。

**维基**：Markdown，完整的版本历史，页面层级，一键回滚。每次编辑都有记录，并与工作项互相关联。

**混合搜索**：按语义找东西，而不只是匹配字面词：你 1 月写的“登录流程”和 3 月写的“认证时序”会一起出现，这是单靠关键词搜索做不到的。全文搜索用 PostgreSQL tsvector，语义搜索用 pgvector 向量嵌入，再用 Reciprocal Rank Fusion 融合。可搜索工作项、维基、评论和附件。附件描述也可搜索：按图里画了什么去找那张架构图，而不是靠文件名。

**MCP 服务器**：提供 Model Context Protocol 工具。兼容 Claude Code、Codex 以及任何支持 MCP 的客户端。工作项、维基页面、评论和附件描述都成为可搜索的上下文，智能体可以拉取和更新——自动执行，或经用户确认后执行。一行配置即可连接。面向 Claude Code、Codex 和 Cursor 的现成智能体指令见 [**specivo-agent-skills**](https://github.com/specivo/specivo-agent-skills)。

**冲刺管理**：待办列表、冲刺规划、开始/完成周期。工作项在冲刺之间移动时完整保留历史。

**自定义元数据架构**：按项目或工作项类型定义结构化字段。为常见工作流提供预设架构。比静态自定义字段更灵活。

**AI 是可选的**。不启用也完全可用。需要时再开启语义搜索，使用 multilingual-e5-small 模型（在 CPU 上运行，支持约 100 种语言，用 `make download-model` 下载一次即可）。

**单点登录**：OpenID Connect，支持 Keycloak、authentik、Microsoft Entra ID 或任意 OIDC 提供商。可同时配置多个提供商，首次登录时自动创建账户，支持用户组和管理员映射、在提供商处退出登录以及后端通道登出（back-channel logout），并保留一条应急（break-glass）登录通道。

**国际化**：内置英语、俄语、中文、法语、西班牙语和泰语。可按用户设置语言，也可设置工作区默认语言。通过 `.po` 文件扩展。

## 从其他系统迁移

把项目、工作项、历史、维基页面和附件一起带过来。独立的迁移工具读取旧系统并生成导入包；
Specivo 校验导入包，让你先演练一遍，再正式导入。

| 迁移来源 | 工具 |
|---|---|
| Jira Cloud 或 Jira Data Center | [jira-migration-tool](https://github.com/specivo/jira-migration-tool)（[文档](https://specivo.io/docs/jira-migration-tool/)） |
| Redmine | [redmine-migration-tool](https://github.com/specivo/redmine-migration-tool) |
| Request Tracker（RT） | [rt-migration-tool](https://github.com/specivo/rt-migration-tool)（[文档](https://specivo.io/docs/rt-migration-tool/)） |

其他系统可以用 [migration-sdk](https://github.com/specivo/migration-sdk) 来生成
[导入包格式](docs/import-bundle/v1.md)的数据。Specivo 这一侧的导入步骤见
[从其他系统导入](https://specivo.io/docs/specivo/zh-cn/install/importing/)。

## 技术栈

- Python 3.12、FastAPI、SQLAlchemy 2.0 异步、Pydantic V2
- PostgreSQL 18（含 ltree 和 pgvector）、Redis
- Celery 处理后台任务
- 前端：Jinja2、Alpine.js、htmx

不用 Elasticsearch，不用外部搜索服务。搜索完全在 PostgreSQL 内部完成。

## 开发

需要 [uv](https://docs.astral.sh/uv/) 和 Docker。Node 可选：前端产物由构建生成（从不提交），
未安装 `npm` 时会在容器中构建。

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

**给 AI 智能体**：[specivo-agent-skills](https://github.com/specivo/specivo-agent-skills)——开箱即用的约定，教 Claude Code、Codex、Cursor 以及任意 MCP 客户端正确使用 Specivo 的工具。

## 许可证

[AGPL-3.0-or-later](LICENSE)
