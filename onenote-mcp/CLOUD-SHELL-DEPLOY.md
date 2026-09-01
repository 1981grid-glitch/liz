# Deploying onenote-mcp from a browser (Azure Cloud Shell)

Needs only a browser and your Azure login — no workstation, no local `az`, no
Docker daemon. Same shape as `throne-mcp/CLOUD-SHELL-DEPLOY.md`; read that one
first if you have not, because the invariants it states apply here too.

**This is a FIRST deploy, not a roll.** Throne's runbook updates an app that
already exists. This one creates it, which means it also has an Entra step and a
connector step that Throne's does not. Steps 0–2 can be done days before the
rest.

---

## 0. The gate — do this before anything else

OneNote requires **delegated** auth; app-only was retired 2025-03-31. If this
tenant will not issue a working delegated OneNote token, nothing downstream can
work and there is no workaround to reach for.

Open **Graph Explorer** (`developer.microsoft.com/graph/graph-explorer`), sign in
as `zach.eltzroth@adaptiveenterprisesllc.com`, and run:

```
GET https://graph.microsoft.com/v1.0/me/onenote/notebooks
```

**Check the tenant indicator top-right reads `Adaptiveenterprisesllc`, not
`Sample`.** A signed-out Graph Explorer returns `401` with OneNote error `40001`
— byte-identical to the fatal failure and meaning something completely
different. This has already cost one debugging session.

| Result | Meaning |
|---|---|
| `200` + notebooks | Gate passed. Continue. |
| `403` / insufficient privileges | Consent did not take. **Modify Permissions** → consent `Notes.ReadWrite.All` → re-run. Not fatal. |
| `401` + `40001`, **signed in and consented** | Delegated OneNote is blocked tenant-side (Conditional Access or licensing). **Stop.** This is not a code problem. |

---

## 1. Entra app registration

New registration, separate from Throne's `Claude-AE-Integration`.

| Setting | Value |
|---|---|
| Name | `AE OneNote MCP` |
| Account types | Single tenant |
| Platform | Web |
| Redirect URI | *leave blank for now — you do not have the hostname until step 4* |

**Delegated** Microsoft Graph permissions, then grant admin consent:

```
Notes.ReadWrite.All
Sites.Read.All
offline_access
openid
profile
email
```

> **Do NOT add an Application-type Notes permission.** The role still exists, the
> portal will let you add it, and admin consent will succeed — so the
> registration looks correct and fails only at runtime with `40001`. It buys
> nothing and widens the tenant blast radius. This is the same trap that put the
> five `onenote_*` functions in `throne_mcp_server.py` under
> `@_onenote_quarantined`.

Create a client secret (24 months). Keep the **value**, the **application (client)
ID**, and the **directory (tenant) ID**.

---

## 2. Open Cloud Shell

**https://shell.azure.com**, pick **Bash**. `az` is already signed in as you, so
there is no `az login`. The Azure mobile app has the same Cloud Shell if you are
on a phone.

Confirm where you are, and ask Azure what already exists rather than trusting
this file:

```bash
az account show --query "{sub:name, id:id}" -o table
az containerapp list -g rg-throne-mcp -o table
```

---

## 3. Get the build context and verify it

```bash
rm -rf ~/liz && git clone --depth 1 https://github.com/1981grid-glitch/liz.git ~/liz
cd ~/liz/onenote-mcp
```

Check what you are about to ship. The suite needs no dependencies by design, so
it runs in Cloud Shell as-is and is the strongest single check available:

```bash
python3 test_onenote_mcp.py | tail -1              # expect 166/166 checks passed
grep -c '^@mcp.tool' onenote_mcp_server.py         # expect 8
grep -c '_check_path_args' onenote_mcp_server.py   # expect 10  (1 definition + 9 call sites)
grep -c 'ALLOW_ANY_CLIENT_REDIRECT' onenote_mcp_server.py  # expect 2
```

Then the positional check — the security fix is easiest to verify by the
**absence** of the bug it replaced. This must print **0**:

```bash
grep -c 'ALLOWED_CLIENT_REDIRECT_URIS or None' onenote_mcp_server.py
```

Non-zero means the redirect allowlist has been reverted to the form where
clearing the env var opens `/authorize` to any redirect URI. Stop and fix it.

> Do **not** try to prove "no app-only path" with `grep -c '/\.default'`. It
> returns 1, from the module docstring explaining why Throne's
> client-credentials identity cannot reach OneNote — prose, not a code path.
> Section 15 of the test suite makes this assertion correctly, by stripping
> docstrings via `ast` before checking. That is what the suite is for.

If a check is wrong, stop and re-check the source; do not build.

---

## 4. Build, create, then set the base URL

Registry and resource group are Throne's — reuse is deliberate, one fewer thing
to hold credentials for.

```bash
RG=rg-throne-mcp
ACR=thronemcpe9ebfc
```

Never hardcode a tag. This is a new repository in the registry, so it starts at
`1.0`, but check rather than assume:

```bash
az acr repository show-tags -n $ACR --repository onenote-mcp -o tsv 2>/dev/null | sort -V | tail -5
TAG=1.0   # <-- set from the command above
```

Take the Container Apps environment from the app that already runs in it:

```bash
ENVID=$(az containerapp show -g $RG -n throne-mcp \
  --query "properties.managedEnvironmentId" -o tsv)
echo "$ENVID"
```

Build. `az acr build` builds **and** pushes server-side — no Docker daemon, which
is why this works from a browser:

```bash
az acr build -r $ACR -t onenote-mcp:$TAG .
```

Create the app. `PUBLIC_BASE_URL` is a placeholder because the FQDN does not
exist until the app does:

```bash
CLIENT_ID=<application-client-id-from-step-1>
CLIENT_SECRET=<secret-value-from-step-1>
TENANT=$(az account show --query tenantId -o tsv)
SITE='netorg39360.sharepoint.com,77fa1078-30b7-4da8-a911-734bb624d6a2,0ea15fff-8d81-45b8-ad92-68e6aacb25d1'

az containerapp create -g $RG -n onenote-mcp --environment "$ENVID" \
  --image $ACR.azurecr.io/onenote-mcp:$TAG \
  --registry-server $ACR.azurecr.io \
  --target-port 8000 --ingress external --transport http \
  --min-replicas 1 --max-replicas 1 \
  --secrets oauth-client-secret="$CLIENT_SECRET" \
  --env-vars AZURE_TENANT_ID="$TENANT" \
             OAUTH_CLIENT_ID="$CLIENT_ID" \
             OAUTH_CLIENT_SECRET=secretref:oauth-client-secret \
             PUBLIC_BASE_URL=https://placeholder.invalid \
             THRONE_SITE_ID="$SITE"
```

`min-replicas 1` is not a performance nicety — a cold start stalls the OAuth
handshake long enough that claude.ai reports the connector as broken.

Now read the FQDN back and set the real base URL:

```bash
FQDN=$(az containerapp show -g $RG -n onenote-mcp \
  --query "properties.configuration.ingress.fqdn" -o tsv)

az containerapp update -g $RG -n onenote-mcp \
  --set-env-vars PUBLIC_BASE_URL="https://$FQDN"

echo "Entra redirect URI:  https://$FQDN/auth/callback"
echo "MCP server URL:      https://$FQDN/mcp"
curl -s "https://$FQDN/health"
```

Getting `PUBLIC_BASE_URL` wrong produces a server whose OAuth metadata advertises
the wrong issuer. That fails at the callback, not at startup — so it reads as an
Entra misconfiguration and is not one.

---

## 5. Register the redirect URI — the step that is easy to get backwards

Back in the `AE OneNote MCP` registration → **Authentication** → **Add a
platform** → **Web**, paste exactly what step 4 printed:

```
https://<fqdn>/auth/callback
```

> **This is THIS SERVER'S callback, not `https://claude.ai/api/mcp/auth_callback`.**
> claude.ai never talks to Entra. It talks OAuth to this server and registers
> itself dynamically; the claude.ai callback belongs in
> `ALLOWED_CLIENT_REDIRECT_URIS`, where it is already the default. Registering
> claude.ai's URI here yields a connector that signs in and then dies at the
> callback — which looks like an Entra problem and is not.

`/health` echoes the correct string back so it can be copied rather than typed.

---

## 6. Add the connector

claude.ai → **Settings → Connectors → Add custom connector**:

```
https://<fqdn>/mcp
```

Connect. You should be redirected to a Microsoft sign-in. **Completing it is the
proof the whole chain works** — and it is the one step that can never be
automated, because the delegated token is issued in your user context by design.

While on that screen, check the callback URL claude.ai shows against
`https://claude.ai/api/mcp/auth_callback`. If it differs, that is an
`ALLOWED_CLIENT_REDIRECT_URIS` env change, not a rebuild.

---

## 7. Prove it actually took

Run these in a **new conversation** — the tool manifest is cached per connection.

```
onenote_healthcheck()
```

This is the cheapest first check because it separates the two failures that look
identical from the client: *never signed in* versus *signed in but cannot see
notebooks*. Expect `token: present`, your UPN, and a notebook count per scope.

Then:

```
onenote_list_notebooks(scope: "me")        -> your OneDrive notebooks
onenote_list_notebooks(scope: "throne")    -> MasterChiefsThrone site notebooks
onenote_get_page(page_id: <one you recognise>)
```

Confirm the tool count is **8** and disambiguate **by name**, not count:
`onenote_healthcheck` and `onenote_append_page` present.

**Do not write until the reads work.** Writes are enabled in v1 (`confirm=True`
required on each), but no call in this server has ever met the live Graph API, so
the first run is where reality gets a vote.

---

## Rollback

There is no previous revision on a first deploy — rollback is deletion:

```bash
az containerapp delete -g rg-throne-mcp -n onenote-mcp --yes
```

Throne is untouched by any of this. The two servers share a registry and a
resource group and nothing else: different app registration, different identity
model, different container.
