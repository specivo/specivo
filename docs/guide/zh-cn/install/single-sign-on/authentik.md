---
description: "将 Specivo 接入 authentik：创建 OAuth2/OpenID 提供商和应用，启用授权码与刷新令牌授权，选择签名密钥，保留默认主体模式，发送组信息，配置注销，在 Specivo 中填写提供商，并处理人员离职。"
---

# authentik {#authentik}

本页介绍如何把 Specivo 接入 authentik。请先阅读 [单点登录](index.md)，了解相关概念以及 Specivo 各项设置的作用。以下步骤已在 authentik 2026.8 上验证：登录、首次登录时自动创建账户、组映射、在 authentik 处退出、后端通道注销以及会话检查均可正常工作。

示例值：Specivo 地址为 `https://specivo.example.com`（`APP_URL`），authentik 地址为 `https://authentik.example.com`，应用 slug 为 `specivo`，提供商 slug 为 `authentik`。

## 1. 在 authentik 中创建提供商和应用 {#1-create-the-provider-and-the-application-in-authentik}

在 authentik 管理界面中，进入 **Applications → Applications**（应用），点击 **New Application**（新建应用）。向导会同时创建应用及其提供商。你也可以先在 **Applications → Providers**（提供商）下创建提供商，再创建一个使用该提供商的应用。

**Application**（应用）

- **Name**（名称）：`Specivo`。**Slug**：`specivo`。该 slug 会成为颁发者（issuer）的一部分。

**Provider: OAuth2/OpenID Provider**（提供商：OAuth2/OpenID 提供商）

- **Authorization Flow**（授权流程）：`default-provider-authorization-implicit-consent` 会跳过同意页面。如果希望用户每次都确认，选择显式同意流程。
- 在 **Protocol settings**（协议设置）下：
    - **Client Type**（客户端类型）：**Confidential**（机密）。
    - **Client ID**：`specivo`，或使用自动生成的值。**Client Secret**：复制自动生成的值。
    - **Grant Types**（授权类型）：只勾选 **Authorization Code**（授权码）和 **Refresh token**（刷新令牌），其余全部取消。表单默认几乎勾选了所有授权类型，但 Specivo 不需要其他任何一种。如果缺少 **Authorization Code**（例如通过 API 或蓝图创建提供商时未指定 `grant_types`），authentik 会拒绝所有登录，用户会看到 `sso_idp_error`。如果缺少 **Refresh token**，authentik 仍会发放刷新令牌，但之后会拒绝使用，Specivo 便无法在 authentik 处检查会话（见 [会话检查](index.md#the-session-check)）；此时测试连接会提示提供商拒绝了刷新令牌。在 API 和蓝图中，这两个值分别为 `authorization_code` 和 `refresh_token`。
    - **Redirect URIs/Origins**（重定向 URI/来源）：添加 `https://specivo.example.com/auth/oidc/authentik/callback/`，匹配模式为 **Strict**（严格），类型为 **Authorization**（授权）。再添加 `https://specivo.example.com/login/?logged_out=1`，匹配模式为 **Strict**，类型为 **Post Logout**（注销后；API 和蓝图中为 `logout`）：这是 Specivo 在退出后返回的地址。不要使用正则表达式或通配符。
    - **Signing Key**（签名密钥）：选择一个证书密钥对，例如内置的 *authentik Self-signed Certificate*。**此项必填。** 不选签名密钥时，authentik 会用客户端密钥（`HS256`）签名令牌，Specivo 会拒绝这类令牌。选择密钥后，令牌为 `RS256`。
- 在 **Advanced flow settings**（高级流程设置）下，把 **Invalidation Flow**（失效流程）设为 `default-invalidation-flow`（名为 *Logout*）。当 Specivo 把用户送到 authentik 退出时，运行的就是这个流程（见 [注销](#3-logout)）。预选的 `default-provider-invalidation-flow` 只会告诉用户已离开该应用，authentik 会话仍保持打开。
- 在 **Advanced protocol settings**（高级协议设置）下：
    - **Scopes**（作用域）：保留 `openid`、`email`、`profile` 和 `offline_access` 的默认映射。`profile` 映射负责把 `groups` 声明放进令牌；`offline_access` 让 authentik 发放会话检查所需的刷新令牌。
    - **Subject Mode**（主体模式）：保留 **Based on the User's hashed ID**（基于用户的哈希 ID，默认值）。
    - **Include claims in id_token**（在 id_token 中包含声明）：开启（默认值），这样组和邮箱才会随 ID 令牌一起返回。
    - **Issuer mode**（颁发者模式）：保留 **Each provider has a different issuer, based on the application slug**（每个提供商根据应用 slug 使用不同的颁发者，默认值）。

!!! warning "之后不要再更改主体模式"
    Specivo 靠 authentik 发送的主体（subject）来识别用户。默认的哈希用户 ID 是一个 64 位字符的值，用户修改用户名或邮箱时不会变化。切换到其他模式会让每个人都得到不同的主体，所有人都必须重新连接账户。请在首次登录前选定模式，之后保持不变。

**谁可以使用该应用。** 在应用的 **Policy Bindings**（策略绑定）标签页中，绑定允许打开 Specivo 的用户组或用户。这是 authentik 自身的访问控制，在 Specivo 看到用户之前就已生效。如果需要，可以把 Specivo 的 **必需的组** 作为第二层限制。

## 2. 组 {#2-groups}

当应用使用默认的 `profile` 作用域映射时，authentik 会在 `groups` 声明中以字符串列表的形式发送用户所属组的名称，无需额外配置。在 Specivo 中，**组声明** 填 `groups`（默认值）。**必需的组**、**管理员组** 和 **组映射** 使用与 authentik 中完全相同的组名，例如 `specivo-users` 和 `specivo-admins`。

该声明列出的是用户所属的全部组，而不只是绑定到该应用的组。请为 Specivo 中要用到的组起明确的名字。不属于任何组的用户会得到一个空列表，因此 **必需的组** 会以 `sso_not_in_required_group` 拒绝他们。

## 3. 注销 {#3-logout}

按上述设置，**在 authentik 处退出** 即可正常工作。用户退出 Specivo 时，Specivo 会把浏览器带着 ID 令牌和注销后地址发送到提供商的会话结束端点（`https://authentik.example.com/application/o/specivo/end-session/`）。authentik 运行失效流程 `default-invalidation-flow`，不做任何询问就结束 authentik 会话，然后把浏览器送回 `https://specivo.example.com/login/?logged_out=1`。注销后地址必须以 **Post Logout** 类型注册（步骤 1），否则 authentik 不会把浏览器送回。如果用的是 `default-provider-invalidation-flow`，用户会回到 Specivo，但在 authentik 处仍保持登录状态，下一次点击 **使用 authentik 登录** 时无需密码即可通过。

**后端通道注销**（可选）会在 authentik 会话结束时同步结束 Specivo 会话：用户在 authentik 处退出、管理员结束其会话、用户被停用或删除时都会触发。authentik 会为每个会话发送一次注销请求，有时会发两次；Specivo 能处理重复请求。在 **Protocol settings** 下，把 **Logout URI**（注销 URI）设为 `https://specivo.example.com/auth/oidc/authentik/backchannel-logout/`，把 **Logout Method**（注销方式）设为 **Back-channel**（后端通道）。这些请求由 authentik 的 **worker** 发出，因此 worker 必须能通过该地址访问 Specivo。在 Specivo 中，勾选 **接受来自提供商的后端通道注销**。

## 4. 在 Specivo 中添加提供商 {#4-add-the-provider-in-specivo}

**管理 → 单点登录 → + 新建提供商**：

| 字段 | 值 |
|---|---|
| 按钮文字 | `authentik` |
| 标识串 | `authentik` |
| 颁发者 | `https://authentik.example.com/application/o/specivo/` |
| 客户端 ID | 提供商中的客户端 ID |
| 客户端认证 | `client_secret_basic` |
| 客户端密钥 | 提供商中的客户端密钥 |
| 作用域 | `openid profile email offline_access` |
| 组声明 | `groups` |
| 会话续期时在提供商处检查会话 | 开启（默认值） |
| 接受后端通道注销 | 如果在步骤 3 中做了设置，则开启 |

颁发者 **以斜杠结尾**，并包含应用 slug。请直接从 authentik 中提供商页面显示的 **Issuer** 复制，不要手动输入：少一个斜杠就会导致颁发者不一致，登录失败。

保存后，点击 **测试连接**，勾选 **已启用**，再保存一次。测试连接应报告会话结束端点和后端通道注销支持。首次登录之后，它还应显示会话会在提供商处检查；如果提示提供商未发送刷新令牌，说明 **作用域** 或提供商的作用域映射中缺少 `offline_access`。关于账户、JIT 和组，见 [单点登录](index.md#who-gets-an-account)。

## authentik 注意事项 {#notes-for-authentik}

- **邮箱验证。** authentik 默认的 `email` 作用域映射始终发送 `email_verified: false`。在这种情况下，请不要开启 **此提供商确实会验证邮箱地址**：开了也没有效果，因为 Specivo 永远收不到已验证的地址。实际影响是：用户的 Specivo 邮箱不会跟随 authentik 更新，自动关联永远不会生效，首次登录时创建的账户邮箱为未验证状态，**允许的邮箱域名** 会拒绝所有人。请用 **必需的组** 或 authentik 的策略绑定来限制谁可以登录，并让用户在个人资料中自行连接已有账户。只有当 authentik 的来源确实会验证邮箱时，才用你自己的、返回 `email_verified: true` 的映射替换默认的 `email` 映射，然后再开启 Specivo 中的这项设置。
- **语言和时区。** 默认映射既不发送 `locale` 也不发送 `zoneinfo`。要从 authentik 设置用户的语言，添加一个返回 `locale` 声明的作用域映射，并在提供商上选中它。
- **测试用户。** 分别用一个属于已映射组的用户和一个不属于任何组的用户试一下；如果设置了必需的组，确认第二个用户被拒绝（`sso_not_in_required_group`）。

## 人员离职 {#offboarding}

要收回某人的访问权限：

1. **在 authentik 中停用该用户**（或删除）。authentik 会立刻拒绝其刷新令牌，因此该用户的 Specivo 会话会在下次续期时结束（15 分钟内）；如果开启了后端通道注销，则会立即结束。
2. 如果使用了 API 密钥，可以在 **当提供商结束会话时** 一项选择 **结束其所有会话并停用其 API 密钥**（这样停用触发的后端通道注销会停用没有 Specivo 密码的用户的密钥），或者同时在 Specivo 中停用该用户（**管理 → 用户 → 停用账户**），后一种方式对有密码的用户同样有效。

只在 authentik 中结束某人的会话（用户的 **Sessions**（会话）标签页）而不停用，只是一次退出，不算离职处理：其为 `offline_access` 发放的刷新令牌仍然有效，因此只有后端通道注销会结束 Specivo 会话，而且该用户可以再次登录。

## 如果不起作用 {#if-it-does-not-work}

| 现象 | 检查 |
|---|---|
| authentik 提示 “Redirect URI Error” | 重定向 URI 与 Specivo 表单中的值不完全（严格）匹配。 |
| 点击按钮后立刻出现 `sso_idp_error`，没有出现 authentik 登录页 | 提供商未允许 **Authorization Code** 授权类型。Specivo 日志中会显示 `invalid_request`。 |
| 登录后出现 `sso_idp_error` | 密钥错误，或该用户没有该应用的访问权限（策略绑定）。 |
| `sso_token_invalid` | 缺少签名密钥（HS256 令牌会被拒绝），或颁发者与 authentik 的不一致（末尾斜杠或应用 slug 不同）。 |
| 组缺失 | 提供商丢失了 `profile` 作用域映射，或 **Include claims in id_token** 已关闭。 |
| 退出后 authentik 会话仍打开，或 authentik 没有返回 Specivo | 失效流程是 `default-provider-invalidation-flow` 而不是 `default-invalidation-flow`，注销后地址未以 **Post Logout** 类型注册，或 Specivo 中的 **退出 Specivo 时也在提供商处退出** 已关闭。 |
| 后端通道注销没有任何效果 | authentik worker 无法访问注销 URI，或 **接受来自提供商的后端通道注销** 已关闭。 |
| 测试连接：提供商未发送刷新令牌 | Specivo 的 **作用域** 中没有 `offline_access`，或提供商丢失了 `offline_access` 作用域映射。 |
| 测试连接：提供商拒绝了刷新令牌 | **Grant Types** 下未勾选 **Refresh token**。 |

完整列表见 [错误表](index.md#troubleshooting)。
