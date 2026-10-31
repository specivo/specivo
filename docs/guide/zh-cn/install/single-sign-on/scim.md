---
description: "让身份提供商通过 SCIM 2.0 创建、更新和停用 Specivo 账户与用户组：authentik、Microsoft Entra ID 和 Okta 的配置步骤、各操作在 Specivo 中的效果、现有账户如何关联以及故障排查。"
---

# 通过 SCIM 预配账户 {#provisioning-with-scim}

通过 SCIM（System for Cross-domain Identity Management，跨域身份管理，2.0 版），身份提供商（IdP）可以把用户和用户组主动推送到 Specivo，而不必等他们自己登录：

- **用户在首次登录之前就已存在**，所以从第一天起你就可以把他们加入项目、给他们指派工作项（Issue）并提及他们。
- **在 IdP 禁用或删除某人，会立即在 Specivo 生效**：账户被停用，所有会话结束，所有 API 密钥关闭。不必等待[会话检查](index.md#the-session-check)或反向通道登出，而且 API 密钥也一并覆盖。
- **用户组跟随 IdP**：IdP 中的组会成为 Specivo 的[用户组](../../projects/index.md)，成员保持同步。

SCIM 是单点登录（SSO）的补充，不是替代：用户仍然通过登录页上同一提供商的按钮登录。先配置好提供商（见[单点登录](index.md)），再为它添加 SCIM。

## Specivo 支持的功能 {#what-specivo-supports}

| | |
|---|---|
| 资源 | 用户和用户组。 |
| 操作 | 两者都支持创建、读取、列出、替换（`PUT`）、更新（`PATCH`）和删除。 |
| 筛选 | 用户：`userName eq`、`externalId eq`、`emails[type eq "work"].value eq`；用户组：`displayName eq`、`externalId eq`；以及 `id eq`，可用 `and` 连接。其他筛选条件返回 `400 invalidFilter`。 |
| 分页 | `startIndex` 和 `count`，每页最多 200 条（`SCIM_MAX_RESULTS`）。 |
| 不支持 | 批量请求、排序、ETag、密码。IdP 发送的密码会被忽略。 |
| 认证 | 每个提供商一个 Bearer 令牌，在 Specivo 中生成。 |

每个提供商的基础 URL 是 `{APP_URL}/scim/v2/{slug}`，例如 `https://specivo.example.com/scim/v2/entra`（如果设置了隐身前缀，前缀位于 `/scim` 之前；提供商页面会显示确切的 URL）。部分 IdP 会先发送发现请求（`/ServiceProviderConfig`、`/ResourceTypes`、`/Schemas`），Specivo 也会响应这些请求。

## 在 Specivo 中开启 {#turning-it-on-in-specivo}

1. 打开 **管理 → 单点登录**，进入对应提供商。如果是新建的，先保存。
2. 在 **预配（SCIM）** 卡片中，复制 **SCIM 基础 URL**。
3. 点击 **生成令牌**。立即复制令牌：它只显示这一次。Specivo 只保存令牌的哈希值，与 API 密钥一样。
4. 在 IdP 中填入基础 URL 和令牌（见下文各 IdP 的步骤）。
5. 勾选 **接受来自此提供商的预配**。勾选之前，所有 SCIM 请求都会被拒绝。

卡片会显示当前令牌的开头几位、IdP 上次发出请求的时间，以及它预配了多少用户和用户组。

**轮换令牌** 会替换令牌：旧令牌立即失效，所以要马上把新令牌填到 IdP。**撤销令牌** 会删除令牌并关闭预配。已经预配的账户和用户组保持不变。生成令牌、轮换令牌和开启预配都需要管理员登录 Specivo 操作；管理员的 API 密钥可以撤销令牌，但不能创建令牌。

令牌错误或缺失、提供商未开启预配、提供商已关闭（未勾选 **已启用**）、提供商不存在，这几种情况返回的都是同一个 `401`，因此从响应无法推断有哪些提供商。所以关闭提供商会同时停止预配和登录。

## authentik {#authentik}

authentik 通过挂在登录应用上的 SCIM 提供商进行预配。

1. 在管理界面打开 **Applications → Providers**，点击 **Create**，选择 **SCIM Provider**。
2. **URL**：Specivo 的 SCIM 基础 URL。**Token**：Specivo 生成的令牌。
3. 在 **Attribute mapping**（属性映射）下保留 **authentik default SCIM Mapping: User** 和 **authentik default SCIM Mapping: Group**。它们会把用户名作为 `userName` 发送，并发送邮箱、姓名和 `active`。
4. 保持 **Exclude service accounts**（排除服务账户）开启。在 **Group filters** 下选择要发送到 Specivo 的组；不选则发送所有组（每个组只包含已预配的成员）。
5. 打开 **Applications → Applications → Specivo**，在 **Backchannel Providers** 下添加新建的提供商。保存。

authentik 预配的是该应用允许访问的用户，即在 **Policy / Group / User Bindings** 下绑定到应用的用户、组和策略。没有绑定的应用允许所有人访问，所以会预配所有用户。要只预配部分人，可以把一个组绑定到应用（这同时也限制了谁能登录），或者为 SCIM 提供商单独创建一个应用，把要预配的组绑定到该应用，并把提供商作为反向通道提供商添加到那里。

用户或组发生变化时 authentik 会立即预配，并按计划运行全量同步。要立即运行一次，打开 SCIM 提供商并点击 **Run sync again**。同步结果（包括 Specivo 返回的错误）会显示在该页面以及 **Events → Logs** 下。

## Microsoft Entra ID {#microsoft-entra-id}

Entra ID 从应用注册对应的企业应用进行预配。

1. 打开 **Microsoft Entra admin center → Enterprise applications → Specivo → Provisioning**。点击 **Get started**（或 **New configuration**），把 **Provisioning Mode** 设为 **Automatic**。
2. **Tenant URL**：Specivo 的 SCIM 基础 URL。**Secret Token**：Specivo 生成的令牌。点击 **Test Connection**。
3. 在 **Mappings** 下打开 **Provision Microsoft Entra ID Users**，检查以下属性：

    | Specivo（SCIM）属性 | Entra ID 属性 |
    |---|---|
    | `userName` | `userPrincipalName`（默认） |
    | `emails[type eq "work"].value` | `mail` |
    | `active` | `Switch([IsSoftDeleted], , "False", "True", "True", "False")`（默认） |
    | `displayName` | `displayName` |
    | `externalId` | `objectId`（默认是 `mailNickname`，请改掉，见下文） |
    | `preferredLanguage` | `preferredLanguage`（可选） |

    默认列表中的其他属性（职位、地址、电话号码、企业扩展）会被接受但忽略。没有 `mail` 的用户无法预配：Specivo 需要邮箱地址。
4. 要预配组，保持 **Provision Microsoft Entra ID Groups** 启用。只会发送分配给应用的组，以及这些组中同样被分配的成员。
5. 在 **Settings** 下把 **Scope** 设为 **Sync only assigned users and groups**，在 **Users and groups** 下分配用户和组，然后把 **Provisioning Status** 设为 **On**。

Entra ID 大约每 40 分钟运行一个周期。要测试单个用户，用 **Provision on demand**。在 Entra 中禁用某人（或移除其分配）会在下一周期发送 `active` 为 false；删除某人则在软删除期结束后约 30 天发送删除请求。

Entra 的邮箱声明永远不会被验证，所以为了让首次登录正常工作，请把 `externalId` 映射到 `objectId`（第 3 步），并在 Specivo 提供商页面的 **声明（Claims）** 下把 **SCIM externalId 声明** 设为 `oid`。这样 Specivo 就能按对象 ID 找到已预配的账户；见[预配后登录](#signing-in-after-provisioning)。

## Okta {#okta}

Okta 从你为登录创建的应用集成进行预配。

1. 打开应用，然后 **General → App Settings → Edit**，勾选 **Enable SCIM provisioning** 并保存。
2. 在新出现的 **Provisioning** 选项卡中，在 **Integration** 下点击 **Edit**：
    - **SCIM connector base URL**：Specivo 的 SCIM 基础 URL。
    - **Unique identifier field for users**：`userName`。
    - **Supported provisioning actions**：**Push New Users**、**Push Profile Updates** 和 **Push Groups**。
    - **Authentication Mode**：**HTTP Header**，以 Specivo 的令牌作为 **Bearer** 令牌。
   点击 **Test Connector Configuration**，然后点击 **Save**。
3. 在 **Provisioning → To App** 下启用 **Create Users**、**Update User Attributes** 和 **Deactivate Users**。
4. 在 **Assignments** 下分配用户。要预配组，在 **Push Groups** 下添加。

当你取消分配或停用某个用户时，Okta 会停用该用户（`active` 为 false）；它不会通过 SCIM 删除用户。

## Keycloak 及其他提供商 {#keycloak-and-other-providers}

Keycloak 没有内置 SCIM 客户端；在那里使用[会话检查和反向通道登出](index.md#offboarding-people)。其他任何支持 SCIM 2.0 的 IdP 用法相同：给它基础 URL，并把令牌作为 Bearer 令牌。

## IdP 各操作在 Specivo 中的效果 {#what-each-idp-action-does-in-specivo}

| 在 IdP | 在 Specivo |
|---|---|
| 创建用户 | 创建一个 Specivo 账户：`userName` 成为登录名（见下文），邮箱标记为已验证（因为 IdP 为其担保），账户没有密码。如果已有账户使用该邮箱，则关联该账户而不新建；见[现有账户](#existing-accounts)。 |
| 修改用户 | 邮箱、显示名、语言和时区跟随变更。`userName` 变更后，如果新登录名未被占用，登录名也跟随变更。 |
| 禁用用户（`active` 为 false） | 账户立即停用，效果与用户管理页上的 **停用账户** 完全相同：所有会话结束，所有 API 密钥关闭。 |
| 重新启用用户（`active` 为 true） | 如果账户是由 SCIM 停用的，则重新激活。API 密钥保持关闭：由本人重新开启或新建。在 Specivo 中停用的账户（由管理员停用，或因登录被拒绝而停用）保持停用，直到管理员重新激活。 |
| 删除用户 | 账户按上述方式停用，从此提供商管理的用户组中移除，且 IdP 不再能看到它。账户不会被删除，所以工作项、评论和历史记录仍保留其作者。如果 IdP 之后再次创建同一个人，还是同一个账户。 |
| 创建组 | 创建同名的 Specivo 用户组。在 **管理 → 用户组** 或项目的成员页面给该用户组分配项目角色。 |
| 修改组成员 | 在 Specivo 用户组中添加或移除对应用户。 |
| 重命名组 | Specivo 用户组同步重命名。 |
| 删除组 | Specivo 用户组被删除，连同其成员关系和它持有的项目成员身份。 |

无论 IdP 发送什么，SCIM 都不会设置密码、管理员权限或服务账户标志。要让某人成为管理员，在 Specivo 中操作，或使用[单点登录](index.md#groups-and-roles)的管理员组。

**管理员和服务账户永远不会通过 SCIM 被停用、删除或更改邮箱地址**。这一点对预配之后才在 Specivo 中成为管理员（或服务账户）的账户很重要。如果 IdP 禁用、删除此类账户或更改其邮箱，Specivo 会返回 `400` 并说明原因，同时记录这次拒绝（`scim_request_refused`）；IdP 会把它显示为预配错误。请先在 Specivo 中移除管理员权限，或直接在 Specivo 中停用该账户。其他属性（显示名、语言、时区、用户组成员关系）仍跟随 IdP。

### 登录名 {#logins}

登录名由 `userName` 生成，规则与单点登录生成登录名相同：小写，只含字母、数字、`_` 和 `-`，邮箱地址只取 `@` 之前的部分。`Alice.Smith@example.com` 变成 `alice-smith`。如果邮箱形式的 `userName` 生成的登录名已被其他账户占用，新账户会得到 `alice-smith-2`。以 `$` 开头的 `userName` 会被拒绝：这类登录名属于系统账户。

### 现有账户 {#existing-accounts}

大多数安装在开启 SCIM 时已经有账户了。当 IdP 创建用户，而已有账户使用相同邮箱地址（不区分大小写）时，Specivo 会关联该账户而不是再建一个，但前提是提供商的 **现有账户** 设置为 **按已验证邮箱自动关联（管理员除外）**。这与允许登录时关联现有账户的选项是同一个（见[关联现有账户](index.md#linking-an-existing-account)）；使用默认设置时，IdP 会收到 `409 uniqueness`，你需要手动关联或重命名账户。现有的登录名和密码保留，此后个人资料跟随 IdP。已在 Specivo 中停用的账户关联后仍保持停用。

SCIM 永远不会更改设有 Specivo 密码的账户的邮箱地址：IdP 会收到 `400 mutability`。密码重置邮件发往账户邮箱，改掉邮箱就等于把密码交给 IdP 指定的任何人。请在 Specivo 中修改邮箱，或先移除密码。只通过 IdP 登录的账户会跟随 IdP 的邮箱变更。

如果提供商设置了 **允许的邮箱域名**，SCIM 也会执行：用域名之外的邮箱创建或更新用户会得到 `400 invalidValue`。

以下账户 **永远不会** 被关联，IdP 会收到 `409 uniqueness`：

- 管理员，
- 服务账户，
- 系统账户（登录名以 `$` 开头），
- 已由另一个提供商的 SCIM 预配，或已由本提供商以另一个 `userName` 预配。

邮箱地址 **从未验证**（或仍在等待验证）的现有账户也会被关联，但其上的任何内容都不被信任：用这个地址注册的人未必就是 IdP 预配的那个人。在标记为已验证之前，Specivo 会移除其密码、结束其会话、关闭其 API 密钥、删除待处理的密码重置链接，并断开其单点登录身份。审计日志记录为 `scim_user_created`，动作为 `adopted_unverified`。之后该用户通过 IdP 登录。邮箱已验证的账户保留密码、会话和密钥。

如果 `userName` 本身就是有效的登录名（如 `alice`，而不是 `alice@example.com`），且已有账户使用该登录名，那么该账户还必须有这个用户的邮箱：登录名相同但邮箱不同的账户会得到 `409 uniqueness` 而不是被关联，因为登录名相同并不说明是同一个人。这类情况需要手动处理：修改 Specivo 账户的邮箱或登录名；如果该账户应由 IdP 管理，则移除其管理员标志。

一个用户或用户组最多属于一个提供商的 SCIM。与任何现有 Specivo 用户组同名的组同样会得到 `409 uniqueness`：IdP 永远不会接管现有用户组及其持有的项目角色。请重命名其中一个。

### 预配后登录 {#signing-in-after-provisioning}

已预配的用户首次通过同一提供商登录时，只要满足以下任一匹配条件，Specivo 就会把其 IdP 身份连接到已预配的账户（不论提供商登录策略中的 **现有账户** 如何设置）：

- **令牌中的邮箱**，规则与自动关联相同：提供商被信任会验证邮箱地址（**此提供商确实会验证邮箱地址**），且令牌中 `email_verified: true`；
- **SCIM `externalId`**，仅当你在 **声明（Claims）** 下把 **SCIM externalId 声明** 设为一个 IdP 保证与 SCIM 发送的 `externalId` 一致的声明时。此项默认关闭。对于 Microsoft Entra ID，把 `externalId` 映射到 `objectId`，并把该声明设为 `oid`。

**仅凭用户名永远不会连接账户**。如果用户名声明（默认为 `preferred_username`，除非你改过 **用户名声明**）等于某个已预配用户的 SCIM `userName`，但令牌中没有可信的已验证邮箱，或邮箱不一致，登录会被拒绝并返回 `sso_account_not_provisioned`，同时记录日志（`sso.account.scim_link_refused`）。此时即使开启了 **首次登录时创建账户**，Specivo 也不会创建第二个账户。否则，能在 IdP 自行选择用户名的人（或不发送邮箱的 IdP）就可能进入他人的账户。

所以在用户登录之前，先配置好两种匹配方式之一：IdP 确实验证邮箱时（Okta、Google、使用已验证地址的 Keycloak）用可信邮箱；否则用 externalId 声明（Entra ID 的邮箱永远不会被验证）。authentik 的默认映射发送 `email_verified: false`；只有在 authentik 确实验证地址的情况下，才把它改为发送 `true`，然后再开启信任设置。

由另一个提供商预配的账户永远不会被连接，即使开启了 **按已验证邮箱自动关联** 也一样。预配之后才在 Specivo 中成为管理员的账户也不会自动连接；请从该用户的个人资料中手动连接。

## 用户组：SCIM 还是 groups 声明 {#groups-scim-or-the-groups-claim}

开启预配后，提供商的 **每次登录时按下方映射更新 Specivo 用户组成员关系** 设置会被跳过：成员关系由 IdP 通过 SCIM 管理，否则登录令牌中的 groups 声明会在每次登录时撤销 SCIM 的变更。提供商页面在该设置下方有相应说明。**必需的组** 和管理员组仍使用 groups 声明。

管理员手动添加到 SCIM 用户组的成员保持不变；SCIM 只添加和移除它自己创建的成员关系。只有由同一提供商预配的用户才能成为用户组的 SCIM 成员。

## 限制 {#limits}

| 设置 | 默认值 | 含义 |
|---|---|---|
| `SCIM_RATE_LIMIT_PER_MINUTE` | `600` | 每个提供商每分钟的请求数。超出后返回 `429` 和 `Retry-After`；IdP 会重试。 |
| `SCIM_UNAUTHENTICATED_RATE_LIMIT_PER_MINUTE` | `30` | 令牌错误的请求数，按客户端地址计。这些请求不会消耗提供商自己的配额。 |
| `SCIM_MAX_BODY_BYTES` | `1048576` | 请求体的最大字节数。更大的请求返回 `413`。 |
| `SCIM_MAX_RESULTS` | `200` | 一次列表响应中最多返回的资源数。 |

每一次 SCIM 变更都会写入日志（`specivo.scim`），开启安全审计日志后还会记录为 `scim_user_created`、`scim_user_updated`、`scim_user_deprovisioned`、`scim_group_created`、`scim_group_updated`、`scim_group_deleted` 或 `scim_request_refused`，来源为该提供商。停用操作还会记录为 `user_deprovisioned`，原因为 `scim_deprovision`。令牌变更记录为 `scim_config_changed`。

## 故障排查 {#troubleshooting}

| 现象 | 检查 |
|---|---|
| 所有请求都返回 `401`，包括 IdP 的连接测试 | 提供商已关闭、Specivo 中未勾选预配、令牌已轮换或撤销，或 IdP 没有以 `Authorization: Bearer` 方式发送令牌。基础 URL 中的 slug 必须是该提供商的。 |
| 之前正常，现在返回 `401` | 有人轮换或撤销了令牌。生成新令牌并填到 IdP。 |
| 创建用户时 `409 uniqueness` | `userName` 已存在、邮箱属于永远不会被关联的账户（管理员、服务账户、另一提供商的账户），或已有账户使用该登录名但邮箱不同。见[现有账户](#existing-accounts)。 |
| 创建组时 `409 uniqueness` | 已有同名的 Specivo 用户组。重命名其中一个。 |
| `400 invalidValue`：“A user needs an email address” | 用户在 IdP 没有邮箱，或缺少邮箱映射（Entra ID：映射 `mail`）。 |
| `400 invalidValue`：“Members must be users provisioned by this identity provider” | 组中有尚未预配的成员。先预配他们，或把他们分配到应用。 |
| `400`：“This account is an administrator in Specivo” | IdP 试图禁用、删除管理员（或服务账户）或更改其邮箱。先在 Specivo 中移除管理员权限。 |
| IdP 已启用用户，但用户仍处于停用状态 | 账户是在 Specivo 中停用的，不是通过 SCIM。在 **管理 → 用户** 下重新激活。 |
| `400 invalidFilter` | IdP 发送了 Specivo 不支持的筛选条件。检查其属性映射中的匹配属性：必须是 `userName`、`externalId` 或工作邮箱。 |
| 用户已预配，但登录提示 `sso_account_not_provisioned` | 可信的已验证邮箱和 externalId 声明都没有匹配上；仅用户名匹配不够。在提供商确实验证邮箱时开启 **此提供商确实会验证邮箱地址**，或设置 **SCIM externalId 声明**（Entra ID：`oid`，并把 `externalId` 映射到 `objectId`）。日志行 `sso.account.scim_link_refused` 会说明原因。 |
| 令牌中的用户组成员关系不再生效 | 开启预配时属正常现象；见[用户组：SCIM 还是 groups 声明](#groups-scim-or-the-groups-claim)。 |
| `429` | IdP 每分钟发送的请求超过 `SCIM_RATE_LIMIT_PER_MINUTE`。它会自行重试；首次大规模同步时可以提高限制。 |
