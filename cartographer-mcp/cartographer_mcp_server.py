"""
Cartographer MCP Server — Google Maps Platform, read-only, remote.

Gives Claude place search, place details, traffic-aware routing, geocoding, and — the
motivating case — a tappable Google Maps deep link that opens turn-by-turn navigation on
a phone.

Deployment mirrors the Throne MCP server exactly (see TASK-0-DISCOVERY.md): FastMCP over
streamable-HTTP, Azure Container Apps, Entra ID OAuth via FastMCP's AzureProvider with a
static-bearer fallback. Nothing here invents a second pattern.

Runtime: FastMCP over streamable-HTTP (remote-connector compatible).
    pip install -r requirements.txt

COST: read COST-MODEL.md before touching a field mask. Google bills Places at the highest
tier any requested field belongs to, so a field mask is a price tag, not a convenience.
"""

import asyncio
import datetime as dt
import hmac
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers

# ---------------------------------------------------------------------------
# Config (from env / Container App secrets)
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")

BEARER     = os.environ.get("MCP_BEARER", "")          # Tier 1 shared secret
ALLOW_ANON = os.environ.get("MCP_ALLOW_ANON") == "1"   # must be explicit to run without one

# Tier 2 — Entra ID OAuth. Same shape as Throne: claude.ai's connector UI needs OAuth,
# static bearers don't fit it.
OAUTH_CLIENT_ID     = os.environ.get("OAUTH_CLIENT_ID")
OAUTH_CLIENT_SECRET = os.environ.get("OAUTH_CLIENT_SECRET")
TENANT_ID           = os.environ.get("AZURE_TENANT_ID")
PUBLIC_BASE_URL     = os.environ.get("PUBLIC_BASE_URL")
OAUTH_SCOPES        = [s.strip() for s in os.environ.get("OAUTH_SCOPES", "User.Read").split(",") if s.strip()]
OAUTH_ENABLED       = bool(OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET and TENANT_ID and PUBLIC_BASE_URL)

PLACES  = "https://places.googleapis.com/v1"
ROUTES  = "https://routes.googleapis.com"
GEOCODE = "https://maps.googleapis.com/maps/api/geocode/json"

# computeRouteMatrix bills per ELEMENT (origins x destinations), not per request. A
# careless 20x20 is 400 billable elements in a single call, which is why this is a hard
# refusal in code rather than a documented guideline.
_MATRIX_MAX_ELEMENTS = 25

_HTTP_TIMEOUT = 30.0
_MAX_RETRIES = 3
_BACKOFF_CAP = 20.0

# ---------------------------------------------------------------------------
# Field masks — MODULE-LEVEL CONSTANTS ONLY.
#
# Never "*". Never built from user input. Places bills at the HIGHEST tier of any field
# requested, so every name below is a billing decision:
#
#   Essentials  places.id, places.name, places.attributions
#   Pro         displayName, formattedAddress, location, businessStatus, googleMapsUri,
#               primaryTypeDisplayName, types, plusCode, photos, addressComponents...
#   Enterprise  rating, userRatingCount, currentOpeningHours, regularOpeningHours,
#               priceLevel, priceRange, nationalPhoneNumber, internationalPhoneNumber,
#               websiteUri
#
# Search and Nearby prefix every field with "places."; Place Details does NOT. Getting
# that wrong is a 400, not a silent overcharge, but it is the easiest mistake here.
# ---------------------------------------------------------------------------
_SEARCH_MASK_PRO = ",".join((
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.location",
    "places.businessStatus",
    "places.primaryTypeDisplayName",
    "places.googleMapsUri",
))

# Opt-in only. Escalates the whole call from Pro (5,000 free/mo) to Enterprise (1,000
# free/mo). Filtering by rating/price/open-now does NOT require these fields — the
# filters are request parameters and cost nothing. See COST-MODEL.md.
_SEARCH_MASK_ENTERPRISE = _SEARCH_MASK_PRO + "," + ",".join((
    "places.rating",
    "places.userRatingCount",
    "places.priceLevel",
    "places.currentOpeningHours.openNow",
))

_DETAILS_MASK_PRO = ",".join((
    "id",
    "displayName",
    "formattedAddress",
    "location",
    "businessStatus",
    "primaryTypeDisplayName",
    "googleMapsUri",
))

# Enterprise by design: phone + hours + website + rating are the entire point of a
# details call. One call per user decision, not one per search result.
_DETAILS_MASK_ENTERPRISE = _DETAILS_MASK_PRO + "," + ",".join((
    "nationalPhoneNumber",
    "internationalPhoneNumber",
    "websiteUri",
    "rating",
    "userRatingCount",
    "priceLevel",
    "regularOpeningHours",
    "currentOpeningHours",
))

_ROUTES_MASK = ",".join((
    "routes.distanceMeters",
    "routes.duration",
    "routes.staticDuration",
    "routes.description",
))

# Steps are behind a flag: a wall of turn text in a chat context, and it inflates the SKU.
_ROUTES_MASK_STEPS = _ROUTES_MASK + "," + ",".join((
    "routes.legs.steps.navigationInstruction",
    "routes.legs.steps.distanceMeters",
    "routes.legs.steps.staticDuration",
))

_MATRIX_MASK = "originIndex,destinationIndex,duration,distanceMeters,status,condition"

# ---------------------------------------------------------------------------
# Logging. JSON lines to stdout -> Container Apps / Log Analytics.
# The API key must never reach this stream; every message goes through _scrub().
# ---------------------------------------------------------------------------
logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
_LOG = logging.getLogger("cartographer")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


_KEY_IN_URL = re.compile(r"([?&]key=)[^&\s\"']+")


def _scrub(text: str) -> str:
    """Remove key material from anything user- or log-facing.

    Two paths matter. Places and Routes take the key in an X-Goog-Api-Key header, so it
    cannot land in a URL. Geocoding is the older web service and only accepts ?key=, so
    the key IS in that URL — and httpx puts the request URL into several of its own
    exception strings. Both are covered here: the literal value, and the query pattern
    (which also catches a key that is not ours, e.g. echoed back by an error body)."""
    if not text:
        return text
    if API_KEY:
        text = text.replace(API_KEY, "***REDACTED***")
    return _KEY_IN_URL.sub(r"\1***REDACTED***", text)


def _slog(evt: str, **extra: Any) -> None:
    rec = {"ts": _now(), "evt": evt}
    rec.update(extra)
    try:
        _LOG.info(_scrub(json.dumps(rec, default=str)))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# SKU accounting. Advisory only — Container Apps scales to zero and takes these with it.
# Google-side quota caps are the real control (GOOGLE-CLOUD-SETUP.md §5).
# ---------------------------------------------------------------------------
_SKU_COUNTS: dict[str, int] = {}


def _count(sku: str, n: int = 1) -> None:
    _SKU_COUNTS[sku] = _SKU_COUNTS.get(sku, 0) + n


# ---------------------------------------------------------------------------
# Auth. Same two-tier gate as Throne, same fail-closed default.
# ---------------------------------------------------------------------------
def _build_auth():
    """Tier 2: protect the whole MCP transport with Entra ID OAuth (AzureProvider acts as
    an OAuth proxy so claude.ai can connect with the OAuth fields left blank). Falls back
    to None (Tier 1 per-tool bearer gate) when not configured.

    Deliberately omits Throne's client_storage / Azure Files session store. That exists
    because losing an OAuth session on every revision swap was breaking a 69-tool
    connector mid-task; it also pins max-replicas to 1 (SQLite WAL over SMB). This server
    is read-only and stateless, so a re-auth after a deploy is a minor annoyance rather
    than a reliability incident, and skipping it keeps scale-out available. If session
    loss becomes irritating in practice, lift the block from throne_mcp_server.py
    verbatim — and inherit the max-replicas=1 constraint with it."""
    if not OAUTH_ENABLED:
        return None
    from fastmcp.server.auth.providers.azure import AzureProvider
    return AzureProvider(
        client_id=OAUTH_CLIENT_ID,
        client_secret=OAUTH_CLIENT_SECRET,
        tenant_id=TENANT_ID,
        base_url=PUBLIC_BASE_URL,
        required_scopes=OAUTH_SCOPES,
    )


mcp = FastMCP("Cartographer (Google Maps)", auth=_build_auth())


def _auth_ok() -> bool:
    """In OAuth (Tier 2) mode FastMCP already rejected unauthenticated requests at the
    transport layer, so reaching a tool means the caller is authenticated. Otherwise use
    the Tier 1 static-bearer check (constant-time)."""
    if OAUTH_ENABLED:
        return True
    if not BEARER:
        return ALLOW_ANON  # fail CLOSED unless MCP_ALLOW_ANON=1 is set explicitly
    # include_all=True: FastMCP strips Authorization by default.
    h = get_http_headers(include_all=True)
    return hmac.compare_digest(h.get("authorization", ""), f"Bearer {BEARER}")


def _unauth() -> dict:
    return {"error": "unauthorized"}


def _no_key() -> dict:
    return {"error": "GOOGLE_MAPS_API_KEY is not configured on this server."}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


async def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        async with _client_lock:
            if _client is None:
                _client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
    return _client


async def _send(method: str, url: str, **kw) -> httpx.Response:
    """Single point every Google request flows through, so 429/503 backoff is uniform.

    Retries are capped low and deliberately: a 429 here is usually the daily quota cap
    doing its job, and hammering it neither succeeds nor is free of latency cost."""
    client = await _http()
    delay = 1.0
    resp = None
    for attempt in range(_MAX_RETRIES + 1):
        resp = await client.request(method, url, **kw)
        if resp.status_code not in (429, 503) or attempt == _MAX_RETRIES:
            return resp
        try:
            wait = float(resp.headers.get("Retry-After", ""))
        except ValueError:
            wait = delay
        await asyncio.sleep(min(wait, _BACKOFF_CAP))
        delay = min(delay * 2, _BACKOFF_CAP)
    return resp


def _err(resp: httpx.Response) -> dict:
    """Structured, human-readable errors — never a raw exception, never key material.

    Places and Routes return {"error": {"status": "...", "message": "..."}} with a gRPC
    status name; the classic Geocoding web service returns a flat {"status": "..."} with
    a different vocabulary. Both are normalised here to the four cases §5.3 of the brief
    calls out, because the caller should not have to know which API it hit."""
    status, message = "", ""
    try:
        body = resp.json()
        if isinstance(body.get("error"), dict):
            status = body["error"].get("status", "") or ""
            message = body["error"].get("message", "") or ""
        else:
            status = body.get("status", "") or ""
            message = body.get("error_message", "") or ""
    except Exception:
        message = resp.text[:400]

    code = resp.status_code
    hint = ""
    if code == 403 or status in ("PERMISSION_DENIED", "REQUEST_DENIED"):
        kind = "REQUEST_DENIED"
        hint = ("The API key was rejected. This is almost always a key restriction "
                "problem: either the calling IP is outside the key's allowed IP list, or "
                "the API being called is not in the key's API restriction list. It can "
                "also mean billing is not enabled on the Google Cloud project.")
    elif code == 429 or status in ("RESOURCE_EXHAUSTED", "OVER_QUERY_LIMIT"):
        kind = "OVER_QUERY_LIMIT"
        hint = ("Quota exceeded. If a daily cap was set in the Google Cloud console, this "
                "means the cap did its job and no further calls will bill today.")
    elif code == 400 or status in ("INVALID_ARGUMENT", "INVALID_REQUEST"):
        kind = "INVALID_REQUEST"
        hint = "Google rejected a parameter. Its own message is quoted below."
    elif code == 404 or status == "NOT_FOUND":
        kind = "NOT_FOUND"
        hint = "No such place id."
    else:
        kind = status or f"HTTP_{code}"

    out = {"error": kind, "detail": _scrub(message) or f"HTTP {code}"}
    if hint:
        out["hint"] = hint
    _slog("api_error", kind=kind, http=code)
    return out


def _zero_results(what: str = "places") -> dict:
    return {"error": "ZERO_RESULTS",
            "detail": f"No {what} matched that search in the given area."}


# ---------------------------------------------------------------------------
# TTL cache. In-memory and per-replica; Container Apps may scale to zero and evict it.
# That is acceptable and deliberate — no Redis for this.
# ---------------------------------------------------------------------------
_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_STATS = {"hit": 0, "miss": 0}

TTL_DETAILS = 24 * 3600      # hours and phone numbers rarely change
TTL_GEOCODE = 7 * 24 * 3600  # addresses are effectively static
TTL_SEARCH = 15 * 60
# Routes are NOT cached. Traffic is the entire point.


def _cache_get(key: str) -> Any | None:
    hit = _CACHE.get(key)
    if hit and hit[0] > time.time():
        _CACHE_STATS["hit"] += 1
        return hit[1]
    if hit:
        _CACHE.pop(key, None)
    _CACHE_STATS["miss"] += 1
    return None


def _cache_put(key: str, value: Any, ttl: int) -> None:
    # Never cache an error: a transient REQUEST_DENIED during key setup would otherwise
    # stick around for a day and make the fix look like it did not work.
    if isinstance(value, dict) and "error" in value:
        return
    _CACHE[key] = (time.time() + ttl, value)


# ---------------------------------------------------------------------------
# Shaping helpers
# ---------------------------------------------------------------------------
_LATLNG_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")
# Google place ids are long, unpadded, and contain no whitespace. An address of that
# shape does not occur in practice, but an explicit "place_id:" prefix always wins so
# the caller can force the interpretation.
_PLACEID_RE = re.compile(r"^[A-Za-z0-9_\-]{20,}$")

_TRAVEL_MODES = {
    "driving": "DRIVE",
    "walking": "WALK",
    "bicycling": "BICYCLE",
    "transit": "TRANSIT",
}

_PRICE_LEVELS = {
    "free": "PRICE_LEVEL_FREE",
    "inexpensive": "PRICE_LEVEL_INEXPENSIVE",
    "moderate": "PRICE_LEVEL_MODERATE",
    "expensive": "PRICE_LEVEL_EXPENSIVE",
    "very_expensive": "PRICE_LEVEL_VERY_EXPENSIVE",
}


def _looks_like_place_id(s: str) -> bool:
    return bool(_PLACEID_RE.match(s)) and " " not in s


def _waypoint(value: str) -> dict:
    """Routes API waypoint from an address, a place id, or "lat,lng"."""
    s = (value or "").strip()
    if s.lower().startswith("place_id:"):
        return {"placeId": s.split(":", 1)[1].strip()}
    m = _LATLNG_RE.match(s)
    if m:
        return {"location": {"latLng": {"latitude": float(m.group(1)),
                                        "longitude": float(m.group(2))}}}
    if _looks_like_place_id(s):
        return {"placeId": s}
    return {"address": s}


def _fmt_duration(raw: str | None) -> str | None:
    """Routes returns durations as protobuf strings like "1234s"."""
    if not raw:
        return None
    try:
        secs = int(float(str(raw).rstrip("s")))
    except (ValueError, TypeError):
        return None
    if secs < 60:
        return f"{secs} sec"
    mins, hours = (secs + 30) // 60, 0
    if mins >= 60:
        hours, mins = divmod(mins, 60)
        return f"{hours} hr {mins} min" if mins else f"{hours} hr"
    return f"{mins} min"


def _fmt_distance(meters: int | None) -> str | None:
    if meters is None:
        return None
    miles = meters / 1609.344
    return f"{miles:.1f} mi" if miles >= 0.1 else f"{int(meters * 3.28084)} ft"


def _shape_place(p: dict) -> dict:
    """Flatten one Places (New) resource. Only fields our masks actually request appear,
    so this stays honest about what was paid for."""
    loc = p.get("location") or {}
    out: dict[str, Any] = {
        "place_id": p.get("id"),
        "name": (p.get("displayName") or {}).get("text"),
        "address": p.get("formattedAddress"),
        "latitude": loc.get("latitude"),
        "longitude": loc.get("longitude"),
    }
    if p.get("primaryTypeDisplayName"):
        out["type"] = (p["primaryTypeDisplayName"] or {}).get("text")
    if p.get("businessStatus"):
        out["business_status"] = p["businessStatus"]
    if p.get("googleMapsUri"):
        out["maps_url"] = p["googleMapsUri"]
    # Enterprise-tier fields, present only when the caller opted in.
    if p.get("rating") is not None:
        out["rating"] = p["rating"]
    if p.get("userRatingCount") is not None:
        out["review_count"] = p["userRatingCount"]
    if p.get("priceLevel"):
        out["price_level"] = p["priceLevel"]
    coh = p.get("currentOpeningHours") or {}
    if "openNow" in coh:
        out["open_now"] = coh["openNow"]
    return {k: v for k, v in out.items() if v is not None}


def _headers(field_mask: str) -> dict:
    """Places and Routes take the key in a header, which keeps it out of URLs entirely."""
    return {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": field_mask,
    }


# ---------------------------------------------------------------------------
# 4.1  maps_open_in_maps — no API call, no cost, highest priority
# ---------------------------------------------------------------------------
def _deep_link(destination: str, destination_place_id: str | None = None,
               origin: str | None = None, mode: str = "driving",
               action: str = "directions") -> str:
    """Build a Google Maps universal URL. Plain function, not the tool, so other tools can
    reuse it without reaching into FastMCP's Tool wrapper.

    urlencode(quote_via=quote) percent-encodes spaces as %20 rather than '+'. The Maps
    universal-URL handler accepts both, but %20 survives being pasted, shortened, and
    re-parsed by chat clients more reliably than '+'."""
    def _q(params: list[tuple[str, str]]) -> str:
        return urllib.parse.urlencode(params, quote_via=urllib.parse.quote)

    dest = (destination or "").strip()
    if action == "directions":
        params = [("api", "1"), ("destination", dest)]
        if destination_place_id:
            params.append(("destination_place_id", destination_place_id))
        if origin:
            params.append(("origin", origin.strip()))
        params.append(("travelmode", mode))
        return "https://www.google.com/maps/dir/?" + _q(params)

    # Google's universal URL for "show me this one place" is the search form carrying a
    # query_place_id; there is no separate api=1 place action.
    params = [("api", "1"), ("query", dest)]
    if destination_place_id:
        params.append(("query_place_id", destination_place_id))
    return "https://www.google.com/maps/search/?" + _q(params)


@mcp.tool
def maps_open_in_maps(
    destination: str,
    destination_place_id: str | None = None,
    origin: str | None = None,
    mode: str = "driving",
    action: str = "directions",
) -> dict:
    """Build a tappable Google Maps link. Tapping it in the Claude app opens turn-by-turn
    navigation. Makes NO API call and costs nothing, so prefer it freely.

    destination: address or place name.
    destination_place_id: strongly preferred when known — name-only lookups resolve to the
        wrong location often enough to matter. Pass the place_id from any search result.
    origin: omit to let the device use its current location.
    mode: driving | walking | bicycling | transit.
    action: directions | search | place.
    """
    if not _auth_ok():
        return _unauth()

    dest = (destination or "").strip()
    if not dest and not destination_place_id:
        return {"error": "INVALID_REQUEST",
                "detail": "destination (or destination_place_id) is required."}

    mode = (mode or "driving").lower()
    if mode not in _TRAVEL_MODES:
        return {"error": "INVALID_REQUEST",
                "detail": f"mode must be one of {sorted(_TRAVEL_MODES)}; got '{mode}'."}

    action = (action or "directions").lower()
    if action not in ("directions", "search", "place"):
        return {"error": "INVALID_REQUEST",
                "detail": f"action must be directions, search, or place; got '{action}'."}

    url = _deep_link(dest, destination_place_id, origin, mode, action)
    _count("free:deep_link")
    return {
        "url": url,
        "destination": dest,
        "mode": mode,
        "action": action,
        "billed_sku": "none (no API call)",
        "note": "Tap the URL to open Google Maps.",
    }


# ---------------------------------------------------------------------------
# 4.2  maps_search_places — Text Search
# ---------------------------------------------------------------------------
@mcp.tool
async def maps_search_places(
    query: str,
    latitude: float | None = None,
    longitude: float | None = None,
    radius_m: int = 5000,
    open_now: bool = False,
    min_rating: float | None = None,
    price_levels: list[str] | None = None,
    max_results: int = 10,
    include_ratings: bool = False,
) -> dict:
    """Find places by free-text query ("gyros", "urgent care", "Tikka Grill").

    open_now / min_rating / price_levels are FILTERS applied by Google. They are request
    parameters, cost nothing, and do not change the billing tier — results come back
    correctly filtered either way.

    include_ratings=False (default) bills Text Search **Pro** (5,000 free/month).
    include_ratings=True adds rating, review count, price level and an open-now flag to
    each result and bills **Enterprise** (1,000 free/month). Leave it off for a list;
    call maps_place_details on the one place the user picks instead.

    price_levels: free | inexpensive | moderate | expensive | very_expensive.
    """
    if not _auth_ok():
        return _unauth()
    if not API_KEY:
        return _no_key()
    if not (query or "").strip():
        return {"error": "INVALID_REQUEST", "detail": "query is required."}

    max_results = max(1, min(int(max_results), 20))  # Places caps maxResultCount at 20

    body: dict[str, Any] = {"textQuery": query.strip(), "maxResultCount": max_results}

    if latitude is not None and longitude is not None:
        radius = float(max(1, min(int(radius_m), 50000)))
        body["locationBias"] = {"circle": {
            "center": {"latitude": float(latitude), "longitude": float(longitude)},
            "radius": radius}}
    elif latitude is not None or longitude is not None:
        return {"error": "INVALID_REQUEST",
                "detail": "latitude and longitude must be supplied together."}

    if open_now:
        body["openNow"] = True
    if min_rating is not None:
        if not 0 <= float(min_rating) <= 5:
            return {"error": "INVALID_REQUEST",
                    "detail": "min_rating must be between 0 and 5."}
        # Google requires a 0.5 cadence and 400s on anything else.
        body["minRating"] = round(float(min_rating) * 2) / 2
    if price_levels:
        mapped, bad = [], []
        for p in price_levels:
            key = str(p).strip().lower()
            (mapped.append(_PRICE_LEVELS[key]) if key in _PRICE_LEVELS else bad.append(p))
        if bad:
            return {"error": "INVALID_REQUEST",
                    "detail": f"price_levels entries not recognised: {bad}. "
                              f"Valid: {sorted(_PRICE_LEVELS)}."}
        body["priceLevels"] = mapped

    mask = _SEARCH_MASK_ENTERPRISE if include_ratings else _SEARCH_MASK_PRO
    sku = "places:text_search:" + ("enterprise" if include_ratings else "pro")

    ck = f"search:{mask}:{json.dumps(body, sort_keys=True)}"
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    r = await _send("POST", f"{PLACES}/places:searchText", headers=_headers(mask), json=body)
    if not r.is_success:
        return _err(r)

    places = [_shape_place(p) for p in (r.json().get("places") or [])]
    if not places:
        return _zero_results()

    _count(sku)
    out = {"results": places, "count": len(places), "billed_sku": sku}
    _cache_put(ck, out, TTL_SEARCH)
    return out


# ---------------------------------------------------------------------------
# 4.3  maps_nearby — Nearby Search
# ---------------------------------------------------------------------------
@mcp.tool
async def maps_nearby(
    latitude: float,
    longitude: float,
    radius_m: int = 2000,
    included_types: list[str] | None = None,
    max_results: int = 10,
    include_ratings: bool = False,
) -> dict:
    """Find places around a coordinate by TYPE rather than by text.

    included_types: Google place types, e.g. ["restaurant"], ["pharmacy"], ["gas_station"].

    Note Nearby Search has no open-now or minimum-rating filter — that is a Text Search
    capability. If the user wants "open now", use maps_search_places instead of filtering
    these results client-side, which would mean paying Enterprise rates for the hours.

    Billing matches maps_search_places: Pro by default, Enterprise if include_ratings.
    """
    if not _auth_ok():
        return _unauth()
    if not API_KEY:
        return _no_key()

    max_results = max(1, min(int(max_results), 20))
    radius = float(max(1, min(int(radius_m), 50000)))

    body: dict[str, Any] = {
        "maxResultCount": max_results,
        "locationRestriction": {"circle": {
            "center": {"latitude": float(latitude), "longitude": float(longitude)},
            "radius": radius}},
    }
    if included_types:
        body["includedTypes"] = [str(t).strip() for t in included_types if str(t).strip()]

    mask = _SEARCH_MASK_ENTERPRISE if include_ratings else _SEARCH_MASK_PRO
    sku = "places:nearby_search:" + ("enterprise" if include_ratings else "pro")

    ck = f"nearby:{mask}:{json.dumps(body, sort_keys=True)}"
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    r = await _send("POST", f"{PLACES}/places:searchNearby", headers=_headers(mask), json=body)
    if not r.is_success:
        return _err(r)

    places = [_shape_place(p) for p in (r.json().get("places") or [])]
    if not places:
        return _zero_results()

    _count(sku)
    out = {"results": places, "count": len(places), "billed_sku": sku}
    _cache_put(ck, out, TTL_SEARCH)
    return out


# ---------------------------------------------------------------------------
# 4.4  maps_place_details
# ---------------------------------------------------------------------------
@mcp.tool
async def maps_place_details(place_id: str, include_contact: bool = True) -> dict:
    """Full detail for one place: phone, website, rating, price, hours, and whether it is
    open right now.

    include_contact=True (default) bills Place Details **Enterprise** (1,000 free/month),
    because phone / hours / website / rating are all Enterprise-tier fields — and they are
    the entire reason to call this. Set False for a Pro-tier name-and-address lookup.

    Cached 24h. This is the call to make on the ONE place a user picked, not on every
    search result.
    """
    if not _auth_ok():
        return _unauth()
    if not API_KEY:
        return _no_key()
    pid = (place_id or "").strip()
    if not pid:
        return {"error": "INVALID_REQUEST", "detail": "place_id is required."}
    if pid.lower().startswith("place_id:"):
        pid = pid.split(":", 1)[1].strip()

    mask = _DETAILS_MASK_ENTERPRISE if include_contact else _DETAILS_MASK_PRO
    sku = "places:details:" + ("enterprise" if include_contact else "pro")

    ck = f"details:{mask}:{pid}"
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    r = await _send("GET", f"{PLACES}/places/{urllib.parse.quote(pid)}", headers=_headers(mask))
    if not r.is_success:
        return _err(r)

    p = r.json()
    out = _shape_place(p)

    if p.get("nationalPhoneNumber"):
        out["phone"] = p["nationalPhoneNumber"]
    if p.get("internationalPhoneNumber"):
        out["phone_international"] = p["internationalPhoneNumber"]
    if p.get("websiteUri"):
        out["website"] = p["websiteUri"]

    hours = p.get("regularOpeningHours") or {}
    if hours.get("weekdayDescriptions"):
        out["hours"] = hours["weekdayDescriptions"]
    current = p.get("currentOpeningHours") or {}
    if "openNow" in current:
        out["open_now"] = current["openNow"]
    elif "openNow" in hours:
        out["open_now"] = hours["openNow"]

    # A deep link costs nothing, and it is the point of the whole server.
    out["directions_url"] = _deep_link(
        out.get("name") or out.get("address") or pid, destination_place_id=pid)

    _count(sku)
    out["billed_sku"] = sku
    _cache_put(ck, out, TTL_DETAILS)
    return out


# ---------------------------------------------------------------------------
# 4.5  maps_directions — Routes API computeRoutes
# ---------------------------------------------------------------------------
@mcp.tool
async def maps_directions(
    origin: str,
    destination: str,
    mode: str = "driving",
    departure_time: str | None = None,
    alternatives: bool = False,
    avoid: list[str] | None = None,
    include_steps: bool = False,
) -> dict:
    """Distance and traffic-aware drive time between two points, plus a tappable link.

    origin / destination: an address, a "lat,lng" pair, or a place id (either bare or
    prefixed "place_id:").

    Driving routes request TRAFFIC_AWARE, which is what makes the duration reflect current
    traffic — and what bills them **Pro** (5,000 free/month). Walking, bicycling and
    transit omit routingPreference (Google rejects it on those modes) and bill
    **Essentials** (10,000 free/month).

    include_steps=False by default on purpose: turn-by-turn text is a wall of prose in a
    chat window, and the deep link gives the user real navigation anyway.

    Never cached — traffic is the entire point.
    """
    if not _auth_ok():
        return _unauth()
    if not API_KEY:
        return _no_key()
    if not (origin or "").strip() or not (destination or "").strip():
        return {"error": "INVALID_REQUEST", "detail": "origin and destination are required."}

    mode = (mode or "driving").lower()
    if mode not in _TRAVEL_MODES:
        return {"error": "INVALID_REQUEST",
                "detail": f"mode must be one of {sorted(_TRAVEL_MODES)}; got '{mode}'."}
    travel_mode = _TRAVEL_MODES[mode]

    body: dict[str, Any] = {
        "origin": _waypoint(origin),
        "destination": _waypoint(destination),
        "travelMode": travel_mode,
        "units": "IMPERIAL",
        "languageCode": "en-US",
    }

    # routingPreference is valid only for DRIVE and TWO_WHEELER; sending it on WALK,
    # BICYCLE or TRANSIT is a hard 400 from Google, not a silently ignored field.
    traffic_aware = travel_mode == "DRIVE"
    if traffic_aware:
        body["routingPreference"] = "TRAFFIC_AWARE"
    if alternatives and travel_mode != "TRANSIT":
        body["computeAlternativeRoutes"] = True

    if avoid:
        allowed = {"tolls": "avoidTolls", "highways": "avoidHighways", "ferries": "avoidFerries"}
        bad = [a for a in avoid if str(a).strip().lower() not in allowed]
        if bad:
            return {"error": "INVALID_REQUEST",
                    "detail": f"avoid entries not recognised: {bad}. Valid: {sorted(allowed)}."}
        # Route modifiers do not apply to transit and Google rejects them there.
        if travel_mode == "TRANSIT":
            return {"error": "INVALID_REQUEST",
                    "detail": "avoid is not supported for transit routes."}
        body["routeModifiers"] = {allowed[str(a).strip().lower()]: True for a in avoid}

    if departure_time:
        # Must be RFC-3339 and in the future; Google 400s otherwise.
        body["departureTime"] = departure_time

    mask = _ROUTES_MASK_STEPS if include_steps else _ROUTES_MASK
    r = await _send("POST", f"{ROUTES}/directions/v2:computeRoutes",
                    headers=_headers(mask), json=body)
    if not r.is_success:
        return _err(r)

    routes = r.json().get("routes") or []
    if not routes:
        return _zero_results("routes")

    sku = "routes:compute_routes:" + ("pro" if traffic_aware else "essentials")
    _count(sku)

    shaped = []
    for rt in routes:
        meters = rt.get("distanceMeters")
        entry: dict[str, Any] = {
            "summary": rt.get("description"),
            "distance_meters": meters,
            "distance": _fmt_distance(meters),
            # duration is traffic-aware on DRIVE; staticDuration never is, so the pair
            # together is what tells you how much of the trip is congestion.
            "duration": _fmt_duration(rt.get("duration")),
            "duration_in_traffic": _fmt_duration(rt.get("duration")) if traffic_aware else None,
            "duration_without_traffic": _fmt_duration(rt.get("staticDuration")),
        }
        if include_steps:
            steps = []
            for leg in rt.get("legs") or []:
                for st in leg.get("steps") or []:
                    instr = (st.get("navigationInstruction") or {}).get("instructions")
                    if instr:
                        steps.append({"instruction": instr,
                                      "distance": _fmt_distance(st.get("distanceMeters"))})
            entry["steps"] = steps
        shaped.append({k: v for k, v in entry.items() if v is not None})

    return {
        "routes": shaped,
        "mode": mode,
        "traffic_aware": traffic_aware,
        "directions_url": _deep_link(destination, origin=origin, mode=mode),
        "billed_sku": sku,
    }


# ---------------------------------------------------------------------------
# 4.6  maps_travel_time_matrix — Routes API computeRouteMatrix
# ---------------------------------------------------------------------------
@mcp.tool
async def maps_travel_time_matrix(
    origins: list[str],
    destinations: list[str],
    mode: str = "driving",
) -> dict:
    """Travel time and distance for every origin/destination pair — "which of these is
    closest?".

    Billed PER ELEMENT (origins x destinations), which is why the total is capped at 25
    and anything larger is refused before a request leaves this process. A 6x6 is 36
    elements and will be rejected. This is the single easiest tool here on which to run up
    a surprising bill.
    """
    if not _auth_ok():
        return _unauth()
    if not API_KEY:
        return _no_key()

    origins = [o for o in (origins or []) if str(o).strip()]
    destinations = [d for d in (destinations or []) if str(d).strip()]
    if not origins or not destinations:
        return {"error": "INVALID_REQUEST",
                "detail": "origins and destinations must each contain at least one entry."}

    elements = len(origins) * len(destinations)
    if elements > _MATRIX_MAX_ELEMENTS:
        return {"error": "INVALID_REQUEST",
                "detail": (f"{len(origins)} origins x {len(destinations)} destinations = "
                           f"{elements} elements, over the {_MATRIX_MAX_ELEMENTS}-element "
                           f"cap. This endpoint bills per element. Narrow the lists and "
                           f"retry.")}

    mode = (mode or "driving").lower()
    if mode not in _TRAVEL_MODES:
        return {"error": "INVALID_REQUEST",
                "detail": f"mode must be one of {sorted(_TRAVEL_MODES)}; got '{mode}'."}
    travel_mode = _TRAVEL_MODES[mode]

    body: dict[str, Any] = {
        "origins": [{"waypoint": _waypoint(o)} for o in origins],
        "destinations": [{"waypoint": _waypoint(d)} for d in destinations],
        "travelMode": travel_mode,
    }
    traffic_aware = travel_mode == "DRIVE"
    if traffic_aware:
        body["routingPreference"] = "TRAFFIC_AWARE"

    r = await _send("POST", f"{ROUTES}/distanceMatrix/v2:computeRouteMatrix",
                    headers=_headers(_MATRIX_MASK), json=body)
    if not r.is_success:
        return _err(r)

    try:
        rows = r.json()
    except Exception:
        return {"error": "INVALID_RESPONSE",
                "detail": "Route matrix response could not be parsed."}
    if not isinstance(rows, list):
        rows = [rows]

    sku = "routes:route_matrix:" + ("pro" if traffic_aware else "essentials")
    _count(sku, elements)

    cells = []
    for cell in rows:
        oi, di = cell.get("originIndex"), cell.get("destinationIndex")
        entry: dict[str, Any] = {
            "origin": origins[oi] if oi is not None and oi < len(origins) else None,
            "destination": destinations[di] if di is not None and di < len(destinations) else None,
        }
        # A per-cell failure (unroutable pair) carries a populated `status`; the whole
        # request still succeeded, so surface it on the cell rather than failing the call.
        if cell.get("condition") == "ROUTE_NOT_FOUND" or (cell.get("status") or {}).get("code"):
            entry["error"] = "No route found for this pair."
        else:
            entry["distance"] = _fmt_distance(cell.get("distanceMeters"))
            entry["distance_meters"] = cell.get("distanceMeters")
            entry["duration"] = _fmt_duration(cell.get("duration"))
        cells.append({k: v for k, v in entry.items() if v is not None})

    return {
        "matrix": cells,
        "elements": elements,
        "mode": mode,
        "traffic_aware": traffic_aware,
        "billed_sku": sku,
        "billing_note": f"{elements} elements billed (origins x destinations).",
    }


# ---------------------------------------------------------------------------
# 4.7  maps_geocode / maps_reverse_geocode
# ---------------------------------------------------------------------------
async def _geocode_call(params: dict, cache_key: str) -> dict:
    """Geocoding is the older web service: it accepts the key ONLY as a ?key= query
    parameter, so unlike Places and Routes the key is genuinely in the URL here. httpx
    embeds the request URL in several of its own exception strings, which is why every
    error path in this module runs through _scrub()."""
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    r = await _send("GET", GEOCODE, params={**params, "key": API_KEY})
    if not r.is_success:
        return _err(r)

    body = r.json()
    status = body.get("status")
    if status == "ZERO_RESULTS":
        return _zero_results("addresses")
    if status != "OK":
        return _err(r)

    _count("geocoding:essentials")
    results = body.get("results") or []
    if not results:
        return _zero_results("addresses")

    top = results[0]
    loc = (top.get("geometry") or {}).get("location") or {}
    out = {
        "formatted_address": top.get("formatted_address"),
        "latitude": loc.get("lat"),
        "longitude": loc.get("lng"),
        "place_id": top.get("place_id"),
        "location_type": (top.get("geometry") or {}).get("location_type"),
        "billed_sku": "geocoding:essentials",
    }
    out = {k: v for k, v in out.items() if v is not None}
    _cache_put(cache_key, out, TTL_GEOCODE)
    return out


@mcp.tool
async def maps_geocode(address: str) -> dict:
    """Address -> coordinates (plus the canonical formatted address and its place_id).

    Bills Geocoding **Essentials** (10,000 free/month). Cached 7 days — addresses are
    effectively static.
    """
    if not _auth_ok():
        return _unauth()
    if not API_KEY:
        return _no_key()
    addr = (address or "").strip()
    if not addr:
        return {"error": "INVALID_REQUEST", "detail": "address is required."}
    return await _geocode_call({"address": addr}, f"geocode:{addr.lower()}")


@mcp.tool
async def maps_reverse_geocode(latitude: float, longitude: float) -> dict:
    """Coordinates -> street address. Bills Geocoding **Essentials**. Cached 7 days."""
    if not _auth_ok():
        return _unauth()
    if not API_KEY:
        return _no_key()
    try:
        lat, lng = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return {"error": "INVALID_REQUEST", "detail": "latitude and longitude must be numbers."}
    if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
        return {"error": "INVALID_REQUEST",
                "detail": "latitude must be -90..90 and longitude -180..180."}
    return await _geocode_call({"latlng": f"{lat},{lng}"}, f"revgeocode:{lat:.6f},{lng:.6f}")


# ---------------------------------------------------------------------------
# Healthcheck
# ---------------------------------------------------------------------------
@mcp.tool
async def maps_healthcheck() -> dict:
    """Verify the API key works, using a single cheap Essentials-tier geocode, and report
    key status, auth mode, cache statistics and per-SKU call counts for this replica.

    Counts are per-replica and reset when Container Apps scales to zero. They are a drift
    signal, not a billing record — the Google Cloud console is authoritative.
    """
    if not _auth_ok():
        return _unauth()

    out: dict[str, Any] = {
        "checked_at": _now(),
        "auth_mode": ("entra-oauth" if OAUTH_ENABLED
                      else "static-bearer" if BEARER
                      else "ANONYMOUS (MCP_ALLOW_ANON=1)" if ALLOW_ANON
                      else "locked (no bearer configured, all calls refused)"),
        "api_key": "absent" if not API_KEY else "present",
        "cache": {**_CACHE_STATS, "entries": len(_CACHE)},
        "sku_calls_this_replica": dict(sorted(_SKU_COUNTS.items())) or {},
    }
    if not API_KEY:
        out["key_status"] = "FAIL: GOOGLE_MAPS_API_KEY is not set."
        return out

    # Deliberately bypasses the 7-day geocode cache so this actually exercises the key.
    r = await _send("GET", GEOCODE,
                    params={"address": "1600 Amphitheatre Parkway, Mountain View, CA",
                            "key": API_KEY})
    if not r.is_success:
        e = _err(r)
        out["key_status"] = f"FAIL: {e.get('error')} — {e.get('detail')}"
        if e.get("hint"):
            out["hint"] = e["hint"]
        return out

    status = (r.json() or {}).get("status")
    if status == "OK":
        _count("geocoding:essentials")
        out["key_status"] = "ok"
        out["apis_verified"] = ["Geocoding API"]
        out["apis_not_verified"] = ["Places API (New)", "Routes API"]
        out["note"] = ("Only Geocoding is exercised, to keep this call cheap. A working "
                       "key here with a failing search means the key's API restriction "
                       "list is missing Places or Routes.")
    else:
        out["key_status"] = f"FAIL: geocoding returned status={status}"
    return out


if __name__ == "__main__":
    # Streamable-HTTP for remote connector use. Container listens on 8080.
    if not OAUTH_ENABLED and not BEARER and not ALLOW_ANON:
        _slog("boot_warning", msg="No auth configured; every tool call will be refused. "
                                  "Set OAuth vars, MCP_BEARER, or MCP_ALLOW_ANON=1.")
    _slog("boot", oauth=OAUTH_ENABLED, bearer=bool(BEARER), key=bool(API_KEY))
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
