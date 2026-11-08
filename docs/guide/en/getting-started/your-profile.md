---
description: Your profile page — your details, changing your email address with a confirmation link, and connected sign-in accounts.
---

# Your profile

Open **Profile** from your account menu (your avatar and name in the header). It shows your
display name, login, email address, language, timezone and when you last signed in. You can change
your display name and photo there, and your email address. Language, timezone and your sessions
are under **Preferences**.

The profile also links to **My AI subscriptions**, where you record the flat-rate AI plans you pay
for, such as Claude Max or ChatGPT Plus; see [AI subscriptions](../install/ai-subscriptions.md).

## Changing your email address

Specivo sends sign-in help, password reset links and notifications to your email address, and
matches your commits in connected repositories by it, so a new address only takes effect after you
prove you can read mail sent to it.

1. Under **Email address**, enter the new address and your current password, then select
   **Change email**.
2. Specivo sends a link to the new address. Until you open it, your account keeps the old address:
   you still sign in, reset your password and receive notifications with it.
3. Open the link and select **Confirm email address**. The link opens a page with that button rather
   than changing anything by itself, so a mail scanner that opens links does not use it. You do not
   need to be signed in, and it works in any browser.

The link is valid for 24 hours and works once. When the change is done:

- the old address gets a message saying the address was changed (the new one is partly hidden), with
  a password reset link and the advice to contact an administrator if it was not you;
- every other session of your account is signed out; the browser you confirmed in stays signed in
  if it was signed in to the same account;
- password reset links sent before the change stop working;
- the change is recorded in the [security audit log](../install/audit-log.md).

### While a change is pending

The profile shows **Change to new@example.com pending** with two buttons:

- **Resend link** sends a new link. Only the most recent link works, so earlier ones stop working.
- **Cancel change** ends the request; its link stops working.

Requesting another change also replaces the pending one. If the link has expired, the profile says
so and **Resend link** sends a new one.

If another account already uses the address you entered, you see the same "check your inbox"
message and the same pending state, so the page does not reveal which addresses are in use. No link
is sent: the owner of that address gets a note that someone tried to use it. If an account takes the
address before you open your link, confirming fails and your address stays as it was.

Requests are limited to a few per hour, for your account and for each address.

### Accounts that sign in through single sign-on

If your account has no password because you sign in with your organisation's
[identity provider](../install/single-sign-on/index.md), there is no password to confirm the change
with. Select **Sign in again with** your provider instead: it asks you to sign in at the provider
again, even if you are signed in there already. Once you are back on your profile you have five
minutes to request the change. You must sign in as the same provider account that is connected to
your profile; signing in as someone else is refused.

Your connected accounts do not change with your email address: Specivo recognises them by the
provider's own account id, not by email.

### When the address is managed by your identity provider

If your organisation creates and updates accounts automatically from its identity provider
([SCIM](../install/single-sign-on/scim.md)), your email address belongs to the provider. The profile
shows it without a way to change it, with **Change it at your identity provider**; the next update
from the provider brings the new address to Specivo.

## Administrators

An administrator can change any person's address from **Admin → Users →** the user, under
**Account Details**, or with `PATCH /api/v1/admin/users/{id}/email/` (a signed-in session is
required; an API key is refused). The new address is used at once and marked verified, without a
confirmation link. The old and the new address both get a notice, and the change is recorded in the
audit log.

## Through the API

The same steps are available to API clients signed in as the account:

| Request | What it does |
|---|---|
| `GET /api/v1/auth/me/email/` | Your address, the pending change (or `null`), and whether a change is possible. |
| `POST /api/v1/auth/me/email/` | `{"new_email": "...", "current_password": "..."}`. Answers `202` with the pending change. |
| `POST /api/v1/auth/me/email/resend/` | Sends a new link for the pending change. |
| `DELETE /api/v1/auth/me/email/` | Cancels the pending change. |
| `POST /api/v1/auth/confirm-email/` | `{"token": "..."}` from the link. No sign-in needed. |

There is no MCP tool for this: changing an account's address stays with the person.
