"""
Regression suite for onenote_mcp_server.py.

Same discipline as the Throne suites (test_todo_v19.py / test_phi_audit.py): every
function and class under test is extracted out of the real source via `ast` and exec'd,
never retyped, so this file cannot drift from what ships. It also means the suite runs
with nothing installed -- no fastmcp, no httpx, no network, no Azure.

    python test_onenote_mcp.py      # exit 0 = green
"""
import ast
import asyncio
import datetime as dt
import hashlib
import html as _html
import json
import os
import re
import sys
import time
from html.parser import HTMLParser

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "onenote_mcp_server.py")
source = open(SRC, encoding="utf-8").read()
tree = ast.parse(source)

WANT_FUNCS = {"_now", "_slog", "_scope_root", "_links", "_html_to_text", "_page_html_body",
              "_unauth", "_graph_err", "_send_with_retry", "_g", "_delegated_token",
              "_whoami", "_health_payload", "_sections_tree",
              "onenote_list_notebooks", "onenote_list_sections", "onenote_list_pages",
              "onenote_get_page", "onenote_search", "onenote_create_page",
              "onenote_append_page", "onenote_healthcheck"}
WANT_CLASSES = {"_TextExtractor", "GraphTokenVerifier"}
WANT_ASSIGNS = {"_PAGE_TOP_MAX", "_TITLE_MAX", "_MAX_RETRIES", "_BACKOFF_CAP", "GRAPH"}

func_src, class_src, assign_src = {}, {}, {}
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in WANT_FUNCS:
        seg = ast.get_source_segment(source, node)
        # strip decorators (@mcp.tool) so the raw function is what we exercise
        func_src[node.name] = "\n".join(
            l for l in seg.splitlines() if not l.strip().startswith("@"))
    elif isinstance(node, (ast.AsyncFunctionDef,)) and node.name in WANT_FUNCS:
        seg = ast.get_source_segment(source, node)
        func_src[node.name] = "\n".join(
            l for l in seg.splitlines() if not l.strip().startswith("@"))
    elif isinstance(node, ast.ClassDef) and node.name in WANT_CLASSES:
        class_src[node.name] = ast.get_source_segment(source, node)
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id in WANT_ASSIGNS:
                assign_src[t.id] = ast.get_source_segment(source, node)

missing = ((WANT_FUNCS - set(func_src)) | (WANT_CLASSES - set(class_src))
           | (WANT_ASSIGNS - set(assign_src)))
if missing:
    print(f"FATAL: could not extract from source: {sorted(missing)}")
    sys.exit(1)
print(f"extracted {len(func_src)} functions + {len(class_src)} classes "
      f"+ {len(assign_src)} constants from the real source\n")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
STATE = {"graph": [], "writes": [], "sleeps": [], "graph_calls": 0}


class R:
    """Minimal httpx.Response stand-in."""
    def __init__(self, code=200, js=None, text=None, headers=None):
        self.status_code = code
        self._j = js if js is not None else {}
        self.text = text if text is not None else json.dumps(self._j)
        self.headers = headers or {}
        self.is_success = 200 <= code < 300
        self.content = (self.text or "").encode()

    def json(self):
        if self._j is None:
            raise ValueError("no json")
        return self._j


class FakeToken:
    def __init__(self, token="GRAPH-TOKEN", claims=None):
        self.token = token
        self.claims = claims or {"upn": "zach@adaptiveenterprisesllc.com"}


class AccessToken:
    """Stand-in for fastmcp's AccessToken (pydantic model) with the same field names."""
    def __init__(self, token, client_id, scopes, claims=None, **kw):
        self.token, self.client_id, self.scopes, self.claims = token, client_id, scopes, claims


class TokenVerifier:
    def __init__(self, **kw):
        pass


ENV = {}


def _exec_env():
    """Build the namespace the extracted code runs in."""
    ns = {
        "dt": dt, "re": re, "os": os, "sys": sys, "time": time, "json": json,
        "hashlib": hashlib, "_html": _html, "HTMLParser": HTMLParser,
        "AccessToken": AccessToken, "TokenVerifier": TokenVerifier,
        "logging": _FakeLogging(), "httpx": _FakeHttpx(),
        "CLIENT_ID": "cid", "OAUTH_SCOPES": ["https://graph.microsoft.com/Notes.ReadWrite.All"],
        "PUBLIC_BASE_URL": "https://onenote-mcp.test.eastus2.azurecontainerapps.io",
        "SITE_ALIASES": {"throne": "netorg39360.sharepoint.com,aaa,bbb"},
        "_WLOG": _FakeLogger(),
        "get_access_token": lambda: ENV.get("token_obj"),
    }
    for name in ("_PAGE_TOP_MAX", "_TITLE_MAX", "_MAX_RETRIES", "_BACKOFF_CAP", "GRAPH"):
        exec(assign_src[name], ns)
    for name in ("_TextExtractor",):
        exec(class_src[name], ns)
    for name in ("_now", "_slog", "_scope_root", "_links", "_html_to_text",
                 "_page_html_body", "_unauth", "_graph_err", "_send_with_retry",
                 "_delegated_token", "_whoami"):
        exec(func_src[name], ns)
    exec(class_src["GraphTokenVerifier"], ns)
    # _g is stubbed: the suite asserts on the (method, path) pairs tools emit.
    ns["_g"] = _fake_g
    for name in ("_g", "_sections_tree", "_health_payload", "onenote_list_notebooks",
                 "onenote_list_sections",
                 "onenote_list_pages", "onenote_get_page", "onenote_search",
                 "onenote_create_page", "onenote_append_page", "onenote_healthcheck"):
        if name == "_g":
            continue
        exec(func_src[name], ns)
    ns["_g"] = _fake_g
    return ns


class _FakeLogger:
    def info(self, m): STATE["writes"].append(m)
    def warning(self, *a, **k): pass


class _FakeLogging:
    def getLogger(self, *a): return _FakeLogger()
    def basicConfig(self, **k): pass


class _FakeHttpx:
    """Only what _send_with_retry touches. `Response` exists because the real
    signatures are annotated `-> httpx.Response`, and annotations are evaluated at
    def time -- exec'ing the real source means honoring that."""
    Response = R

    @staticmethod
    def request(method, url, **kw):
        return ENV["responder"](method, url, **kw)


def _fake_g(method, path, token, **kw):
    STATE["graph"].append((method, path))
    STATE["graph_calls"] += 1
    return ENV["g_responder"](method, path, token, **kw)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {detail}")


def reset(token="GRAPH-TOKEN", g=None, responder=None):
    STATE["graph"], STATE["writes"], STATE["sleeps"] = [], [], []
    STATE["graph_calls"] = 0
    ENV["token_obj"] = FakeToken(token) if token else None
    ENV["g_responder"] = g or (lambda *a, **k: R(200, {"value": []}))
    ENV["responder"] = responder or (lambda *a, **k: R(200, {}))


NS = _exec_env()
G = lambda n: NS[n]


# ===========================================================================
print("1. scope routing (OneDrive vs SharePoint site notebooks)")
# ===========================================================================
reset()
_scope_root = G("_scope_root")
check("scope 'me' -> /me/onenote", _scope_root("me")[0] == "/me/onenote")
check("empty scope defaults to me", _scope_root("")[0] == "/me/onenote")
check("None scope defaults to me", _scope_root(None)[0] == "/me/onenote")
check("alias resolves to site id",
      _scope_root("throne")[0] == "/sites/netorg39360.sharepoint.com,aaa,bbb/onenote",
      _scope_root("throne")[0])
check("alias is case-insensitive", _scope_root("THRONE")[0] == _scope_root("throne")[0])
check("raw site id passes through",
      _scope_root("contoso.sharepoint.com,1,2")[0] == "/sites/contoso.sharepoint.com,1,2/onenote")
check("site id returned as second element", _scope_root("throne")[1] == "netorg39360.sharepoint.com,aaa,bbb")
check("me returns no site id", _scope_root("me")[1] is None)

# ===========================================================================
print("\n2. HTML -> text (context control)")
# ===========================================================================
h2t = G("_html_to_text")
check("paragraphs become lines", h2t("<p>one</p><p>two</p>").splitlines() == ["one", "two"],
      repr(h2t("<p>one</p><p>two</p>")))
check("script/style dropped", "alert" not in h2t("<p>hi</p><script>alert(1)</script>"))
check("entities decoded", "A&B" in h2t("<p>A&amp;B</p>"))
check("table cells tab-separated", "\t" in h2t("<table><tr><td>a</td><td>b</td></tr></table>"))
check("image alt surfaced", "[image: chart]" in h2t('<p><img alt="chart"/></p>'))
check("image without alt still marked", "[image]" in h2t('<p><img src="x"/></p>'))
check("blank runs collapsed", "\n\n\n" not in h2t("<p>a</p><br/><br/><br/><p>b</p>"))
check("empty input safe", h2t("") == "")
check("None input safe", h2t(None) == "")
check("malformed html does not raise", isinstance(h2t("<p>unclosed <b>x"), str))
check("real onenote-ish page reads cleanly",
      "Note body" in h2t('<html><head><title>T</title></head><body>'
                         '<div data-id="d1"><p>Note body</p></div></body></html>'))

# ===========================================================================
print("\n3. page HTML wrapping")
# ===========================================================================
pb = G("_page_html_body")
check("fragment gets wrapped", "<title>Hi</title>" in pb("Hi", "<p>x</p>"))
check("fragment body preserved", "<p>x</p>" in pb("Hi", "<p>x</p>"))
check("full document passed through unchanged",
      pb("Hi", "<html><body>z</body></html>") == "<html><body>z</body></html>")
check("title is html-escaped", "&lt;b&gt;" in pb("<b>", "<p>x</p>"))

# ===========================================================================
print("\n4. error translation (the app-only trap)")
# ===========================================================================
ge = G("_graph_err")
e = ge(R(401, {"error": {"code": "40001", "message": "The request doesn't contain a valid OAuth token"}}))
check("40001 names the app-only retirement", "app-only" in e["error"].lower(), e["error"])
check("40001 mentions the retirement date", "2025-03-31" in e["error"])
e = ge(R(400, {"error": {"code": "20266", "message": "maximum number of sections"}}))
check("20266 tells you to page per-section", "ONE SECTION AT A TIME" in e["error"])
e = ge(R(507, {"error": {"code": "20155", "message": "too many pages"}}))
check("507 suggests a new section", "page limit" in e["error"])
e = ge(R(429, {"error": {"code": "20166", "message": "too many requests"}}))
check("20166 notes backoff already happened", "throttling" in e["error"])
e = ge(R(400, {"error": {"code": "10008", "message": "too many items"}}))
check("10008 explains the library ceiling", "ceiling" in e["error"])
check("non-json body does not raise", "graph 500" in ge(R(500, js=None, text="<html>boom"))["error"])

# ===========================================================================
print("\n5. throttling / retry")
# ===========================================================================
swr = G("_send_with_retry")
NS["time"] = type("T", (), {"sleep": staticmethod(lambda s: STATE["sleeps"].append(s)),
                            "monotonic": staticmethod(time.monotonic)})()
calls = {"n": 0}
def _throttle(method, url, **kw):
    calls["n"] += 1
    return R(200, {"ok": 1}) if calls["n"] > 2 else R(429, {}, headers={"Retry-After": "3"})
reset(responder=_throttle)
r = swr("GET", "https://x")
check("retries past 429 to success", r.status_code == 200)
check("honors numeric Retry-After", STATE["sleeps"][:2] == [3.0, 3.0], STATE["sleeps"])

calls["n"] = 0
def _always429(method, url, **kw):
    calls["n"] += 1
    return R(429, {}, headers={"Retry-After": "not-a-number"})
reset(responder=_always429)
r = swr("GET", "https://x")
check("gives up after _MAX_RETRIES", r.status_code == 429)
check("bounded attempt count", calls["n"] == NS["_MAX_RETRIES"] + 1, calls["n"])
check("non-numeric Retry-After falls back to backoff", STATE["sleeps"] == [1.0, 2.0, 4.0, 8.0, 16.0],
      STATE["sleeps"])

calls["n"] = 0
reset(responder=lambda m, u, **k: (calls.__setitem__("n", calls["n"] + 1), R(500, {}))[1])
r = swr("GET", "https://x")
check("500 is NOT retried (only 429/503/504)", calls["n"] == 1, calls["n"])

# ===========================================================================
print("\n6. unauthenticated calls refuse, and say why")
# ===========================================================================
reset(token=None)
for name in ("onenote_list_notebooks", "onenote_list_pages"):
    out = G(name)("x") if name == "onenote_list_pages" else G(name)()
    check(f"{name} refuses without a token", "error" in out[0] and "unauthorized" in out[0]["error"])
reset(token=None)
check("onenote_get_page refuses", "unauthorized" in G("onenote_get_page")("p")["error"])
reset(token=None)
check("onenote_create_page refuses", "unauthorized" in G("onenote_create_page")("s", "t", "<p/>", confirm=True)["error"])
reset(token=None)
check("onenote_append_page refuses", "unauthorized" in G("onenote_append_page")("p", "<p/>", confirm=True)["error"])
reset(token=None)
check("refusal names the sign-in requirement",
      "sign-in" in G("onenote_get_page")("p")["error"])
reset(token=None)
check("healthcheck reports absent token", G("onenote_healthcheck")()["token"].startswith("absent"))

# ===========================================================================
print("\n7. list_pages: per-section, capped, correctly ordered")
# ===========================================================================
reset(g=lambda m, p, t, **k: R(200, {"value": [
    {"id": "p1", "title": "A", "createdDateTime": "2026-01-01T00:00:00Z"}]}))
out = G("onenote_list_pages")("sec1", top=50)
path = STATE["graph"][0][1]
check("uses the per-section endpoint", "/sections/sec1/pages" in path, path)
check("never uses the all-pages endpoint", "/onenote/pages?" not in path)
check("overrides default ordering", "$orderby=createdDateTime" in path, path)
check("selects a minimal property set", "$select=id,title,createdDateTime" in path)
check("returns the page", out[0]["id"] == "p1")

reset(g=lambda m, p, t, **k: R(200, {"value": []}))
G("onenote_list_pages")("sec1", top=5000)
check("top capped at Graph's ceiling of 100", "$top=100" in STATE["graph"][0][1], STATE["graph"][0][1])
reset(g=lambda m, p, t, **k: R(200, {"value": []}))
G("onenote_list_pages")("sec1", top=0)
check("top floored at 1", "$top=1" in STATE["graph"][0][1])

reset(g=lambda m, p, t, **k: R(200, {"value": [{"id": "p1", "title": "A"}],
                                     "@odata.nextLink": "https://next"}))
out = G("onenote_list_pages")("sec1", top=1)
check("says so when more pages exist", any("more pages exist" in str(x.get("note", "")) for x in out),
      out)

# ===========================================================================
print("\n8. list_sections: section groups are not silently dropped")
# ===========================================================================
NB = {"displayName": "Cases",
      "sections": [{"id": "s-top", "displayName": "Top"}],
      "sectionGroups": [{"id": "g1", "displayName": "2026",
                         "sections": [{"id": "s-in", "displayName": "Q1"}],
                         "sectionGroups": []}]}
reset(g=lambda m, p, t, **k: R(200, NB))
out = G("onenote_list_sections")("nb1")   # thin wrapper -> _sections_tree
ids = {s["id"] for s in out["sections"]}
check("top-level section present", "s-top" in ids)
check("section INSIDE a group present", "s-in" in ids, ids)
check("nested section carries its group path",
      [s for s in out["sections"] if s["id"] == "s-in"][0]["path"] == "2026")
check("top-level section has empty path",
      [s for s in out["sections"] if s["id"] == "s-top"][0]["path"] == "")
check("group listed", any(g.get("id") == "g1" for g in out["section_groups"]))
check("section_count counts all levels", out["section_count"] == 2)
check("single $expand round-trip", STATE["graph_calls"] == 1, STATE["graph_calls"])
check("expand requests both sections and groups",
      "$expand=sections" in STATE["graph"][0][1] and "sectionGroups" in STATE["graph"][0][1])

# ===========================================================================
print("\n9. get_page: text by default, includeIDs opt-in")
# ===========================================================================
PAGE_HTML = '<html><head><title>T</title></head><body><div data-id="d"><p>Body text</p></div></body></html>'
def _page_g(m, p, t, **k):
    if p.endswith("/content") or "/content?" in p:
        return R(200, None, text=PAGE_HTML)
    return R(200, {"id": "p1", "title": "T", "links": {"oneNoteWebUrl": {"href": "https://web"}}})
reset(g=_page_g)
out = G("onenote_get_page")("p1")
check("defaults to text", out["format"] == "text")
check("text is stripped of markup", "<p>" not in out["content"] and "Body text" in out["content"])
check("reports both char counts", out["chars"] < out["html_chars"])
check("returns webUrl", out["webUrl"] == "https://web")
check("no includeIDs by default", not any("includeIDs" in p for _, p in STATE["graph"]))

reset(g=_page_g)
out = G("onenote_get_page")("p1", format="html")
check("html format returns raw markup", "<p>Body text</p>" in out["content"])
reset(g=_page_g)
G("onenote_get_page")("p1", include_ids=True)
check("include_ids adds includeIDs=true", any("includeIDs=true" in p for _, p in STATE["graph"]))
reset(g=_page_g)
check("invalid format refused", "error" in G("onenote_get_page")("p1", format="pdf"))

# ===========================================================================
print("\n10. create_page: confirm gate + read-after-write")
# ===========================================================================
reset()
out = G("onenote_create_page")("sec1", "Title", "<p>x</p>")
check("refuses without confirm", "refused" in out["error"])
check("refusal states nothing was written", "Nothing was written" in out["error"])
check("no Graph call made when refused", STATE["graph_calls"] == 0)

def _create_g(m, p, t, **k):
    if m == "POST":
        return R(201, {"id": "new-page", "title": "Title",
                       "links": {"oneNoteWebUrl": {"href": "https://web/new"}}})
    return R(200, None, text="<html><body><p>x</p></body></html>")
reset(g=_create_g)
out = G("onenote_create_page")("sec1", "Title", "<p>x</p>", confirm=True)
check("creates with confirm", out.get("ok") is True)
check("returns authoritative id", out["id"] == "new-page")
check("returns webUrl", out["webUrl"] == "https://web/new")
check("verified by read-back", out["verified"] is True)
check("read-back actually happened", any(m == "GET" and "/content" in p for m, p in STATE["graph"]))
check("write logged", len(STATE["writes"]) == 1)
check("log carries no title (PHI discipline)", "Title" not in STATE["writes"][0], STATE["writes"])

def _create_noread(m, p, t, **k):
    return R(201, {"id": "new-page"}) if m == "POST" else R(404, {"error": {"code": "x", "message": "gone"}})
reset(g=_create_noread)
out = G("onenote_create_page")("sec1", "T", "<p>x</p>", confirm=True)
check("unverifiable create is flagged, not claimed clean", out["verified"] is False and "warning" in out)

reset(g=_create_g)
check("empty title refused", "error" in G("onenote_create_page")("s", "  ", "<p/>", confirm=True))
reset(g=_create_g)
check("over-long title refused",
      "128" in G("onenote_create_page")("s", "x" * 129, "<p/>", confirm=True)["error"])
reset(g=lambda m, p, t, **k: R(201, {"title": "T"}))
check("create with no id returned is not claimed as success",
      "error" in G("onenote_create_page")("s", "T", "<p/>", confirm=True))

# ===========================================================================
print("\n11. append_page: the truncation guarantee")
# ===========================================================================
BEFORE = "<html><body><p>original line one</p><p>original line two</p></body></html>"
GREW = BEFORE.replace("</body>", "<p>appended line</p></body>")
CLOBBERED = "<html><body><p>appended line</p></body></html>"

reset()
out = G("onenote_append_page")("p1", "<p>x</p>")
check("refuses without confirm", "refused" in out["error"])
check("no Graph call when refused", STATE["graph_calls"] == 0)

state = {"phase": "before"}
def _append_ok(m, p, t, **k):
    if m == "PATCH":
        state["phase"] = "after"
        return R(204, None, text="")
    return R(200, None, text=GREW if state["phase"] == "after" else BEFORE)
reset(g=_append_ok)
out = G("onenote_append_page")("p1", "<p>appended line</p>", confirm=True)
check("append succeeds", out["ok"] is True)
check("prior content confirmed intact", out["content_intact"] is True)
check("page grew", out["grew"] is True)
check("reports both char counts", out["chars_after"] > out["chars_before"])
check("PATCH used, not PUT", any(m == "PATCH" for m, _ in STATE["graph"]))
check("read before and after", sum(1 for m, _ in STATE["graph"] if m == "GET") == 2)

state["phase"] = "before"
def _append_clobber(m, p, t, **k):
    if m == "PATCH":
        state["phase"] = "after"
        return R(204, None, text="")
    return R(200, None, text=CLOBBERED if state["phase"] == "after" else BEFORE)
reset(g=_append_clobber)
out = G("onenote_append_page")("p1", "<p>appended line</p>", confirm=True)
check("TRUNCATION detected", out["ok"] is False, out)
check("truncation reported explicitly", "TRUNCATED" in out["error"])
check("content_intact false", out["content_intact"] is False)
check("names the lost lines", out["lost_sample"] and "original line one" in out["lost_sample"][0])

state["phase"] = "before"
reset(g=_append_clobber)
out = G("onenote_append_page")("p1", "<p>new</p>", action="replace", confirm=True)
check("replace is allowed to remove content", out["ok"] is True)
check("replace still reports what changed", out["content_intact"] is False)

reset(g=lambda m, p, t, **k: R(404, {"error": {"code": "20156", "message": "no such page"}}))
out = G("onenote_append_page")("p1", "<p>x</p>", confirm=True)
check("aborts when the page cannot be read first", "aborted" in out["error"])
check("abort states it refused to PATCH blind", "PATCH blind" in out["error"])
check("no PATCH attempted after failed pre-read", not any(m == "PATCH" for m, _ in STATE["graph"]))

reset(g=_append_ok)
check("empty html refused", "error" in G("onenote_append_page")("p1", "   ", confirm=True))
reset(g=_append_ok)
check("bad action refused", "error" in G("onenote_append_page")("p1", "<p/>", action="delete", confirm=True))
state["phase"] = "before"
reset(g=_append_ok)
G("onenote_append_page")("p1", "<p>x</p>", target="d1", confirm=True)
check("targets a data-id when asked",
      any(m == "PATCH" for m, _ in STATE["graph"]))

# ===========================================================================
print("\n12. search: honest about being title-only")
# ===========================================================================
def _search_g(m, p, t, **k):
    if "/notebooks" in p and "$expand" not in p:
        return R(200, {"value": [{"id": "nb1", "displayName": "Cases"}]})
    if "$expand" in p:
        return R(200, {"displayName": "Cases",
                       "sections": [{"id": "s1", "displayName": "S"}], "sectionGroups": []})
    return R(200, {"value": [{"id": "p1", "title": "Intake Notes"},
                             {"id": "p2", "title": "Billing"}]})
reset(g=_search_g)
out = G("onenote_search")("intake")
check("declares title-only in the payload", "not full text" in out["search_kind"], out["search_kind"])
check("matches case-insensitively", out["match_count"] == 1 and out["matches"][0]["id"] == "p1")
check("reports sections scanned", out["sections_scanned"] == 1)
check("carries the notebook name", out["matches"][0]["notebook"] == "Cases")
reset(g=_search_g)
check("empty query refused", "error" in G("onenote_search")("  "))
reset(g=_search_g)
out = G("onenote_search")("intake", section_id="s1")
check("section_id scopes to one round-trip", out["sections_scanned"] == 1 and STATE["graph_calls"] == 1)
check("truncated flag present", out["truncated"] is False)

# ===========================================================================
print("\n13. healthcheck")
# ===========================================================================
reset(g=lambda m, p, t, **k: R(200, {"value": [{"id": "n1"}, {"id": "n2"}]}))
out = G("onenote_healthcheck")()
check("reports delegated mode", "delegated" in out["auth_mode"])
check("states app-only is retired", "app-only is retired" in out["auth_mode"])
check("publishes the Entra redirect URI", out["entra_redirect_uri"].endswith("/auth/callback"),
      out["entra_redirect_uri"])
check("counts notebooks per scope", out["notebooks"]["me"] == 2, out["notebooks"])
check("checks configured site aliases too", "throne" in out["notebooks"])
check("reports the signed-in user", out["user"] == "zach@adaptiveenterprisesllc.com")

# ===========================================================================
print("\n14. GraphTokenVerifier: validation, handoff, caching, fail-closed")
# ===========================================================================
class _FakeAsyncClient:
    calls = 0
    def __init__(self, **kw): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, url, headers=None):
        _FakeAsyncClient.calls += 1
        return ENV["verify_response"](url, headers)

NS["httpx"] = type("H", (), {"AsyncClient": _FakeAsyncClient})()
V = NS["GraphTokenVerifier"]()
ENV["verify_response"] = lambda u, h: R(200, {"id": "oid-1", "userPrincipalName": "zach@ae.com",
                                              "displayName": "Zach"})
_FakeAsyncClient.calls = 0
at = asyncio.run(V.verify_token("THE-GRAPH-TOKEN"))
check("returns an AccessToken on 200", at is not None)
check("HANDOFF: .token IS the delegated Graph token", at.token == "THE-GRAPH-TOKEN")
check("claims carry the signed-in upn", at.claims["upn"] == "zach@ae.com")
check("validated against Graph /me", _FakeAsyncClient.calls == 1)

asyncio.run(V.verify_token("THE-GRAPH-TOKEN"))
check("second call served from cache (no extra Graph hit)", _FakeAsyncClient.calls == 1)
check("cache key is a hash, never the token itself",
      all("THE-GRAPH-TOKEN" not in k for k in V._cache))

ENV["verify_response"] = lambda u, h: R(401, {"error": {"code": "InvalidAuthenticationToken"}})
check("401 from Graph fails closed", asyncio.run(V.verify_token("BAD-TOKEN")) is None)

def _boom(u, h):
    raise RuntimeError("dns down")
ENV["verify_response"] = _boom
check("transport error fails closed", asyncio.run(V.verify_token("OTHER-TOKEN")) is None)

# ===========================================================================
print("\n15. delegated token is per-request, never module-cached")
# ===========================================================================
src_dt = func_src["_delegated_token"]
check("_delegated_token reads from get_access_token()", "get_access_token()" in src_dt)
check("_delegated_token holds no module-level cache",
      "global " not in src_dt and "_CACHE" not in src_dt)
# These assert on CODE, not prose. The module docstring necessarily discusses
# client-credentials, CertificateCredential and /.default in order to explain why this
# server does not use them, so grepping the raw file would fail on its own explanation.
# Docstrings and comments are stripped first, leaving executable code.
def _code_only(text: str) -> str:
    t = ast.parse(text)
    for node in ast.walk(t):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(t)


CODE = _code_only(source)
check("no client-credentials token path in code",
      "client_credentials" not in CODE and "/.default" not in CODE)
check("no CertificateCredential / azure.identity import in code",
      "CertificateCredential" not in CODE and "azure.identity" not in CODE)
check("delegated Notes scope is what gets requested",
      "Notes.ReadWrite.All" in CODE and "/.default" not in CODE)
check("offline_access requested (so a resumed session can refresh)",
      "offline_access" in CODE)
# /pages/{id} is legitimate (one page). The banned shape is the all-pages COLLECTION,
# i.e. {root}/pages immediately followed by a query or the end of the string.
check("all-pages collection endpoint never constructed",
      not re.search(r"\{root\}/pages(\?|['\"]|$)", CODE),
      re.findall(r".{0,40}\{root\}/pages.{0,10}", CODE))

# ===========================================================================
print("\n16. no tool calls another tool in-process")
# ===========================================================================
# @mcp.tool rebinds the decorated name to a FunctionTool, which is NOT callable. A tool
# that calls another tool by name therefore raises TypeError at runtime -- and because the
# ast suite exercises the undecorated functions, it would pass while production crashed.
# onenote_search hit exactly this before _sections_tree was split out. This guard reads the
# decorators off the real tree, so it stays true as tools are added.
TOOL_NAMES, TOOL_NODES = set(), []
for _n in ast.walk(tree):
    if isinstance(_n, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for _d in _n.decorator_list:
            if "mcp.tool" in ast.unparse(_d):
                TOOL_NAMES.add(_n.name)
                TOOL_NODES.append(_n)
check("tool decorators found", len(TOOL_NAMES) >= 8, sorted(TOOL_NAMES))
offenders = []
for _n in TOOL_NODES:
    for _c in ast.walk(_n):
        if (isinstance(_c, ast.Call) and isinstance(_c.func, ast.Name)
                and _c.func.id in TOOL_NAMES):
            offenders.append(f"{_n.name} -> {_c.func.id}()")
check("no tool invokes another tool by name", not offenders, offenders)
check("the shared section walk is a plain function, not a tool",
      "_sections_tree" not in TOOL_NAMES)
check("search uses the plain helper",
      "_sections_tree(" in func_src["onenote_search"])

# ===========================================================================
print(f"\n{PASS}/{PASS + FAIL} checks passed")
sys.exit(0 if FAIL == 0 else 1)
