---
description: 汇总 Specivo 用户最常遇到问题的实用解答，涵盖无法登录或忘记密码、搜索无结果、AI 智能体权限错误、文件上传被拒以及注册已关闭等情况。
---

# 故障排查与常见问题 {#troubleshooting-faq}

这里是用户最常遇到的问题的简短解答。如果没有找到你的问题，请查阅指南中对应的章节，或者询问你的 Specivo 管理员。

## 无法登录或忘记密码 {#i-cant-sign-in-i-forgot-my-password}

先确认用户名和实例地址都没有错。忘记密码时，点击登录页上的 **忘记密码** 链接，系统会发送一封重置邮件；前提是运维人员已经配置好外发邮件（SMTP）。

如果收不到重置邮件，或者实例没有启用密码重置，请联系管理员帮你重置密码。多次登录失败可能会被暂时锁定，稍等片刻再试。

如果你所在的组织使用单点登录（SSO），请点击登录页上的提供商按钮。只通过提供商登录的账户没有密码，因此 **忘记密码** 也无法为它设置密码。单点登录失败时，登录页上的提示会说明原因，参见[单点登录故障排查](../install/single-sign-on/index.md#troubleshooting)。已停用的账户完全无法登录，请联系管理员重新启用。

## 搜索没有结果 {#search-returns-nothing}

Specivo 有两种搜索模式。**关键词搜索** 开箱即用，匹配工作项（Issue）、评论、维基（Wiki）页面和附件描述中的文字。**语义搜索**（按语义查找结果）则需要运维人员先下载在服务器本机运行的嵌入模型，再构建索引。

如果语义搜索没有结果，很可能是服务器数据目录中缺少模型文件，或者已有内容尚未建立索引。此时关键词搜索仍可正常使用。运维人员需要下载模型（`docker compose exec api python -m specivo.cli.download_model` 或 `make download-model`），再运行索引命令（`docker compose exec api python -m specivo.cli.backfill_embeddings`）。之后，新建和编辑的内容会在保存时自动索引：关键词搜索立即可见，语义搜索则在几秒后可见，前提是 `celery-embeddings` 容器正在运行。各个步骤、每一步的检查方法以及修复办法见[语义搜索与向量嵌入](../install/semantic-search.md)。另见[查找工作项](../issues/finding.md)。

如果搜索 `ACME-42` 这样的工作项编号只能找到该工作项本身，却找不到提及它的较早工作项或维基页面，说明搜索索引建立于支持编号匹配之前。管理员可以在 **管理 → 设置** 中点击 **立即重建索引**，或在命令行运行 `make reindex-fts` 重建索引。重建在后台分小批次执行。你编辑的内容会在保存时随即更新。

## AI 智能体报权限错误 {#an-ai-agent-gets-a-permission-error}

通过 [MCP 服务器](../ai-agents/index.md) 发起的工具调用若因权限错误失败，检查两点：

- **密钥所属用户在该项目中有角色**。每次写入都会校验调用用户的项目权限。如果 API 密钥属于一个在该项目中没有成员身份的用户（通常是服务账户），请为其添加合适的角色，一般是 **代理** 或 **Developer**（开发者）。
- **密钥能访问该项目**。仅限部分项目的密钥对其他所有项目都会返回 *not found*（未找到）。在 **个人资料 → API 密钥** 中创建一个不带此限制的密钥，或在限制中加入该项目。参见 [API 密钥](../ai-agents/api-keys.md#limit-a-key-to-some-projects)。

## 看不到工时记录 {#i-cant-see-time-entries}

查看工时需要项目中的 **查看工时记录** 权限，它与 **记录工时** 权限相互独立。默认情况下，**Reporter**（报告者）和非成员都没有该权限。角色也可能被限制为 **仅自己的工时记录**，此时只能看到自己记录的工时。请项目经理或管理员检查你的角色。参见[工时记录](../projects/time-tracking.md#who-can-do-what)。

## 脚本收到“CSRF validation failed”错误 {#a-script-gets-csrf-validation-failed}

使用登录 **cookie** 发出的请求，在任何会修改数据的操作（包括修改密码和退出登录）上都必须带上 `X-CSRF-Token` 请求头，其值取自 `csrf_token` cookie。脚本和智能体应改用 [API 密钥](../ai-agents/api-keys.md)，无需此令牌。

## 文本未经格式化显示 {#text-is-shown-without-formatting}

当描述、评论或页面太大或太复杂而无法格式化时，其上方会显示一条简短提示。内容并未丢失。参见[大小与复杂度限制](markdown.md#size-and-complexity-limits)。

## 文件上传被拒绝 {#a-file-upload-was-rejected}

Specivo 没有内置的文件类型限制，所以上传被拒几乎总是因为 **大小限制**。最大上传大小由运维人员设置（即 Web 服务器的 `client_max_body_size`）。向管理员询问具体限制，或者把文件拆分后再上传。参见[附件](../issues/attachments.md)。

## 注册已关闭 {#registration-is-closed}

如果注册页面不可用或拒绝新账户，说明实例运行在关闭注册的模式下，即自助注册已被关闭。请管理员为你创建账户或发送邀请。

## 提交没有出现在工作项上，或 Webhook 没有送达 {#commits-dont-show-up-on-an-issue-or-a-webhook-doesnt-arrive}

两者在项目设置中都有投递日志，查看需要 *管理项目设置* 权限。对于提交和拉取请求，打开 **设置 → 仓库 → 投递日志**：其中列出代码托管平台发送的每次投递，以及未关联任何内容的原因（参见[用投递日志排查问题](../integrations/repositories.md#troubleshooting-with-the-delivery-log)）。如果日志为空，代码托管平台自己的 Webhook 页面会显示 Specivo 返回的错误。对于外发 Webhook，打开 **设置 → Webhooks → 投递日志**，参见[投递日志](../integrations/webhooks.md#the-delivery-log)。
