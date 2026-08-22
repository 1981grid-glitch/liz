# Cartographer — Google Maps MCP server

Read-only remote MCP server giving Claude place search, place details, traffic-aware
routing, geocoding, and — the motivating case — **a tappable Google Maps link that opens
turn-by-turn navigation on a phone**.

Deployment mirrors the Throne MCP server in `../throne-mcp/` exactly: FastMCP over
streamable-HTTP, Azure Container Apps in eastus2, Entra ID OAuth via FastMCP's
`AzureProvider`. No second pattern was invented.

## Status

| | |
|---|---|
| Server + tests | **Built.** 9 tools, 123 assertions passing |
| Google Cloud project + key | **Not done** — needs your account (`GOOGLE-CLOUD-SETUP.md`) |
| Azure deploy | **Not done** — no `az` in the build session (`DEPLOY.md`) |
| Acceptance tests §7 | **Not run** — all require the deployed endpoint and a live key |

Nothing here has called a Google API or spent a cent.

## Read these in order

1. **`TASK-0-DISCOVERY.md`** — the Task 0 spike. Throne's auth/transport/deploy pattern,
   and the two findings that contradict the original brief.
2. **`COST-MODEL.md`** — why field masks are price tags, and how this stays at $0/month.
3. **`GOOGLE-CLOUD-SETUP.md`** — provisioning runbook. Do this first.
4. **`DEPLOY.md`** — Azure Cloud Shell deploy runbook. Browser only.

## Tools

| Tool | What it does | Billed tier |
|---|---|---|
| `maps_open_in_maps` | Tappable Maps deep link | **none — no API call** |
| `maps_search_places` | Text search, with free open-now / rating / price filters | Pro |
| `maps_nearby` | Type-driven search around a coordinate | Pro |
| `maps_place_details` | Phone, hours, website, rating, open-now | Enterprise |
| `maps_directions` | Distance + traffic-aware duration + deep link | Pro (driving) |
| `maps_travel_time_matrix` | Which of these is closest — capped at 25 elements | Pro, per element |
| `maps_geocode` / `maps_reverse_geocode` | Address ↔ coordinates | Essentials |
| `maps_healthcheck` | Key validity, auth mode, cache + SKU counters | Essentials |

## The one design decision worth knowing

Google bills Places at the **highest tier of any field you request**. The original brief's
suggested field mask included `rating`, `userRatingCount`, `currentOpeningHours` and
`priceLevel` — all Enterprise-tier — which would have made *every search* an Enterprise
call at 1,000 free/month.

But `openNow`, `minRating` and `priceLevels` are **request filters**, not response fields.
Filtering is free. So this server filters server-side at Google and returns Pro-tier fields
only:

```python
maps_search_places(query="gyros", open_now=True, min_rating=4)
# -> correctly filtered to open, well-rated places
# -> bills Text Search Pro (5,000 free/month), not Enterprise (1,000)
```

Enterprise is paid once, on `maps_place_details`, for the single place the user picked.
`COST-MODEL.md` has the full argument.

## Local test

No API key needed, no network calls made:

```bash
pip install -r requirements.txt
python test_cartographer.py     # 123 passed, 0 failed
```

## Environment

| Variable | Purpose |
|---|---|
| `GOOGLE_MAPS_API_KEY` | Container App secret. Never a `.env`, never the image. |
| `OAUTH_CLIENT_ID` / `OAUTH_CLIENT_SECRET` / `AZURE_TENANT_ID` / `PUBLIC_BASE_URL` | Tier 2 Entra OAuth |
| `MCP_BEARER` | Tier 1 static bearer fallback |
| `MCP_ALLOW_ANON=1` | Local dev only. Never on public ingress. |
| `PORT` | Ingress target port, default 8080 |

**The server fails closed.** With none of the auth variables set, every tool returns
`unauthorized`. That is deliberate.

## Out of scope

No writes to Google accounts. No Street View or static map images. No real-time location
tracking — Claude's client-side location tool supplies coordinates when the user permits.

And no programmatic launching of the Maps app: an MCP server has no access to the device.
A link the user taps is the ceiling, on Android and iOS alike.
