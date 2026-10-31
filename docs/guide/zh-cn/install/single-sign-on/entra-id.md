---
description: "将 Specivo 接入 Microsoft Entra ID：注册应用、使用租户专用的签发者、安全地传递组或应用角色，并避开邮箱声明和组超限这两个常见陷阱。"
---

# Microsoft Entra ID {#microsoft-entra-id}

本页介绍如何把 Specivo 接入 Microsoft Entra ID（原 Azure Active Directory）。请先阅读
[单点登录](index.md)了解基本概念。Entra ID 与其他提供商有一些事关安全的差别，所以在启用基础配置
以外的任何功能之前，先读完页末的注意事项。

示例值：Specivo 地址为 `https://specivo.example.com`（`APP_URL`），租户 ID 为
`11111111-2222-3333-4444-555555555555`，提供商 slug 为 `entra`。

!!! danger "切勿为 Entra ID 启用自动关联或邮箱信任"
    Entra 的 `email` 声明未经验证：用户自己可以修改，其他租户的管理员也能设置，而且 Entra 不发送
    `email_verified` 声明。**现有账户** 保持为“用户自行关联账户”，**此提供商确实会验证邮箱地址**
    保持关闭。用户在 **个人资料 → 已关联的账户**（账户菜单中）里自行关联账户。

## 1. 注册应用 {#1-register-the-application}

在 **Microsoft Entra admin center**（Microsoft Entra 管理中心）中，进入 **Identity → Applications → App registrations → New
registration**。

- **Name**：`Specivo`。
- **Supported account types**（支持的账户类型）：**Accounts in this organizational directory only**（仅此组织目录中的账户，即单租户）。
- **Redirect URI**：平台选 **Web**，值填
  `https://specivo.example.com/auth/oidc/entra/callback/`，与 Specivo 表单中显示的完全一致。

注册完成后：

- **Overview**：复制 **Application (client) ID** 和 **Directory (tenant) ID**。
- **Certificates & secrets → New client secret**：选择有效期，然后立即复制密钥的 **Value**。
  它只显示一次。不要复制 *Secret ID*。
- **Authentication**：在 **Web → Redirect URIs** 下添加
  `https://specivo.example.com/login/?logged_out=1`，这样 Entra 才能在退出登录后把用户送回 Specivo。
  Entra 只会重定向到这里登记过的地址。

!!! warning "客户端密钥会过期"
    Entra 密钥最长有效期为 24 个月。密钥一过期，所有登录都会失败并报 `sso_idp_error`。
    把到期日记进日历，在旧密钥过期前创建新密钥，并粘贴到 Specivo 中（或更新对应的环境变量）。

## 2. 添加 Specivo 需要的声明 {#2-add-the-claims-specivo-needs}

在 **Token configuration** 下：

- **Add optional claim**：令牌类型选 **ID**，勾选 **email**。不加的话 Entra 可能不发送邮箱，
  Specivo 也就无法在首次登录时创建账户（`sso_email_missing`）。如果门户提示添加 Microsoft Graph
  的 `email` 权限，接受即可。
- Specivo 中的 **邮箱声明** 保持为标准的 `email`。只有从 `email` 读取的邮箱，Specivo 才会视为
  已验证；`upn` 之类的自定义声明始终视为未验证。对 Entra 来说这正是想要的结果，但也意味着
  邮箱域名列表对它不起作用。

`preferred_username`（登录名，通常是 UPN）默认包含在 v2 ID 令牌中，Specivo 用它作为首次登录时
所创建账户的登录名。

## 3. 组与角色 {#3-groups-and-roles}

只有用到 **必需的组**、**管理员组** 或 **组映射** 时，Specivo 才需要组声明。三者都不用，可以跳过
这一步。如果要用，先读下面的组超限说明。

Entra 提供两种方式，选其一。

### 方式 A：分配给应用的组（推荐） {#option-a-groups-assigned-to-the-application-recommended}

1. **Token configuration → Add groups claim**。
2. 选择 **Groups assigned to the application**（分配给应用的组）。不要选 *All groups* 或 *Security groups*。
3. **ID** 的类型保持为 **Group ID**。
4. 保存。然后进入 **Enterprise applications → Specivo → Users and groups**，把允许使用 Specivo
   的组分配给该应用。

之后令牌中会带有 `groups`，即用户所属、且已分配给该应用的组的 **对象 ID**（GUID）列表。在 Specivo
中，**组声明** 填 `groups`，**必需的组**、**管理员组** 和 **组映射** 中的组值填 GUID，例如
`7c1b9d0e-aaaa-bbbb-cccc-0123456789ab`。GUID 在 **Groups → *组名* → Object Id** 下可以找到。Specivo 对输入的值做精确匹配，所以请直接粘贴 GUID；想用可读的名称，请改用方式 B。

### 方式 B：应用角色 {#option-b-app-roles}

1. **App registrations → Specivo → App roles → Create app role**。例如 **Display name**
   填 `Specivo administrators`，**Allowed member types** 选 *Users/Groups*，**Value** 填 `specivo-admins`。
2. 在 **Enterprise applications → Specivo → Users and groups** 中把用户或组分配给该角色。
3. 之后 ID 令牌会带有 `roles` 声明，即角色 **Value** 的列表。在 Specivo 中把
   **组声明** 设为 `roles`，并在各个组字段中使用这些值（`specivo-admins`）。

应用角色不受超限影响，名称也可读，能用就优先用它。

### 组超限 {#group-overage}

一个令牌只能容纳有限数量的组（JWT 中为 200 个）。用户所属的组超过这个数时，Entra 不再发送列表，
而是发送一个标记，这就是“组超限”（group overage）。Specivo 无法通过 Microsoft Graph API 读取组。
当组决定访问权限时（设置了 **必需的组**，或设置了 **管理员组** 且开启了管理员标志同步），Specivo
会以 `sso_groups_overage` 拒绝登录，而不是去猜。否则它照常让用户登录，且不改动任何组成员关系。

解决办法在 Entra 一侧：使用方式 A（只有分配给应用的组才计入，进入令牌的组会少得多）或方式 B。

## 4. 在 Specivo 中添加提供商 {#4-add-the-provider-in-specivo}

**管理 → 单点登录 → + 新建提供商**：

| 字段 | 值 |
|---|---|
| 按钮文字 | `Microsoft` |
| Slug | `entra` |
| 签发者（Issuer） | `https://login.microsoftonline.com/11111111-2222-3333-4444-555555555555/v2.0` |
| 客户端 ID | Application (client) ID |
| 客户端认证 | `client_secret_post` 或 `client_secret_basic` |
| 客户端密钥 | 密钥的 **Value** |
| 作用域 | `openid profile email` |
| 组声明 | 如果使用组，填 `groups` 或 `roles` |
| 接受后端通道登出 | **关闭** |

使用带你租户 ID（或其已验证域名）的 **租户专用** 签发者。不要使用
`common`、`organizations` 或 `consumers`：它们的令牌对每个租户给出不同的签发者，Specivo 无法验证。
末尾的 `/v2.0` 要保留。

点击 **测试连接**，并处理它显示的所有警告。

然后勾选 **已启用** 并保存。

## Entra ID 注意事项 {#notes-for-entra-id}

- **不支持后端通道登出**。Entra 只支持前端通道（front-channel）登出，而 Specivo 没有实现它。
  **接受后端通道登出** 保持关闭。从 Specivo 退出登录仍然可以结束 Entra 会话（RP 发起的登出，
  默认开启，使用第 1 步登记的登出后地址）。要让 Specivo 察觉用户已在 Entra 中被禁用，请在
  **作用域** 中加入 `offline_access`：Entra 只在带此作用域时才签发刷新令牌，而 Specivo 的
  [会话检查](index.md#the-session-check)需要刷新令牌。没有它，在 Entra 中禁用某人并不会结束其
  已打开的 Specivo 会话，因此请把 **最长会话时长** 设短一些，并同时在 Specivo 中停用该用户。
  参见[人员离职处理](index.md#offboarding-people)。更好的做法是开启
  [SCIM 预配](scim.md#microsoft-entra-id)：Entra 会在下一个预配周期停用该 Specivo 账户，
  包括其 API 密钥。
- **重新创建应用注册会改变所有 `sub`**。Entra 的 subject 对每个应用唯一。如果删除并重建注册，
  或者换到一个新的注册，所有人的已关联账户都无法再匹配，每个人都必须在个人资料中重新关联。
  请改为编辑现有注册。
- **邮箱与域名列表**。由于 Entra 的邮箱未经验证，**允许的邮箱域名** 不会产生任何可依赖的效果
  （信任关闭时它会拒绝所有人，这也是正确的行为）。请改用组、应用角色来限制访问，或在 Entra 中
  把用户分配给应用，并在 **Enterprise applications → Specivo → Properties** 下开启
  **Assignment required**（需要分配）。
- **来宾与多租户**。来自其他租户的来宾用户，其 `sub` 和邮箱格式都不同。单租户注册加上
  **Assignment required**，可以把能登录的人限定在你分配过的范围内。

## 出问题时 {#if-it-does-not-work}

| 现象 | 检查 |
|---|---|
| Microsoft 错误 AADSTS50011（回复 URL 不匹配） | 应用注册中的重定向 URI 与 Specivo 表单不完全一致。平台必须是 **Web**。 |
| AADSTS7000215（客户端密钥无效） | 粘贴的是 Secret ID 而不是 Value，或者密钥已过期。 |
| `sso_token_invalid` | 签发者是 `common`、缺少 `/v2.0`，或者没有使用你的租户 ID。 |
| `sso_email_missing` | 添加 `email` 可选声明，或在 Entra 中为该用户设置邮箱地址。 |
| `sso_groups_overage` | 参见[组超限](#group-overage)。 |
| `sso_not_in_required_group` | 未配置组声明，用户所在的组未分配给应用，或 Specivo 中填的 GUID 有误。 |

完整列表见[错误表](index.md#troubleshooting)。
