# Deploying cartographer to Azure Container Apps

Mirrors `../throne-mcp/CLOUD-SHELL-DEPLOY.md`. Browser only — Azure Cloud Shell has `az`
already signed in, and `az acr build` builds server-side, so no workstation and no Docker
daemon are needed.

**Prerequisite:** `GOOGLE-CLOUD-SETUP.md` complete, and you have the API key to hand.

Open **https://shell.azure.com**, pick **Bash**.

---

## 0. Confirm where you are

```bash
az account show --query "{sub:name, id:id}" -o table
```

Recommended target (Open Question 3): the **same** resource group and Container Apps
environment as Throne — cheaper, simpler, and it shares the outbound IP you allow-listed
on the Maps key.

```bash
RG=rg-throne-mcp
ACR=thronemcpe9ebfc
APP=cartographer-mcp
ENV_ID=$(az containerapp show -g $RG -n throne-mcp --query "properties.environmentId" -o tsv)
echo "$ENV_ID"
```

## 1. Get the build context

```bash
rm -rf ~/liz && git clone --depth 1 https://github.com/1981grid-glitch/liz.git ~/liz
cd ~/liz/cartographer-mcp
```

## 2. Verify what you are about to ship — do not skip

The image is whatever `cartographer_mcp_server.py` is in this folder. Check before
building rather than diagnosing a bad image afterwards.

```bash
grep -c '^@mcp.tool' cartographer_mcp_server.py    # expect 9
grep -c 'transport="http"' cartographer_mcp_server.py  # expect 1  (streamable HTTP)
```

The cost guards are the part that is easy to regress silently, so check them positionally.
Each must print **0** — an Enterprise-tier field leaking into a Pro mask is a bill, not an
error:

```bash
# Pro search mask must contain no Enterprise field
sed -n '/^_SEARCH_MASK_PRO = /,/^))/p' cartographer_mcp_server.py \
  | grep -cE 'rating|priceLevel|OpeningHours|PhoneNumber|websiteUri'

# No wildcard field mask anywhere
grep -c 'FieldMask.*\*' cartographer_mcp_server.py
```

And the matrix cap must still be 25:

```bash
grep -c '_MATRIX_MAX_ELEMENTS = 25' cartographer_mcp_server.py   # expect 1
```

Run the suite if you want certainty — it needs no key and makes no network calls:

```bash
pip install -q fastmcp==2.14.7 'httpx>=0.27'
python test_cartographer.py     # expect 123 passed, 0 failed
```

If a check is wrong, stop and fix the source. Do not build.

## 3. Pick the next free tag — never hardcode one

Cartographer is a new repository in the same registry, so it starts at `1.0`. After that,
compute it; never assume.

```bash
az acr repository show-tags -n $ACR --repository cartographer-mcp -o tsv 2>/dev/null \
  | sort -V | tail -5
TAG=1.0   # <-- set from the command above
```

**Never reuse an existing tag.** Tag reuse is what corrupted this registry's history
(`throne-mcp` `1.8` and `2.2` share a digest). If `show-tags` lists it, pick higher.

## 4. Build

```bash
az acr build -r $ACR -t cartographer-mcp:$TAG .
```

Builds **and** pushes server-side. This is why the whole thing works from a browser.

## 5. First deploy — create the app

Only for the very first deploy. Skip to §6 for subsequent ones.

```bash
az containerapp create \
  -g $RG -n $APP \
  --environment "$ENV_ID" \
  --image $ACR.azurecr.io/cartographer-mcp:$TAG \
  --target-port 8080 \
  --ingress external \
  --min-replicas 0 --max-replicas 2 \
  --registry-server $ACR.azurecr.io \
  --secrets google-maps-api-key="PASTE_KEY_HERE" \
  --env-vars GOOGLE_MAPS_API_KEY=secretref:google-maps-api-key
```

`--min-replicas 0` lets it scale to zero. The in-memory cache dies with it; that is
accepted and documented — no Redis for this.

Unlike Throne, max-replicas is not pinned to 1: cartographer has no Azure Files session
store, so the SQLite-over-SMB constraint does not apply.

Capture the URL:

```bash
FQDN=$(az containerapp show -g $RG -n $APP --query "properties.configuration.ingress.fqdn" -o tsv)
echo "https://$FQDN"
```

### Then wire up auth — do not leave it open

The server **fails closed**: with no auth configured, every tool returns `unauthorized`.
That is intentional. Pick one tier.

**Tier 2 — Entra ID OAuth (what claude.ai's connector UI wants, and what Throne runs):**

Register an Entra app for cartographer (its own, not Throne's — separate blast radius, and
the redirect URI differs), then:

```bash
az containerapp secret set -g $RG -n $APP \
  --secrets oauth-client-secret="ENTRA_APP_SECRET"

az containerapp update -g $RG -n $APP --set-env-vars \
  OAUTH_CLIENT_ID="ENTRA_APP_CLIENT_ID" \
  OAUTH_CLIENT_SECRET=secretref:oauth-client-secret \
  AZURE_TENANT_ID="YOUR_TENANT_ID" \
  PUBLIC_BASE_URL="https://$FQDN"
```

The Entra app's redirect URI must be `https://$FQDN/auth/callback`.

**Tier 1 — static bearer (simpler; fine for a solo read-only server):**

```bash
az containerapp secret set -g $RG -n $APP --secrets mcp-bearer="$(openssl rand -hex 32)"
az containerapp update -g $RG -n $APP --set-env-vars MCP_BEARER=secretref:mcp-bearer
```

Note claude.ai's custom-connector UI is built around OAuth; a static bearer is the easier
path for Claude Desktop and the harder one for the web/mobile clients. If mobile is the
motivating use case — and per the brief it is — **use Tier 2**.

Never set `MCP_ALLOW_ANON=1` on a public ingress. It exists for local development.

## 6. Subsequent deploys

```bash
PREV=$(az containerapp show -g $RG -n $APP \
  --query "properties.template.containers[0].image" -o tsv)
echo "rollback with: az containerapp update -g $RG -n $APP --image $PREV"

az containerapp update -g $RG -n $APP --image $ACR.azurecr.io/cartographer-mcp:$TAG
```

Zero-downtime revision swap.

## 7. Confirm

```bash
az containerapp show -g $RG -n $APP \
  --query "{image:properties.template.containers[0].image, \
            revision:properties.latestRevisionName, \
            health:properties.runningStatus, \
            fqdn:properties.configuration.ingress.fqdn}" -o table
```

Wait for the new revision to report healthy. If it does not, roll back with the line §6
printed.

## 8. Register as a connector in claude.ai

Settings → Connectors → **Add custom connector**.

- URL: `https://$FQDN/mcp`
- Leave the OAuth fields blank — `AzureProvider` acts as the OAuth proxy, same as Throne.

Then **both**, explicitly — desktop-only is a failed build per §6 of the brief:

1. Claude Desktop on SKYNET
2. The Claude mobile app

## 9. Prove it took

Cheapest end-to-end check, no cost, exercises the motivating path:

```
maps_open_in_maps(destination="Tikka Grill, West Chester OH")
```

Returns a URL and makes no API call — so it works even before the Google key is valid, and
isolates "is the server reachable" from "is the key good".

Then:

```
maps_healthcheck()
```

Expect `key_status: ok`, and an `auth_mode` that is **not** `ANONYMOUS`.

Then run the §7 acceptance tests from the brief against this endpoint — not a local
instance.

## 10. Watch the first bill

After the acceptance run, check Billing → Reports grouped by **SKU**. Expected set is in
`GOOGLE-CLOUD-SETUP.md` §9. `Text Search Pro` is correct; `Text Search Enterprise` on
every search is not, and means the cost model has been broken.
