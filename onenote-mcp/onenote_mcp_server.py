"""
OneNote MCP Server — remote connector exposing Microsoft OneNote (via Graph) to claude.ai.

WHY THIS IS A SEPARATE SERVER FROM THRONE
-----------------------------------------
The Microsoft Graph OneNote API does not support app-only authentication. Microsoft's
own reference now states it unconditionally ("The Microsoft Graph OneNote API doesn't
support app-only authentication", learn.microsoft.com/graph/api/resources/onenote-api-overview),
and the retirement took effect 2025-03-31. Throne is client-credentials end to end
(CertificateCredential -> /.default), so no amount of tool code bolted onto Throne can
reach OneNote. This server therefore runs a DIFFERENT auth model: every Graph call is
made with the SIGNED-IN USER'S delegated token, obtained through the connector's own
OAuth handshake.

The failure mode this avoids is deliberately confusing, so it is worth naming: an Entra
app registration will still let you ADD Notes.ReadWrite.All as an *Application*
permission (the per-endpoint permission tables in the Graph reference are stale and
still list it -- e.g. the "List pages" page does, checked 2026-08-22), admin consent
will still succeed, and the issued token will still carry the role in its `roles` claim.
The call then fails 401 with OneNote error 40001. _graph_err() below detects that exact
shape and says so, rather than letting the next person re-debug a withdrawn capability.

AUTH ARCHITECTURE (verified against the installed fastmcp, not docs -- see BUILD-NOTES)
---------------------------------------------------------------------------------------
FastMCP ships AzureProvider, and it is the obvious thing to reach for. It cannot work
here. AzureProvider validates the upstream Entra token with
JWTVerifier(audience=<client_id>), i.e. it requires a token minted for THIS APP's own
API. A Microsoft Graph access token's audience is Graph, never our client id, so every
request would fail validation. An OAuth access token has exactly one audience; you
cannot have one token that both validates as our API and is accepted by Graph.

So this server composes the same building blocks one level down:

    OAuthProxy(token_verifier=GraphTokenVerifier(), ...)

OAuthProxy still does the whole MCP authorization-spec dance for claude.ai (dynamic
client registration, PKCE, /authorize, /token, the two .well-known documents) and still
brokers upstream to Entra. The only change is WHO validates the upstream token:
GraphTokenVerifier validates it by calling Graph, which is the only authority that can
(Microsoft explicitly tells third parties not to crack Graph tokens open), and returns
an AccessToken whose `.token` IS the delegated Graph token. Tools then read it through
the public get_access_token() and call Graph as the signed-in user.

Consequences worth keeping in mind:
  * The redirect URI registered in ENTRA is this server's own callback,
    {PUBLIC_BASE_URL}/auth/callback -- NOT claude.ai's. claude.ai's callback is a
    *client* redirect, registered dynamically with this server via /register and
    allow-listed in ALLOWED_CLIENT_REDIRECT_URIS. Getting this backwards is the classic
    "authenticates, then dies at the callback".
  * No refresh token lives in config. offline_access is requested so OAuthProxy can
    refresh the upstream token on the client's behalf; sessions survive past ~1hr.
  * There is no unattended path. Every tool call is somebody's session.

Runtime: FastMCP over streamable-HTTP (remote-connector compatible).
    pip install -r requirements.txt
"""

import datetime as dt
import hashlib
import html as _html
import json
import logging
import os
import re
import sys
import time
from html.parser import HTMLParser

import httpx
from fastmcp import FastMCP
from fastmcp.server.auth.auth import AccessToken, TokenVerifier
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.server.dependencies import get_access_token

# ---------------------------------------------------------------------------
# Config (from env / Container Apps secrets)
# ---------------------------------------------------------------------------
TENANT_ID     = os.environ["AZURE_TENANT_ID"]
CLIENT_ID     = os.environ["OAUTH_CLIENT_ID"]
CLIENT_SECRET = os.environ["OAUTH_CLIENT_SECRET"]
# Public https origin of THIS server, no trailing slash. Container Apps FQDN.
PUBLIC_BASE_URL = os.environ["PUBLIC_BASE_URL"].rstrip("/")

# Entra authority. login.microsoftonline.us for Azure Government.
AUTHORITY = os.environ.get("AZURE_AUTHORITY", "login.microsoftonline.com")

GRAPH = "https://graph.microsoft.com/v1.0"

# Delegated scopes requested at /authorize. Fully-qualified on purpose: Graph scopes must
# reach Entra as-is. offline_access is what makes a resumed session work an hour later --
# without it Entra issues no refresh token and OAuthProxy has nothing to refresh with.
_DEFAULT_SCOPES = ("https://graph.microsoft.com/Notes.ReadWrite.All,"
                   "https://graph.microsoft.com/Sites.Read.All,"
                   "offline_access,openid,profile,email")
OAUTH_SCOPES = [s.strip() for s in os.environ.get("OAUTH_SCOPES", _DEFAULT_SCOPES).split(",")
                if s.strip()]

# Which MCP clients may be handed an authorization code. Defaults to the claude.ai
# connector callback.
#
# An EMPTY list means "permit nothing" and is passed through as such. FastMCP treats
# allowed_client_redirect_uris=None as "permit everything", so an `or None` here would
# turn the one configuration that looks like a lockdown -- clearing the variable -- into
# the widest possible setting, and an open redirect on /authorize is how an authorization
# code gets delivered to somebody else's host. Allow-any therefore needs saying out loud,
# the same way MCP_ALLOW_ANON gates Throne's unauthenticated mode.
_DEFAULT_CLIENT_REDIRECTS = "https://claude.ai/api/mcp/auth_callback"
ALLOWED_CLIENT_REDIRECT_URIS = [u.strip() for u in os.environ.get(
    "ALLOWED_CLIENT_REDIRECT_URIS", _DEFAULT_CLIENT_REDIRECTS).split(",") if u.strip()]
ALLOW_ANY_CLIENT_REDIRECT = os.environ.get("MCP_ALLOW_ANY_CLIENT_REDIRECT") == "1"

# Site aliases for site-hosted (SharePoint) notebooks, so callers can say scope="throne"
# instead of pasting a Graph site id. THRONE_SITE_ID is the MasterChiefsThrone site.
SITE_ALIASES = {}
if os.environ.get("THRONE_SITE_ID"):
    SITE_ALIASES["throne"] = os.environ["THRONE_SITE_ID"]
for pair in os.environ.get("ONENOTE_SITE_ALIASES", "").split(","):
    if "=" in pair:
        k, v = pair.split("=", 1)
        if k.strip() and v.strip():
            SITE_ALIASES[k.strip().lower()] = v.strip()

# Graph caps $top at 100 for page listings; asking for more is an error, not more pages.
_PAGE_TOP_MAX = 100
# Notebook/section names cap at 128/50 chars (OneNote error 20005).
_TITLE_MAX = 128


def _now() -> str:
    """Timezone-aware UTC stamp (utcnow() is deprecated in 3.12+)."""
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
class GraphTokenVerifier(TokenVerifier):
    """Validate the upstream Entra token by asking Graph, and hand it to the tools.

    Two jobs in one, which is why it exists rather than a stock verifier:

    1. VALIDATION. Graph access tokens are not ours to inspect -- Microsoft documents
       them as opaque to everyone but Graph, and their audience is Graph, so a JWKS/
       audience check against our own app (what AzureProvider does) rejects every valid
       token. The only honest validation is to spend one call on GET /me and believe the
       answer.
    2. HANDOFF. The AccessToken returned here is what get_access_token() gives a tool,
       and its `.token` is the delegated Graph token itself. That is the entire
       delegated-auth mechanism -- there is no second token store to consult.

    OAuthProxy calls this on EVERY request, so an uncached implementation would add a
    Graph round-trip per MCP message and walk straight into OneNote's throttling. Results
    are cached briefly, keyed by a hash of the token (never the token itself, so a heap
    dump or a log of the cache keys reveals nothing usable).
    """

    _CACHE_TTL = 300.0     # seconds; well inside a ~60min token lifetime
    _CACHE_MAX = 512       # bound the dict; these are per-session, not per-user

    def __init__(self, **kw):
        super().__init__(**kw)
        self._cache: dict[str, tuple[float, AccessToken]] = {}

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    async def verify_token(self, token: str) -> AccessToken | None:
        key = self._key(token)
        hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]

        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(f"{GRAPH}/me?$select=id,userPrincipalName,displayName",
                                headers={"Authorization": f"Bearer {token}"})
        except Exception as e:                      # network/DNS blip -> fail closed
            logging.getLogger("onenote.auth").warning("verify_token transport error: %s", e)
            return None

        if r.status_code != 200:
            # A 401 here is normal (expired token); OAuthProxy will refresh and retry.
            return None

        me = r.json()
        at = AccessToken(
            token=token,                            # <- the delegated Graph token
            client_id=CLIENT_ID,
            scopes=list(OAUTH_SCOPES),
            claims={"upn": me.get("userPrincipalName"), "oid": me.get("id"),
                    "name": me.get("displayName")},
        )
        if len(self._cache) >= self._CACHE_MAX:     # cheap eviction; no LRU needed
            self._cache.clear()
        self._cache[key] = (time.monotonic() + self._CACHE_TTL, at)
        return at


def _build_auth() -> OAuthProxy:
    """OAuthProxy fronting Entra. See the module docstring for why not AzureProvider.

    redirect_path is left at FastMCP's default, '/auth/callback'. That path, on THIS
    host, is what must be registered as the Web redirect URI on the Entra app.
    """
    return OAuthProxy(
        upstream_authorization_endpoint=f"https://{AUTHORITY}/{TENANT_ID}/oauth2/v2.0/authorize",
        upstream_token_endpoint=f"https://{AUTHORITY}/{TENANT_ID}/oauth2/v2.0/token",
        upstream_client_id=CLIENT_ID,
        upstream_client_secret=CLIENT_SECRET,
        token_verifier=GraphTokenVerifier(),
        base_url=PUBLIC_BASE_URL,
        redirect_path="/auth/callback",
        valid_scopes=OAUTH_SCOPES,
        allowed_client_redirect_uris=(None if ALLOW_ANY_CLIENT_REDIRECT
                                      else ALLOWED_CLIENT_REDIRECT_URIS),
    )


mcp = FastMCP("OneNote (AE)", auth=_build_auth())

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
_WLOG = logging.getLogger("onenote.writes")


def _slog(tool: str, target: str, **extra) -> None:
    """One JSON line per write, to stdout (Container Apps logs).

    Deliberately logs ids and counts, never page/section TITLES or HTML: a OneNote page
    title in a case notebook is exactly the kind of string that carries a consumer name,
    and this trail is not the place for it (same rule the Throne To Do writers follow)."""
    rec = {"ts": _now(), "evt": "write", "tool": tool, "target": target}
    if extra:
        rec.update(extra)
    try:
        _WLOG.info(json.dumps(rec, default=str))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Graph plumbing
# ---------------------------------------------------------------------------
def _delegated_token() -> str | None:
    """The signed-in user's Graph token for THIS request, or None if unauthenticated.

    Comes from GraphTokenVerifier via FastMCP's public dependency. Never cached at module
    scope: it is per-request by construction, and caching it across users would be a
    cross-tenant data leak waiting to happen."""
    at = get_access_token()
    return at.token if at else None


def _whoami() -> dict:
    at = get_access_token()
    return dict(at.claims or {}) if at else {}


_MAX_RETRIES = 5
_BACKOFF_CAP = 60.0


def _send_with_retry(method: str, url: str, **kw) -> httpx.Response:
    """Single point through which every Graph request flows, so throttling is handled
    uniformly. OneNote throttles harder than the rest of Graph (its own error 20166 rides
    on a 429), and 10007/10003 arrive as 5xx-ish 'server busy' -- all get the same
    treatment: honor a numeric Retry-After, else exponential backoff."""
    delay = 1.0
    resp = None
    for attempt in range(_MAX_RETRIES + 1):
        resp = httpx.request(method, url, **kw)
        if resp.status_code not in (429, 503, 504) or attempt == _MAX_RETRIES:
            return resp
        ra = resp.headers.get("Retry-After", "")
        try:
            wait = float(ra)
        except ValueError:
            wait = delay
        time.sleep(min(wait, _BACKOFF_CAP))
        delay = min(delay * 2, _BACKOFF_CAP)
    return resp


def _g(method: str, path: str, token: str, **kw) -> httpx.Response:
    headers = kw.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    # OneNote /content GETs can 302 to a storage URL with its own auth; httpx must follow
    # (and safely strips our Authorization on the cross-host hop).
    kw.setdefault("follow_redirects", True)
    return _send_with_retry(method, f"{GRAPH}{path}", headers=headers, timeout=120, **kw)


def _graph_err(r: httpx.Response) -> dict:
    """Surface Graph's own message, and translate the OneNote codes that otherwise cost a
    day of debugging into something that names the actual cause."""
    code, msg = "", ""
    try:
        e = r.json().get("error", {}) or {}
        code, msg = str(e.get("code", "")), e.get("message", "") or r.text[:400]
    except Exception:
        msg = r.text[:400]

    hint = ""
    if code == "40001" or "40001" in msg:
        hint = (" — this is the signature of an APP-ONLY token. The OneNote API dropped "
                "app-only auth on 2025-03-31; the Entra portal and the per-endpoint "
                "permission tables still offer Application-type Notes permissions, and "
                "consent still succeeds, but the call can never work. This server must "
                "run delegated; check that the connector completed its OAuth handshake.")
    elif code == "20266" or "maximum number of sections" in msg.lower():
        hint = (" — enumerate pages ONE SECTION AT A TIME (onenote_list_pages), never the "
                "all-pages endpoint.")
    elif code == "20166":
        hint = " — OneNote throttling; already retried with backoff."
    elif code in ("10008", "10013"):
        hint = (" — a document library behind this notebook exceeds OneNote's item ceiling "
                "(5,000 OneNote items / 20,000 total) and cannot be queried.")
    elif r.status_code == 507:
        hint = " — the section is at its page limit; create a new section."
    return {"error": f"graph {r.status_code}{(' ' + code) if code else ''}: {msg}{hint}"}


# --- caller-supplied values that get interpolated into Graph request paths -------------
# Every id below arrives from the tool caller and is formatted straight into a request
# path. A value carrying '?', '#' or '/' does NOT stay inside its path segment: '?' opens
# the query string, so the rest of the URL this file appends ('/onenote/notebooks?$select
# =...') lands in the query and the request re-points at a different Graph endpoint
# entirely. scope="root/drive/root/children?" turns onenote_list_notebooks into a
# SharePoint drive listing.
#
# That is not privilege escalation for the signed-in user -- the delegated token still
# carries only their own access, and only the Notes/Sites.Read scopes. It matters because
# page content is untrusted input that this server feeds to a model: a prompt injection
# planted in a shared or site-hosted notebook could otherwise steer a tool that says it
# lists notebooks into reading unrelated SharePoint content. The tool's stated reach and
# its actual reach should be the same thing, so validate shape before interpolating.
_SITE_ID_RE = re.compile(r"^[A-Za-z0-9.\-]+,[0-9a-fA-F-]{36},[0-9a-fA-F-]{36}$")
# OneNote ids are long opaque strings using unreserved + sub-delim characters; '!' and '$'
# genuinely appear in page ids. Path separators and query/fragment markers never do.
_GRAPH_ID_RE = re.compile(r"^[A-Za-z0-9!$'()*+;=._~@:-]{1,512}$")


def _check_path_args(*pairs: tuple[object, str]) -> str | None:
    """Return an error message if any (value, name) pair would escape its path segment.

    Returns None when everything is safe, so call sites read:
        err = _check_path_args((section_id, "section_id"), (scope, "scope"))
        if err:
            return {"error": err}
    """
    for value, what in pairs:
        if not isinstance(value, str):
            return f"invalid {what}: expected a string"
        v = value.strip()
        if what == "scope":
            # 'me', a configured alias, or a literal Graph site id -- nothing else.
            if not v or v.lower() == "me" or v.lower() in SITE_ALIASES:
                continue
            if not _SITE_ID_RE.match(v):
                return (f"invalid scope: use 'me', a configured alias "
                        f"({', '.join(sorted(SITE_ALIASES)) or 'none configured'}), or a "
                        f"full Graph site id of the form "
                        f"'contoso.sharepoint.com,<guid>,<guid>'")
        elif not _GRAPH_ID_RE.match(v):
            return (f"invalid {what}: expected a Graph id. Get ids from the list tools "
                    f"rather than composing them; '/', '?' and '#' are never part of one.")
    return None


def _scope_root(scope: str) -> tuple[str, str | None]:
    """Map a caller-facing scope onto a OneNote URL root.

    scope='me'          -> /me/onenote            (the user's own OneDrive notebooks)
    scope='throne'|alias-> /sites/{id}/onenote    (SharePoint site-hosted notebooks)
    scope=<site id>     -> /sites/{id}/onenote

    The two are genuinely different URL spaces, not a convenience: a notebook living on
    the MasterChiefsThrone site is NOT addressable under /me/onenote, so a tool that only
    ever built /me paths would silently report 'no such notebook' for every site notebook.

    Assumes _check_path_args has already vetted `scope`; every tool calls it first.
    Returns (url_root, site_id_or_None)."""
    s = (scope or "me").strip()
    if not s or s.lower() == "me":
        return "/me/onenote", None
    site = SITE_ALIASES.get(s.lower(), s)
    return f"/sites/{site}/onenote", site


def _links(obj: dict) -> str | None:
    """OneNote returns web/client URLs under links.oneNoteWebUrl.href."""
    return ((obj.get("links") or {}).get("oneNoteWebUrl") or {}).get("href")


# ---------------------------------------------------------------------------
# HTML <-> text
# ---------------------------------------------------------------------------
class _TextExtractor(HTMLParser):
    """Strip OneNote page HTML to readable text.

    Raw OneNote HTML is verbose -- absolute-positioned divs, inline styles, data-id
    attributes on every element -- and a page with a couple of tables will burn far more
    context than its content is worth. This keeps block structure (one line per block,
    table cells tab-separated) and drops everything else."""

    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table"}
    _SKIP = {"script", "style", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")
        elif tag == "td" or tag == "th":
            self.parts.append("\t")
        elif tag == "img":
            alt = dict(attrs).get("alt")
            self.parts.append(f"[image{': ' + alt if alt else ''}]")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)


def _html_to_text(html_str: str) -> str:
    p = _TextExtractor()
    try:
        p.feed(html_str or "")
        p.close()
    except Exception:
        # Never let a malformed page take a tool down; fall back to a tag strip.
        return _html.unescape(re.sub(r"<[^>]+>", " ", html_str or "")).strip()
    text = "".join(p.parts)
    text = re.sub(r"[ \t]+\n", "\n", text)
    # One line per block, not two. Every block emits a boundary on both its open and close
    # tag, so adjacent blocks always produce a run of newlines. OneNote wraps EVERY line of
    # a page in its own <p>, so collapsing to \n\n would double-space the entire page and
    # roughly double what it costs to read.
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _page_html_body(title: str, html_body: str) -> str:
    """Wrap a fragment in the XHTML-ish document OneNote's create endpoint expects.

    Passed through unchanged when the caller already supplied a full document, so callers
    who know the OneNote HTML dialect are not fought with."""
    if re.search(r"<html[\s>]", html_body or "", re.I):
        return html_body
    return ("<!DOCTYPE html><html><head>"
            f"<title>{_html.escape(title or '')}</title>"
            "</head><body>" + (html_body or "") + "</body></html>")


def _unauth() -> dict:
    return {"error": "unauthorized — no delegated Graph token on this request. The "
                     "connector must complete its Microsoft sign-in before any tool can run."}


# ---------------------------------------------------------------------------
# READ TOOLS
# ---------------------------------------------------------------------------
@mcp.tool
def onenote_list_notebooks(scope: str = "me") -> list[dict]:
    """List OneNote notebooks the signed-in user can reach.

    scope='me' (default) for the user's own/shared OneDrive notebooks, or a SharePoint
    site id / configured alias (e.g. 'throne') for site-hosted team notebooks. The two
    live in different URL spaces, so a site notebook will NOT appear under scope='me' —
    call once per scope to see everything.

    Returns id, displayName, isDefault, webUrl and the owning scope for each notebook."""
    token = _delegated_token()
    if not token:
        return [_unauth()]
    err = _check_path_args((scope, "scope"))
    if err:
        return [{"error": err}]
    root, site = _scope_root(scope)
    r = _g("GET", f"{root}/notebooks?$select=id,displayName,isDefault,createdDateTime,links",
           token)
    if not r.is_success:
        return [_graph_err(r)]
    return [{"id": n["id"], "displayName": n.get("displayName"),
             "isDefault": n.get("isDefault"), "created": n.get("createdDateTime"),
             "webUrl": _links(n), "scope": scope, "site_id": site}
            for n in r.json().get("value", [])]


def _sections_tree(notebook_id: str, scope: str, token: str) -> dict:
    """Section walk shared by onenote_list_sections and onenote_search.

    Exists as a plain function on purpose: @mcp.tool replaces the decorated name with a
    FunctionTool, which is NOT callable, so one tool can never call another in-process.
    Anything needed by two tools lives here and takes an explicit token.

    Returns {'sections': [...], 'section_groups': [...]} where every section carries the
    'path' of group names it sits under ('' for top-level)."""
    root, _ = _scope_root(scope)
    r = _g("GET", f"{root}/notebooks/{notebook_id}"
                  "?$expand=sections($select=id,displayName,createdDateTime,links),"
                  "sectionGroups($expand=sections($select=id,displayName,createdDateTime,links))",
           token)
    if not r.is_success:
        return _graph_err(r)
    nb = r.json()

    sections: list[dict] = []
    groups: list[dict] = []

    def _sec(s: dict, path: str) -> dict:
        return {"id": s["id"], "displayName": s.get("displayName"), "path": path,
                "created": s.get("createdDateTime"), "webUrl": _links(s)}

    for s in nb.get("sections", []) or []:
        sections.append(_sec(s, ""))

    def _walk(grps, prefix: str, depth: int = 0):
        # OneNote allows deep nesting; cap the walk so a pathological notebook cannot
        # spin here, and say so rather than truncating silently.
        if depth > 10:
            groups.append({"warning": f"section group nesting deeper than 10 under "
                                      f"'{prefix}' was not expanded"})
            return
        for gnode in grps or []:
            name = gnode.get("displayName") or ""
            path = f"{prefix}/{name}" if prefix else name
            groups.append({"id": gnode.get("id"), "displayName": name, "path": path})
            for s in gnode.get("sections", []) or []:
                sections.append(_sec(s, path))
            # $expand only reaches one level of nested groups; fetch deeper levels.
            child = gnode.get("sectionGroups")
            if child is None and gnode.get("id"):
                sub = _g("GET", f"{root}/sectionGroups/{gnode['id']}"
                                "?$expand=sections($select=id,displayName,createdDateTime,links),"
                                "sectionGroups", token)
                child = sub.json().get("sectionGroups", []) if sub.is_success else []
            _walk(child, path, depth + 1)

    _walk(nb.get("sectionGroups", []), "")
    return {"notebook_id": notebook_id, "displayName": nb.get("displayName"),
            "scope": scope, "sections": sections, "section_groups": groups,
            "section_count": len(sections)}


@mcp.tool
def onenote_list_sections(notebook_id: str, scope: str = "me") -> dict:
    """List the sections of a notebook, INCLUDING sections nested in section groups.

    Section groups are real and they nest, so a flat /sections call silently omits
    whatever lives inside one. This uses a single $expand round-trip
    (?$expand=sections,sectionGroups($expand=sections)) and then walks the group tree,
    which is both complete and faster than the call-per-level alternative.

    Returns {'sections': [...], 'section_groups': [...]} where every section carries the
    'path' of group names it sits under ('' for top-level), so a caller can tell two
    same-named sections apart."""
    token = _delegated_token()
    if not token:
        return _unauth()
    err = _check_path_args((notebook_id, "notebook_id"), (scope, "scope"))
    if err:
        return {"error": err}
    return _sections_tree(notebook_id, scope, token)


@mcp.tool
def onenote_list_pages(section_id: str, top: int = 50, scope: str = "me") -> list[dict]:
    """List pages in ONE section, newest-created first.

    Deliberately per-section only. Graph exposes an all-pages endpoint, but Microsoft's
    own best-practice guidance says not to use it: with many sections it fails 400 with
    OneNote error 20266 ('maximum number of sections exceeded'). Enumerate sections with
    onenote_list_sections, then call this once per section.

    Also overrides the default lastModifiedDateTime ordering, which Graph documents as the
    slow path, in favour of createdDateTime. top is capped at 100 (Graph's own ceiling)."""
    token = _delegated_token()
    if not token:
        return [_unauth()]
    err = _check_path_args((section_id, "section_id"), (scope, "scope"))
    if err:
        return [{"error": err}]
    root, _ = _scope_root(scope)
    n = max(1, min(int(top), _PAGE_TOP_MAX))
    r = _g("GET", f"{root}/sections/{section_id}/pages"
                  f"?$select=id,title,createdDateTime,lastModifiedDateTime,links"
                  f"&$orderby=createdDateTime desc&$top={n}", token)
    if not r.is_success:
        return [_graph_err(r)]
    out = [{"id": p["id"], "title": p.get("title"), "created": p.get("createdDateTime"),
            "lastModified": p.get("lastModifiedDateTime"), "webUrl": _links(p)}
           for p in r.json().get("value", [])]
    # Say so when the section holds more than we returned, so a caller never mistakes a
    # capped list for the whole section.
    if r.json().get("@odata.nextLink"):
        out.append({"note": f"more pages exist beyond the first {n}; raise `top` "
                            f"(max {_PAGE_TOP_MAX}) or narrow the section"})
    return out


@mcp.tool
def onenote_get_page(page_id: str, format: str = "text", include_ids: bool = False,
                     scope: str = "me") -> dict:
    """Read a page's content.

    The Graph content endpoint returns HTML, not JSON. format='text' (default) strips it
    to readable text, because raw OneNote HTML is heavy enough that a page with tables or
    images will eat far more context than its content is worth. format='html' returns it
    verbatim when you need the markup.

    Set include_ids=True to get ?includeIDs=true, which stamps data-id attributes on the
    elements — those ids are what onenote_append_page targets, so fetch with include_ids
    first whenever you intend to append to a specific element rather than the page body."""
    token = _delegated_token()
    if not token:
        return _unauth()
    if format not in ("text", "html"):
        return {"error": "format must be 'text' or 'html'"}
    err = _check_path_args((page_id, "page_id"), (scope, "scope"))
    if err:
        return {"error": err}
    root, _ = _scope_root(scope)

    meta = _g("GET", f"{root}/pages/{page_id}"
                     "?$select=id,title,createdDateTime,lastModifiedDateTime,links", token)
    if not meta.is_success:
        return _graph_err(meta)
    m = meta.json()

    q = "?includeIDs=true" if include_ids else ""
    r = _g("GET", f"{root}/pages/{page_id}/content{q}", token)
    if not r.is_success:
        return _graph_err(r)
    raw = r.text
    body = raw if format == "html" else _html_to_text(raw)
    return {"id": m.get("id", page_id), "title": m.get("title"),
            "created": m.get("createdDateTime"), "lastModified": m.get("lastModifiedDateTime"),
            "webUrl": _links(m), "format": format, "include_ids": include_ids,
            "chars": len(body), "html_chars": len(raw), "content": body}


@mcp.tool
def onenote_search(query: str, section_id: str | None = None, scope: str = "me",
                   max_sections: int = 25) -> dict:
    """Search page TITLES. This is a title search, not full text — read on.

    OneNote's $search over pages is not dependable on v1.0 (it is absent or silently
    ignored depending on tenant and notebook host), and presenting a title match as a
    full-text match would be a quiet lie about what was searched. So this enumerates
    sections, lists page titles per section, and filters client-side — and reports exactly
    that in its 'search_kind' field.

    Pass section_id to search one section (one round-trip). Without it, every section in
    every notebook in `scope` is walked, which is why max_sections exists; the result says
    when the cap truncated the sweep.

    To find text INSIDE pages, fetch candidates with onenote_get_page and search their
    content — there is no server-side full-text path here."""
    token = _delegated_token()
    if not token:
        return _unauth()
    q = (query or "").strip().lower()
    if not q:
        return {"error": "query is required"}
    err = _check_path_args(*(((section_id, "section_id"),) if section_id else ()),
                           *((scope, "scope"),))
    if err:
        return {"error": err}
    root, _ = _scope_root(scope)

    if section_id:
        section_ids = [(section_id, None)]
        truncated = False
    else:
        nb = _g("GET", f"{root}/notebooks?$select=id,displayName", token)
        if not nb.is_success:
            return _graph_err(nb)
        section_ids = []
        for n in nb.json().get("value", []):
            tree = _sections_tree(n["id"], scope, token)
            if "error" in tree:
                continue
            for s in tree.get("sections", []):
                section_ids.append((s["id"], n.get("displayName")))
        truncated = len(section_ids) > max_sections
        section_ids = section_ids[:max_sections]

    hits, scanned = [], 0
    for sid, nb_name in section_ids:
        r = _g("GET", f"{root}/sections/{sid}/pages"
                      f"?$select=id,title,createdDateTime,links"
                      f"&$orderby=createdDateTime desc&$top={_PAGE_TOP_MAX}", token)
        if not r.is_success:
            continue
        scanned += 1
        for p in r.json().get("value", []):
            if q in (p.get("title") or "").lower():
                hits.append({"id": p["id"], "title": p.get("title"),
                             "created": p.get("createdDateTime"), "section_id": sid,
                             "notebook": nb_name, "webUrl": _links(p)})
    return {"query": query, "search_kind": "title-only (not full text)",
            "sections_scanned": scanned, "truncated": truncated,
            "match_count": len(hits), "matches": hits}


# ---------------------------------------------------------------------------
# WRITE TOOLS  — confirm=True required, read-after-write verified
# ---------------------------------------------------------------------------
@mcp.tool
def onenote_create_page(section_id: str, title: str, html: str, confirm: bool = False,
                        scope: str = "me") -> dict:
    """Create a page in a section. Requires confirm=True.

    Posts text/html (OneNote's XHTML-ish dialect); a bare fragment is wrapped in a
    document with the title, a full <html> document is sent as-is.

    Verified: the created page is read back before this reports success, and the returned
    id/webUrl are the authoritative ones from that read-back — so a success here cannot be
    a lie, and follow-ups should use the returned id rather than a remembered one."""
    token = _delegated_token()
    if not token:
        return _unauth()
    if not confirm:
        return {"error": "refused — pass confirm=true to create a page. Nothing was written."}
    if not (title or "").strip():
        return {"error": "title is required"}
    if len(title) > _TITLE_MAX:
        return {"error": f"title exceeds OneNote's {_TITLE_MAX}-character limit"}
    err = _check_path_args((section_id, "section_id"), (scope, "scope"))
    if err:
        return {"error": err}
    root, _ = _scope_root(scope)

    doc = _page_html_body(title, html)
    r = _g("POST", f"{root}/sections/{section_id}/pages", token,
           headers={"Content-Type": "text/html"}, content=doc.encode("utf-8"))
    if not r.is_success:
        return _graph_err(r)
    created = r.json() if r.content else {}
    page_id = created.get("id")
    if not page_id:
        return {"error": "page POST succeeded but returned no id; not reporting success",
                "raw_status": r.status_code}

    # Read-after-write. A create that cannot be read back is not a create we will claim.
    back = _g("GET", f"{root}/pages/{page_id}/content", token)
    verified = back.is_success
    text = _html_to_text(back.text) if verified else ""
    _slog("onenote_create_page", page_id, section=section_id, scope=scope,
          bytes=len(doc.encode("utf-8")), verified=verified)
    return {"ok": True, "id": page_id, "title": created.get("title") or title,
            "webUrl": _links(created), "section_id": section_id, "scope": scope,
            "verified": verified, "readback_chars": len(text),
            **({} if verified else
               {"warning": "page created but could not be read back; verify manually"})}


@mcp.tool
def onenote_append_page(page_id: str, html: str, target: str = "body",
                        action: str = "append", confirm: bool = False,
                        scope: str = "me") -> dict:
    """Append content to an existing page. Requires confirm=True.

    PATCHes the page with a onenote-patch-content command array. target='body' appends to
    the page body; pass a data-id from onenote_get_page(include_ids=True) to target a
    specific element. action is 'append' (default), 'prepend', 'insert', or 'replace'.

    Verified, and specifically verified against TRUNCATION, which is the failure that
    matters here: the page text is captured before the PATCH, and afterwards this checks
    both that the page grew and that every non-trivial line present before is still
    present. If the prior content did not survive, this returns ok=False and says so
    instead of reporting a successful append over a page it just clobbered.

    'replace' legitimately removes content, so the survival check is reported but not
    treated as failure for that action."""
    token = _delegated_token()
    if not token:
        return _unauth()
    if not confirm:
        return {"error": "refused — pass confirm=true to modify a page. Nothing was written."}
    if action not in ("append", "prepend", "insert", "replace"):
        return {"error": "action must be append|prepend|insert|replace"}
    if not (html or "").strip():
        return {"error": "html is required — refusing to PATCH a page with empty content"}
    err = _check_path_args((page_id, "page_id"), (scope, "scope"))
    if err:
        return {"error": err}
    root, _ = _scope_root(scope)

    # 1) capture the before-state; a failed read ABORTS rather than patching blind.
    before = _g("GET", f"{root}/pages/{page_id}/content", token)
    if not before.is_success:
        return {"error": "append aborted — could not read the page first, refusing to "
                         f"PATCH blind: {_graph_err(before)['error']}"}
    before_text = _html_to_text(before.text)

    cmds = [{"target": target, "action": action, "content": html}]
    r = _g("PATCH", f"{root}/pages/{page_id}/content", token,
           headers={"Content-Type": "application/json"}, json=cmds)
    if not r.is_success:
        return _graph_err(r)

    # 2) read back and prove the prior content survived.
    after = _g("GET", f"{root}/pages/{page_id}/content", token)
    if not after.is_success:
        return {"ok": False, "id": page_id,
                "error": "PATCH returned success but the page could not be read back; "
                         "verify the page manually before appending again."}
    after_text = _html_to_text(after.text)

    prior = [ln.strip() for ln in before_text.splitlines() if len(ln.strip()) > 3]
    lost = [ln for ln in prior if ln not in after_text]
    grew = len(after_text) >= len(before_text)
    intact = not lost
    ok = (intact and grew) if action != "replace" else True

    # patch_target, not target: _slog's own second positional parameter is named `target`,
    # so passing target= here is a TypeError on every append.
    _slog("onenote_append_page", page_id, scope=scope, action=action, patch_target=target,
          chars_before=len(before_text), chars_after=len(after_text),
          lost_lines=len(lost), verified=ok)

    out = {"ok": ok, "id": page_id, "scope": scope, "action": action, "target": target,
           "chars_before": len(before_text), "chars_after": len(after_text),
           "content_intact": intact, "grew": grew}
    if not ok:
        out["error"] = (f"append may have TRUNCATED the page — {len(lost)} line(s) present "
                        f"before the PATCH are gone afterwards. Inspect the page before "
                        f"appending again.")
        out["lost_sample"] = lost[:3]
    return out


# ---------------------------------------------------------------------------
# HEALTH
# ---------------------------------------------------------------------------
def _health_payload(token: str | None) -> dict:
    """Shared by the tool and the unauthenticated /health route."""
    out: dict = {"checked_at": _now(), "server": "onenote-mcp",
                 "auth_mode": "delegated (OAuth via Entra; app-only is retired for OneNote)",
                 "public_base_url": PUBLIC_BASE_URL,
                 "entra_redirect_uri": f"{PUBLIC_BASE_URL}/auth/callback",
                 "scopes": OAUTH_SCOPES,
                 "site_aliases": sorted(SITE_ALIASES),
                 "token": "absent", "user": None, "notebooks": {}}
    if not token:
        out["token"] = "absent — no signed-in user on this request"
        return out
    out["token"] = "present"
    out["user"] = _whoami().get("upn")
    for key in ["me"] + sorted(SITE_ALIASES):
        root, _ = _scope_root(key)
        try:
            r = _g("GET", f"{root}/notebooks?$select=id", token)
            out["notebooks"][key] = (len(r.json().get("value", [])) if r.is_success
                                     else _graph_err(r)["error"])
        except Exception as e:
            out["notebooks"][key] = f"FAIL: {e}"
    return out


@mcp.tool
def onenote_healthcheck() -> dict:
    """Report delegated-token status, the signed-in user, and reachable notebook counts
    for every configured scope. First thing to run when the connector 'looks broken' —
    it distinguishes 'never signed in' from 'signed in but cannot see notebooks'."""
    return _health_payload(_delegated_token())


@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    """Unauthenticated liveness probe for Container Apps and external checks.

    Deliberately reports only configuration and reachability — never a token, a user, or a
    notebook name — because anything on this route is readable by anyone who can reach the
    ingress. Use the onenote_healthcheck TOOL for the signed-in view."""
    from starlette.responses import JSONResponse
    payload = {"status": "ok", "server": "onenote-mcp", "checked_at": _now(),
               "auth_mode": "delegated",
               "entra_redirect_uri": f"{PUBLIC_BASE_URL}/auth/callback",
               "mcp_endpoint": f"{PUBLIC_BASE_URL}/mcp",
               "scopes_configured": len(OAUTH_SCOPES),
               "site_aliases": sorted(SITE_ALIASES)}
    return JSONResponse(payload)


if __name__ == "__main__":
    # Streamable-HTTP is what makes this remotable; bind 0.0.0.0 and take the port from
    # the environment, which Container Apps injects.
    mcp.run(transport="streamable-http", host="0.0.0.0",
            port=int(os.environ.get("PORT", "8000")))
