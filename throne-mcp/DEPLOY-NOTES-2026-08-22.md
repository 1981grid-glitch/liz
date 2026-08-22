# Throne MCP — fixes 2026-08-22

Five changes to `throne_mcp_server.py`. Tool count unchanged at **69**; no tool added,
renamed, or removed, so the served manifest keeps the same shape and §4's
"verify by NAME not count" check is unaffected.

## 1. `_normalize_body_html` was dead code at all 7 call sites  ← the big one

The call was indented one level too deep, landing *inside* `if not _auth_ok():` and
*after* its `return`:

```python
    if not _auth_ok():
        return {"error": "unauthorized"}
        body_html = _normalize_body_html(body_html)   # unreachable
```

So the function has never executed once. It was added on **2026-08-20** specifically to
stop entity-escaped bodies shipping literal `<p>`/`<table>` text to recipients — the
incident its own docstring cites (Client ID 357257, two vendors + two state counselors).
That repair was inert the moment it landed, and the source has not been deployed since
2026-08-10, so it has never run in production either.

Affected: `send_mail`, `outlook_create_draft`, `outlook_update_draft`, `outlook_send`,
`outlook_reply`, `calendar_create_event`, `calendar_update_event`.

## 2. `outlook_add_attachment` — big files

The plain `POST /messages/{id}/attachments` endpoint caps at 3MB. Anything larger failed
outright. Now: unchanged plain POST at or under 3MB, resumable **attachment upload
session** above it, up to Graph's 150MB ceiling.

Note this is a *different* endpoint and a *different* limit from the drive uploads
`_upload_bytes` already handled — which is why large evaluation PDFs failed here while
`throne_write_binary` worked fine on the same file.

- Chunks reuse `_UPLOAD_CHUNK` (1.6MB, a multiple of 320KiB as Graph requires).
- The session URL is pre-authorized, so chunks go out through bare `httpx` and our bearer
  token is never sent to a non-Graph host — same approach `_upload_bytes` uses.
- The final PUT answers 201 with an empty body and the id in the `Location` header, so the
  id is parsed from there rather than dropped.
- Over-ceiling, empty, and invalid-base64 payloads are refused before any Graph call.
- Response gains `bytes` and `upload` (`"simple"` | `"session"`).

## 3. `calendar_create_event` — Teams meetings

New `is_online_meeting: bool = False`. When true, sets `isOnlineMeeting` +
`onlineMeetingProvider: "teamsForBusiness"` and returns `joinUrl`.

Graph will accept the flag on an event it cannot actually provision (mailbox not
Teams-licensed, or policy blocks it) and answer 201 with **no join link**. Returning a bare
`ok` there would hand back a "Teams meeting" nobody can join, so that case returns an
explicit `warning`.

Create-time only, deliberately: Graph cannot remove online-meeting info from an event once
set, so it is not exposed on `calendar_update_event`.

## 4. `onedrive_*` — short aliases

All six `onedrive_*` tools passed `user_id` straight to Graph, so `user_id="zach"` gave
`404 ResourceNotFound: User not found` while every other tool family accepted that alias.
Now routed through `_mbx()`. Full UPNs still pass through unchanged.

## 5. `careers` mailbox alias

Added `"careers": "careers@adaptiveenterprisesllc.com"`.

Verified reachable **app-only before adding** (mailFolders returned 8 folders, Inbox 37
unread), so it is already in the Exchange scope group and cannot become one of the
"resolves fine and then 403s at Graph" entries the alias table warns about. It is also
already delegate-accessible to Zach, so no Exchange admin work is required — only the
alias was missing.

## Tests

```bash
python test_todo_v19.py                # 21/21  (their suite, no regression)
python test_phi_audit.py               # 23/23  (their suite, no regression)
python test_mcp_fixes_2026_08_22.py    # 30/30  (new, covers all five fixes)
```

All three extract the real functions out of `throne_mcp_server.py` via `ast`, so they
cannot drift from shipping code. The two existing suites are byte-identical to the
canonical copies except for the `SRC` path, which is repointed at the same directory so
they run outside Skynet.

## Deploy

Not deployed — no Azure CLI in the session that produced this. Per `DEPLOY_V18.md` §2:

```powershell
.\deploy-throne-mcp.ps1 -WhatIf     # dry run
.\deploy-throne-mcp.ps1             # next free 3.x tag, auto-detected
```

**Live is `3.2` / revision `throne-mcp--0000020`; next free tag is `3.3`.** Do not reuse
`3.2` or `2.7`. The script computes the tag itself — do not hardcode one.

⚠️ **This deploy also ships the undeployed 2026-08-20 work.** Source was last written
2026-08-20 17:40Z, live image was built 2026-08-10 — so `_normalize_body_html` itself
(not just the indent fix) goes out with this. Expect the diff to be larger than these five
items.

After deploying, toggle the connector off/on in claude.ai **and start a new conversation** —
the tool manifest is cached per connection, and `calendar_create_event` gained a parameter.
