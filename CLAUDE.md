# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

The canonical source and build context for the **Throne MCP connector** — a remote MCP
server that exposes an app-only Microsoft Graph identity (`Claude-AE-Integration`) to
claude.ai as SharePoint, OneDrive, Outlook, Calendar, and To Do tools, scoped to the
Throne site via `Sites.Selected`.

Everything lives in `throne-mcp/`. The deployable unit is a container image built from
exactly two files (`throne_mcp_server.py`, `requirements.txt`) and rolled onto Azure
Container Apps.

**Canonical must equal deployable.** An `az acr build` from this tree ships whatever is in
`throne_mcp_server.py` — there is no staging filter. Treat every edit to that file as a
production change.

### Rule and section references in the source

Comments cite `CLAUDE.md Rule 6`, `Rule 8`, `§17`, and `DEPLOY_V18.md §2/§4/§9/§10`. Those
point at the **operator handbook in the canonical workspace, not this file.** Known
meanings, from the code that cites them:

- **Rule 6** — this tree is canonical; canonical must equal deployable.
- **Rule 8** — PHI containment in the audit trail.
- **§17** — arm's-length vendor comms: Client ID only, never consumer names.

Do not renumber these, and do not assume a rule number in this file maps to one of theirs.

## Commands

All commands run from `throne-mcp/`.

```bash
python test_todo_v19.py                # 21 checks — To Do v19 surface
python test_phi_audit.py               # 23 checks — PHI scrubbing of the audit trail
python test_mcp_fixes_2026_08_22.py    # 30 checks — the five 2026-08-22 fixes
```

Exit 0 is green. These are plain scripts, not pytest — **there is no `-k` or per-test
selector**; each suite runs whole in under a second. To isolate a failure, read the labeled
`check(...)` line in the output and work backwards from it.

There is no linter, formatter, build system, or dependency lockfile in the repo. Do not
invent one; match the surrounding style instead.

The server itself is not runnable locally without tenant credentials — it reads
`AZURE_TENANT_ID`, `GRAPH_CLIENT_ID`, and `THRONE_SITE_ID` at import time and will
`KeyError` on boot without them.

### Deploy

Two paths, same result. Browser-only via Azure Cloud Shell is documented step-by-step in
`CLOUD-SHELL-DEPLOY.md`; the Skynet workstation path is `push-through-2026-08-22.ps1`
(patch → test → deploy, stops at first failure).

Invariants that have each already cost an incident:

- **Ask Azure what is live.** No file in this repo is authoritative about the deployed
  image. Query the Container App.
- **Never hardcode or reuse an image tag.** Compute the next free tag from ACR. Tag reuse
  already corrupted this registry's history — `1.8` and `2.2` share a digest.
- **Run the pre-build greps in `CLOUD-SHELL-DEPLOY.md` §2** before building. They check the
  fixes positionally, not just textually.
- **Verify the served manifest by tool NAME, not count.** 69 is the current count, but
  count alone cannot distinguish a correct build from a wrong one.
- **After any change to a tool signature**, toggle the connector off/on in claude.ai *and*
  start a new conversation. The tool manifest is cached per connection; reconnecting alone
  does not refresh it.
- **PowerShell scripts here are deliberately pure ASCII.** Windows PowerShell 5.1 reads a
  BOM-less file as cp1252, where an em-dash's third byte (`0x94`) becomes a curly quote that
  closes a string early and parses the rest of the line as code.
- **`.dockerignore` excludes everything except the two files the image needs.** Never add
  `throne_mcp_server.py` or `requirements.txt` to it.

## Architecture

`throne_mcp_server.py` is a single ~2,900-line module — FastMCP over streamable-HTTP,
listening on `PORT` (8080). 69 registered tools across seven families: `sp_*` (17),
`outlook_*` (15), `throne_*` (13), `todo_*` (10), `calendar_*` (7), `onedrive_*` (6), and
`send_mail`.

The layering that matters is not visible from any single tool — it is a set of choke points
every tool is expected to pass through.

### Auth is two-tiered and every tool re-checks it

`_auth_ok()` returns True immediately under Tier 2 (Entra OAuth, `OAUTH_*` env set — FastMCP
has already rejected unauthenticated requests at the transport layer). Otherwise it does a
constant-time compare against the Tier 1 static bearer, and **fails closed** unless
`MCP_ALLOW_ANON=1` is set explicitly.

Every tool opens with the same guard:

```python
if not _auth_ok():
    return {"error": "unauthorized"}
```

**Indentation after that guard is load-bearing.** One level too deep puts your code inside
the `if` and after its `return`, where it is unreachable. That exact mistake made
`_normalize_body_html` dead at all seven call sites for two days without any test noticing —
it is the single most repeated bug in this file's history. Check placement, not presence.

### Graph access funnels through `_g()`

`_g()` wraps `_send_with_retry` over Graph v1.0 with an app-only `CertificateCredential`,
sourced from Key Vault via managed identity in Azure and from a local PFX in dev. Errors
normalize through `_graph_err()`. Do not call `httpx` directly against Graph — the one
deliberate exception is chunked upload sessions, whose URLs are pre-authorized, so the
bearer token must *not* be sent to them.

### Aliases are indirection, not convenience

- `_mbx()` — `zach|admin|jeff|steven|careers` → UPN; full UPNs pass through.
- `_resolve_site()` — `throne` (pinned) | `opshub` | a URL | a `host:/sites/Name` path | an
  already-composite Graph id.

**Any new tool taking a mailbox or site must route through these.** Six `onedrive_*` tools
passed `user_id` straight to Graph and returned `404 ResourceNotFound` for aliases that
worked everywhere else — that is what these helpers exist to prevent.

### Two upload ceilings, not one

Drive uploads and mail attachments are different Graph endpoints with different limits.
Conflating them is why large evaluation PDFs failed to attach while `throne_write_binary`
handled the same file fine.

| Path | Simple POST/PUT up to | Above that | Ceiling |
|---|---|---|---|
| Drive (`_upload_bytes`) | `_UPLOAD_SIMPLE_MAX` (4MB) | upload session | — |
| Mail attachment | `_ATTACH_SIMPLE_MAX` (3MB) | attachment upload session | `_ATTACH_MAX` (150MB) |

Chunks use `_UPLOAD_CHUNK` (1.6MB), a multiple of 320KiB as Graph requires. The final
attachment PUT answers 201 with an **empty body** and the id in the `Location` header.

### Guardrails are server-side, deliberately

`_mail_guard()` and `_consumer_block()` enforce §17 in code, not in prompt text:

1. Any blocklisted consumer name in subject or body → hard refuse, on every mailbox and
   every recipient, internal included.
2. Any *external* recipient with no Client ID token in subject/body → refuse.

`send_mail` additionally requires `client_id_ack=True` as an explicit statement of human
intent. Rosters come from env (`CONSUMER_NAME_BLOCKLIST`, `INTERNAL_MAIL_DOMAINS`), so they
can be armed without a rebuild.

### PHI scrubbing sits at the audit choke point

`_scrub_phi()` is applied inside `_audit()` rather than at its ~50 call sites, so the
containment holds by construction — a future writer cannot reintroduce the leak by
formatting a consumer name into a target string.

The SharePoint mirror (`Claude-Writes-Log.md`) is **always** scrubbed; it is a shared,
root-level file readable by anyone with library access. The stdout/Log Analytics trail keeps
full fidelity by default because it sits behind Azure RBAC instead — set
`AUDIT_SCRUB_STDOUT=1` to scrub both.

**`PHI_SCRUB_NAMES` and `CONSUMER_NAME_BLOCKLIST` are separate on purpose.** The scrub roster
wants every consumer who has ever appeared in a path or subject (84 names as of 2026-08-10);
the blocklist also *refuses mail*. Merging them would redact the log correctly and
simultaneously start refusing routine internal mail.

`_audit` never raises — a failed audit must not block the operation it was recording. It
also refuses to rewrite the log on a transient read failure, which would truncate it.

### The OneNote quarantine

Five `onenote_*` functions are in-tree but carry `@_onenote_quarantined` — a no-op decorator
that returns the function instead of registering it — so they stay out of the served
manifest and cannot ship as a side effect of a build.

**Do not "re-enable" them by swapping the decorator back to `@mcp.tool`.** App-only Graph
OneNote was retired 2025-03-31; every call 401s. The trap is that the `Notes.Read.All` app
role still exists and admin consent still succeeds, so the app registration looks correct
and fails only at runtime. Granting it buys nothing and widens the tenant blast radius
(tenant-wide, no per-site scoping — `Sites.Selected` does not cover `/onenote`). Registering
these is the *last* step of a delegated-token rework, not the first.

## Testing convention

Every suite extracts the functions under test out of `throne_mcp_server.py` via `ast` and
`exec`s them — **nothing is ever retyped into a test file.** That is what keeps the tests
from drifting away from shipping code, and it is why they can assert on things like operator
indentation and constant values.

New tests must follow this pattern: declare `WANT_FUNCS` / `WANT_ASSIGNS`, extract, fail
loudly if extraction misses, then exercise the real objects against the fake `R` response
class and the `STATE` recorder.

## Working posture in this repo

This is a solo-operated production connector handling VR case data. Bias accordingly.

- **Pull skills without asking.** `Skill` is pre-approved; invoking one needs no permission
  round-trip.
- **Finish the whole task.** If part is blocked, complete everything else and say plainly
  what was left and why. Scaling the work down is not your call.
- **Verify before asserting.** This codebase's documentation habit is to check reality
  (query Azure, grep the source, run the suite) rather than trust a file or a memory. Match
  it — and when something is undeployed, unverified, or assumed, say so in those words.
- **Correct false premises directly**, including in the request itself, then proceed with
  the work.
- **Never soften a trap.** The comments here shout (`DO NOT GRANT IT`, `do not hardcode one`)
  because each one already went wrong once. When you find a new one, document it the same way
  — with the incident that justifies it.
