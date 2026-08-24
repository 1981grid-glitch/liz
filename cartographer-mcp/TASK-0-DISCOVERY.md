# Task 0 — Discovery and provisioning spike

**Status:** 0.1 complete, 0.3 complete. 0.2 blocked — see §4.
**Date:** 2026-08-22

---

## 0.1 — The Throne deployment

The Throne MCP source is in this same repo (`../throne-mcp/`), so none of this is inferred
from a running service; it is read from the shipping source and its own deploy runbooks.

### Container build and push

`../throne-mcp/Dockerfile`:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY throne_mcp_server.py .
ENV PORT=8080
EXPOSE 8080
CMD ["python", "throne_mcp_server.py"]
```

Single-file server, two COPYs, no build stage. `.dockerignore` exists specifically to keep
tests, docs and `.bak-*` backups out of the build context.

Build and push is **server-side via ACR** — there is no local Docker daemon in the loop:

```bash
az acr build -r thronemcpe9ebfc -t throne-mcp:$TAG .
az containerapp update -g rg-throne-mcp -n throne-mcp \
  --image thronemcpe9ebfc.azurecr.io/throne-mcp:$TAG
```

That is what makes deploying from a browser (Azure Cloud Shell) possible, which the Throne
team already documented in `CLOUD-SHELL-DEPLOY.md`. Cartographer reuses this exactly.

### Azure Container App configuration

| Setting | Value |
|---|---|
| Resource group | `rg-throne-mcp` |
| Container App | `throne-mcp` |
| ACR | `thronemcpe9ebfc` |
| Region | eastus2 |
| Ingress target port | 8080 |
| Revisions mode | `activeRevisionsMode=Single` |
| Max replicas | 3 (**but see below — effectively pinned at 1**) |
| Secrets | Container App secrets / env vars; cert pulled from Key Vault via managed identity |

Two operational conventions worth inheriting:

- **Never reuse an image tag.** The registry's history is already corrupted by it — tags
  `1.8` and `2.2` share a digest. Tags are computed from `az acr repository show-tags`,
  never hardcoded.
- **Ask Azure what is live, never a file.** Step zero of their runbook is
  `az containerapp show ... --query "properties.template.containers[0].image"`.

### Authentication — the answer to Open Question 1

**Throne is NOT an unauthenticated obscure URL.** It runs a deliberate two-tier gate:

**Tier 2 (what is actually deployed for claude.ai):** Entra ID OAuth, via FastMCP's
`AzureProvider`, which acts as an OAuth proxy so the claude.ai connector UI can connect
with its OAuth fields left blank. Restricted to tenant `netorg39360`. Configured through
`OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, `AZURE_TENANT_ID`, `PUBLIC_BASE_URL`.

**Tier 1 (fallback):** a static shared secret in `MCP_BEARER`, compared with
`hmac.compare_digest` against the `Authorization` header. Note it reads headers with
`get_http_headers(include_all=True)` because FastMCP strips `Authorization` by default —
an easy detail to get wrong and silently fail open.

**It fails closed.** With no OAuth and no bearer, `_auth_ok()` returns `ALLOW_ANON`, which
is False unless `MCP_ALLOW_ANON=1` is set explicitly.

So the pattern is safe to replicate, and cartographer replicates it verbatim. The brief
asked for this to be flagged if it turned out to be an obscure URL — it did not.

**One deliberate divergence.** Throne also externalises the OAuth session store to an Azure
Files mount (Fernet-encrypted `DiskStore`) because losing sessions on every revision swap
was breaking a 69-tool connector mid-task. That fix carries a real constraint: SQLite WAL
over SMB is a corruption risk with concurrent writers, so their own source says **keep
max-replicas at 1 while that store is in use.** Cartographer omits it. It is read-only and
stateless; a re-auth after a deploy is an annoyance, not an incident, and skipping it keeps
scale-out available. If session loss becomes irritating, the block lifts verbatim from
`throne_mcp_server.py` — and inherits the max-replicas=1 constraint with it.

### MCP transport — confirmed, not assumed

`throne_mcp_server.py`, final line:

```python
mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
```

FastMCP's `transport="http"` is **Streamable HTTP**, not SSE. Its module docstring says the
same. Cartographer matches.

### Versions

`fastmcp==2.14.7`, `httpx>=0.27`, Python 3.12-slim. Cartographer pins the same FastMCP
version — the auth provider API is the part that matters, and matching it removes a class
of surprise.

---

## 0.3 — Current Google Maps Platform status

Checked against current sources, because the brief correctly anticipated this had moved.
**Two findings contradict the brief.**

### Finding 1 — the $200/month credit no longer exists

Retired **2026-02-28**. From **2026-03-01**, each SKU tier gets its own free monthly
allowance instead:

| Tier | Free events / month / SKU |
|---|---|
| Essentials | 10,000 |
| Pro | 5,000 |
| Enterprise | 1,000 |

For personal-volume use this is *better* than the old credit, but it changes what the cost
question is. It is no longer "how many calls?" — it is "which tier does each call land in?"

### Finding 2 — the brief's suggested field masks bill at Enterprise

§5.1 proposed this for search and nearby:

```
places.id, places.displayName, places.formattedAddress, places.rating,
places.userRatingCount, places.currentOpeningHours.openNow, places.priceLevel,
places.location
```

`rating`, `userRatingCount`, `currentOpeningHours` and `priceLevel` are all
**Enterprise-tier** fields, and Places bills at the **highest tier any requested field
belongs to**. That mask makes every search an Enterprise call — 1,000 free/month, then
~$20/1K.

**The fix, and the core design decision of this build:** `openNow`, `minRating` and
`priceLevels` are *request parameters* on Text Search, not response fields. Filtering by
them is free and does not touch the field mask. So the server filters server-side at Google
and returns Pro-tier fields only. Results come back correctly filtered; the server just
doesn't pay Enterprise rates to restate the rating on all ten of them.

Enterprise is then paid once, on `maps_place_details`, for the one place the user picked.

Full breakdown in `COST-MODEL.md`.

### Finding 3 — the brief's API choices are correct

- **Places API (New)** — right call. The legacy Places API was frozen in March 2025 and is
  closed to new customers, so a new project *must* use the New surface. No final shutdown
  date is set; Google has committed to 12 months' notice.
- **Routes API** — right call, same reasoning versus the legacy Directions API.
- **Geocoding API** — unchanged and current. Note it is the older-style web service: it
  accepts the key **only** as a `?key=` query parameter, unlike Places and Routes which
  take `X-Goog-Api-Key` as a header. That asymmetry is handled explicitly in the server.

### SKU tiers this server actually lands in

| Tool | SKU | Tier | Free/month |
|---|---|---|---|
| `maps_open_in_maps` | none | — | ∞ (no API call) |
| `maps_search_places` | Text Search | Pro | 5,000 |
| `maps_nearby` | Nearby Search | Pro | 5,000 |
| `maps_place_details` | Place Details | Enterprise | 1,000 |
| `maps_directions` (driving) | computeRoutes | Pro | 5,000 |
| `maps_directions` (walk/bike/transit) | computeRoutes | Essentials | 10,000 |
| `maps_travel_time_matrix` | computeRouteMatrix | Pro | 5,000 **per element** |
| `maps_geocode` / `_reverse_geocode` | Geocoding | Essentials | 10,000 |

`TRAFFIC_AWARE` is what moves a driving route from Essentials to Pro — and traffic-aware
duration is an explicit requirement in §4.5, so that is a deliberate, correct cost.

**Expected steady-state spend at personal volume: $0/month.**

---

## 0.2 — Google Cloud provisioning: BLOCKED

Not done, and not doable from here. This session has no `gcloud`, no Google Cloud
credentials, and no billing authority. Creating a project, attaching billing, and minting
an API key are all actions that require your account.

The full runbook is written and ready to execute: **`GOOGLE-CLOUD-SETUP.md`**. It covers
project creation, enabling exactly the three required APIs, the two-way key restriction,
per-API quota caps, and the $10 budget alert.

Two items in it need your decision first — see below.

---

## 4. Open questions — answered, and what is still yours

| # | Question | Answer |
|---|---|---|
| 1 | What auth does Throne use? | **Resolved.** Entra ID OAuth via FastMCP `AzureProvider`, static-bearer fallback, fails closed. Not an obscure URL. Safe to replicate; cartographer does. |
| 2 | Static outbound IP available? | **Still open — needs an `az` session.** Container Apps environments do have a static outbound IP, but whether *this* environment's is stable and what it is must be read from Azure. Command in `GOOGLE-CLOUD-SETUP.md` §4. This gates the key's IP restriction. |
| 3 | Same Container Apps environment as Throne, or separate? | **Recommend same** (`rg-throne-mcp`, eastus2) — cheaper, simpler, and it shares the outbound IP you will have just allow-listed. Nothing in 0.1 argues for isolation: cartographer is read-only, holds no PHI, and touches no Graph scope. |
| 4 | Business or personal Google Cloud billing? | **Yours to decide before enabling billing.** The use case is personal navigation, which argues personal. But the key will be IP-restricted to an Adaptive Enterprises Azure resource and deployed beside a connector that handles consumer case data — if that reads as business infrastructure to you, put it on the business account. Decide first; moving a project between billing accounts later is more friction than it looks. |

---

## What is built and what is not

**Built, tested, in this directory:**

- `cartographer_mcp_server.py` — 9 tools, FastMCP streamable-HTTP, Throne's auth pattern
- `test_cartographer.py` — 123 assertions, no API key, no network calls
- `Dockerfile`, `requirements.txt`, `.dockerignore` — mirroring Throne
- `COST-MODEL.md`, `GOOGLE-CLOUD-SETUP.md`, `DEPLOY.md`

**Not done, and why:**

- **Google Cloud project + key** — needs your credentials (§0.2).
- **Azure deploy** — no `az` and no Azure credentials in this session. `DEPLOY.md` is a
  copy-paste Cloud Shell runbook.
- **All 7 acceptance tests in §7 of the brief** — every one of them requires the deployed
  endpoint and a live key. None have been run. The 123 local tests cover logic, billing
  tier selection, URL correctness, error mapping and key redaction; they do **not** prove
  anything about Google's live responses.
- **Mobile + desktop reachability confirmation** — requires the deployment.

Nothing in this repo has called a Google API or spent a cent.
