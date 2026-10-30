---
description: Connect Specivo to Microsoft Entra ID — register the app, use the tenant-specific issuer, send groups or app roles safely, and avoid the email and group-overage pitfalls.
---

# Microsoft Entra ID

This page connects Specivo to Microsoft Entra ID (formerly Azure Active Directory). Read
[Single sign-on](index.md) first for the concepts. Entra ID differs from other providers in ways
that matter for security, so read the notes at the end before you enable anything beyond the basics.

Sample values: Specivo at `https://specivo.example.com` (`APP_URL`), tenant ID
`11111111-2222-3333-4444-555555555555`, provider slug `entra`.

!!! danger "Never enable automatic linking or email trust for Entra ID"
    Entra's `email` claim is not verified, it can be changed by the user or set by another tenant's
    administrator, and Entra sends no `email_verified` claim. Leave **Existing accounts** on
    "Users connect their account themselves" and leave **This provider really verifies email
    addresses** off. People connect their account from **Profile → Connected accounts** (in the account menu).

## 1. Register the application

In the **Microsoft Entra admin center**, go to **Identity → Applications → App registrations → New
registration**.

- **Name**: `Specivo`.
- **Supported account types**: **Accounts in this organizational directory only** (single tenant).
- **Redirect URI**: platform **Web**, value
  `https://specivo.example.com/auth/oidc/entra/callback/`, exactly as the Specivo form shows it.

After registering:

- **Overview**: copy the **Application (client) ID** and the **Directory (tenant) ID**.
- **Certificates & secrets → New client secret**: choose an expiry and copy the secret **Value**
  now. It is shown once. Do not copy the *Secret ID*.
- **Authentication**: under **Web → Redirect URIs** add
  `https://specivo.example.com/login/?logged_out=1` so Entra can send people back after sign-out.
  Entra only redirects to addresses registered here.

!!! warning "The client secret expires"
    Entra secrets last at most 24 months. When one expires, every sign-in fails with `sso_idp_error`.
    Put the date in a calendar, create the new secret before the old one expires, and paste it in
    Specivo (or update the environment variable).

## 2. Add the claims Specivo needs

Under **Token configuration**:

- **Add optional claim**: token type **ID**, select **email**. Without it Entra may send no email,
  and Specivo cannot create accounts on first sign-in (`sso_email_missing`). Accept the offer to
  add the Microsoft Graph `email` permission if the portal asks.
- Keep the **Email claim** in Specivo set to the standard `email`. Specivo only treats an email as
  verified when it is read from `email`; a custom claim such as `upn` is always unverified. For
  Entra that is the right outcome, but it also means the email domain list does not work with it.

`preferred_username` (the sign-in name, usually the UPN) is in v2 ID tokens by default and is what
Specivo uses for the login name of accounts created on first sign-in.

## 3. Groups and roles

Specivo needs a groups claim only for **Required groups**, **Administrator groups** and **Group
mappings**. Skip this step if you use none of them. If you do use them, read the overage note
below first.

Entra gives you two ways. Choose one.

### Option A: groups assigned to the application (recommended)

1. **Token configuration → Add groups claim**.
2. Select **Groups assigned to the application**. Do not select *All groups* or *Security groups*.
3. For **ID**, keep the type **Group ID**.
4. Save. Then go to **Enterprise applications → Specivo → Users and groups** and assign the
   groups that may use Specivo.

The token then contains `groups`, a list of the **object IDs** (GUIDs) of the assigned groups the
user belongs to. In Specivo the **Groups claim** is `groups`, and the group values in **Required
groups**, **Administrator groups** and **Group mappings** are the GUIDs, for example
`7c1b9d0e-aaaa-bbbb-cccc-0123456789ab`. Find them under **Groups → *name* → Object Id**. Specivo matches the values you enter exactly, so paste the GUIDs, or use Option B for
readable names.

### Option B: app roles

1. **App registrations → Specivo → App roles → Create app role**. For example **Display name**
   `Specivo administrators`, **Allowed member types** *Users/Groups*, **Value** `specivo-admins`.
2. Assign users or groups to the role in **Enterprise applications → Specivo → Users and groups**.
3. The ID token then carries the `roles` claim, a list of the role **values**. In Specivo set the
   **Groups claim** to `roles` and use the values (`specivo-admins`) in the group fields.

App roles are not subject to overage and give readable names, so prefer them when you can.

### Group overage

A token can hold only a limited number of groups (200 in a JWT). If a user is in more than that,
Entra sends a marker instead of the list, the "group overage". Specivo cannot read the groups from
the Microsoft Graph API. When groups decide access (**Required groups**, or **Administrator
groups** with the administrator flag sync on), Specivo refuses the sign-in with
`sso_groups_overage` rather than guess. Otherwise it signs the person in and changes no
memberships.

The fix is in Entra: use Option A (only the groups assigned to the application count, so far fewer
reach the token) or Option B.

## 4. Add the provider in Specivo

**Administration → Single sign-on → + New provider**:

| Field | Value |
|---|---|
| Button label | `Microsoft` |
| Slug | `entra` |
| Issuer | `https://login.microsoftonline.com/11111111-2222-3333-4444-555555555555/v2.0` |
| Client ID | The Application (client) ID |
| Client authentication | `client_secret_post` or `client_secret_basic` |
| Client secret | The secret **Value** |
| Scopes | `openid profile email` |
| Groups claim | `groups` or `roles`, if you use groups |
| Accept back-channel logout | **Off** |

Use the **tenant-specific** issuer with your tenant ID (or its verified domain). Do not use
`common`, `organizations` or `consumers`: their tokens carry a different issuer for each tenant, so
Specivo cannot validate them. Keep `/v2.0` at the end.

Press **Test connection** and fix any warnings it shows.

Then tick **Enabled** and save.

## Notes for Entra ID

- **No back-channel logout.** Entra supports only front-channel logout, which Specivo does not
  implement. Leave **Accept back-channel logout** off. Signing out of Specivo can still end the
  Entra session (RP-initiated logout, on by default, with the post-logout address registered in
  step 1). To have Specivo notice a person disabled in Entra, add `offline_access` to the
  **Scopes**: Entra issues refresh tokens only for it, and Specivo's
  [session check](index.md#the-session-check) needs one. Without it, disabling someone in Entra
  does not end an open Specivo session, so set a short **Maximum session length**, and deactivate
  the user in Specivo too. See [Offboarding people](index.md#offboarding-people).
- **Recreating the app registration changes every `sub`.** Entra's subject is unique per
  application. If you delete and recreate the registration, or move to a new one, nobody matches
  their connected account any more and everybody must connect again from their profile. Edit the
  existing registration instead.
- **Email and domain list.** Because Entra's email is not verified, **Allowed email domains** has
  no effect that you can rely on (it refuses everybody when trust is off, as it should). Limit
  access with groups, app roles, or by assigning users to the application in Entra and turning on
  **Assignment required** under **Enterprise applications → Specivo → Properties**.
- **Guests and multi-tenant.** Guest users from other tenants have a different `sub` and email form.
  Single-tenant registration plus **Assignment required** keeps the sign-in list to the people you
  assigned.

## If it does not work

| Symptom | Check |
|---|---|
| Microsoft error AADSTS50011 (reply URL mismatch) | The redirect URI in the app registration is not identical to the Specivo form. The platform must be **Web**. |
| AADSTS7000215 (invalid client secret) | You pasted the secret ID, not the Value, or the secret expired. |
| `sso_token_invalid` | The issuer is `common` or lacks `/v2.0`, or does not use your tenant ID. |
| `sso_email_missing` | Add the `email` optional claim, or give the user a mail address in Entra. |
| `sso_groups_overage` | See [Group overage](#group-overage). |
| `sso_not_in_required_group` | The group claim is not configured, the user's group is not assigned to the application, or the GUID in Specivo is wrong. |

The full list is in the [error table](index.md#troubleshooting).
