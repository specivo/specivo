# Specivo

[English](README.md) | **简体中文**

<p align="center">
  <a href="https://specivo.io"><img src="https://specivo.io/static/img/og/cover-zh-cn-v2.jpg" alt="Specivo：团队的知识，终于找得到了" width="820"></a>
</p>

**看清 AI 预算到底花在了哪里**。Specivo 是一个自托管（私有化部署）的项目追踪、维基（Wiki）与知识库平台，还能告诉你团队的 AI 智能体（AI agent）花了多少钱：细到每个人、每项功能，直至每一次智能体会话。

开源，不按人头收费，为与 AI 智能体协作的团队而生。

## AI 用量与支出追踪

<p align="center">
  <a href="https://specivo.io/ai-usage/"><img src="docs/assets/readme/ai-spend-ledger.png" alt="AI 支出从每位成员流向他们处理的各项功能，并汇总出公司总额（演示数据）" width="640"></a>
</p>

你的团队每天都在用 Claude Code、Codex、Cursor 等 AI 工具，有人用个人订阅，有人用公司席位，也有人用 API 密钥。账单只告诉你花了多少，Specivo 告诉你是谁花的、花在了哪项功能上。

- **自动采集**。开源的 [`ai-usage`](https://github.com/specivo/ai-usage-reporter) 上报工具读取 Claude Code、Codex、Gemini CLI 和 OpenCode 的本地日志，只发送用量数字，绝不发送提示词、代码或文件内容。
- **每个会话都关联到工作项**。用量会归属到智能体当时处理的工作项（Issue），依据是它自己发出的 Specivo 调用、提交、分支名或所在仓库。长会话按工作项拆分。
- **逐层下钻**。从公司总额到项目、个人、功能，再到背后的每个会话，并提供按人员、工具、模型和项目划分的图表。
- **涵盖各类成本**。个人订阅、团队席位和按量付费的 API 密钥汇总在同一个视图里，支持任意货币，按内置的 300 多个模型价目表计价。
- **每个工作项都能看**。打开一个工作项，就能看到它（含子工作项）的 AI 成本：

<p align="center">
  <img src="docs/assets/readme/ai-usage-issue-tab.png" alt="工作项的“AI 使用”标签页：支出、API 等价价值、token 数和人员，按人员、工具和模型划分的支出，以及每周价值（演示数据）" width="720">
</p>

更多内容见 [specivo.io/ai-usage](https://specivo.io/ai-usage/) 和用户指南：
[上报 AI 用量](https://specivo.io/docs/specivo/ai-agents/reporting-ai-usage/) · [项目、工作项和个人的 AI 用量](https://specivo.io/docs/specivo/projects/ai-usage/) · [AI 支出报告](https://specivo.io/docs/specivo/projects/ai-spend-reports/)。

## 要解决的问题

团队的知识一直在流失。决策在聊天里做出，转眼就找不到了。架构文档散落在五个工具里。AI 编程智能体让情况更糟：它们在黑盒里交付代码，会话一结束，做了什么、为什么这么做、和其他工作有什么关系，就再也无从追溯。而且现在每个人用的智能体、订阅和 API 密钥各不相同，谁也看不清每个人究竟是怎么用 AI 的，又花了多少钱。

Specivo 的解法是让智能体在项目追踪系统内部工作，而不是游离在外。用户在智能体的协助下规划冲刺（Sprint），把工作项指派给人或智能体，然后在熟悉的工作项界面里检查完成情况。智能体通过 MCP（Model Context Protocol，模型上下文协议）工具创建工作项、更新维基页面、记录工时、管理版本，只需一行配置，不用写胶水代码。每个操作都有记录、可搜索、彼此关联。针对每个人、每个项目、每项功能，Specivo 都会显示用了哪些工具和模型、投入了多少 AI 费用和多少时间，以及是哪些智能体会话完成了工作；每个版本、每个冲刺也会显示对应的 AI 支出和已记录的工时。你可以把任务指派给不同的模型，跟踪结果，看清哪些智能体交付的是高质量成果，哪些产出的是还得靠人返工的低质内容。

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

用于生产环境时，把 `.env.example` 复制为 `.env`，并设置一个真实的 `SECRET_KEY`。

要追踪 AI 用量，在每台开发者机器上安装上报工具（[ai-usage-reporter](https://github.com/specivo/ai-usage-reporter)），用个人 API 密钥把它指向你的 Specivo，再安装它的钩子：

```bash
ai-usage init
ai-usage hooks install --tool all
```

分步自托管指南：[使用 Docker 自托管 Specivo](https://specivo.io/self-host-specivo-docker/)。

## 功能一览

**AI 用量与支出**：每个人、每个项目、每项功能的 AI 成本，数据来自 Claude Code、Codex、Gemini CLI 和 OpenCode，涵盖订阅、团队席位和 API 密钥；支持从公司到会话的逐层下钻，提供图表、CSV，并可通过 REST API 和 MCP 访问。详见[上文](#ai-usage-and-spend-tracking)。

**工作项**：最多 16 层的层级结构（nested set）、工作流引擎、工时记录、版本、关联（9 种）、关注者、批量操作、自定义元数据架构。

**维基**：使用 Markdown 编写，有完整的版本历史和页面层级，可一键回滚。每次编辑都有记录，并与工作项相互链接。

**混合搜索**：按语义查找，而不只是匹配字面。你 1 月写的“登录流程”和 3 月写的“认证时序”会一起被搜出来，这是单靠关键词搜索做不到的。全文搜索基于 PostgreSQL tsvector，语义搜索基于 pgvector 向量嵌入，两者的结果用 Reciprocal Rank Fusion（倒数排名融合）合并。搜索范围包括工作项、维基、评论和附件。附件描述也可搜索：按架构图画了什么来找它，而不是靠文件名。

**MCP 服务器**：提供一组 Model Context Protocol 工具，可与 Claude Code、Codex 或任何支持 MCP 的工具配合使用。工作项、维基页面、评论和附件描述都成为可搜索的上下文，智能体可以读取并更新，既可以自动执行，也可以等用户确认。只需一行配置即可连接。面向 Claude Code、Codex 和 Cursor 的现成智能体指令放在 [**specivo-agent-skills**](https://github.com/specivo/specivo-agent-skills) 中。

**冲刺管理**：待办列表、冲刺规划、开始与完成冲刺的完整周期。工作项在冲刺之间移动时，历史完整保留。

**自定义元数据架构**：按项目或工作项类型定义结构化字段。常见工作流有预设架构可用。比固定的自定义字段更灵活。

**AI 是可选的**。不用 AI 也能正常使用。需要时再开启语义搜索，它使用 multilingual-e5-small 模型（在 CPU 上运行，支持约 100 种语言，用 `make download-model` 下载一次即可）。

**单点登录**（SSO）：通过 OpenID Connect 对接 Keycloak、authentik、Microsoft Entra ID 或任何 OIDC 提供商。可同时配置多个提供商，首次登录时自动创建账户，支持用户组和管理员映射，支持在提供商侧退出登录和后端通道注销（back-channel logout），并留有应急登录入口。

**国际化**：内置英语、俄语、中文、法语、西班牙语和泰语。每个用户可单独设置语言，也可设置工作区默认语言。可通过 `.po` 文件扩展。

## 从其他系统迁移

把项目、工作项、历史记录、维基页面和附件一并带过来。独立的迁移工具读取旧系统并生成导入包；Specivo 校验导入包，让你先演练一遍导入，再正式载入。

| 迁移来源 | 工具 |
|---|---|
| Jira Cloud 或 Jira Data Center | [jira-migration-tool](https://github.com/specivo/jira-migration-tool)（[文档](https://specivo.io/docs/jira-migration-tool/)） |
| Redmine | [redmine-migration-tool](https://github.com/specivo/redmine-migration-tool) |
| Request Tracker（RT） | [rt-migration-tool](https://github.com/specivo/rt-migration-tool)（[文档](https://specivo.io/docs/rt-migration-tool/)） |

如果是其他系统，可以用 [migration-sdk](https://github.com/specivo/migration-sdk) 生成符合[导入包格式](docs/import-bundle/v1.md)的文件。Specivo 一侧的导入步骤见[从其他系统导入](https://specivo.io/docs/specivo/install/importing/)。

## 技术栈

- Python 3.12、FastAPI、SQLAlchemy 2.0 async、Pydantic V2
- PostgreSQL 18（含 ltree 和 pgvector）、Redis
- Celery 处理后台任务
- 前端使用 Jinja2、Alpine.js 和 htmx

不需要 Elasticsearch，不需要外部搜索服务。搜索完全在 PostgreSQL 内部完成。

## 开发

需要 [uv](https://docs.astral.sh/uv/) 和 Docker。Node 可选：前端构建产物是生成出来的（从不提交到仓库），没有安装 `npm` 时会在容器里构建。

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

[specivo.io/docs/specivo](https://specivo.io/docs/specivo/)

**面向 AI 智能体**：[specivo-agent-skills](https://github.com/specivo/specivo-agent-skills) 提供即插即用的约定，教会 Claude Code、Codex、Cursor 以及任何 MCP 客户端用好 Specivo 的工具。

## 许可证

[AGPL-3.0-or-later](LICENSE)
