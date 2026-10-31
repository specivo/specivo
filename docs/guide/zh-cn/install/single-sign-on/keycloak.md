---
description: "将 Specivo 接入 Keycloak：创建启用 PKCE 的机密 OpenID Connect 客户端，注册重定向与后端通道注销地址，发送组信息，在 Specivo 中填写提供商，并在人员离职时收回访问权限。"
---

# Keycloak {#keycloak}

本页介绍如何把 Specivo 接入一个 Keycloak realm。请先阅读[单点登录](index.md)，了解相关概念以及 Specivo 各项设置的作用。下面给出的值，就是 Specivo 自己的兼容性测试所使用的值。

示例值：Specivo 地址为 `https://specivo.example.com`（`APP_URL`），Keycloak 地址为 `https://keycloak.example.com`，realm 为 `example`，提供商 slug 为 `keycloak`。

## 1. 在 Keycloak 中创建客户端 {#1-create-the-client-in-keycloak}

在 **Admin console**（管理控制台）中选择你的 realm，然后进入 **Clients → Create client**。

**General settings**（常规设置）

- **Client type**：OpenID Connect。
- **Client ID**：`specivo`。稍后在 Specivo 中填写同样的值。

**Capability config**（能力配置）

- **Client authentication**（客户端认证）：**On**。这样它就是一个带密钥的机密客户端。
- **Authorization**：Off。
- **Authentication flow**：只勾选 **Standard flow**。取消勾选 **Direct access grants**，隐式流程和服务账户保持关闭。

**Login settings**（登录设置）

- **Valid redirect URIs**：`https://specivo.example.com/auth/oidc/keycloak/callback/`。必须与 Specivo 表单中显示的完全一致，不要用通配符。Keycloak 按字面匹配该地址。
- **Valid post logout redirect URIs**：`https://specivo.example.com/login/?logged_out=1`。
- **Web origins**：登录不需要。

保存客户端，然后：

- **Advanced → Advanced settings → Proof Key for Code Exchange Code Challenge Method**：**S256**。
  这样 Keycloak 会强制要求 PKCE，而 Specivo 总是会发送 PKCE。
- **Settings → Logout settings**（仅当你需要后端通道注销时）：
    - **Front channel logout**：Off。
    - **Backchannel logout URL**：`https://specivo.example.com/auth/oidc/keycloak/backchannel-logout/`
    - **Backchannel logout session required**：**On**。这样 Specivo 只会结束发起注销的那个浏览器会话，
      而不是该用户的所有会话。
    - **Backchannel logout revoke offline sessions**：Off。
- **Credentials**：复制 **Client secret**。

!!! note "后端通道注销要求 Keycloak 能访问 Specivo"
    后端通道注销是 Keycloak 服务器向 Specivo 发出的请求。该地址必须能从 Keycloak 一侧解析并访问，
    且 Keycloak 必须信任 Specivo 的证书。

## 2. 发送组信息 {#2-send-the-groups}

如果你在 Specivo 中不使用用户组，可跳过这一步。

**组。** Keycloak 默认不会把组放进令牌。需要在客户端自身的作用域中添加一个映射器：

1. **Clients → specivo → Client scopes → specivo-dedicated → Configure a new mapper → Group
   Membership**。
2. **Name**：`groups`。**Token Claim Name**：`groups`。
3. **Full group path**：**Off**。开启时组以 `/engineering/admins` 的形式传来；关闭时则是 `admins`。
4. **Add to ID token**：**On**。如果打算使用 **同时从 userinfo 端点读取声明**，也要同时加入 userinfo。

然后在 Specivo 中，**组声明** 填 `groups`（默认值），**必需的组**、**管理员组** 和 **组映射** 使用与 Keycloak
中完全相同的组名。

**用角色代替组。** 你也可以使用角色。Keycloak 的 realm 角色位于 `roles` 客户端作用域中，声明为
`realm_access.roles`；客户端角色则在 `resource_access.<client ID>.roles` 中。把 Specivo 的 **组声明**
设为对应的点分路径，例如 `realm_access.roles`。确保角色映射器开启了 **Add to ID token**（在
**Client scopes** 下打开 `roles` 客户端作用域，检查其映射器），或者在 Specivo 中开启
**同时从 userinfo 端点读取声明**。

## 3. 在 Specivo 中添加提供商 {#3-add-the-provider-in-specivo}

**管理 → 单点登录 → + 新建提供商**：

| 字段 | 值 |
|---|---|
| 按钮文字 | `Keycloak` |
| Slug | `keycloak` |
| 签发者（Issuer） | `https://keycloak.example.com/realms/example` |
| 客户端 ID | `specivo` |
| 客户端认证 | `client_secret_basic` |
| 客户端密钥 | 从 **Credentials** 标签页复制的密钥 |
| 作用域 | `openid profile email` |
| 组声明 | `groups`（或 `realm_access.roles`） |
| 会话续期时向提供商核查会话 | 开（默认） |
| 接受后端通道注销 | 开（如果你在第 1 步中配置了） |

签发者就是 realm 地址，末尾没有斜杠，也不带 `/.well-known/...` 部分，Specivo 会自行补上。不要在作用域中加
`offline_access`：Keycloak 对 `openid` 已经会签发会话检查所需的刷新令牌，而离线令牌在 Keycloak 退出登录后仍然有效。

保存后点击 **测试连接**，然后勾选 **已启用** 再保存一次。测试结果应显示支持后端通道注销。其余选项（账户、JIT、用户组）见
[单点登录](index.md#who-gets-an-account)。

## Keycloak 注意事项 {#notes-for-keycloak}

- **邮箱验证。** Keycloak 根据每个用户的 *Email verified* 标志设置 `email_verified`，而且默认情况下用户可以自行修改邮箱，
  除非你在 realm 的用户资料设置中关闭了该功能。只有当你的 realm 在设置该标志前会先验证邮箱
  （**Realm settings → Login → Verify email**）时，才开启 **此提供商确实会验证邮箱地址**，并考虑关闭
  **Edit username** 以及用户的邮箱编辑权限。
- **Subject。** Keycloak 的 `sub` 是用户的内部 ID。在 Keycloak 中删除并重建用户会得到新的 `sub`，
  该用户必须重新关联。
- **语言。** 如果希望界面语言跟随 Keycloak，添加一个把用户 `locale` 属性放进 ID 令牌的映射器，
  并把 **语言声明** 保持为 `locale`。
- **注销。** 除非收到 `id_token_hint`（Specivo 会发送），Keycloak 在注销时会显示确认页面。
  确保第 1 步中的注销后重定向地址已注册，否则 Keycloak 不会把用户送回 Specivo。
- **Realm 会话时长。** 开启会话检查后，Specivo 会话会随背后的 Keycloak 会话一起结束：
  **Realm settings → Sessions → SSO Session Idle**（默认 30 分钟）和 **SSO Session Max**（10 小时）
  与 Specivo 的 **最长会话时长** 同时生效，取其中最短者决定何时要求用户重新登录。如果有人离开 Specivo
  的时间超过空闲超时，下次访问时就需要重新登录。如果觉得太频繁，可以提高空闲超时，或为该提供商关闭会话检查。

## 人员离职 {#offboarding}

当你禁用或删除用户时，Keycloak **不会** 发送后端通道注销。要收回某人的访问权限：

1. **禁用该用户**（**Users → *用户* → Enabled: off**），或直接删除。Keycloak 会立刻拒绝其刷新令牌，
   因此在开启会话检查的情况下，其 Specivo 会话会在下次续期时结束，最多 15 分钟。
2. 若要 **立即** 结束会话，还要打开该用户的 **Sessions** 标签页并点击 **Sign out**。如果已配置
   后端通道注销，Keycloak 会通知 Specivo 立即结束会话。
3. 如果你使用 API 密钥，把 **当提供商结束会话时** 设为 **结束其所有会话并关闭其 API 密钥**
   （这样第 2 步也会关闭没有 Specivo 密码的用户的密钥），或者同时在 Specivo 中停用该用户
   （**管理 → 用户 → 停用账户**），后者对设有密码的用户同样有效。

如果没有会话检查（已关闭，或客户端不签发刷新令牌），仅做第 1 步会让 Specivo 会话一直持续到
**最长会话时长**；这时务必执行第 2 步。

## 遇到问题时 {#if-it-does-not-work}

| 现象 | 检查 |
|---|---|
| Keycloak 显示“Invalid parameter: redirect_uri” | Keycloak 中的重定向 URI 与 Specivo 表单上的不是逐字节一致。检查 `APP_URL`、slug 和末尾斜杠。 |
| “Invalid parameter: code_challenge_method”或类似错误 | Keycloak 客户端设置的 PKCE 方法不是 S256。设为 S256 或留空。 |
| Keycloak 接受密码后出现 `sso_idp_error` | 客户端密钥错误，或客户端是公开客户端而 Specivo 发送了密钥。 |
| `sso_token_invalid` | 签发者不匹配。在浏览器中打开 `{issuer}/.well-known/openid-configuration`，比较其中的 `issuer` 与字段值。如果 Keycloak 位于代理之后，其配置的主机名必须是对外公开的那个。 |
| `sso_not_in_required_group` | 缺少组映射器、**Add to ID token** 已关闭，或组名不一致（开启了完整路径，或大小写不同）。 |

完整列表见[错误表](index.md#troubleshooting)。
