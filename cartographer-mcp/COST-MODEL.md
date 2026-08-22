# Cartographer — staying under $10/month

The `$200/month` credit this kind of budget used to lean on **no longer exists**. Google
retired it on **2026-02-28**; from **2026-03-01** every billing account instead gets a
**free monthly call allowance per SKU tier**:

| Tier | Free events / month / SKU |
|---|---|
| Essentials | 10,000 |
| Pro | 5,000 |
| Enterprise | 1,000 |

Because the allowance is **per SKU**, the cost question is no longer "how many calls?" —
at personal volume that is never the binding constraint. It is **"which tier does each
call land in?"** A single badly-chosen field mask moves a call from a 10,000-free bucket
to a 1,000-free bucket that then bills at ~$20/1K.

## The trap in the original field masks

The build brief's §5.1 suggested this mask for search and nearby:

```
places.id, places.displayName, places.formattedAddress, places.rating,
places.userRatingCount, places.currentOpeningHours.openNow, places.priceLevel,
places.location
```

Four of those — `rating`, `userRatingCount`, `currentOpeningHours`, `priceLevel` — are
**Enterprise-tier fields**. Places bills at the **highest tier any requested field belongs
to**, so that mask makes *every search* an Enterprise call: 1,000 free per month, then
~$20/1K. Ten searches a day would clear the free cap in about three months and start
billing on a SKU where it is easy not to notice.

## What this server does instead

**Filters are free; fields are billed.** `openNow`, `minRating`, and `priceLevels` are
*request parameters* on Places Text Search, not response fields. Filtering by them costs
nothing and does not touch the field mask. So the server filters server-side at Google and
returns only Pro-tier fields.

`maps_search_places(query="gyros", open_now=True, min_rating=4)` therefore:

- sends `openNow: true` and `minRating: 4.0` in the request body (free),
- requests a **Pro** field mask (name, address, location, business status, maps URI),
- bills **Text Search Pro** — 5,000 free/month.

The results are correctly filtered to open, well-rated places. The server just doesn't pay
Enterprise rates to *restate* the rating on all ten of them.

When the user picks one, `maps_place_details` on that **single** place pulls the Enterprise
fields — phone, hours, website, rating. That is one Enterprise call per actual decision,
not ten per search.

## Tier per tool

| Tool | Endpoint | SKU tier | Free/month | Notes |
|---|---|---|---|---|
| `maps_open_in_maps` | none | **$0** | ∞ | Pure string building. No API call. |
| `maps_search_places` | Text Search | **Pro** | 5,000 | Enterprise only if `include_ratings=True` |
| `maps_nearby` | Nearby Search | **Pro** | 5,000 | Enterprise only if `include_ratings=True` |
| `maps_place_details` | Place Details | **Enterprise** | 1,000 | Deliberate — this is the payoff call |
| `maps_directions` | computeRoutes | **Pro** | 5,000 | `TRAFFIC_AWARE` is what makes it Pro |
| `maps_travel_time_matrix` | computeRouteMatrix | **Pro** | 5,000 | Billed **per element**, capped at 25 |
| `maps_geocode` / `_reverse_geocode` | Geocoding | **Essentials** | 10,000 | |
| `maps_healthcheck` | Geocoding | **Essentials** | 10,000 | One cheap call |

`maps_directions` for walking / bicycling / transit omits `routingPreference` entirely
(Google rejects it on those modes anyway), so those calls bill **Essentials**, not Pro.

## What the budget actually looks like

Realistic personal navigation — say 20 lookups a day, every day:

- ~600 searches/month → Pro, free cap 5,000. **$0**
- ~600 detail calls/month → Enterprise, free cap 1,000. **$0**
- ~600 routes/month → Pro, free cap 5,000. **$0**
- geocoding, incidental → Essentials, free cap 10,000. **$0**

**Expected steady-state cost: $0/month.** The $10 budget is headroom for a bad month, not
a target to spend. The realistic path to a surprise bill is not normal use — it is a loop,
a retry storm, or a leaked key. Those are what the guards below are for.

## Guards, in order of how much they actually protect you

1. **Google-side quota caps** — the only guard Google enforces. Set per-API daily caps in
   the Cloud console (`GOOGLE-CLOUD-SETUP.md` §5). A cap refuses the call; nothing bills.
2. **IP restriction on the key** — a leaked key that only works from the Container App's
   egress IP is not a usable key. See `GOOGLE-CLOUD-SETUP.md` §4.
3. **The matrix element cap** — 25 elements, enforced in this server before any request
   leaves. `computeRouteMatrix` bills origins × destinations, so a careless 20×20 is 400
   elements in one call.
4. **In-process SKU counters** — `maps_healthcheck` reports calls by tier so drift is
   visible. Advisory only: Container Apps scales to zero and the counters reset with it.
5. **Caching** — details 24h, geocoding 7d, search 15min, routes never. Cuts repeat calls;
   not a billing control.
6. **Budget alert at $10** — tells you after the money is gone. Useful, last.

Quotas are the protection. The alert is the smoke detector.
