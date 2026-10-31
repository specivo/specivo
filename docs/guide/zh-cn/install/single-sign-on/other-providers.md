---
description: 连接 Okta、Google、Zitadel 和 Authelia 等 OpenID Connect 提供商的简要说明，涵盖颁发者格式、组声明和注销支持等差异点。
---

# Okta、Google、Zitadel 和 Authelia {#okta-google-zitadel-and-authelia}

Specivo 使用标准的 OpenID Connect，任何发布了发现文档的提供商都可以接入。
本页只说明这四家提供商各自的差异点，完整步骤见
[单点登录](index.md#setting-up-a-provider-the-outline)。与
[Keycloak](keycloak.md)、[authentik](authentik.md) 和 [Microsoft Entra ID](entra-id.md) 页面不同，
这里的内容没有逐屏实际验证过，而且身份提供商（IdP）的界面也会变化。请对照提供商的最新文档核对每一项
设置，并使用 Specivo 中的 **测试连接**：它会显示提供商声明支持哪些注销功能和客户端认证方式。

无论哪家提供商，重定向 URI 都要严格按 Specivo 表单显示的内容注册；如果提供商要求 PKCE，使用 `S256`；
除非测试连接显示提供商支持，否则不要开启 **接受反向通道注销**。

要启用[会话检查](index.md#the-session-check)，需要为客户端允许刷新令牌授权类型；如果提供商只在请求了
`offline_access` 时才签发刷新令牌（Okta、Zitadel 和 Authelia 都是如此），还要把它加入 **作用域**。
首次登录后，测试连接会显示会话是否受到检查。Google 只在带有特定请求参数时才签发刷新令牌，而 Specivo
不会发送这些参数，因此使用 Google 时不会检查会话：请把 **最长会话时长** 设得短一些。

## Okta {#okta}

- **应用**：创建一个 **OIDC - OpenID Connect** 应用，类型选 **Web Application**，授权类型选
  **Authorization Code**。在 **Sign-in redirect URIs** 下添加重定向 URI，在
  **Sign-out redirect URIs** 下添加 `{APP_URL}/login/?logged_out=1`。
- **颁发者**：使用组织授权服务器时为 `https://example.okta.com`；使用自定义授权服务器（例如名为
  `default` 的那个）时为 `https://example.okta.com/oauth2/default`。以你的服务器在发现文档中显示的地址
  为准：两个颁发者并不相同，令牌只会携带你实际使用的那一个。
- **用户组**：Okta 默认不发送用户组。在应用的 **Sign On** 设置中，找到 **OpenID Connect ID Token**，
  把 **Groups claim type** 设为 **Filter**，**Groups claim name** 设为 `groups`，再加一个筛选条件，例如
  **Matches regex** `.*`（或者只匹配 Specivo 所需用户组的前缀）。如果用的是自定义授权服务器，则改为在
  **Security → API → Authorization Servers → *server* → Claims** 下添加该声明。如果按这种方式配置，
  还要把 `groups` 作用域加入 Specivo 的 **作用域**。把 Specivo 的 **组声明** 设为 `groups`。
- **注销**：Okta 支持在提供商处注销（RP 发起的注销）。除非测试连接显示支持，否则不要开启反向通道注销。

## Google {#google}

- **客户端**：在 Google Cloud 控制台中，进入 **APIs & Services → Credentials → Create credentials →
  OAuth client ID**，类型选 **Web application**。在 **Authorized redirect URIs** 下添加重定向 URI。
  如果你使用 Google Workspace 并且只想允许本组织的用户，同意屏幕可以选 **Internal**。
- **颁发者**：`https://accounts.google.com`。
- **用户组**：Google **不发送组声明**。不要使用必需用户组、管理员用户组或用户组映射：一旦设置了必需
  用户组，所有人都无法登录。请改用 **允许的邮箱域名** 来限制访问。
- **允许的邮箱域名**：对于 Google Workspace 账户，Specivo 检查 Google 的 `hd`（托管域）声明，Google
  会根据账户所属的 Workspace 域设置该值。这是唯一一家会用到 `hd` 的 IdP。如果账户没有 `hd`（例如个人
  Google 账户），检查会退回到已验证邮箱的域名，这需要开启 **此提供商确实会验证邮箱地址**。Google 确实
  会为其账户设置 `email_verified`，所以在这里开启该选项是合理的。同时建议把同意屏幕设为
  **Internal**，这样只有你的 Workspace 用户能访问该客户端。
- **注销**：Google 不提供会话结束端点，也不支持反向通道注销。如果开启了在提供商处注销，测试连接会对
  前者给出警告。你可以关闭 **退出 Specivo 时也在提供商处退出**。这样退出 Specivo 时不会同时退出
  Google 账户。请改用较短的 **最长会话时长**。

## Zitadel {#zitadel}

- **应用**：在项目中创建一个类型为 **Web**、认证方式为 **Code** 的应用。如果想使用公共客户端
  （Specivo 中 **客户端认证** 选 `none`），就启用 PKCE；如果使用机密客户端，则使用密钥
  （`client_secret_basic`）。添加重定向 URI，并把 `{APP_URL}/login/?logged_out=1` 设为注销后 URI。
  如果希望角色和个人资料声明包含在 ID 令牌中，在 **Token Settings** 中启用
  **User roles inside ID Token** 和 **User info inside ID Token**；否则在 Specivo 中开启
  **同时从 userinfo 端点读取声明**。
- **颁发者**：你的 Zitadel 实例地址，例如 `https://zitadel.example.com`，不带路径，末尾不加斜杠。
- **用户组**：Zitadel 在 `urn:zitadel:iam:org:project:roles` 声明中发送角色，但它是以角色名为键的对象，
  而不是列表。Specivo 只读取字符串列表或单个字符串形式的声明，所以这个声明不能直接使用。如果需要用户组，
  可以用 Zitadel Action 添加一个包含角色名纯列表的声明，再把 Specivo 的 **组声明** 指向它。否则就不使用
  用户组，改用 Zitadel 自身的项目设置来控制访问。
- **注销**：Zitadel 提供会话结束端点。用测试连接查看你的版本是否声明支持反向通道注销。

## Authelia {#authelia}

- **客户端**：Authelia 在配置文件的 `identity_providers.oidc.clients` 下配置客户端。在 Specivo 中填写
  客户端 ID 和*明文*密钥。Authelia 只保存密钥的哈希值，所以请把明文值存入你的密码管理器。把
  `redirect_uris` 设为 Specivo 的重定向 URI，`scopes` 设为 `openid`、`profile`、`email` 和 `groups`，
  `token_endpoint_auth_method` 设为 `client_secret_basic`（或你在 Specivo 中选择的方式），并要求使用
  `S256` 的 PKCE。
- **颁发者**：Authelia 的公开地址，例如 `https://auth.example.com`，末尾不加斜杠。
- **用户组**：带上 `groups` 作用域后，Authelia 会在 `groups` 声明中发送用户所属的用户组。把 Specivo 的
  **组声明** 设为 `groups`。用户组名称与 Authelia 用户数据库中的完全一致。
- **注销**：大多数版本的 Authelia 没有 RP 发起的会话结束端点，也不支持反向通道注销。如果测试连接给出
  相关警告，请关闭 **退出 Specivo 时也在提供商处退出** 和 **接受反向通道注销**，并把 **最长会话时长**
  设得短一些。
