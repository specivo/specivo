---
description: Send Hermes Agent's activity (sessions, tool calls, approvals, subagents) to Specivo as agent sessions with a signed Hermes receiver.
---

# Hermes Agent activity

[Hermes Agent](https://github.com/NousResearch/hermes-agent) can post its activity to other
systems as signed webhooks (`hooks.outbound` in its `config.yaml`). A **Hermes receiver** is the
address in Specivo that accepts them. Each event is recorded as [agent session](capabilities.md#agent-sessions)
activity of the account the receiver belongs to, so you can see when a Hermes session started,
which tools it ran, which approvals it asked for and which subagents it started.

Specivo keeps **metadata only**: tool names, status, durations, ids, approval pattern keys, subagent
role and status. Tool arguments, tool output, approval commands, subagent goals and summaries are
read and dropped, never stored.

!!! note "Activity, not cost"
    Hermes keeps undelivered events in memory only and drops them when it is busy or exits, so the
    receiver never records AI usage or cost. Report usage with the
    [AI usage reporter](reporting-ai-usage.md) instead.

## Create a receiver

A receiver belongs to one account. Usually that is a service account for the agent; people can
also create one for their own account.

- **Your own account:** open **Preferences → Hermes receivers** (`/my/hermes-receivers/`).
- **A service account:** an administrator opens **Administration → Users**, picks the account and
  uses the **Hermes receivers** panel. Creating a receiver for another account needs an
  administrator signed in to the web interface; an administrator API key cannot do it.

Enter a name that tells the Hermes instance or profile apart (for example `build host`) and click
**Create receiver**. Specivo shows, once:

- the **URL** Hermes posts to (`https://your-specivo-host/api/v1/hermes/events/<token>/`);
- the **secret** Hermes signs each event with;
- a ready-made `hooks.outbound` block for Hermes' `config.yaml`.

!!! warning "Copy the values now"
    The URL and the secret are shown only once. Anyone who has both can post activity in the
    account's name. If you lose them, delete the receiver and create a new one, or rotate the secret.

## Configure Hermes

Add the block to Hermes' `config.yaml`:

```yaml
hooks:
  outbound:
    - name: specivo
      url: https://your-specivo-host/api/v1/hermes/events/<token>/
      events: [on_session_start, post_tool_call, pre_approval_request, post_approval_response, subagent_start, subagent_stop, on_session_end]
      secret_env: SPECIVO_WEBHOOK_SECRET
      timeout: 10
```

and put the secret in Hermes' environment (its `.env` file, for example):

```
SPECIVO_WEBHOOK_SECRET=the-secret-shown-at-creation
```

Keep the URL exactly as shown, with the trailing slash: Hermes does not follow redirects. Use
`https://`; Hermes warns about plain `http://` because events travel unencrypted.

To check the URL, open it in a browser or with `curl`: an active receiver answers
`{"ok": true, "receiver": "<name>"}`.

## What Specivo records

| Hermes event | Recorded as | Details kept |
|---|---|---|
| `on_session_start` | `agent_session_started`; the session is created | model, platform, profile |
| `post_tool_call` | `agent_tool_called`; the session's tool call count goes up | tool, status, error type, duration, call and turn ids |
| `pre_approval_request` | `agent_approval_requested` | pattern key(s), surface |
| `post_approval_response` | `agent_approval_resolved` | choice, who decided, pattern key |
| `subagent_start` | `agent_subagent_started` on the parent; the subagent gets a session linked to its parent | subagent role and id |
| `subagent_stop` | `agent_subagent_stopped` on the parent; the subagent's session ends | status, duration, names of the tools it used |
| `on_session_end` | `agent_session_ended`; the session ends | completed, interrupted |

Other Hermes events are accepted and ignored. Sessions created this way show **Webhook** as their
type and `hermes-agent` as their client. Hermes events that carry no session id (some approvals in
gateway mode) are ignored.

Events appear on a project's **Agent sessions** page when the session works on an issue of that
project: when the agent tells Specivo, over MCP, which Hermes session it is in and which issue it is
working on, the Hermes session takes that issue. Events of a session on no issue are kept in the
security audit log only.

## Manage receivers

The list shows each receiver's name, when it was created and when its last event arrived. You can:

- **Deactivate** a receiver: Hermes' events are refused until you activate it again.
- **Rotate secret**: Specivo shows a new secret once; the old one stops working at once, so update
  `SPECIVO_WEBHOOK_SECRET` in Hermes straight away.
- **Delete** a receiver: its sessions and audit records stay.

Creating, changing and deleting receivers is recorded in the security audit log, without the URL
token or the secret.

## Security and limits

- Every event must carry a valid `X-Hermes-Signature-256` signature made with the receiver's secret,
  and a timestamp within ten minutes of the server clock. Configure `secret_env` (or `secret`) in
  Hermes: unsigned events are refused.
- A redelivered event (the same delivery id) is accepted once.
- Events are recorded only as the receiver's account. An event that names another account's session
  id starts a session of this account instead.
- Hermes gives up on an event that is refused (any 4xx answer) and retries once after a server error.
  Refused events are recorded in the security audit log as `hermes_event_rejected` with the reason.

| Variable | Purpose / default |
|---|---|
| `HERMES_EVENT_MAX_BODY_BYTES` | Largest event body accepted. Default `1048576` (1 MB): a tool call event carries the tool's input and output, which Specivo reads and drops. |
| `HERMES_EVENT_RATE_LIMIT_PER_MINUTE` | Events per minute one client address may send. Default `600`. |
