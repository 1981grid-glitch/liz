"""
Cartographer test suite — runs with NO API key and makes NO network calls.

Every Google call is stubbed at the _send() seam, so the suite is safe to run anywhere
and cannot bill anything. It loads the real cartographer_mcp_server.py by path rather
than re-declaring behaviour, so it cannot drift from shipping code.

    python test_cartographer.py
"""

import asyncio
import importlib.util
import os
import sys
import urllib.parse

os.environ.setdefault("MCP_ALLOW_ANON", "1")   # Tier 1 gate open so tools are reachable
os.environ.setdefault("GOOGLE_MAPS_API_KEY", "TEST_KEY_dGVzdA_NOT_REAL")

_spec = importlib.util.spec_from_file_location(
    "carto", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "cartographer_mcp_server.py"))
carto = importlib.util.module_from_spec(_spec)
sys.modules["carto"] = carto
_spec.loader.exec_module(carto)

_PASS, _FAIL = 0, 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ok   {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}" + (f"  -- {detail}" if detail else ""))


class FakeResponse:
    """Minimal httpx.Response stand-in for the _send() seam."""

    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text or str(payload)
        self.headers = {}

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def stub(payload, status_code=200):
    """Replace _send and capture what the server would have sent."""
    calls = []

    async def _fake(method, url, **kw):
        calls.append({"method": method, "url": url, **kw})
        return FakeResponse(payload, status_code)

    carto._send = _fake
    carto._CACHE.clear()
    return calls


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
print("\n[1] Deep links — no API call, correct encoding")
# ---------------------------------------------------------------------------
r = carto.maps_open_in_maps.fn(destination="Tikka Grill, West Chester OH")
check("directions URL uses the api=1 universal form",
      r["url"].startswith("https://www.google.com/maps/dir/?api=1"), r["url"])
check("spaces encode as %20 not '+'", "%20" in r["url"] and "+" not in r["url"], r["url"])
check("comma in destination is encoded", "%2C" in r["url"], r["url"])
check("costs nothing", r["billed_sku"] == "none (no API call)")

r = carto.maps_open_in_maps.fn(destination="Tikka Grill",
                               destination_place_id="ChIJN1t_tDeuEmsRUsoyG83frY4")
check("place_id is carried on directions links",
      "destination_place_id=ChIJN1t_tDeuEmsRUsoyG83frY4" in r["url"], r["url"])

r = carto.maps_open_in_maps.fn(destination="coffee", action="search")
check("search action uses the /maps/search/ form",
      r["url"].startswith("https://www.google.com/maps/search/?api=1"), r["url"])

r = carto.maps_open_in_maps.fn(destination="X", destination_place_id="PID", action="place")
check("place action carries query_place_id", "query_place_id=PID" in r["url"], r["url"])

r = carto.maps_open_in_maps.fn(destination="X", origin="Y", mode="transit")
check("origin and travelmode round-trip",
      "origin=Y" in r["url"] and "travelmode=transit" in r["url"], r["url"])

check("bad mode rejected",
      carto.maps_open_in_maps.fn(destination="X", mode="teleport").get("error") == "INVALID_REQUEST")
check("bad action rejected",
      carto.maps_open_in_maps.fn(destination="X", action="nope").get("error") == "INVALID_REQUEST")
check("empty destination rejected",
      carto.maps_open_in_maps.fn(destination="  ").get("error") == "INVALID_REQUEST")

amp = carto.maps_open_in_maps.fn(destination="Bob & Sons #1")["url"]
check("ampersand cannot inject a query parameter", "%26" in amp and "&Sons" not in amp, amp)


# ---------------------------------------------------------------------------
print("\n[2] Field masks — the billing surface")
# ---------------------------------------------------------------------------
_ENTERPRISE_FIELDS = ("rating", "userRatingCount", "priceLevel", "priceRange",
                      "currentOpeningHours", "regularOpeningHours",
                      "nationalPhoneNumber", "internationalPhoneNumber", "websiteUri")

for name in ("_SEARCH_MASK_PRO", "_SEARCH_MASK_ENTERPRISE", "_DETAILS_MASK_PRO",
             "_DETAILS_MASK_ENTERPRISE", "_ROUTES_MASK", "_ROUTES_MASK_STEPS",
             "_MATRIX_MASK"):
    check(f"{name} never uses a wildcard", "*" not in getattr(carto, name), name)

leaked = [f for f in _ENTERPRISE_FIELDS if f in carto._SEARCH_MASK_PRO]
check("Pro search mask contains NO Enterprise-tier field", not leaked, f"leaked: {leaked}")

leaked = [f for f in _ENTERPRISE_FIELDS if f in carto._DETAILS_MASK_PRO]
check("Pro details mask contains NO Enterprise-tier field", not leaked, f"leaked: {leaked}")

check("Enterprise search mask does add ratings",
      "places.rating" in carto._SEARCH_MASK_ENTERPRISE)
check("Enterprise details mask does add phone + hours",
      "nationalPhoneNumber" in carto._DETAILS_MASK_ENTERPRISE
      and "regularOpeningHours" in carto._DETAILS_MASK_ENTERPRISE)

check("search masks use the 'places.' prefix",
      all(f.startswith("places.") for f in carto._SEARCH_MASK_ENTERPRISE.split(",")))
check("details masks do NOT use the 'places.' prefix",
      not any(f.startswith("places.") for f in carto._DETAILS_MASK_ENTERPRISE.split(",")))
check("route masks use the 'routes.' prefix",
      all(f.startswith("routes.") for f in carto._ROUTES_MASK_STEPS.split(",")))


# ---------------------------------------------------------------------------
print("\n[3] Search — filters are free, fields are billed")
# ---------------------------------------------------------------------------
PLACES_PAYLOAD = {"places": [{
    "id": "PID1", "displayName": {"text": "Gyro Palace"},
    "formattedAddress": "1 Main St", "location": {"latitude": 39.3, "longitude": -84.4},
    "businessStatus": "OPERATIONAL"}]}

calls = stub(PLACES_PAYLOAD)
r = run(carto.maps_search_places.fn(query="gyros", latitude=39.3269, longitude=-84.4270,
                                    open_now=True, min_rating=4.0))
sent = calls[0]
check("open_now goes out as a request FILTER", sent["json"].get("openNow") is True)
check("min_rating goes out as a request FILTER", sent["json"].get("minRating") == 4.0)
check("filtered search still bills PRO, not Enterprise",
      r["billed_sku"] == "places:text_search:pro", r["billed_sku"])
check("Pro field mask was sent",
      sent["headers"]["X-Goog-FieldMask"] == carto._SEARCH_MASK_PRO)
check("API key travels in a header, never the URL",
      sent["headers"].get("X-Goog-Api-Key") == carto.API_KEY and "key=" not in sent["url"])
check("locationBias circle built from lat/lng",
      sent["json"]["locationBias"]["circle"]["radius"] == 5000.0)
check("result is flattened", r["results"][0]["name"] == "Gyro Palace")

calls = stub(PLACES_PAYLOAD)
r = run(carto.maps_search_places.fn(query="gyros", include_ratings=True))
check("include_ratings escalates to Enterprise",
      r["billed_sku"] == "places:text_search:enterprise", r["billed_sku"])
check("Enterprise mask sent only when asked",
      calls[0]["headers"]["X-Goog-FieldMask"] == carto._SEARCH_MASK_ENTERPRISE)

calls = stub(PLACES_PAYLOAD)
run(carto.maps_search_places.fn(query="x", min_rating=4.3))
check("min_rating snapped to Google's 0.5 cadence",
      calls[0]["json"]["minRating"] == 4.5, str(calls[0]["json"].get("minRating")))

calls = stub(PLACES_PAYLOAD)
run(carto.maps_search_places.fn(query="x", max_results=99))
check("max_results clamped to the API ceiling of 20",
      calls[0]["json"]["maxResultCount"] == 20)

r = run(carto.maps_search_places.fn(query="x", price_levels=["cheap"]))
check("unknown price level rejected with the valid set named",
      r.get("error") == "INVALID_REQUEST" and "inexpensive" in r.get("detail", ""))

calls = stub(PLACES_PAYLOAD)
run(carto.maps_search_places.fn(query="x", price_levels=["moderate", "expensive"]))
check("price levels map to Google enums",
      calls[0]["json"]["priceLevels"] == ["PRICE_LEVEL_MODERATE", "PRICE_LEVEL_EXPENSIVE"])

r = run(carto.maps_search_places.fn(query="x", latitude=39.3))
check("half a coordinate pair is rejected", r.get("error") == "INVALID_REQUEST")

stub({"places": []})
check("empty result set becomes ZERO_RESULTS",
      run(carto.maps_search_places.fn(query="nothing")).get("error") == "ZERO_RESULTS")


# ---------------------------------------------------------------------------
print("\n[4] Nearby + details")
# ---------------------------------------------------------------------------
calls = stub(PLACES_PAYLOAD)
r = run(carto.maps_nearby.fn(latitude=39.3, longitude=-84.4, included_types=["restaurant"]))
check("nearby uses locationRestriction, not bias",
      "locationRestriction" in calls[0]["json"])
check("nearby defaults to Pro", r["billed_sku"] == "places:nearby_search:pro")
check("included_types passed through",
      calls[0]["json"]["includedTypes"] == ["restaurant"])

calls = stub(PLACES_PAYLOAD)
run(carto.maps_nearby.fn(latitude=0, longitude=0, radius_m=99999))
check("nearby radius clamped to the 50km API ceiling",
      calls[0]["json"]["locationRestriction"]["circle"]["radius"] == 50000.0)

DETAIL_PAYLOAD = {
    "id": "PID1", "displayName": {"text": "Gyro Palace"},
    "formattedAddress": "1 Main St", "location": {"latitude": 39.3, "longitude": -84.4},
    "nationalPhoneNumber": "(513) 555-0100", "websiteUri": "https://example.com",
    "rating": 4.6, "userRatingCount": 812,
    "regularOpeningHours": {"weekdayDescriptions": ["Monday: 11:00 AM – 9:00 PM"]},
    "currentOpeningHours": {"openNow": True},
}
calls = stub(DETAIL_PAYLOAD)
r = run(carto.maps_place_details.fn(place_id="PID1"))
check("details bills Enterprise by default (that is the point)",
      r["billed_sku"] == "places:details:enterprise")
check("phone surfaced", r["phone"] == "(513) 555-0100")
check("hours surfaced", r["hours"] == ["Monday: 11:00 AM – 9:00 PM"])
check("open_now surfaced", r["open_now"] is True)
check("details carries a free deep link",
      r["directions_url"].startswith("https://www.google.com/maps/dir/?api=1")
      and "destination_place_id=PID1" in r["directions_url"])
check("details is a GET on the place resource",
      calls[0]["method"] == "GET" and calls[0]["url"].endswith("/places/PID1"))

calls = stub(DETAIL_PAYLOAD)
r = run(carto.maps_place_details.fn(place_id="PID1", include_contact=False))
check("include_contact=False drops to Pro", r["billed_sku"] == "places:details:pro")

calls = stub(DETAIL_PAYLOAD)
run(carto.maps_place_details.fn(place_id="place_id:PID1"))
check("a 'place_id:' prefix is tolerated and stripped", calls[0]["url"].endswith("/places/PID1"))


# ---------------------------------------------------------------------------
print("\n[5] Caching")
# ---------------------------------------------------------------------------
calls = stub(DETAIL_PAYLOAD)
run(carto.maps_place_details.fn(place_id="PID1"))
run(carto.maps_place_details.fn(place_id="PID1"))
check("identical details call served from cache (one network call)", len(calls) == 1,
      f"{len(calls)} calls")

calls = stub(DETAIL_PAYLOAD)
run(carto.maps_place_details.fn(place_id="PID1"))
run(carto.maps_place_details.fn(place_id="PID1", include_contact=False))
check("a different field mask is a different cache key", len(calls) == 2, f"{len(calls)} calls")

check("routes TTL is absent — traffic must never be cached",
      not hasattr(carto, "TTL_ROUTES"))
check("details TTL is 24h", carto.TTL_DETAILS == 24 * 3600)
check("geocode TTL is 7d", carto.TTL_GEOCODE == 7 * 24 * 3600)
check("search TTL is 15min", carto.TTL_SEARCH == 15 * 60)

carto._CACHE.clear()
carto._cache_put("k", {"error": "REQUEST_DENIED"}, 60)
check("errors are never cached", carto._cache_get("k") is None)


# ---------------------------------------------------------------------------
print("\n[6] Routes")
# ---------------------------------------------------------------------------
ROUTE_PAYLOAD = {"routes": [{"distanceMeters": 18500, "duration": "1500s",
                             "staticDuration": "1200s", "description": "I-465 N"}]}
calls = stub(ROUTE_PAYLOAD)
r = run(carto.maps_directions.fn(origin="A St", destination="B St"))
check("driving requests TRAFFIC_AWARE",
      calls[0]["json"]["routingPreference"] == "TRAFFIC_AWARE")
check("traffic-aware driving bills Pro",
      r["billed_sku"] == "routes:compute_routes:pro", r["billed_sku"])
check("distance humanised", r["routes"][0]["distance"] == "11.5 mi",
      r["routes"][0].get("distance"))
check("duration humanised", r["routes"][0]["duration"] == "25 min",
      r["routes"][0].get("duration"))
check("static duration retained for traffic comparison",
      r["routes"][0]["duration_without_traffic"] == "20 min")
check("routes carry a deep link",
      r["directions_url"].startswith("https://www.google.com/maps/dir/?api=1"))
check("steps withheld by default", "steps" not in r["routes"][0])
check("step fields absent from the default mask",
      calls[0]["headers"]["X-Goog-FieldMask"] == carto._ROUTES_MASK)

calls = stub(ROUTE_PAYLOAD)
r = run(carto.maps_directions.fn(origin="A", destination="B", mode="walking"))
check("walking omits routingPreference (Google 400s on it)",
      "routingPreference" not in calls[0]["json"])
check("walking bills Essentials, not Pro",
      r["billed_sku"] == "routes:compute_routes:essentials", r["billed_sku"])
check("walking is reported as not traffic-aware", r["traffic_aware"] is False)

calls = stub(ROUTE_PAYLOAD)
run(carto.maps_directions.fn(origin="A", destination="B", include_steps=True))
check("include_steps widens the mask",
      calls[0]["headers"]["X-Goog-FieldMask"] == carto._ROUTES_MASK_STEPS)

calls = stub(ROUTE_PAYLOAD)
run(carto.maps_directions.fn(origin="39.77,-86.15", destination="place_id:ChIJPID",
                             avoid=["tolls"]))
check("lat,lng origin becomes a latLng waypoint",
      calls[0]["json"]["origin"]["location"]["latLng"]["latitude"] == 39.77)
check("place_id: destination becomes a placeId waypoint",
      calls[0]["json"]["destination"]["placeId"] == "ChIJPID")
check("avoid maps to routeModifiers",
      calls[0]["json"]["routeModifiers"] == {"avoidTolls": True})

r = run(carto.maps_directions.fn(origin="A", destination="B", avoid=["potholes"]))
check("unknown avoid value rejected", r.get("error") == "INVALID_REQUEST")
r = run(carto.maps_directions.fn(origin="A", destination="B", mode="transit", avoid=["tolls"]))
check("avoid on transit rejected rather than 400ing at Google",
      r.get("error") == "INVALID_REQUEST")
r = run(carto.maps_directions.fn(origin="", destination="B"))
check("missing origin rejected", r.get("error") == "INVALID_REQUEST")

stub({"routes": []})
check("no route becomes ZERO_RESULTS",
      run(carto.maps_directions.fn(origin="A", destination="B")).get("error") == "ZERO_RESULTS")


# ---------------------------------------------------------------------------
print("\n[7] Route matrix — the per-element bill")
# ---------------------------------------------------------------------------
check("cap constant is 25", carto._MATRIX_MAX_ELEMENTS == 25)

six = [f"o{i}" for i in range(6)]
calls = stub([])
r = run(carto.maps_travel_time_matrix.fn(origins=six, destinations=six))
check("6x6 = 36 elements is refused", r.get("error") == "INVALID_REQUEST")
check("refusal names the element math", "36 elements" in r.get("detail", ""), r.get("detail"))
check("refusal happens BEFORE any request leaves", len(calls) == 0, f"{len(calls)} calls")

MATRIX_PAYLOAD = [{"originIndex": 0, "destinationIndex": 0,
                   "distanceMeters": 3200, "duration": "480s", "condition": "ROUTE_EXISTS"}]
calls = stub(MATRIX_PAYLOAD)
r = run(carto.maps_travel_time_matrix.fn(origins=["a"], destinations=["b", "c", "d", "e", "f"]))
check("1x5 = 5 elements is allowed", "matrix" in r, r.get("error"))
check("element count reported for cost visibility", r["elements"] == 5)
check("matrix bills per element", "5 elements billed" in r["billing_note"])

calls = stub([{"originIndex": 0, "destinationIndex": 0, "condition": "ROUTE_NOT_FOUND"}])
r = run(carto.maps_travel_time_matrix.fn(origins=["a"], destinations=["b"]))
check("an unroutable pair fails the CELL, not the call",
      r["matrix"][0].get("error") is not None and "matrix" in r)

r = run(carto.maps_travel_time_matrix.fn(origins=[], destinations=["b"]))
check("empty origins rejected", r.get("error") == "INVALID_REQUEST")


# ---------------------------------------------------------------------------
print("\n[8] Geocoding")
# ---------------------------------------------------------------------------
GEO_PAYLOAD = {"status": "OK", "results": [{
    "formatted_address": "1600 Amphitheatre Pkwy, Mountain View, CA 94043, USA",
    "place_id": "ChIJgeo",
    "geometry": {"location": {"lat": 37.42, "lng": -122.08}, "location_type": "ROOFTOP"}}]}

calls = stub(GEO_PAYLOAD)
r = run(carto.maps_geocode.fn(address="1600 Amphitheatre Parkway"))
check("geocode bills Essentials", r["billed_sku"] == "geocoding:essentials")
check("coordinates extracted", (r["latitude"], r["longitude"]) == (37.42, -122.08))
check("place_id returned for onward deep linking", r["place_id"] == "ChIJgeo")

calls = stub(GEO_PAYLOAD)
run(carto.maps_geocode.fn(address="X"))
run(carto.maps_geocode.fn(address="x"))
check("geocode cache is case-insensitive", len(calls) == 1, f"{len(calls)} calls")

stub({"status": "ZERO_RESULTS", "results": []})
check("geocode ZERO_RESULTS mapped",
      run(carto.maps_geocode.fn(address="nowhere at all")).get("error") == "ZERO_RESULTS")

r = run(carto.maps_reverse_geocode.fn(latitude=999, longitude=0))
check("out-of-range latitude rejected", r.get("error") == "INVALID_REQUEST")
check("empty address rejected", run(carto.maps_geocode.fn(address=" ")).get("error") == "INVALID_REQUEST")


# ---------------------------------------------------------------------------
print("\n[9] Error mapping — §5.3")
# ---------------------------------------------------------------------------
stub({"error": {"status": "PERMISSION_DENIED", "message": "API key not valid"}}, 403)
r = run(carto.maps_search_places.fn(query="x"))
check("403 -> REQUEST_DENIED", r.get("error") == "REQUEST_DENIED")
check("REQUEST_DENIED explains it is a key restriction problem",
      "restriction" in r.get("hint", "").lower())

stub({"error": {"status": "RESOURCE_EXHAUSTED", "message": "quota"}}, 429)
r = run(carto.maps_search_places.fn(query="x"))
check("429 -> OVER_QUERY_LIMIT", r.get("error") == "OVER_QUERY_LIMIT")
check("OVER_QUERY_LIMIT says the cap did its job", "cap did its job" in r.get("hint", ""))

stub({"error": {"status": "INVALID_ARGUMENT", "message": "bad field mask 'places.nope'"}}, 400)
r = run(carto.maps_search_places.fn(query="x"))
check("400 -> INVALID_REQUEST", r.get("error") == "INVALID_REQUEST")
check("INVALID_REQUEST echoes which parameter Google rejected",
      "places.nope" in r.get("detail", ""), r.get("detail"))

stub({"error": {"status": "NOT_FOUND", "message": "no such place"}}, 404)
check("404 -> NOT_FOUND",
      run(carto.maps_place_details.fn(place_id="nope")).get("error") == "NOT_FOUND")


# ---------------------------------------------------------------------------
print("\n[10] The API key must never leave the process")
# ---------------------------------------------------------------------------
KEY = carto.API_KEY
check("_scrub removes a literal key", KEY not in carto._scrub(f"failed for key={KEY} oops"))
check("_scrub removes any ?key= query value",
      "SOMEONE_ELSES" not in carto._scrub("https://x/y?key=SOMEONE_ELSES&z=1"))
check("_scrub leaves surrounding text intact",
      carto._scrub("a key=B c").startswith("a key="))

# An error body that echoes the key back at us must still come out clean.
stub({"error": {"status": "PERMISSION_DENIED",
                "message": f"key {KEY} is not authorized for https://maps.googleapis.com/x?key={KEY}"}}, 403)
r = run(carto.maps_search_places.fn(query="x"))
check("key echoed in an error body is redacted before it reaches the caller",
      KEY not in str(r), str(r)[:160])

stub(None, 500)
r = run(carto.maps_geocode.fn(address="unique-nocache-1"))
check("unparseable error body still yields a structured error",
      "error" in r and KEY not in str(r))


# ---------------------------------------------------------------------------
print("\n[11] Auth gate fails closed")
# ---------------------------------------------------------------------------
_saved = (carto.OAUTH_ENABLED, carto.BEARER, carto.ALLOW_ANON)
try:
    carto.OAUTH_ENABLED, carto.BEARER, carto.ALLOW_ANON = False, "", False
    check("no OAuth + no bearer + no explicit anon => refused",
          carto._auth_ok() is False)
    check("a refused tool returns unauthorized, not data",
          carto.maps_open_in_maps.fn(destination="X").get("error") == "unauthorized")
    carto.OAUTH_ENABLED, carto.BEARER, carto.ALLOW_ANON = False, "", True
    check("MCP_ALLOW_ANON=1 must be explicit to open the gate", carto._auth_ok() is True)
    carto.OAUTH_ENABLED = True
    check("OAuth mode trusts the transport layer", carto._auth_ok() is True)
finally:
    carto.OAUTH_ENABLED, carto.BEARER, carto.ALLOW_ANON = _saved


# ---------------------------------------------------------------------------
print("\n[12] Formatting helpers")
# ---------------------------------------------------------------------------
check("seconds", carto._fmt_duration("45s") == "45 sec")
check("minutes round to nearest", carto._fmt_duration("1500s") == "25 min")
check("hours", carto._fmt_duration("3900s") == "1 hr 5 min")
check("exact hour drops the minutes", carto._fmt_duration("3600s") == "1 hr")
check("None duration is tolerated", carto._fmt_duration(None) is None)
check("garbage duration is tolerated", carto._fmt_duration("abc") is None)
check("miles", carto._fmt_distance(1609) == "1.0 mi")
check("short distances fall back to feet", carto._fmt_distance(50) == "164 ft")
check("None distance is tolerated", carto._fmt_distance(None) is None)

check("bare place id detected", carto._waypoint("ChIJN1t_tDeuEmsRUsoyG83frY4") ==
      {"placeId": "ChIJN1t_tDeuEmsRUsoyG83frY4"})
check("street address stays an address",
      carto._waypoint("123 Main St, Indianapolis IN") ==
      {"address": "123 Main St, Indianapolis IN"})
check("negative coordinates parse",
      carto._waypoint("-39.5,-86.1")["location"]["latLng"]["longitude"] == -86.1)


# ---------------------------------------------------------------------------
print("\n[13] /healthz liveness route")
# ---------------------------------------------------------------------------
# The deploy script curls this to decide whether a rollout succeeded, so it is
# load-bearing infrastructure, not a nicety. It must exist, must answer 200
# without auth, and must never carry key material or make a billed call.
_app = carto.mcp.http_app()
_paths = [r.path for r in _app.routes if hasattr(r, "path")]
check("/healthz is registered", "/healthz" in _paths, str(_paths))
check("/mcp is the transport path", "/mcp" in _paths, str(_paths))

try:
    from starlette.testclient import TestClient
    with TestClient(_app) as _c:
        _r = _c.get("/healthz")
        check("/healthz answers 200 without auth", _r.status_code == 200,
              f"got {_r.status_code}")
        _b = _r.json()
        check("reports the key is configured", _b.get("api_key_configured") is True)
        check("reports the armed auth mode", _b.get("auth_mode") == "anonymous",
              str(_b.get("auth_mode")))
        # The single most important property of this endpoint: it is public.
        check("never leaks key material",
              os.environ["GOOGLE_MAPS_API_KEY"] not in _r.text)
        # A public endpoint that made a billed Google call would be a way for
        # anyone who found the URL to burn the daily quota.
        check("makes no outbound call", "GEOCOD" not in _r.text.upper())
except ImportError:
    check("starlette TestClient available", False, "install starlette to test /healthz")


# ---------------------------------------------------------------------------
print(f"\n{'='*58}\n  {_PASS} passed, {_FAIL} failed\n{'='*58}")
sys.exit(1 if _FAIL else 0)
