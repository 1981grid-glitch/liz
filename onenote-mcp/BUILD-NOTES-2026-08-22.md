# onenote-mcp — build notes, 2026-08-22

New remote MCP server exposing OneNote via Graph with **delegated** auth, separate from
Throne Reclaimed. Built to the 2026-08-22 brief.

**Status: code complete and locally verified. Not deployed** — this session has no `az`
CLI and no Docker daemon, the same constraint recorded in `DEPLOY-NOTES-2026-08-22.md`
("Not deployed — no Azure CLI in the session that produced this"). Everything that can be
proven without an Azure subscription has been; §7 lists exactly what remains.

**Two corrections to the brief, both load-bearing.** §3 registers the wrong redirect URI,
and §4.1's suggested auth provider cannot work for OneNote. Both are §2 and §3 below.
Neither is a judgement call — each was verified against the installed source or Microsoft's
own reference.

---

## 1. §0 confirmed, and it is stronger than the brief states

The brief cites the deprecation notice. Microsoft's reference page has since moved past
the future tense entirely:

> "The Microsoft Graph OneNote API doesn't support app-only authentication."
> — *Use the OneNote REST API*, learn.microsoft.com/graph/api/resources/onenote-api-overview

The dated notice is still on the overview page, and the Q&A thread the brief quotes is
still the accepted answer. Checked 2026-08-22.

The brief's warning about stale permission tables also checks out, verbatim: the **List
pages** reference page still advertises `Notes.Read.All` / `Notes.ReadWrite.All` under
**Application**. That table is what will lead the next person to configure app-only,
succeed at every step, and fail at the call.

So the server refuses to help with that mistake in the one place it will actually be seen —
`_graph_err()` detects OneNote error `40001` and answers with the reason rather than the
raw message. Nobody should have to re-derive this.

Also carried into the code from Microsoft's best-practices guidance:

| Guidance | Where it lives |
|---|---|
| Never call all-pages (`400`, error `20266`) — page per section | `onenote_list_pages` is per-section only; a test asserts the collection endpoint is never constructed |
| Override the default `lastModifiedDateTime` ordering | `$orderby=createdDateTime desc` |
| `$select` the minimum property set | every read tool |
| Use `$expand` instead of a call per level | `onenote_list_sections` gets the whole tree in one round-trip |
| Section groups nest, and hold sections | walked recursively; every section reports its group `path` |
| Throttling is aggressive | `_send_with_retry` honors `Retry-After` on 429/503/504 |
| `$top` ceiling is 100 for pages | capped, and says so when more pages exist |

---

## 2. Correction 1 — `AzureProvider` cannot be used here

The brief (§4.1) says to use "FastMCP's built-in Entra/Azure OAuth provider if the
installed version ships one", and to verify against the installed API rather than
remembered signatures. Verified — and the built-in provider is the wrong tool:

```
AzureProvider.__init__ ->  JWTVerifier(jwks_uri=..., issuer=...,
                                       audience=settings.client_id, algorithm="RS256")
```
*(fastmcp 2.14.7, read out of the installed source)*

`AzureProvider` validates the upstream Entra token as a JWT whose **audience is our own
client id** — i.e. a token minted for *this app's* API. A Microsoft Graph access token's
audience is Graph, never our client id. Every request would fail validation.

This is not a configuration problem, it is arithmetic: an OAuth access token has exactly
one audience. There is no token that both validates as our API *and* is accepted by Graph.
Requesting scopes from two resources in one authorization request does not fix it either —
Entra rejects multi-resource scope at the token endpoint.

**What the server does instead** — compose the same building blocks one level down:

```python
OAuthProxy(
    upstream_authorization_endpoint = .../oauth2/v2.0/authorize,
    upstream_token_endpoint         = .../oauth2/v2.0/token,
    token_verifier = GraphTokenVerifier(),   # <- the only change
    base_url = PUBLIC_BASE_URL,
    redirect_path = "/auth/callback",
)
```

`OAuthProxy` still does the entire MCP authorization-spec dance for claude.ai — dynamic
client registration, PKCE (`forward_pkce=True` by default), `/authorize`, `/token`, and
both `.well-known` documents. Only the validator changes.

`GraphTokenVerifier` validates the upstream token by calling `GET /me` — Graph is the only
authority that can validate a Graph token, and Microsoft tells third parties not to try —
and returns an `AccessToken` whose `.token` **is** the delegated Graph token. Tools then
read it through the public `get_access_token()`. That is the whole delegated mechanism;
there is no second token store and no private FastMCP API in the call path.

Because that validator runs on every MCP message, results are cached for 300s keyed by
`sha256(token)` — never the token itself. Without the cache, every message would cost a
Graph round-trip and walk straight into OneNote's throttling.

---

## 3. Correction 2 — the redirect URI in the brief is the wrong one

The brief's §3 table registers the claude.ai connector callback as the Entra redirect URI,
and §0.3 rightly flags a wrong value here as "the single most common cause of a connector
that authenticates and then dies at the callback."

Under the OAuth-proxy architecture there are **two** redirect URIs, and they belong in
different places:

| Redirect URI | Whose | Registered where |
|---|---|---|
| `https://<fqdn>/auth/callback` | this server's, at Entra | **Entra app registration → Web → Redirect URI** |
| `https://claude.ai/api/mcp/auth_callback` | claude.ai's, at this server | `ALLOWED_CLIENT_REDIRECT_URIS` env (already the default) |

claude.ai never talks to Entra. It talks OAuth to *this server*, and registers itself
dynamically via `/register`. This server is the only thing that talks to Entra, so the
callback Entra needs is this server's.

Verified in fastmcp 2.14.7: `redirect_path` is documented as "Redirect path configured in
Azure App registration", defaults to `/auth/callback`, and the running server exposes
exactly that route. `/health` echoes the value back so it can be copied rather than typed:

```json
{"entra_redirect_uri": "https://<fqdn>/auth/callback"}
```

The claude.ai value itself (`https://claude.ai/api/mcp/auth_callback`) is Anthropic's
documented connector callback. `claude.com` is blocked by this environment's egress proxy,
so it could not be read off the vendor doc directly from here — **confirm it on the
connector setup screen when adding the connector**, per the brief's own instruction. It is
a one-line env change (`ALLOWED_CLIENT_REDIRECT_URIS`) if it differs, and needs no rebuild.

---

## 4. Entra app registration — corrected

| Setting | Value |
|---|---|
| Name | `AE OneNote MCP` |
| Supported account types | Single tenant (`netorg39360`) |
| Platform | Web |
| **Redirect URI** | **`https://<fqdn>/auth/callback`** ← this server, not claude.ai |
| Client type | Confidential (client secret) |

Delegated Graph permissions — **delegated only, no Application-type Notes permission**:

| Permission | Purpose |
|---|---|
| `Notes.ReadWrite.All` | read/write notebooks the user can reach, including site-hosted |
| `Sites.Read.All` | resolve SharePoint site ids for site-hosted notebooks |
| `offline_access` | refresh tokens — without this a session dies at ~1hr |
| `openid`, `profile`, `email` | sign-in claims |

Grant admin consent. Client secret at 24 months; calendar the rotation.

No "Expose an API" configuration is needed. Fully-qualified Graph scopes
(`https://graph.microsoft.com/...`) pass through to Entra unprefixed — confirmed by
calling `_prefix_scopes_for_azure` on the installed provider — so there is no custom API
scope to define.

---

## 5. Tool surface

Seven tools per §4.3, plus a healthcheck. All prefixed `onenote_` so they never collide
with Throne's in a shared session.

| Tool | Notes |
|---|---|
| `onenote_list_notebooks(scope="me")` | `scope` is `"me"`, a site id, or an alias (`throne`) |
| `onenote_list_sections(notebook_id, scope)` | one `$expand` round-trip; section groups walked, each section reports its `path` |
| `onenote_list_pages(section_id, top=50, scope)` | per-section only; `top` capped at 100; flags when more exist |
| `onenote_get_page(page_id, format="text", include_ids=False, scope)` | text by default; `include_ids=True` for append targets |
| `onenote_search(query, section_id=None, scope, max_sections=25)` | **title-only**, and says so in `search_kind` |
| `onenote_create_page(section_id, title, html, confirm=False, scope)` | read-after-write verified |
| `onenote_append_page(page_id, html, target, action, confirm=False, scope)` | truncation-verified — see below |

House conventions carried over from Throne: every write takes `confirm: bool = False` and
refuses without it; every write returns the authoritative `id` and `webUrl`; every write
is read back before success is reported; writes emit one JSON line to stdout.

**On `onenote_append_page` and "a success can never be a lie".** The brief's checklist
requires appending *without truncating*. Rather than trusting the PATCH, the tool captures
the page text first, and afterwards checks both that the page grew and that every
non-trivial line present before is still present. If prior content did not survive it
returns `ok: False` with the lost lines, instead of reporting a clean append over a page it
just clobbered. A failed pre-read aborts rather than patching blind. `action="replace"` is
exempt from the survival check, since removing content is what it is for — but the result
still reports what changed.

**On `onenote_search` (§4.5.4).** Implemented as title-only, deliberately. `$search` on
OneNote pages could not be exercised against the live tenant from here, and presenting a
title match as full text would be a quiet lie about what was searched. Every result carries
`"search_kind": "title-only (not full text)"`. If full text turns out to be a hard
requirement, that is a v1.1 content-scan fallback — see §8 question 4.

**PHI discipline.** The stdout write log records ids and counts, never page titles or HTML.
A OneNote page title in a case notebook is exactly the kind of string that carries a
consumer name; this follows the rule the Throne To Do writers already do. The
unauthenticated `/health` route reports configuration only — never a token, user, or
notebook name — since anything on it is readable by whoever can reach the ingress.

---

## 6. Tests

```bash
python test_onenote_mcp.py      # 166/166
```

Same discipline as the Throne suites: every function and class under test is extracted from
`onenote_mcp_server.py` via `ast` and exec'd, never retyped, so the suite cannot drift from
what ships. It needs nothing installed — no fastmcp, no httpx, no network, no Azure.

It caught three real defects during the build, all of which would have shipped:

1. **`onenote_append_page` crashed on every call.** `_slog(tool, target, **extra)` takes
   `target` positionally, and the append call passed `target=` as a keyword —
   `TypeError: got multiple values for argument 'target'`. Every append would have failed.
2. **Text extraction double-spaced every page.** Block tags emit a boundary on both open and
   close, and OneNote wraps *every line* of a page in its own `<p>`, so whole pages came
   back double-spaced — roughly twice the tokens to read, against a tool whose stated job is
   controlling token spend.
3. **`onenote_search` could never run a notebook-wide search.** `@mcp.tool` rebinds the
   decorated name to a `FunctionTool`, which is **not callable**, so a tool can never call
   another tool in-process — `onenote_search` called `onenote_list_sections` and would have
   raised `TypeError` on every search that was not scoped to a single section. The shared
   walk is now a plain `_sections_tree()` helper. A guard (§16 of the suite) reads the
   decorators off the AST and fails if any tool calls another by name, so the bug class
   cannot come back — it immediately caught a follow-on slip where the refactor left
   `@mcp.tool` attached to the new helper.

   Worth stating plainly: the ast suite exercises the *undecorated* functions, so it is
   blind to this by construction. Only running the real module found it.

Beyond the unit checks, the server was run for real in this session: it binds, serves
`/health`, publishes both `.well-known` documents advertising the delegated Graph scopes,
exposes `/auth/callback` and `/mcp`, and returns **401 on an unauthenticated `/mcp`** call.

---

## 7. Verification checklist — honest status

From the brief's §6. Everything needing an Azure subscription, a browser sign-in, or
Zach's credentials is **not done and could not be done from this session**.

| # | Check | Status |
|---|---|---|
| 1 | Graph Explorer `200` on `/me/onenote/notebooks` (Task 0.1) | **Not run** — needs an interactive sign-in as Zach. **This is still the first gate; run it before deploying.** |
| 2 | Container app `200` on `/health` externally | **Not run** — not deployed. Verified locally. |
| 3 | claude.ai connector completes OAuth round-trip | **Not run** — needs deployment |
| 4 | `onenote_list_notebooks` returns the real list | **Not run** — needs deployment |
| 5 | `onenote_get_page` returns readable text | Logic tested against fixtures; **not run live** |
| 6 | `onenote_create_page` + read-back | Read-after-write tested; **not run live** |
| 7 | `onenote_append_page` without truncating | Truncation detection tested both ways; **not run live** |
| 8 | Site-hosted notebooks under MasterChiefsThrone reachable | Routing tested (`/sites/{id}/onenote`); **not run live** |
| 9 | Session resumed >1hr still works | `offline_access` requested and asserted; **needs a live 1hr test** |

Task 0.2 (enumerate the notebook surface) also could not be run — it needs the same
delegated token as 0.1. The tenant's app-only Throne credentials cannot substitute; that is
the whole point of §0.

### Deploy runbook

**Browser-only (recommended, and the only path that works from a phone):
`CLOUD-SHELL-DEPLOY.md` in this folder.** It follows `throne-mcp/CLOUD-SHELL-DEPLOY.md`
step for step, with the two steps Throne's does not need — the Entra registration
and the connector — since this is a first deploy rather than a roll.

The three values that were previously unknown are now resolved, read off Throne's
own runbook rather than guessed:

| | |
|---|---|
| Resource group | `rg-throne-mcp` |
| Registry | `thronemcpe9ebfc` |
| Container Apps environment | not hardcoded anywhere — take it from the app that already runs in it: `az containerapp show -g rg-throne-mcp -n throne-mcp --query "properties.managedEnvironmentId" -o tsv` |

That last one is deliberate. `CLAUDE.md` is explicit that no file in this repo is
authoritative about live infrastructure; ask Azure.

### Deploy from a workstation

```powershell
$env:AE_RESOURCE_GROUP    = "<rg>"
$env:AE_REGISTRY          = "<acr>"
$env:AE_ACA_ENVIRONMENT   = "<aca-env>"
$env:AE_ONENOTE_TENANT_ID = "<tenant guid>"
$env:AE_ONENOTE_CLIENT_ID = "<client id>"
$env:AE_ONENOTE_CLIENT_SECRET = "<secret>"
$env:AE_THRONE_SITE_ID    = "<site id>"   # optional, enables scope="throne"

.\deploy-onenote-mcp.ps1 -WhatIf     # dry run
.\deploy-onenote-mcp.ps1             # next free 1.x tag, auto-detected
```

The script computes the tag itself — do not hardcode one. It creates with external ingress
on port 8000, HTTPS only, `min-replicas 1` (a cold start stalls the OAuth handshake long
enough that claude.ai reports the connector as broken), and the client secret as a
Container Apps secret, never in the image.

It handles one ordering trap: `PUBLIC_BASE_URL` must be the app's own FQDN, which does not
exist until the app does. So it creates with a placeholder, reads the FQDN back, then sets
it. Getting that wrong produces a server whose OAuth metadata advertises the wrong issuer —
which fails at the callback, not at startup, and therefore looks like an Entra
misconfiguration when it isn't.

The script is pure ASCII for the reason `DEPLOY_V18.md` §9 documents (PowerShell 5.1 reads
a BOM-less file as cp1252 and an em-dash's third byte becomes a string delimiter).

Note the port differs from Throne: this app listens on **8000** (per the brief), Throne on
8080. Do not copy Throne's deploy arguments verbatim.

Then, in order:

1. Register `https://<fqdn>/auth/callback` on the Entra app — the exact string `/health`
   reports.
2. claude.ai → Settings → Connectors → Add custom connector → `https://<fqdn>/mcp`
3. Connect. Expect a Microsoft sign-in prompt; completing it is the proof the chain works.
4. Run `onenote_healthcheck` first — it separates "never signed in" from "signed in but
   cannot see notebooks", which are the two failures that look identical from the client.

---

## 7a. Deploying from a Claude Code cloud session (added 2026-08-31)

The whole build stalled on one gap: a cloud session has no `az` and no Azure
identity, so every deploy bounced back to a laptop. Two pieces close it. The first
is done; the second needs a human once.

**Done — `.claude/hooks/session-start.sh`.** A SessionStart hook that installs the
Azure CLI and onenote-mcp's runtime deps into every web session. Note the install
route: Microsoft's official installer lives behind `aka.ms`, which this
environment's egress proxy **blocks** (`curl` returns `000`, not a redirect), so
azure-cli comes from PyPI instead. Don't "fix" that back to the aka.ms script.
Cold run ~1m40s, warm ~1s. It handles no secrets and performs no login.

**Needed once — a scoped service principal.** The CLI without credentials reaches
no subscription. In Azure Cloud Shell (`shell.azure.com`, works in a phone
browser):

```bash
az ad sp create-for-rbac \
  --name "claude-code-onenote-deploy" \
  --role Contributor \
  --scopes /subscriptions/<sub-id>/resourceGroups/<rg-holding-throne-and-onenote-mcp>
```

Scope it to the one resource group — that is the blast radius. It prints `appId`,
`password`, `tenant`. Put them in the Claude Code **environment config** (not this
repo, not a chat message) as:

| Variable | Value |
|---|---|
| `AZURE_CLIENT_ID` | `appId` |
| `AZURE_CLIENT_SECRET` | `password` (mark as a secret) |
| `AZURE_TENANT_ID` | `tenant` |

Environment configuration is documented at
<https://code.claude.com/docs/en/claude-code-on-the-web>.

After that the hook reports `this session can deploy` at startup, and a session
logs in with:

```bash
az login --service-principal -u "$AZURE_CLIENT_ID" -p "$AZURE_CLIENT_SECRET" --tenant "$AZURE_TENANT_ID"
```

**The trade, stated plainly.** This is a standing credential in configuration —
the same class of tech debt as the brief's §7 fallback, and a real change from
holding nothing persistent. Scoping to one resource group bounds it; set a
rotation date. What it buys is that steps 2–4 of the activation runbook stop
needing a laptop. **Step 5 never moves** — completing the Microsoft sign-in is
the delegated handshake, and that is the security property, not an obstacle.

---

## 8. Open questions (brief §9)

Answers change the config, not the code, except where noted.

1. **Scope of access** — currently reaches whatever the signed-in user can reach, which for
   Zach includes the contractor/client notebooks on the Throne site. Delegated auth means
   the server can never exceed Zach's own access, which is the main safety property here.
   If PHI containment argues for narrower reach, the lever is `THRONE_SITE_ID` /
   `ONENOTE_SITE_ALIASES` — leave them unset and only `scope="me"` resolves.
2. **Write access in v1?** **DECIDED 2026-08-23: read-write ships in v1.** Zach's call,
   after the read-only-first recommendation was put to him. No code change — the shipped
   `OAUTH_SCOPES` default is already `Notes.ReadWrite.All`, and the write tools are built.

   What carries the risk instead of a narrowed scope: both write tools refuse without
   `confirm=True`; `onenote_create_page` reads the page back and returns the authoritative
   id rather than a remembered one; `onenote_append_page` captures the page text first and
   proves every prior line survived the PATCH, returning `ok: False` with the lost lines if
   not, and aborting outright if the pre-read fails rather than patching blind. A failed
   write is loud; a silent truncation is the failure mode that was designed against.

   If it ever needs narrowing, it is still one permission edit and no redeploy —
   `Notes.Read.All` in `OAUTH_SCOPES` makes the write tools fail at Graph.
3. **Registry / resource group** — the deploy script takes both as parameters and defaults
   to `AE_*` env vars, so either choice works untouched. **Recommend reusing Throne's ACR
   and resource group**: one fewer thing to hold credentials for, and teardown is a single
   `az containerapp delete` regardless.
4. **Search fidelity** — shipping title-only, labelled as such. If full text is a hard
   requirement, the fallback is enumerate → fetch page content → scan, which is
   O(pages) Graph calls and will throttle on a large notebook. Worth building only against
   a real requirement, and worth scoping to one section when it is.

---

## 8a. Security review (2026-08-23)

Ran before deployment rather than after, since this server sits on public HTTPS ingress,
holds a client secret, brokers OAuth, and hands delegated Microsoft tokens to tools. Two
findings, both fixed on this branch.

### Graph endpoint confusion via unvalidated ids  — fixed

`scope`, `notebook_id`, `section_id` and `page_id` were formatted straight into Graph
request paths. A `?` in one of those values does not stay inside its path segment: it
opens the query string, so the suffix the tool appends lands in the query and the request
re-points at a different endpoint. `scope="root/drive/root/children?"` turned
`onenote_list_notebooks` into a SharePoint drive listing.

This was never privilege escalation for the signed-in user — the delegated token carries
only their own access, under only `Notes.*` and `Sites.Read.All`. It mattered for a
different reason: **page content is untrusted input that this server feeds to a model.** A
prompt injection planted in a shared or site-hosted notebook could steer a tool that says
it lists notebooks into reading unrelated SharePoint content. A tool's stated reach and
its actual reach should be the same thing.

Fixed with `_check_path_args()`: `scope` must be `me`, a configured alias, or a literal
Graph site id; ids must match the OneNote id shape. `/`, `?` and `#` are rejected. Every
tool calls it before building a URL, so a crafted value costs zero Graph calls.

### Empty redirect allowlist inverted to allow-any  — fixed

`allowed_client_redirect_uris=ALLOWED_CLIENT_REDIRECT_URIS or None` — FastMCP reads `[]`
as "permit nothing" and `None` as "permit everything", so clearing the env var, the one
action that looks like a lockdown, produced the widest possible setting. An open redirect
on `/authorize` is how an authorization code gets delivered to somebody else's host, and
PKCE does not help when the attacker originates the flow.

Now an empty list stays empty, and allow-any needs `MCP_ALLOW_ANY_CLIENT_REDIRECT=1` said
out loud — the same fail-closed shape as Throne's `MCP_ALLOW_ANON`.

### Checked and cleared

- `Authorization` on redirect-following requests — httpx strips it cross-origin, so the
  claim in `_g()`'s comment holds.
- Unauthenticated `/health` — exposes public OAuth metadata and alias *names*, never site
  ids, tokens, users, or notebook names.
- `_slog` — ids and counts only.
- `GraphTokenVerifier`'s 300s cache — a stale entry passes the MCP gate, but every tool
  then calls Graph with that same token, so a revoked or expired token yields no data.
- Startup fails closed: `_build_auth()` raises on missing credentials before the server
  can bind.

Suite is now **166 checks** (sections 17 and 18 cover the two fixes).

---

## 9. Risks and tech debt

- **fastmcp is pinned to 2.14.7.** The auth composition here depends on `OAuthProxy`'s
  constructor, `TokenVerifier.verify_token`, and `AccessToken.token` being what
  `get_access_token()` hands a tool. All three are public API, but FastMCP's auth surface
  has moved between minors. Re-verify by introspection before moving the pin — the same
  method used here, not the docs.
- **Not deployed and not exercised against live Graph.** Every Graph interaction is written
  against Microsoft's current reference and tested against fixtures, but no call in this
  server has met the real API. Expect the first live run to find something; `/health` and
  `onenote_healthcheck` exist to make that fast.
- **The claude.ai callback URI was not read off the vendor doc** (egress-blocked). Confirm
  on the connector screen; it is an env change, not a rebuild.
- **`GraphTokenVerifier` spends one `GET /me` per cache miss.** 300s TTL keeps that cheap.
  If `/me` ever becomes the throttled call, raise the TTL — it is well inside the ~60min
  token lifetime.
- **No session store on a volume.** Unlike Throne, a restart drops OAuth sessions and the
  connector re-runs sign-in. That is acceptable precisely because there is no long-lived
  credential to preserve; if re-consent becomes annoying at `min-replicas 1`, Throne's
  Azure Files + Fernet `client_storage` pattern ports over directly (mind the concurrency
  caveat in its comments).
- **Out of scope, as instructed:** no Application-type Notes permission, no change to
  Throne's auth, and the local SKYNET OneNote server is untouched.
