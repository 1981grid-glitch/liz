# Google Cloud provisioning for cartographer

Task 0.2. Needs your Google account and billing authority — none of this can be done from
a Claude session. Roughly 15 minutes.

**Decide first (Open Question 4): business or personal billing account.** Moving a project
between billing accounts afterwards is more friction than it looks.

---

## 1. Create a dedicated project

Console → project picker → **New project**.

- Name: `adaptive-maps-mcp`
- Do **not** reuse a project that has unrelated billing. A dedicated project is what makes
  the budget alert and the quota caps mean something.

## 2. Attach billing

Billing → link a billing account.

Maps Platform serves **nothing** without billing attached — not even inside the free
allowance. A key on an unbilled project returns `REQUEST_DENIED`, which looks exactly like
a key restriction problem and will waste your afternoon.

## 3. Enable exactly three APIs

APIs & Services → Library. Enable:

- **Places API (New)** — note the "(New)". The legacy Places API is a different entry and
  is frozen to new customers.
- **Routes API**
- **Geocoding API**

Nothing else. Every additional enabled API is another SKU that a leaked key could bill.

## 4. Create a restricted server key

APIs & Services → Credentials → **Create credentials → API key**.

Restrict it **both** ways. One without the other is not enough.

### API restriction

Restrict key → **Restrict key** → select exactly the three APIs above.

### Application restriction → IP addresses

You need the Container App's outbound IP. **This is Open Question 2 and must be read from
Azure, not assumed.** In Cloud Shell:

```bash
# If deploying beside Throne (recommended), this is the environment already in use:
ENV_ID=$(az containerapp show -g rg-throne-mcp -n throne-mcp \
  --query "properties.environmentId" -o tsv)

az containerapp env show --ids "$ENV_ID" \
  --query "{static:properties.staticIp, outbound:properties.vnetConfiguration}" -o json
```

Container Apps environments expose a static outbound IP for egress. Put that address in the
key's IP allow-list.

**If it comes back empty or the environment turns out to use dynamic egress**, stop and
provision a NAT gateway with a static public IP *before* creating the key. An
unrestricted Maps key is a live financial liability, not a hygiene item — it is the single
most common way people get a four-figure Maps bill.

> Interim option while you sort the IP out: leave the key API-restricted but IP-unrestricted
> **only** with the daily quota caps in §5 already set, and treat it as temporary. The caps
> bound the damage to a few dollars a day. Do not leave it there.

## 5. Set quota caps — this is the real protection

APIs & Services → each of the three APIs → **Quotas & System Limits** → set a daily
requests-per-day cap.

Suggested for personal use, generous but bounded:

| API | Daily cap |
|---|---|
| Places API (New) | 200 |
| Routes API | 200 |
| Geocoding API | 200 |

A quota cap **refuses the call**. Nothing bills. That is the difference between it and a
budget alert, which only tells you after the money is gone.

At these caps the theoretical worst case is a few dollars a day even if the key leaks
entirely — and the server surfaces the refusal cleanly as `OVER_QUERY_LIMIT` with a note
saying the cap did its job.

## 6. Budget alert at $10

Billing → Budgets & alerts → **Create budget**.

- Scope: the `adaptive-maps-mcp` project only
- Amount: **$10/month**
- Alert thresholds: 50%, 90%, 100%
- Email: your address

Last line of defence, not first. See `COST-MODEL.md` for the ordering.

## 7. Hand the key to Azure — never to the repo

Do not put the key in a `.env`, a Dockerfile, or a commit. It goes in as a Container App
secret:

```bash
az containerapp secret set -g rg-throne-mcp -n cartographer-mcp \
  --secrets google-maps-api-key="PASTE_KEY_HERE"

az containerapp update -g rg-throne-mcp -n cartographer-mcp \
  --set-env-vars GOOGLE_MAPS_API_KEY=secretref:google-maps-api-key
```

Full sequence in `DEPLOY.md`.

## 8. Verify

Once deployed, from a Claude client:

```
maps_healthcheck()
```

- `key_status: ok` — the key works and Geocoding is reachable.
- `REQUEST_DENIED` — the key is rejected. Almost always: the calling IP is outside the
  allow-list, the API is missing from the key's API restriction list, or billing is not
  attached. The tool's `hint` field says exactly this.

The healthcheck deliberately exercises **only Geocoding**, to stay cheap. A green
healthcheck with a failing `maps_search_places` means Places is missing from the key's API
restriction list — check §4 before anything else.

## 9. Confirm the SKUs after your first test run

Billing → Reports → group by **SKU**.

Expected after the §7 acceptance tests:

- Text Search **Pro** — not Enterprise. If you see Text Search Enterprise, something is
  passing `include_ratings=True` by default and `COST-MODEL.md` has been violated.
- Place Details Enterprise — a small number, one per detail lookup.
- Compute Routes Pro — driving routes.
- Geocoding Essentials — healthchecks and address lookups.

Anything outside that set is unexpected and worth chasing down immediately.
