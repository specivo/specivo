---
description: Let your identity provider create, update and deactivate Specivo accounts and groups over SCIM 2.0. Setup in authentik, Microsoft Entra ID and Okta, what each action does in Specivo, how existing accounts are linked, and troubleshooting.
---

# Provisioning with SCIM

With SCIM (System for Cross-domain Identity Management, version 2.0) your identity provider (IdP)
pushes people and groups to Specivo instead of waiting for them to sign in:

- **People exist before their first sign-in**, so you can add them to projects, assign issues to
  them and mention them on day one.
- **Disabling or deleting someone at the IdP takes effect in Specivo at once**: the account is
  deactivated, every session ends and every API key is switched off. You do not have to wait for
  the [session check](index.md#the-session-check) or a back-channel logout, and API keys are
  covered too.
- **Groups follow the IdP**: an IdP group becomes a Specivo [user group](../../projects/index.md),
  and its members are kept in step.

SCIM works next to single sign-on, not instead of it: people still sign in through the same
provider's button on the login page. Set up the provider first (see [Single sign-on](index.md)),
then add SCIM to it.

## What Specivo supports

| | |
|---|---|
| Resources | Users and groups. |
| Operations | Create, read, list, replace (`PUT`), update (`PATCH`) and delete, for both. |
| Filters | `userName eq`, `externalId eq`, `emails[type eq "work"].value eq` (users), `displayName eq` and `externalId eq` (groups), `id eq`, joined with `and`. Other filters get `400 invalidFilter`. |
| Paging | `startIndex` and `count`, at most 200 results per page (`SCIM_MAX_RESULTS`). |
| Not supported | Bulk requests, sorting, ETags, passwords. A password sent by the IdP is ignored. |
| Authentication | A bearer token per provider, generated in Specivo. |

The base URL of each provider is `{APP_URL}/scim/v2/{slug}`, for example
`https://specivo.example.com/scim/v2/entra` (with a stealth prefix, the prefix comes before
`/scim`; the provider page shows the exact URL). Specivo also answers the discovery requests
(`/ServiceProviderConfig`, `/ResourceTypes`, `/Schemas`) that some IdPs send first.

## Turning it on in Specivo

1. Open **Administration → Single sign-on** and the provider. Save it first if it is new.
2. In the **Provisioning (SCIM)** card, copy the **SCIM base URL**.
3. Press **Generate token**. Copy the token at once: it is shown only this time. Specivo keeps only
   a hash of it, like an API key.
4. Enter the base URL and the token at the IdP (see the steps for your IdP below).
5. Tick **Accept provisioning from this provider**. Until then every SCIM request is refused.

The card shows the start of the current token, when the IdP last made a request, and how many
users and groups it provisioned.

**Rotate token** replaces the token: the old one stops working at once, so enter the new one at the
IdP straight away. **Revoke token** removes it and turns provisioning off. Accounts and groups
already provisioned stay as they are. Generating, rotating and turning provisioning on need an
administrator signed in to Specivo; an administrator's API key can revoke the token but not create
one.

A wrong or missing token, a provider with provisioning off, a provider that is switched off
(**Enabled** unticked) and a provider that does not exist all get the same `401` answer, so the
answer does not tell anyone which providers exist. Switching the provider off therefore stops
provisioning as well as sign-in.

## authentik

authentik provisions through a SCIM provider attached to the application you created for sign-in.

1. In the admin interface, open **Applications → Providers** and press **Create**. Choose
   **SCIM Provider**.
2. **URL**: the SCIM base URL from Specivo. **Token**: the token from Specivo.
3. Under **Attribute mapping**, keep **authentik default SCIM Mapping: User** and
   **authentik default SCIM Mapping: Group**. They send the username as `userName`, the email,
   the name and `active`.
4. Leave **Exclude service accounts** on. Under **Group filters**, pick the groups to send to
   Specivo; with none picked, every group is sent (each with only its provisioned members).
5. Open **Applications → Applications → Specivo** and add the new provider under
   **Backchannel Providers**. Save.

authentik provisions the users the application lets in: the users, groups and policies bound to
it under **Policy / Group / User Bindings**. An application without bindings lets everyone in, so
every user is provisioned. To provision only some people, bind a group to the application (which
also limits who can sign in), or give the SCIM provider an application of its own, bound to the
group to provision, and add the provider there as the backchannel provider instead.

authentik provisions when a user or group changes, and runs a full sync on a schedule. To start one
now, open the SCIM provider and press **Run sync again**. Its result, with any error Specivo
returned, is shown there and under **Events → Logs**.

## Microsoft Entra ID

Entra ID provisions from the enterprise application of your app registration.

1. Open **Microsoft Entra admin center → Enterprise applications → Specivo → Provisioning**.
   Press **Get started** (or **New configuration**) and set **Provisioning Mode** to
   **Automatic**.
2. **Tenant URL**: the SCIM base URL from Specivo. **Secret Token**: the token from Specivo.
   Press **Test Connection**.
3. Under **Mappings**, open **Provision Microsoft Entra ID Users** and check these attributes:

    | Specivo (SCIM) attribute | Entra ID attribute |
    |---|---|
    | `userName` | `userPrincipalName` (the default) |
    | `emails[type eq "work"].value` | `mail` |
    | `active` | `Switch([IsSoftDeleted], , "False", "True", "True", "False")` (the default) |
    | `displayName` | `displayName` |
    | `externalId` | `objectId` (the default is `mailNickname`; change it, see below) |
    | `preferredLanguage` | `preferredLanguage` (optional) |

    Other attributes in the default list (job title, addresses, phone numbers, the enterprise
    extension) are accepted and ignored. A user without `mail` cannot be provisioned: Specivo needs
    an email address.
4. To provision groups, keep **Provision Microsoft Entra ID Groups** enabled. Only groups assigned
   to the application, and their members who are also assigned, are sent.
5. Under **Settings**, set **Scope** to **Sync only assigned users and groups**, assign the users
   and groups under **Users and groups**, and set **Provisioning Status** to **On**.

Entra ID runs a cycle about every 40 minutes. To test one person, use **Provision on demand**.
Disabling someone in Entra (or removing their assignment) sends `active` false in the next cycle;
deleting them sends a delete about 30 days later, after the soft-delete period.

Entra's email claim is never verified, so for the first sign-in map `externalId` to `objectId`
(step 3) and set **SCIM externalId claim** to `oid` under **Claims** on the Specivo provider page.
Specivo then finds the provisioned account by the object id; see
[Signing in after provisioning](#signing-in-after-provisioning).

## Okta

Okta provisions from the application integration you created for sign-in.

1. Open the application, then **General → App Settings → Edit**, tick **Enable SCIM
   provisioning** and save.
2. On the new **Provisioning** tab, under **Integration**, press **Edit**:
    - **SCIM connector base URL**: the SCIM base URL from Specivo.
    - **Unique identifier field for users**: `userName`.
    - **Supported provisioning actions**: **Push New Users**, **Push Profile Updates** and
      **Push Groups**.
    - **Authentication Mode**: **HTTP Header**, with the token from Specivo as **Bearer** token.
   Press **Test Connector Configuration**, then **Save**.
3. Under **Provisioning → To App**, enable **Create Users**, **Update User Attributes** and
   **Deactivate Users**.
4. Assign people under **Assignments**. To provision groups, add them under **Push Groups**.

Okta deactivates a user (`active` false) when you unassign or deactivate them; it does not delete
users over SCIM.

## Keycloak and other providers

Keycloak has no built-in SCIM client; use [the session check and back-channel
logout](index.md#offboarding-people) there. Any other IdP that speaks SCIM 2.0 works the same way:
give it the base URL and the token as a bearer token.

## What each IdP action does in Specivo

| At the IdP | In Specivo |
|---|---|
| Create a user | A Specivo account is created: the `userName` becomes the login (see below), the email is marked verified because the IdP vouches for it, and the account has no password. If an account with that email already exists, it is linked instead; see [Existing accounts](#existing-accounts). |
| Change a user | Email, display name, language and time zone follow. The login follows a changed `userName` when the new login is free. |
| Disable a user (`active` false) | The account is deactivated at once, exactly like **Deactivate account** on the user's admin page: every session ends and every API key is switched off. |
| Enable a user again (`active` true) | The account is active again, if SCIM deactivated it. API keys stay off: the person switches them back on or creates new ones. An account deactivated in Specivo (by an administrator, or by a refused sign-in) stays deactivated until an administrator reactivates it. |
| Delete a user | The account is deactivated as above, removed from the groups this provider manages, and no longer visible to the IdP. It is not deleted, so issues, comments and history keep their author. If the IdP creates the same person again later, the same account comes back. |
| Create a group | A Specivo user group with that name. Give the group roles on projects under **Administration → Groups** or on a project's members page. |
| Change a group's members | People are added to or removed from the Specivo group. |
| Rename a group | The Specivo group is renamed. |
| Delete a group | The Specivo group is deleted, with its memberships and the project memberships it held. |

SCIM never sets a password, administrator rights or the service-account flag, whatever the IdP
sends. To make someone an administrator, do it in Specivo, or with the administrator groups of
[single sign-on](index.md#groups-and-roles).

**Administrators and service accounts are never deactivated, deleted or given another email
address through SCIM.** This matters for an account that became an administrator (or a service
account) in Specivo after it was provisioned. If the IdP disables, deletes or re-addresses such an
account, Specivo answers `400` with the reason and records the refusal (`scim_request_refused`);
the IdP shows it as a provisioning error. Remove the administrator rights in Specivo first, or
deactivate the account there. The other attributes (display name, language, time zone, group
memberships) still follow the IdP.

### Logins

The login is made from the `userName` the way single sign-on makes logins: lower case, letters,
digits, `_` and `-`, and only the part before the `@` of an email address. `Alice.Smith@example.com`
becomes `alice-smith`. When an email-shaped `userName` gives a login that another account already
has, the new account gets `alice-smith-2`. A `userName` starting with `$` is refused: such logins
belong to system accounts.

### Existing accounts

Most installs already have accounts when SCIM is turned on. When the IdP creates a user and an
account with the same email address (ignoring case) already exists, Specivo links that account
instead of creating a second one, but only when the provider's **Existing accounts** setting is
**Link automatically by verified email (never for administrators)**. That is the same choice that
lets sign-in link existing accounts ([Linking an existing account](index.md#linking-an-existing-account));
with the default, the IdP gets `409 uniqueness` and you link or rename the account by hand. The existing login and password stay, and the profile follows the IdP from then on. A
linked account that was deactivated in Specivo stays deactivated.

SCIM never changes the email address of an account that has a Specivo password: the IdP gets
`400 mutability`. A password reset goes to the account's email, so moving it would hand the
password to whoever the IdP names. Change it in Specivo, or remove the password first. Accounts
that sign in only through the IdP follow its email changes.

When the provider lists **Allowed email domains**, SCIM applies them as well: a user created or
updated with an email outside them gets `400 invalidValue`.

An account is **never** linked, and the IdP gets `409 uniqueness`, when it is:

- an administrator,
- a service account,
- a system account (a login starting with `$`),
- already provisioned by another provider's SCIM, or by this one under another `userName`.

An existing account whose email address was **never verified** (or that still waits for
verification) is linked too, but nothing on it is trusted: whoever set it up with that address may
not be the person the IdP provisions. Before it is marked verified, Specivo removes its password,
ends its sessions, switches off its API keys, deletes pending password-reset links and
disconnects its single sign-on identities. The audit log records this as `scim_user_created` with
the action `adopted_unverified`. The person then signs in through the IdP. Accounts with a
verified email keep their password, sessions and keys.

When the `userName` is itself a valid login (`alice`, not `alice@example.com`) and an account
already has that login, that account must have the user's email too: an account with the same
login and another email is `409 uniqueness` rather than linked, because the same login says
nothing about the same person. Resolve those by hand: change the email or the login of the
Specivo account, or remove the administrator flag if the account should be managed by the IdP.

A user, or a group, belongs to one provider's SCIM at most. A group name that any Specivo group
already uses is `409 uniqueness` too: an existing group, and the project roles it holds, is never
taken over by the IdP. Rename one of the two.

### Signing in after provisioning

At a provisioned person's first sign-in through the same provider, Specivo connects their IdP
identity to the provisioned account (whatever **Existing accounts** says in the provider's
sign-in policy) when one of these matches:

- **the email in the token**, under the same rules as automatic linking: the provider is trusted
  to verify email addresses (**This provider really verifies email addresses**) and the token says
  `email_verified: true`;
- **the SCIM `externalId`**, only if you set **SCIM externalId claim** under **Claims** to a
  claim the IdP guarantees to equal the `externalId` it sends over SCIM. It is off by default.
  For Microsoft Entra ID, map `externalId` to `objectId` and set the claim to `oid`.

**The username alone never connects an account.** If the username claim (`preferred_username`
unless you changed **Username claim**) equals a provisioned person's SCIM `userName` but the token
has no trusted, verified email, or another one, the sign-in is refused with
`sso_account_not_provisioned` and logged (`sso.account.scim_link_refused`). Specivo then does not
create a second account either, even with **Create accounts on first sign-in** on. Otherwise
somebody who can choose their own username at the IdP (or an IdP that sends no email) could
reach another person's account.

So set up one of the two matches before people sign in: trusted email where the IdP really
verifies it (Okta, Google, Keycloak with verified addresses), or the externalId claim (Entra ID,
whose email is never verified). authentik's default mapping sends `email_verified: false`; change
it to send `true` only if authentik really verifies addresses, then turn the trust setting on.

An account another provider provisioned is never connected, also not by **Link automatically by
verified email**. An account that became an administrator in Specivo after it was provisioned is
not connected automatically either; connect it from the person's profile.

## Groups: SCIM or the groups claim

With provisioning on, the provider's **Update Specivo group memberships from the mappings below at
every sign-in** setting is skipped: the IdP manages memberships through SCIM, and the groups claim
in the sign-in token would otherwise undo SCIM's changes at every sign-in. The provider page says so
under that setting. **Required groups** and the administrator groups still use the groups claim.

Memberships an administrator added by hand to a SCIM group stay; SCIM adds and removes only the
memberships it made. Only people provisioned by the same provider can be SCIM members of a group.

## Limits

| Setting | Default | Meaning |
|---|---|---|
| `SCIM_RATE_LIMIT_PER_MINUTE` | `600` | Requests per minute per provider. Over it, `429` with `Retry-After`; IdPs retry. |
| `SCIM_UNAUTHENTICATED_RATE_LIMIT_PER_MINUTE` | `30` | Requests with a wrong token, per client address. They never use up the provider's own allowance. |
| `SCIM_MAX_BODY_BYTES` | `1048576` | Largest request body. Larger requests get `413`. |
| `SCIM_MAX_RESULTS` | `200` | Most resources in one list response. |

Every SCIM change is logged (`specivo.scim`) and, with the security audit log, recorded as
`scim_user_created`, `scim_user_updated`, `scim_user_deprovisioned`, `scim_group_created`,
`scim_group_updated`, `scim_group_deleted` or `scim_request_refused`, with the provider as the
source. Deactivations are also recorded as `user_deprovisioned` with the reason
`scim_deprovision`. Token changes are recorded as `scim_config_changed`.

## Troubleshooting

| Symptom | Check |
|---|---|
| `401` on every request, also on the IdP's connection test | The provider is switched off, provisioning is not ticked in Specivo, the token was rotated or revoked, or the IdP sends it in another way than `Authorization: Bearer`. The slug in the base URL must be the provider's. |
| `401` after it worked | Somebody rotated or revoked the token. Generate a new one and enter it at the IdP. |
| `409 uniqueness` when creating a user | The `userName` exists already, the email belongs to an account that is never linked (an administrator, a service account, an account of another provider), or an account has that login with another email. See [Existing accounts](#existing-accounts). |
| `409 uniqueness` when creating a group | A Specivo group with that name exists. Rename one of them. |
| `400 invalidValue`: "A user needs an email address" | The user has no email at the IdP, or the email mapping is missing (Entra ID: map `mail`). |
| `400 invalidValue`: "Members must be users provisioned by this identity provider" | The group has members that are not provisioned (yet). Provision them first, or assign them to the application. |
| `400`: "This account is an administrator in Specivo" | The IdP tried to disable, delete or change the email of an administrator (or a service account). Remove the administrator rights in Specivo first. |
| A user stays inactive although the IdP enables them | The account was deactivated in Specivo, not through SCIM. Reactivate it under **Administration → Users**. |
| `400 invalidFilter` | The IdP sent a filter Specivo does not support. Check its attribute mapping for the matching attribute: it must be `userName`, `externalId` or the work email. |
| The person is provisioned but sign-in says `sso_account_not_provisioned` | Neither a trusted, verified email nor the externalId claim matched; a matching username alone is not enough. Turn on **This provider really verifies email addresses** where that is true, or set **SCIM externalId claim** (Entra ID: `oid`, with `externalId` mapped to `objectId`). The log line `sso.account.scim_link_refused` names the reason. |
| Group memberships from the token no longer change | Expected while provisioning is on; see [Groups: SCIM or the groups claim](#groups-scim-or-the-groups-claim). |
| `429` | The IdP sends more than `SCIM_RATE_LIMIT_PER_MINUTE` requests a minute. It retries on its own; raise the limit for a large first sync. |
