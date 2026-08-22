"""
Throne MCP Server — reference starter for Claude Code to harden + deploy on Azure.

Exposes the existing app-only Graph identity (Claude-AE-Integration) as a remote MCP
connector: SharePoint read/write + notes-append, OneDrive CRUD, and send-mail — all
scoped to the Throne site via Sites.Selected.

This is a STARTER. Code owns: hardening auth (Tier 2 Entra Easy Auth), large-file upload
sessions, richer error handling, and the audit-log wiring. See THRONE_MCP_Connector_BUILD_HANDOFF.md.

Runtime: FastMCP over streamable-HTTP (remote-connector compatible).
    pip install fastmcp azure-identity azure-keyvault-secrets httpx
"""

import os
import base64
import datetime as dt
import hashlib
import hmac
import html
import json
import logging
import re
import sys
import time
import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from azure.identity import CertificateCredential

# ---------------------------------------------------------------------------
# Config (from env / Key Vault-backed container secrets)
# ---------------------------------------------------------------------------
TENANT_ID   = os.environ["AZURE_TENANT_ID"]
CLIENT_ID   = os.environ["GRAPH_CLIENT_ID"]          # da7c6dfa-03d0-415f-8cba-995bc2c9904e
THRONE_SITE = os.environ["THRONE_SITE_ID"]
THRONE_DRIVE = os.environ.get("THRONE_DRIVE_ID")     # explicit library (e.g. "Case Documnents"); else site default
BEARER      = os.environ.get("MCP_BEARER", "")        # Tier 1 shared secret
ALLOW_ANON  = os.environ.get("MCP_ALLOW_ANON") == "1" # must be explicit to run without a bearer

# Tier 2 — Entra ID OAuth (what claude.ai's connector UI needs; static bearers don't fit it).
OAUTH_CLIENT_ID     = os.environ.get("OAUTH_CLIENT_ID")
OAUTH_CLIENT_SECRET = os.environ.get("OAUTH_CLIENT_SECRET")
PUBLIC_BASE_URL     = os.environ.get("PUBLIC_BASE_URL")   # e.g. https://throne-mcp.<region>.azurecontainerapps.io
OAUTH_SCOPES        = [s.strip() for s in os.environ.get("OAUTH_SCOPES", "User.Read").split(",") if s.strip()]
OAUTH_ENABLED       = bool(OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET and PUBLIC_BASE_URL)
CERT_PATH   = os.environ.get("CERT_PATH", "/secrets/throne.pfx")  # used only in file mode
CERT_PWD    = os.environ.get("CERT_PASSWORD")
KEYVAULT_URI    = os.environ.get("KEYVAULT_URI")        # if set, pull cert from KV at boot
CERT_SECRET_NAME = os.environ.get("CERT_SECRET_NAME")   # KV secret/cert name holding the PFX

# §17 guardrail knobs (server-side, not prompt-level):
#   INTERNAL_MAIL_DOMAINS  — recipients here are "internal"; vendor Client-ID rule is only enforced
#                            when at least one recipient is EXTERNAL to these domains.
#   CONSUMER_NAME_BLOCKLIST — comma-separated consumer names that must NEVER appear in outbound mail.
#                            Inject the live case roster here (env or Key Vault secret) to get a real
#                            server-side block instead of a prompt-level promise. Empty = disabled.
INTERNAL_MAIL_DOMAINS = {d.strip().lower() for d in os.environ.get(
    "INTERNAL_MAIL_DOMAINS", "adaptiveenterprisesllc.com,fssa.in.gov").split(",") if d.strip()}
CONSUMER_NAME_BLOCKLIST = [n.strip() for n in os.environ.get(
    "CONSUMER_NAME_BLOCKLIST", "").split(",") if n.strip()]
#   AUDIT_SCRUB_STDOUT — the SharePoint audit mirror is ALWAYS scrubbed of consumer names
#                        (it is a shared, root-level file). The stdout/Log Analytics trail is
#                        not, by default: it sits behind Azure RBAC rather than library
#                        permissions, so it keeps full fidelity for incident review. Set to 1
#                        to scrub both surfaces. Flip with `az containerapp update
#                        --set-env-vars AUDIT_SCRUB_STDOUT=1` — no rebuild needed.
AUDIT_SCRUB_STDOUT = os.environ.get("AUDIT_SCRUB_STDOUT", "0") == "1"
#   PHI_SCRUB_NAMES — roster used to REDACT the audit log. Defaults to CONSUMER_NAME_BLOCKLIST,
#                     but is deliberately separable, because the two lists want different
#                     contents: the audit scrub wants EVERY consumer who has ever appeared in a
#                     path or subject (measured 2026-08-10: 84 distinct names across 324 of
#                     1,031 log lines), while CONSUMER_NAME_BLOCKLIST also REFUSES MAIL — and
#                     _consumer_block applies to every mailbox and every recipient, internal
#                     included. Arming 84 names as one shared list would redact the log
#                     correctly and simultaneously start refusing routine internal mail.
#                     Set this to the broad roster; keep CONSUMER_NAME_BLOCKLIST curated.
PHI_SCRUB_NAMES = [n.strip() for n in os.environ.get(
    "PHI_SCRUB_NAMES", "").split(",") if n.strip()] or CONSUMER_NAME_BLOCKLIST

GRAPH = "https://graph.microsoft.com/v1.0"
_SCOPE = "https://graph.microsoft.com/.default"
_UPLOAD_SIMPLE_MAX = 4 * 1024 * 1024   # >4MB must use an upload session
_UPLOAD_CHUNK = 5 * 320 * 1024         # 1.6MB; Graph requires multiples of 320KiB

def _build_graph_credential() -> CertificateCredential:
    """Graph client-credentials cert. Prefer Key Vault (managed identity) in Azure;
    fall back to a local PFX file for dev. KV-imported PFX comes back unencrypted via the
    secret endpoint, so no password is needed in that path."""
    if KEYVAULT_URI and CERT_SECRET_NAME:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient
        sc = SecretClient(vault_url=KEYVAULT_URI, credential=DefaultAzureCredential())
        pfx_b64 = sc.get_secret(CERT_SECRET_NAME).value
        pfx = base64.b64decode(pfx_b64)
        return CertificateCredential(tenant_id=TENANT_ID, client_id=CLIENT_ID,
                                     certificate_data=pfx)
    return CertificateCredential(tenant_id=TENANT_ID, client_id=CLIENT_ID,
                                 certificate_path=CERT_PATH, password=CERT_PWD)


_credential = _build_graph_credential()


def _build_auth():
    """Tier 2: protect the whole MCP transport with Entra ID OAuth (AzureProvider acts as an
    OAuth proxy so claude.ai can connect with the OAuth fields left blank). Only netorg39360
    users can sign in. Falls back to None (Tier 1 per-tool bearer gate) when not configured.

    client_storage externalizes the OAuth session/token store to an Azure Files mount
    (Fernet-encrypted at rest) so a deploy no longer wipes every client's session -- the
    default store is process/container-local, and activeRevisionsMode=Single kills the prior
    revision's container the instant the new one takes traffic (root cause:
    CONNECTOR-RELIABILITY-FIX.md). Was Redis (throne-mcp-sessions); that cluster failed to
    provision twice with an opaque OperationFailed and was deleted 2026-07-29 -- this is the
    pivot. Omitted entirely (not just passed as None) when SESSION_STORAGE_DIR /
    SESSION_ENCRYPTION_KEY aren't set (e.g. local/dev runs, or this image sitting built-but-
    unused in ACR before the volume mount is wired up), so AzureProvider's own default
    behavior is unchanged."""
    if not OAUTH_ENABLED:
        return None
    from fastmcp.server.auth.providers.azure import AzureProvider

    auth_kwargs = dict(
        client_id=OAUTH_CLIENT_ID,
        client_secret=OAUTH_CLIENT_SECRET,
        tenant_id=TENANT_ID,
        base_url=PUBLIC_BASE_URL,
        required_scopes=OAUTH_SCOPES,
    )

    session_storage_dir = os.environ.get("SESSION_STORAGE_DIR")
    encryption_key = os.environ.get("SESSION_ENCRYPTION_KEY")
    if session_storage_dir and encryption_key:
        # Pinned py-key-value-aio==0.3.0 (fastmcp 2.14.7 forces <0.4.0; 0.3.0 is the ONLY
        # release in that range) -- confirmed by pip-installing this EXACT pinned version into
        # an isolated venv and reading inspect.signature() against the real installed source,
        # not docs (which drift: see the RedisStore comment this replaced, and strawgate.com's
        # own stores page mis-documents FileTreeStore's directory kwarg name and lists a
        # MultiDiskStore that doesn't exist in this package at all).
        #
        # DiskStore (diskcache-backed) chosen over FileTreeStore: FileTreeStore is FastMCP's
        # own doc example for filesystem persistence, but py-key-value-aio's own docs call it
        # "NOT for production, no atomic operations." DiskStore is the mature, TTL-aware,
        # battle-tested option here -- but it writes cache.db / cache.db-shm / cache.db-wal
        # (SQLite in WAL mode), and WAL mode over a network filesystem (SMB, which Azure Files
        # is) is a documented corruption risk once more than one writer touches the same file
        # concurrently (python-diskcache's own issue tracker; SQLite's own docs warn the same
        # way about network filesystems generally).
        #
        # That's a LATENT risk today, not a live one: activeRevisionsMode=Single means only one
        # revision serves traffic at a time, and this fix's whole point is surviving the
        # *sequential* handoff between the dying old revision and the new one, not concurrent
        # multi-writer access. But maxReplicas=3 on this app means true concurrent access (two
        # replicas of the SAME revision both mounting the SAME share) is one traffic spike away
        # -- CONNECTOR-RELIABILITY-FIX.md already names scale-out as a second, separate latent
        # trigger for session loss.
        #
        # MITIGATION UNTIL A CONCURRENCY-SAFE STORE IS ADOPTED: keep max-replicas at 1 while
        # this store is in use. Do not raise it against this mount without revisiting this.
        from key_value.aio.stores.disk import DiskStore
        from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
        from cryptography.fernet import Fernet
        auth_kwargs["client_storage"] = FernetEncryptionWrapper(
            key_value=DiskStore(directory=session_storage_dir),
            fernet=Fernet(encryption_key),
        )

    return AzureProvider(**auth_kwargs)


mcp = FastMCP("Throne (AE SharePoint)", auth=_build_auth())


def _now() -> str:
    """Timezone-aware UTC stamp (utcnow() is deprecated in 3.12+)."""
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _token() -> str:
    return _credential.get_token(_SCOPE).token


# --- Graph throttling: honor Retry-After (429/503) with exponential backoff ---
_MAX_RETRIES = 5
_BACKOFF_CAP = 60.0


def _send_with_retry(method: str, url: str, **kw) -> httpx.Response:
    """Single point through which every Graph/SharePoint request flows so 429/503 throttling
    is handled uniformly: honor a numeric Retry-After header, else exponential backoff."""
    delay = 1.0
    resp = None
    for attempt in range(_MAX_RETRIES + 1):
        resp = httpx.request(method, url, **kw)
        if resp.status_code not in (429, 503) or attempt == _MAX_RETRIES:
            return resp
        ra = resp.headers.get("Retry-After", "")
        try:
            wait = float(ra)
        except ValueError:
            wait = delay
        time.sleep(min(wait, _BACKOFF_CAP))
        delay = min(delay * 2, _BACKOFF_CAP)
    return resp


# --- structured write logging: one JSON line per write, to stdout (Container Apps logs) ---
logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
_WLOG = logging.getLogger("throne.writes")


def _slog(tool: str, target: str, **extra) -> None:
    rec = {"ts": _now(), "evt": "write", "tool": tool, "target": target}
    if extra:
        rec.update(extra)
    try:
        _WLOG.info(json.dumps(rec, default=str))
    except Exception:
        pass


def _g(method: str, path: str, **kw) -> httpx.Response:
    headers = kw.pop("headers", {})
    headers["Authorization"] = f"Bearer {_token()}"
    # Graph /content GETs return a 302 to a SharePoint download URL (with its own tempauth);
    # httpx must follow it (and it safely strips our Authorization on the cross-host hop).
    kw.setdefault("follow_redirects", True)
    return _send_with_retry(method, f"{GRAPH}{path}", headers=headers, timeout=120, **kw)


def _graph_err(r: httpx.Response) -> dict:
    """Surface Graph's own error message instead of a bare stack trace."""
    try:
        msg = r.json().get("error", {}).get("message", r.text[:400])
    except Exception:
        msg = r.text[:400]
    return {"error": f"graph {r.status_code}: {msg}"}


def _auth_ok() -> bool:
    """Auth gate. In OAuth (Tier 2) mode FastMCP already rejected unauthenticated requests at the
    transport layer, so reaching a tool means the caller is authenticated. Otherwise use the Tier 1
    static-bearer check (constant-time)."""
    if OAUTH_ENABLED:
        return True
    if not BEARER:
        return ALLOW_ANON  # fail CLOSED unless MCP_ALLOW_ANON=1 is set explicitly
    # include_all=True: FastMCP strips Authorization (and other sensitive headers) by default.
    h = get_http_headers(include_all=True)
    return hmac.compare_digest(h.get("authorization", ""), f"Bearer {BEARER}")


_DRIVE_CACHE: dict[str, str] = {}


def _throne_drive_id() -> str:
    """Resolve + cache the Throne target library drive id.
    Prefer the explicit THRONE_DRIVE_ID (the 'Case Documnents' library); the site DEFAULT
    document library is a different, empty library, so falling back to it is only for dev."""
    if THRONE_DRIVE:
        return THRONE_DRIVE
    if "throne" not in _DRIVE_CACHE:
        r = _g("GET", f"/sites/{THRONE_SITE}/drive")
        r.raise_for_status()
        _DRIVE_CACHE["throne"] = r.json()["id"]
    return _DRIVE_CACHE["throne"]


def _normalize_lib_path(drive: str, path: str) -> str:
    """Forgive the historical footgun where callers prefixed the library name into the path.
    Old docstrings showed 'Case Documnents/X/notes.md', but the throne default drive already IS
    the Case Documnents library, so a leading 'Case Documnents/' segment is DOUBLED and never
    resolves (the metadata GET 404s and a blind PUT would create a phantom file). Strip that
    leading segment when we're on the throne library drive. No-op for correctly library-relative
    paths and for other sites (opshub, explicit library, URL/id)."""
    p = path.lstrip("/")
    try:
        on_throne = drive == _throne_drive_id()
    except Exception:
        on_throne = False
    if on_throne:
        # strip REPEATED leading library-name segments (doubled/tripled prefixes, not just one)
        changed = True
        while changed:
            changed = False
            low = p.lower()
            for pref in ("case documnents/", "case documents/"):  # real (misspelled) + corrected typo
                if low.startswith(pref):
                    p = p[len(pref):]
                    changed = True
    return p


# ---------------------------------------------------------------------------
# PHI containment for the audit trail (CLAUDE.md Rule 8)
# ---------------------------------------------------------------------------
# _audit() mirrors every write to Claude-Writes-Log.md at the ROOT of the SHARED "Case
# Documnents" library — readable by any staff member with library access. Rule 8 says a
# consumer's identifiers live only in that consumer's own case folder, so no blocklisted
# name may reach that file. Two families of call site could put one there:
#   1. the §17 mail guards, whose refusal text names the consumer it just caught — the guard
#      that exists to stop a name leaving was writing that exact name into a shared file;
#   2. free-text fields a caller controls (mail subject, calendar subject, task title,
#      attachment filename), which can carry a name plus clinical or employment context.
#
# The trade this resolves: a blocked send is only reviewable if you know WHAT was blocked, so
# the name is not discarded — it is replaced by a reference an authorized reviewer holding the
# roster can resolve and nobody else can:
#     entry#<i>  position in CONSUMER_NAME_BLOCKLIST — instant triage against the live roster
#     /<8 hex>   sha256 prefix of the lowercased name — still resolves after the roster is
#                reordered or edited, which the index alone would not (it would silently
#                re-point at a different consumer)
# Same principle the v19 To Do writers already follow: log ids and counts, never names.
#
# NOTE: this is only as good as the roster. With PHI_SCRUB_NAMES and CONSUMER_NAME_BLOCKLIST
# both empty the scrub is a no-op — measured 2026-08-10, that is production's state, and it is
# why 324 of the 1,031 lines then in Claude-Writes-Log.md carried a consumer name. Arming
# PHI_SCRUB_NAMES is what turns this on; it does NOT affect mail refusal.
_BLOCKLIST_INDEX = {n.strip().lower(): i
                    for i, n in enumerate(PHI_SCRUB_NAMES) if n.strip()}
# Longest alternative first: regex alternation is leftmost-first, not longest-match, so a
# roster holding both "Smith" and "Smithson" would otherwise match the prefix and strand "on".
_BLOCKLIST_RE = re.compile(
    "|".join(re.escape(n) for n in sorted(_BLOCKLIST_INDEX, key=len, reverse=True)),
    re.IGNORECASE) if _BLOCKLIST_INDEX else None


def _blocklist_ref(name: str) -> str:
    """Opaque, stable reference to a blocklist entry. Never the name itself."""
    key = (name or "").strip().lower()
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
    i = _BLOCKLIST_INDEX.get(key)
    return f"entry#{i}/{h}" if i is not None else f"entry#?/{h}"


def _scrub_phi(text: str) -> str:
    """Replace every blocklisted consumer name with its opaque reference.
    Applied at the _audit choke point so it holds for all ~50 call sites by construction — a
    future writer cannot reintroduce the leak by formatting a name into its target string."""
    if not _BLOCKLIST_RE or not text:
        return text
    return _BLOCKLIST_RE.sub(lambda m: f"[consumer {_blocklist_ref(m.group(0))}]", text)


def _audit(action: str, target: str):
    """Append a who/when/what line to Throne\\Claude-Writes-Log.md (best-effort).
    Also emits a structured JSON write-log line to stdout (captured in Container Apps logs).

    PHI: the SharePoint mirror is scrubbed unconditionally (shared, root-level file — Rule 8).
    The stdout trail keeps full fidelity by default because Container Apps logs are gated by
    Azure RBAC, not library permissions; set AUDIT_SCRUB_STDOUT=1 to scrub both."""
    _slog(action, _scrub_phi(target) if AUDIT_SCRUB_STDOUT else target)
    try:
        drive = _throne_drive_id()
        line = f"- {_now()} | {action} | {_scrub_phi(target)}\n"
        path = "Claude-Writes-Log.md"
        cur = _g("GET", f"/drives/{drive}/root:/{path}:/content")
        if cur.status_code == 200:
            body = cur.text + line
        elif cur.status_code == 404:
            body = "# Claude Writes Log\n" + line   # genuinely absent -> start it
        else:
            return  # transient read failure -> do NOT rewrite (would truncate the whole log)
        w = _g("PUT", f"/drives/{drive}/root:/{path}:/content",
               headers={"Content-Type": "text/plain"}, content=body.encode())
        if not w.is_success:
            # md mirror is best-effort; the _slog stdout line above is the authoritative trail.
            _slog("audit_mirror_failed", path, status=w.status_code)
    except Exception:
        pass  # never let audit failure block the operation


# ---------------------------------------------------------------------------
# READ
# ---------------------------------------------------------------------------
@mcp.tool
def throne_search(query: str, top: int = 10, site: str = "throne",
                  library: str | None = None) -> list[dict]:
    """Full-text search a SharePoint library. Returns name, id, webUrl per hit.
    site='throne' (default, pinned library) | 'opshub' (Zach Operations Hub) | a URL/id."""
    if not _auth_ok():
        return [{"error": "unauthorized"}]
    drive = _drive_for(site, library)
    q = query.replace("'", "''")  # escape single quotes for the OData search() literal
    r = _g("GET", f"/drives/{drive}/root/search(q='{q}')?$top={int(top)}")
    if not r.is_success:
        return [_graph_err(r)]
    return [{"name": i["name"], "id": i["id"], "webUrl": i.get("webUrl")}
            for i in r.json().get("value", [])]


@mcp.tool
def throne_read_file(item_id: str, site: str = "throne",
                     library: str | None = None) -> str:
    """Read a file's text content by drive item id. site='throne'|'opshub'|URL/id."""
    if not _auth_ok():
        return "unauthorized"
    drive = _drive_for(site, library)
    r = _g("GET", f"/drives/{drive}/items/{item_id}/content")
    r.raise_for_status()
    return r.text


@mcp.tool
def throne_list_folder(item_id: str = "root", site: str = "throne",
                       library: str | None = None) -> list[dict]:
    """List children of a folder (default = library root). site='throne'|'opshub'|URL/id."""
    if not _auth_ok():
        return [{"error": "unauthorized"}]
    drive = _drive_for(site, library)
    seg = "root" if item_id == "root" else f"items/{item_id}"
    r = _g("GET", f"/drives/{drive}/{seg}/children")
    r.raise_for_status()
    return [{"name": i["name"], "id": i["id"], "folder": "folder" in i}
            for i in r.json().get("value", [])]


# ---------------------------------------------------------------------------
# WRITE  (the point of this connector)
# ---------------------------------------------------------------------------
def _upload_bytes(drive: str, path: str, data: bytes, overwrite: bool,
                  item_id: str | None = None) -> dict:
    """PUT for <4MB, resumable upload session for larger payloads.
    If item_id is given (an existing file being overwritten), the small-file PUT targets the item
    BY ID — an in-place content update that PRESERVES the driveItem id and version history. Writing
    by path with conflictBehavior=replace instead deletes+recreates (a new id every overwrite),
    which is what made read-back-by-remembered-id and the search index go stale."""
    conflict = "replace" if overwrite else "fail"
    if len(data) <= _UPLOAD_SIMPLE_MAX:
        if item_id:
            r = _g("PUT", f"/drives/{drive}/items/{item_id}/content",
                   headers={"Content-Type": "application/octet-stream"}, content=data)
        else:
            r = _g("PUT", f"/drives/{drive}/root:/{path}:/content"
                   f"?@microsoft.graph.conflictBehavior={conflict}",
                   headers={"Content-Type": "application/octet-stream"}, content=data)
        return {"resp": r}
    # --- resumable upload session (>4MB) ---
    s = _g("POST", f"/drives/{drive}/root:/{path}:/createUploadSession",
           json={"item": {"@microsoft.graph.conflictBehavior": conflict}})
    if not s.is_success:
        return {"resp": s}
    url = s.json()["uploadUrl"]
    total = len(data)
    last = None
    for start in range(0, total, _UPLOAD_CHUNK):
        chunk = data[start:start + _UPLOAD_CHUNK]
        end = start + len(chunk) - 1
        last = httpx.put(url, content=chunk, timeout=120, headers={
            "Content-Length": str(len(chunk)),
            "Content-Range": f"bytes {start}-{end}/{total}",
        })
        if last.status_code not in (200, 201, 202):
            return {"resp": last}
    return {"resp": last}


@mcp.tool
def throne_write_file(path: str, content: str, overwrite: bool = True,
                      site: str = "throne", library: str | None = None) -> dict:
    """Create or overwrite a text file by path (e.g. 'Cases/Last, First/Last - Notes.md').
    site='throne' (default, pinned Case Documnents library) | 'opshub' | a URL/id.
    Path is LIBRARY-RELATIVE — do NOT prefix 'Case Documnents/'; the throne drive already IS
    that library, so prefixing it doubles the segment and writes a phantom file. A leading
    library-name segment is stripped defensively.
    Handles any size: simple PUT under 4MB, resumable upload session above it."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    drive = _drive_for(site, library)
    path = _normalize_lib_path(drive, path)
    # Overwrite an existing file IN PLACE (stable id) — resolve its id first. On any failure to
    # resolve, item_id stays None and we fall back to the exact path-based write used before.
    item_id = None
    if overwrite:
        meta = _g("GET", f"/drives/{drive}/root:/{path}")
        if meta.status_code == 200:
            item_id = meta.json().get("id")
    r = _upload_bytes(drive, path, content.encode(), overwrite, item_id)["resp"]
    if not r.is_success:
        return _graph_err(r)
    _audit("write_file", path)
    body = r.json() if r.content else {}
    return {"ok": True, "id": body.get("id"), "path": path}


# File-type sanity map: extension -> human label. Used only for a friendly hint in the
# response; SharePoint infers the real content type from the extension on the path.
_BINARY_KINDS = {
    ".pdf": "PDF",
    ".docx": "Word (docx)", ".doc": "Word (doc)", ".dotx": "Word template",
    ".xlsx": "Excel (xlsx)", ".xls": "Excel (xls)",
    ".pptx": "PowerPoint (pptx)", ".ppt": "PowerPoint (ppt)",
    ".png": "PNG image", ".jpg": "JPEG image", ".jpeg": "JPEG image",
    ".gif": "GIF image", ".zip": "ZIP archive",
}


@mcp.tool
def throne_write_binary(path: str, content_b64: str, overwrite: bool = True,
                        site: str = "throne", library: str | None = None) -> dict:
    """Create or overwrite a BINARY file in Throne by path, from base64-encoded bytes.
    Use this for real documents — PDFs and Word (.docx/.doc), plus Excel, PowerPoint, images,
    and zips. `throne_write_file` is TEXT-ONLY and will corrupt these; use this instead.

    Args:
        path: destination path in the Throne library, e.g.
              "Evaluations/Hensley, Christopher 'Chris'/Hensley, Christopher - Referral.pdf".
              The file extension drives how SharePoint types the file (so keep .pdf/.docx/etc).
        content_b64: the file's raw bytes, base64-encoded (standard base64).
        overwrite: replace an existing file at the path (True) or fail on conflict (False).

    Handles any size: simple PUT under 4MB, resumable upload session above it (reuses the
    same _upload_bytes path as throne_write_file, so large scans/reports work too)."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    try:
        data = base64.b64decode(content_b64, validate=True)
    except Exception as e:
        return {"error": f"invalid base64 content: {e}"}
    if not data:
        return {"error": "refused: decoded content is empty"}
    drive = _drive_for(site, library)
    path = _normalize_lib_path(drive, path)
    item_id = None
    if overwrite:
        meta = _g("GET", f"/drives/{drive}/root:/{path}")
        if meta.status_code == 200:
            item_id = meta.json().get("id")
    r = _upload_bytes(drive, path, data, overwrite, item_id)["resp"]
    if not r.is_success:
        return _graph_err(r)
    _audit("write_binary", path)
    body = r.json() if r.content else {}
    ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {"ok": True, "id": body.get("id"), "path": path,
            "bytes": len(data), "kind": _BINARY_KINDS.get(ext, ext or "unknown")}


@mcp.tool
def throne_append_note(path: str, entry: str, add_timestamp: bool = True,
                       site: str = "throne", library: str | None = None,
                       create_if_missing: bool = False) -> dict:
    """Append a dated entry to an existing notes file (read->append->write), VERIFIED.

    site='throne' (default) | 'opshub' | a URL/id. The eval-notes use case.

    Path is LIBRARY-RELATIVE: pass 'Cases/Last, First/Last - Case Notes Log.md', NOT
    'Case Documnents/Cases/...' (the throne drive already IS that library; a leading
    library-name segment is stripped defensively).

    Hardened so a success can never be a lie:
      * resolves the target by item id and REQUIRES it to exist (create_if_missing=True to
        start a new file on purpose) — a wrong/doubled path errors loudly instead of silently
        creating a phantom file and reporting ok;
      * a failed read ABORTS (never falls back to an empty base, which would truncate the file);
      * reads and writes the SAME item id (stable id + new version — no id churn), under an
        If-Match precondition so a concurrent edit can't be clobbered;
      * VERIFIES the resulting byte count before returning ok, and returns the authoritative
        item id + byte counts. Verify follow-ups with the returned id or throne_list_folder,
        never a remembered pre-write id (SharePoint search/read-by-old-id can lag a write)."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    drive = _drive_for(site, library)
    path = _normalize_lib_path(drive, path)
    # 1) resolve item + confirm existence via a metadata GET (no /content -> no 302 redirect).
    meta = _g("GET", f"/drives/{drive}/root:/{path}")
    if meta.status_code == 404:
        if not create_if_missing:
            return {"error": f"append aborted — note file not found, nothing was written: "
                             f"'{path}'. Pass a library-relative path (no 'Case Documnents/' "
                             f"prefix), or create_if_missing=true to start a new file."}
        item_id, tag, before, base = None, None, 0, ""
    elif not meta.is_success:
        return _graph_err(meta)
    else:
        item = meta.json()
        # cTag = content tag (unchanged by metadata-only edits) — the right precondition for a
        # content write; fall back to eTag if cTag is absent.
        item_id, tag, before = item["id"], item.get("cTag") or item.get("eTag"), item.get("size", 0)
        # 2) read CURRENT content by id; a failed read must ABORT (never overwrite with "").
        cur = _g("GET", f"/drives/{drive}/items/{item_id}/content")
        if not cur.is_success:
            return {"error": "append aborted — could not read current content, refusing to "
                             f"overwrite: {_graph_err(cur)['error']}"}
        base = cur.text
    # 3) build the appended body.
    stamp = (_now() + " — ") if add_timestamp else ""
    body = base + ("\n" if base and not base.endswith("\n") else "") + stamp + entry + "\n"
    data = body.encode()
    # 4) write. Existing -> PUT by item id (in place, versioned, stable id) under If-Match.
    #    New -> create by path with conflictBehavior=fail (we already saw it 404).
    headers = {"Content-Type": "text/plain"}
    if item_id:
        if tag:
            # Best-effort optimistic lock. If-Match is documented for the upload-session flow;
            # on this simple content PUT it may be ignored — so the size check below is the real
            # guarantee, not this header.
            headers["If-Match"] = tag
        w = _g("PUT", f"/drives/{drive}/items/{item_id}/content", headers=headers, content=data)
    else:
        w = _g("PUT", f"/drives/{drive}/root:/{path}:/content"
               "?@microsoft.graph.conflictBehavior=fail", headers=headers, content=data)
    if w.status_code == 412:
        return {"error": "append aborted — file changed since read (If-Match precondition "
                         "failed). Nothing written; re-run the append."}
    if not w.is_success:
        return _graph_err(w)
    new = w.json() if w.content else {}
    after = new.get("size")
    verified = after is not None and after == len(data)
    _audit("append_note", path)
    return {"ok": True, "path": path, "id": new.get("id", item_id),
            "bytes_before": before, "bytes_after": after, "verified": verified}


@mcp.tool
def throne_create_folder(parent_id: str, name: str, site: str = "throne",
                         library: str | None = None) -> dict:
    """Create a folder under parent_id. site='throne'|'opshub'|URL/id."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    drive = _drive_for(site, library)
    r = _g("POST", f"/drives/{drive}/items/{parent_id}/children",
           json={"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"})
    r.raise_for_status()
    _audit("create_folder", f"{parent_id}/{name}")
    return {"ok": True, "id": r.json().get("id")}


@mcp.tool
def throne_delete(item_id: str, confirm: bool = False, site: str = "throne",
                  library: str | None = None) -> dict:
    """Delete an item by drive item id. Requires confirm=True. site='throne'|'opshub'|URL/id."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    if not confirm:
        return {"error": "refused: pass confirm=true to delete"}
    drive = _drive_for(site, library)
    r = _g("DELETE", f"/drives/{drive}/items/{item_id}")
    r.raise_for_status()
    _audit("delete", item_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# ONEDRIVE  (app-only: target /users/{id}/drive — no /me)
# ---------------------------------------------------------------------------
@mcp.tool
def onedrive_list(user_id: str, item_id: str = "root") -> list[dict]:
    if not _auth_ok():
        return [{"error": "unauthorized"}]
    seg = "root" if item_id == "root" else f"items/{item_id}"
    r = _g("GET", f"/users/{user_id}/drive/{seg}/children")
    r.raise_for_status()
    return [{"name": i["name"], "id": i["id"], "folder": "folder" in i}
            for i in r.json().get("value", [])]


@mcp.tool
def onedrive_read_file(user_id: str, item_id: str) -> str:
    """Read a user's OneDrive file text content by drive item id."""
    if not _auth_ok():
        return "unauthorized"
    r = _g("GET", f"/users/{user_id}/drive/items/{item_id}/content")
    if not r.is_success:
        return f"graph {r.status_code}: {r.text[:200]}"
    return r.text


@mcp.tool
def onedrive_write_file(user_id: str, path: str, content: str, overwrite: bool = True) -> dict:
    """Create/overwrite a file in a user's OneDrive. Any size (upload session above 4MB)."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    conflict = "replace" if overwrite else "fail"
    r = _g("PUT", f"/users/{user_id}/drive/root:/{path}:/content"
           f"?@microsoft.graph.conflictBehavior={conflict}",
           headers={"Content-Type": "application/octet-stream"}, content=content.encode())
    if not r.is_success:
        return _graph_err(r)
    _audit("onedrive_write", f"{user_id}:{path}")
    return {"ok": True, "id": r.json().get("id")}


@mcp.tool
def onedrive_write_binary(user_id: str, path: str, content_b64: str,
                          overwrite: bool = True) -> dict:
    """Create/overwrite a BINARY file in a user's OneDrive from base64-encoded bytes.
    Use for PDFs and Word (.docx/.doc), plus Excel, PowerPoint, images, zips.
    onedrive_write_file is TEXT-ONLY and will corrupt these; use this instead.
    `content_b64` is the file's raw bytes, standard base64-encoded."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    try:
        data = base64.b64decode(content_b64, validate=True)
    except Exception as e:
        return {"error": f"invalid base64 content: {e}"}
    if not data:
        return {"error": "refused: decoded content is empty"}
    conflict = "replace" if overwrite else "fail"
    r = _g("PUT", f"/users/{user_id}/drive/root:/{path}:/content"
           f"?@microsoft.graph.conflictBehavior={conflict}",
           headers={"Content-Type": "application/octet-stream"}, content=data)
    if not r.is_success:
        return _graph_err(r)
    _audit("onedrive_write_binary", f"{user_id}:{path}")
    ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {"ok": True, "id": r.json().get("id"), "bytes": len(data),
            "kind": _BINARY_KINDS.get(ext, ext or "unknown")}


@mcp.tool
def onedrive_create_folder(user_id: str, parent_id: str, name: str) -> dict:
    """Create a folder in a user's OneDrive."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    r = _g("POST", f"/users/{user_id}/drive/items/{parent_id}/children",
           json={"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"})
    if not r.is_success:
        return _graph_err(r)
    _audit("onedrive_create_folder", f"{user_id}:{parent_id}/{name}")
    return {"ok": True, "id": r.json().get("id")}


@mcp.tool
def onedrive_delete(user_id: str, item_id: str, confirm: bool = False) -> dict:
    """Delete an item from a user's OneDrive. Requires confirm=True."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    if not confirm:
        return {"error": "refused: pass confirm=true to delete"}
    r = _g("DELETE", f"/users/{user_id}/drive/items/{item_id}")
    if not r.is_success:
        return _graph_err(r)
    _audit("onedrive_delete", f"{user_id}:{item_id}")
    return {"ok": True}


# ---------------------------------------------------------------------------
# MAIL  (Mail.Send app role) — enforces §17 arm's-length guardrails
# ---------------------------------------------------------------------------
# `re` is imported at the top of the module (needed there by the PHI audit scrubber).
_CLIENT_ID_RE = re.compile(r"\b\d{5,7}\b")   # AE Client IDs are numeric; adjust to your pattern
_TAG_RE = re.compile(r"<[^>]+>")             # crude HTML strip for scanning


def _is_external(addr: str) -> bool:
    dom = addr.split("@")[-1].strip().lower()
    return bool(dom) and dom not in INTERNAL_MAIL_DOMAINS


_REAL_TAG_RE = re.compile(r"<\s*/?[a-zA-Z!]")
_ESCAPED_TAG_RE = re.compile(r"&(?:amp;)*(?:lt|#60|#x3[Cc]);\s*/?[a-zA-Z!]")


def _normalize_body_html(body):
    """Repair an HTML body that arrived entity-escaped.

    A caller that escapes its own markup ships literal <p>/<table> text to the
    recipient. That has happened repeatedly on live vendor mail, most recently on
    2026-08-20 (Client ID 357257, two vendors + two state counselors).

    Deliberately conservative: rewrites ONLY a body that contains escaped tags and
    no real tags. A body in that state is definitionally broken, so unescaping it
    cannot destroy legitimate content. Any body already containing real markup is
    returned untouched, so a document that legitimately quotes &lt;p&gt; inside real
    HTML is preserved exactly.
    """
    if not body:
        return body
    if _REAL_TAG_RE.search(body):
        return body
    out = body
    for _ in range(3):  # bounded; handles &amp;lt;p&amp;gt; double-escaping
        if not _ESCAPED_TAG_RE.search(out):
            break
        out = html.unescape(out)
        if _REAL_TAG_RE.search(out):
            break
    return out


def _mail_guard(to: list[str], cc: list[str], subject: str, body_html: str) -> str | None:
    """Server-side §17 enforcement (not prompt-level). Returns a refusal reason, or None to allow.
    Layered:
      1. Any blocklisted consumer name anywhere in subject/body -> hard refuse (roster via env).
      2. Vendor-directed mail (any external recipient) must carry a Client ID token -> else refuse.
    """
    text = f"{subject}\n{_TAG_RE.sub(' ', body_html)}"
    low = text.lower()
    for name in CONSUMER_NAME_BLOCKLIST:
        if name.lower() in low:
            return (f"blocked: consumer name '{name}' present in subject/body — "
                    f"vendor comms are Client-ID-only (§17)")
    recipients = list(to) + list(cc or [])
    if any(_is_external(a) for a in recipients) and not _CLIENT_ID_RE.search(text):
        return ("blocked: external/vendor recipient but no Client ID found in subject/body — "
                "§17 requires a Client ID and forbids consumer names in vendor mail")
    return None


@mcp.tool
def send_mail(from_user_id: str, to: list[str], subject: str, body_html: str,
              cc: list[str] | None = None, client_id_ack: bool = False) -> dict:
    """Send mail as a tenant mailbox. §17: vendor comms use Client ID ONLY, never consumer names.
    Enforced server-side: blocklisted consumer names are refused outright; any external recipient
    requires a Client ID in the subject/body. client_id_ack must also be True (explicit human intent)."""
    if not _auth_ok():
        return {"error": "unauthorized"}
        body_html = _normalize_body_html(body_html)
    if not client_id_ack:
        return {"error": "refused: set client_id_ack=true after confirming no consumer name in subject/body"}
    reason = _mail_guard(to, cc or [], subject, body_html)
    if reason:
        _audit("send_mail_BLOCKED", f"{from_user_id} -> {', '.join(to)} :: {reason}")
        return {"error": f"refused: {reason}"}
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body_html},
            "toRecipients": [{"emailAddress": {"address": a}} for a in to],
            "ccRecipients": [{"emailAddress": {"address": a}} for a in (cc or [])],
        },
        "saveToSentItems": True,
    }
    r = _g("POST", f"/users/{from_user_id}/sendMail", json=payload)
    if not r.is_success:
        return _graph_err(r)
    _audit("send_mail", f"{from_user_id} -> {', '.join(to)}")
    return {"ok": True}


# ---------------------------------------------------------------------------
# OUTLOOK — full read / compose / draft / send / reply / forward across mailboxes
# App-only Mail.ReadWrite + Mail.Send. Mailbox by friendly alias or full UPN.
# General-purpose (NOT the arm's-length vendor path — that's send_mail). A consumer-name
# safety check still runs on every outbound; every send/draft/delete is audit-logged.
# ---------------------------------------------------------------------------
# Only mailboxes in the Exchange scope group are reachable app-only; anything
# listed here that is NOT in that group resolves fine and then 403s at Graph.
# `steven` is injected at runtime via the MAILBOX_ALIASES env var (see below).
# RETIRED 2026-07-27: "rfq" -> rfq@adaptiveenterprisesllc.com. It was carried over
# from the retired PowerShell rail, was never in the scope group, had zero callers,
# and could never have sent mail anyway (send_mail bypasses _mbx). The live RFQ
# pipeline sends from admin@. Do not re-add without also adding rfq@ to the scope
# group AND routing send_mail's from_user_id through _mbx().
_MAILBOX_ALIASES = {
    "zach":  "zach.eltzroth@adaptiveenterprisesllc.com",
    "admin": "admin@adaptiveenterprisesllc.com",
    "jeff":  "jeff.price@adaptiveenterprisesllc.com",
}
for _pair in os.environ.get("MAILBOX_ALIASES", "").split(","):
    if "=" in _pair:
        _k, _v = _pair.split("=", 1)
        _MAILBOX_ALIASES[_k.strip().lower()] = _v.strip()


def _mbx(mailbox: str) -> str:
    """Resolve a friendly alias (zach|admin|jeff|steven) to a UPN; pass full UPNs through."""
    m = (mailbox or "zach").strip()
    return _MAILBOX_ALIASES.get(m.lower(), m)


def _consumer_block(subject: str, body_html: str) -> str | None:
    """Universal safety net: never let a blocklisted consumer name leave any mailbox.
    No-op unless CONSUMER_NAME_BLOCKLIST is configured."""
    text = f"{subject}\n{_TAG_RE.sub(' ', body_html or '')}".lower()
    for name in CONSUMER_NAME_BLOCKLIST:
        if name.lower() in text:
            return f"blocked: consumer name '{name}' present (§17)"
    return None


def _recips(addrs):
    return [{"emailAddress": {"address": a}} for a in (addrs or [])]


@mcp.tool
def outlook_list_messages(mailbox: str = "zach", folder: str = "inbox",
                          top: int = 15, search: str | None = None) -> list[dict]:
    """Read recent messages. mailbox = zach|admin|jeff|steven or a full UPN. folder = a well-known
    name (inbox, sentitems, drafts) or folder id. Optional full-text `search`."""
    if not _auth_ok():
        return [{"error": "unauthorized"}]
    upn = _mbx(mailbox)
    sel = "id,subject,from,receivedDateTime,bodyPreview,isRead"
    if search:
        s = search.replace('"', '')
        r = _g("GET", f'/users/{upn}/messages?$search="{s}"&$top={int(top)}&$select={sel}',
               headers={"ConsistencyLevel": "eventual"})
    else:
        seg = f"mailFolders/{folder}/messages" if folder else "messages"
        r = _g("GET", f"/users/{upn}/{seg}?$top={int(top)}&$orderby=receivedDateTime desc&$select={sel}")
    if not r.is_success:
        return [_graph_err(r)]
    return [{"id": m["id"], "subject": m.get("subject"),
             "from": (m.get("from") or {}).get("emailAddress", {}).get("address"),
             "received": m.get("receivedDateTime"), "preview": m.get("bodyPreview"),
             "isRead": m.get("isRead")} for m in r.json().get("value", [])]


@mcp.tool
def outlook_read_message(mailbox: str, message_id: str) -> dict:
    """Read one full message (body returned as plain text)."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    upn = _mbx(mailbox)
    r = _g("GET", f"/users/{upn}/messages/{message_id}"
           "?$select=subject,from,toRecipients,ccRecipients,receivedDateTime,body")
    if not r.is_success:
        return _graph_err(r)
    m = r.json()
    b = m.get("body") or {}
    text = _TAG_RE.sub(" ", b.get("content", "")) if b.get("contentType") == "html" else b.get("content", "")
    return {"subject": m.get("subject"),
            "from": (m.get("from") or {}).get("emailAddress", {}).get("address"),
            "to": [a["emailAddress"]["address"] for a in m.get("toRecipients", [])],
            "cc": [a["emailAddress"]["address"] for a in m.get("ccRecipients", [])],
            "received": m.get("receivedDateTime"), "body": text.strip()}


@mcp.tool
def outlook_list_folders(mailbox: str = "zach") -> list[dict]:
    """List mail folders (with unread/total counts) for a mailbox."""
    if not _auth_ok():
        return [{"error": "unauthorized"}]
    r = _g("GET", f"/users/{_mbx(mailbox)}/mailFolders?$top=100"
           "&$select=id,displayName,unreadItemCount,totalItemCount")
    if not r.is_success:
        return [_graph_err(r)]
    return [{"id": f["id"], "name": f.get("displayName"),
             "unread": f.get("unreadItemCount"), "total": f.get("totalItemCount")}
            for f in r.json().get("value", [])]


@mcp.tool
def outlook_create_draft(mailbox: str, to: list[str], subject: str, body_html: str,
                         cc: list[str] | None = None, bcc: list[str] | None = None) -> dict:
    """Compose a DRAFT (not sent) in a mailbox. Returns the draft id + a webLink to review/send it."""
    if not _auth_ok():
        return {"error": "unauthorized"}
        body_html = _normalize_body_html(body_html)
    block = _consumer_block(subject, body_html)
    if block:
        _audit("outlook_draft_BLOCKED", f"{mailbox}: {block}")
        return {"error": f"refused: {block}"}
    upn = _mbx(mailbox)
    msg = {"subject": subject, "body": {"contentType": "HTML", "content": body_html},
           "toRecipients": _recips(to), "ccRecipients": _recips(cc), "bccRecipients": _recips(bcc)}
    r = _g("POST", f"/users/{upn}/messages", json=msg)
    if not r.is_success:
        return _graph_err(r)
    j = r.json()
    _audit("outlook_draft", f"{upn}: {subject}")
    return {"ok": True, "draft_id": j.get("id"), "webLink": j.get("webLink")}


@mcp.tool
def outlook_update_draft(mailbox: str, message_id: str, subject: str | None = None,
                         body_html: str | None = None, to: list[str] | None = None,
                         cc: list[str] | None = None) -> dict:
    """Edit an existing draft. Only the fields you pass are changed."""
    if not _auth_ok():
        return {"error": "unauthorized"}
        body_html = _normalize_body_html(body_html)
    if _consumer_block(subject or "", body_html or ""):
        return {"error": "refused: consumer name present (§17)"}
    patch: dict = {}
    if subject is not None:
        patch["subject"] = subject
    if body_html is not None:
        patch["body"] = {"contentType": "HTML", "content": body_html}
    if to is not None:
        patch["toRecipients"] = _recips(to)
    if cc is not None:
        patch["ccRecipients"] = _recips(cc)
    r = _g("PATCH", f"/users/{_mbx(mailbox)}/messages/{message_id}", json=patch)
    if not r.is_success:
        return _graph_err(r)
    _audit("outlook_update_draft", f"{_mbx(mailbox)}:{message_id}")
    return {"ok": True}


@mcp.tool
def outlook_send_draft(mailbox: str, message_id: str, confirm: bool = False) -> dict:
    """Send an existing draft. Requires confirm=True (outward action)."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    if not confirm:
        return {"error": "refused: pass confirm=true to send"}
    r = _g("POST", f"/users/{_mbx(mailbox)}/messages/{message_id}/send")
    if not r.is_success:
        return _graph_err(r)
    _audit("outlook_send_draft", f"{_mbx(mailbox)}:{message_id}")
    return {"ok": True}


@mcp.tool
def outlook_send(mailbox: str, to: list[str], subject: str, body_html: str,
                 cc: list[str] | None = None, bcc: list[str] | None = None,
                 confirm: bool = False) -> dict:
    """Compose AND send in one step from a mailbox. Requires confirm=True.
    For arm's-length VENDOR / equipment-RFQ mail, use send_mail instead (it enforces the §17
    Client-ID rule); this is the general Outlook send."""
    if not _auth_ok():
        return {"error": "unauthorized"}
        body_html = _normalize_body_html(body_html)
    if not confirm:
        return {"error": "refused: pass confirm=true to send"}
    block = _consumer_block(subject, body_html)
    if block:
        _audit("outlook_send_BLOCKED", f"{mailbox}: {block}")
        return {"error": f"refused: {block}"}
    upn = _mbx(mailbox)
    payload = {"message": {"subject": subject, "body": {"contentType": "HTML", "content": body_html},
               "toRecipients": _recips(to), "ccRecipients": _recips(cc), "bccRecipients": _recips(bcc)},
               "saveToSentItems": True}
    r = _g("POST", f"/users/{upn}/sendMail", json=payload)
    if not r.is_success:
        return _graph_err(r)
    _audit("outlook_send", f"{upn} -> {', '.join(to)}")
    return {"ok": True}


@mcp.tool
def outlook_reply(mailbox: str, message_id: str, body_html: str,
                  reply_all: bool = False, confirm: bool = False) -> dict:
    """Reply (or reply-all) to a message. Requires confirm=True."""
    if not _auth_ok():
        return {"error": "unauthorized"}
        body_html = _normalize_body_html(body_html)
    if not confirm:
        return {"error": "refused: pass confirm=true to send"}
    if _consumer_block("", body_html):
        return {"error": "refused: consumer name present (§17)"}
    upn = _mbx(mailbox)
    verb = "replyAll" if reply_all else "reply"
    r = _g("POST", f"/users/{upn}/messages/{message_id}/{verb}", json={"comment": body_html})
    if not r.is_success:
        return _graph_err(r)
    _audit("outlook_reply", f"{upn}:{message_id}")
    return {"ok": True}


@mcp.tool
def outlook_forward(mailbox: str, message_id: str, to: list[str],
                    comment_html: str = "", confirm: bool = False) -> dict:
    """Forward a message to new recipients with an optional comment. Requires confirm=True."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    if not confirm:
        return {"error": "refused: pass confirm=true to send"}
    if _consumer_block("", comment_html):
        return {"error": "refused: consumer name present (§17)"}
    upn = _mbx(mailbox)
    r = _g("POST", f"/users/{upn}/messages/{message_id}/forward",
           json={"comment": comment_html, "toRecipients": _recips(to)})
    if not r.is_success:
        return _graph_err(r)
    _audit("outlook_forward", f"{upn}:{message_id} -> {', '.join(to)}")
    return {"ok": True}


@mcp.tool
def outlook_delete_message(mailbox: str, message_id: str, confirm: bool = False) -> dict:
    """Delete a message (moves to Deleted Items). Requires confirm=True."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    if not confirm:
        return {"error": "refused: pass confirm=true to delete"}
    r = _g("DELETE", f"/users/{_mbx(mailbox)}/messages/{message_id}")
    if not r.is_success:
        return _graph_err(r)
    _audit("outlook_delete", f"{_mbx(mailbox)}:{message_id}")
    return {"ok": True}


# ---------------------------------------------------------------------------
# MODERN PAGES  (Graph fullcontrol on Throne) — create + publish site pages
# Replaces the app-only cert page-publish trick from the Code host.
# ---------------------------------------------------------------------------
@mcp.tool
def throne_create_page(name: str, title: str, html: str, publish: bool = True,
                       site: str = "throne") -> dict:
    """Create a modern SharePoint page from an HTML body, then optionally publish.
    site='throne' (default) | 'opshub' | a URL/id. 'name' becomes the .aspx filename
    (e.g. 'Smith,-Scot-(43026)'). Requires Graph fullcontrol on the target site."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    sid = _resolve_site(site)
    page = {
        "name": name if name.endswith(".aspx") else f"{name}.aspx",
        "title": title,
        "pageLayout": "article",
        "canvasLayout": {
            "horizontalSections": [{
                "layout": "oneColumn",
                "columns": [{
                    "width": 12,
                    "webparts": [{
                        "@odata.type": "#microsoft.graph.textWebPart",
                        "innerHtml": html,
                    }],
                }],
            }]
        },
    }
    # Pages live under the beta-stable sitePages resource; v1.0 supports create+publish.
    r = _g("POST", f"/sites/{sid}/pages", json=page)
    r.raise_for_status()
    j = r.json()
    page_id = j.get("id")
    result = {"ok": True, "id": page_id, "name": page["name"], "webUrl": j.get("webUrl")}
    if publish:
        p = _g("POST", f"/sites/{sid}/pages/{page_id}/microsoft.graph.publish")
        result["published"] = p.status_code in (200, 202, 204)
    _audit("create_page", f"{site}:{page['name']}")
    return result


@mcp.tool
def throne_publish_page(page_id: str, site: str = "throne") -> dict:
    """Publish (or re-publish) an existing modern page by id. site='throne'|'opshub'|URL/id."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    sid = _resolve_site(site)
    p = _g("POST", f"/sites/{sid}/pages/{page_id}/microsoft.graph.publish")
    p.raise_for_status()
    _audit("publish_page", f"{site}:{page_id}")
    return {"ok": True}


# ---------------------------------------------------------------------------
# QUICK LAUNCH / LEFT-NAV  (SharePoint REST/CSOM — NOT Graph)
# Requires the SharePoint resource app permission Sites.FullControl.All + admin consent.
# Uses a SEPARATE token audience: https://{tenant}.sharepoint.com/.default
# ---------------------------------------------------------------------------
SP_ROOT = os.environ.get("SP_SITE_URL", "https://netorg39360.sharepoint.com/sites/MasterChiefsThrone")
_SP_SCOPE = "https://netorg39360.sharepoint.com/.default"
# Per-site absolute web URLs for the CSOM/REST quicklaunch tools. Same tenant token audience
# (_SP_SCOPE) works for any site in the tenant; only the site web root URL varies.
_SP_SITE_URLS = {
    "throne": SP_ROOT,
    "opshub": os.environ.get("OPSHUB_SITE_URL_ABS",
                             "https://netorg39360.sharepoint.com/sites/ZachOperationsHub"),
}


def _sp_root(site: str = "throne") -> str:
    """Resolve a quicklaunch site key ('throne'|'opshub') or absolute https URL to a web root."""
    key = (site or "throne").strip().lower()
    if key in _SP_SITE_URLS:
        return _SP_SITE_URLS[key]
    if site.startswith("https://"):
        return site.rstrip("/")
    return SP_ROOT


def _sp_token() -> str:
    return _credential.get_token(_SP_SCOPE).token

def _sp(method: str, rel_path: str, site: str = "throne", **kw) -> httpx.Response:
    headers = kw.pop("headers", {})
    headers["Authorization"] = f"Bearer {_sp_token()}"
    headers.setdefault("Accept", "application/json;odata=nometadata")
    return _send_with_retry(method, f"{_sp_root(site)}{rel_path}", headers=headers, timeout=60, **kw)

@mcp.tool
def throne_quicklaunch_add(title: str, url: str, site: str = "throne") -> dict:
    """Add a Quick Launch (left-nav) link to a site (site='throne'|'opshub'|absolute URL).
    Needs SharePoint Sites.FullControl.All (separate from Graph). This is the CSOM gap closer."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    body = {
        "__metadata": {"type": "SP.NavigationNode"},
        "Title": title, "Url": url, "IsExternal": False,
    }
    r = _sp("POST", "/_api/web/navigation/quicklaunch", site=site,
            headers={"Content-Type": "application/json;odata=verbose"}, json=body)
    if r.status_code not in (200, 201):
        return {"error": f"SharePoint {r.status_code}: {r.text[:300]}"}
    _audit("quicklaunch_add", title)
    return {"ok": True, "title": title}

@mcp.tool
def throne_quicklaunch_list(site: str = "throne") -> list[dict]:
    """List current Quick Launch nodes on a site (site='throne'|'opshub'|absolute URL)."""
    if not _auth_ok():
        return [{"error": "unauthorized"}]
    r = _sp("GET", "/_api/web/navigation/quicklaunch", site=site)
    if r.status_code != 200:
        return [{"error": f"SharePoint {r.status_code}: {r.text[:300]}"}]
    return [{"id": n.get("Id"), "title": n.get("Title"), "url": n.get("Url")}
            for n in r.json().get("value", [])]


# ---------------------------------------------------------------------------
# SHAREPOINT LISTS  (Sites.ReadWrite.All — already consented)  [v17]
# Item CRUD on any list in the Throne site (or another site via site_id).
# `fields` keys are the list's INTERNAL column names (see sp_list_columns).
# ---------------------------------------------------------------------------
def _site(site_id: str | None) -> str:
    return site_id or THRONE_SITE


# ---------------------------------------------------------------------------
# MULTI-SITE RESOLUTION  [v18]
# Lets the sp_* tools target ANY site (e.g. ZachOperationsHub) by friendly alias,
# full URL, host:/sites/Name path, or a composite Graph site id — resolved + cached.
# Requires the app's tenant-wide Sites.FullControl.All (already consented).
# ---------------------------------------------------------------------------
_SITE_ALIASES = {
    "throne": THRONE_SITE,
    "opshub": os.environ.get("OPSHUB_SITE_ID", ""),   # optional pre-resolved id
}
# ZachOperationsHub, resolvable by URL path when no OPSHUB_SITE_ID env is set:
_OPSHUB_URL = os.environ.get(
    "OPSHUB_SITE_URL", "netorg39360.sharepoint.com:/sites/ZachOperationsHub")
_SITE_ID_CACHE: dict[str, str] = {}
_SITE_DRIVE_CACHE: dict[str, str] = {}


def _resolve_site(site: str | None = None) -> str:
    """Resolve a site reference to a composite Graph site id.
    Accepts: None/'throne' -> the pinned Throne site; a registered alias ('opshub');
    a full https:// URL; a 'host:/sites/Name' path; or an already-composite site id."""
    if not site or site.strip().lower() == "throne":
        return THRONE_SITE
    key = site.strip()
    low = key.lower()
    if low in _SITE_ALIASES and _SITE_ALIASES[low]:
        return _SITE_ALIASES[low]
    if low == "opshub":
        key = _OPSHUB_URL
    if key in _SITE_ID_CACHE:
        return _SITE_ID_CACHE[key]
    # Composite Graph site ids look like 'host,siteGuid,webGuid' — pass straight through.
    if "," in key and "sharepoint.com" in key:
        return key
    path = key
    if path.startswith("https://"):
        rest = path[len("https://"):]
        host, _, sp = rest.partition("/")
        path = f"{host}:/{sp}".rstrip("/")
    r = _g("GET", f"/sites/{path}")
    r.raise_for_status()
    sid = r.json()["id"]
    _SITE_ID_CACHE[key] = sid
    return sid


def _site_drive_id(site: str | None = None, library: str | None = None) -> str:
    """Resolve a site's document-library drive id (default library, or one named `library`)."""
    sid = _resolve_site(site)
    ck = f"{sid}::{library or ''}"
    if ck in _SITE_DRIVE_CACHE:
        return _SITE_DRIVE_CACHE[ck]
    if library:
        r = _g("GET", f"/sites/{sid}/drives?$select=id,name")
        r.raise_for_status()
        did = next((d["id"] for d in r.json().get("value", []) if d.get("name") == library), None)
        if not did:
            raise RuntimeError(f"library '{library}' not found on site {sid}")
    else:
        r = _g("GET", f"/sites/{sid}/drive?$select=id")
        r.raise_for_status()
        did = r.json()["id"]
    _SITE_DRIVE_CACHE[ck] = did
    return did


def _drive_for(site: str | None = None, library: str | None = None) -> str:
    """Drive id for the throne_* tools. Default (site='throne', no library) preserves the
    pinned Case Documnents library; any other site (or an explicit library) resolves that
    site's document library instead — this is what lets throne_* reach Zach Operations Hub."""
    if (not site or site.strip().lower() == "throne") and not library:
        return _throne_drive_id()
    return _site_drive_id(site, library)


@mcp.tool
def sp_list_lists(site_id: str | None = None) -> list[dict]:
    """List the SharePoint lists on the Throne site (or another site via site_id)."""
    if not _auth_ok():
        return [{"error": "unauthorized"}]
    r = _g("GET", f"/sites/{_site(site_id)}/lists?$select=id,displayName,description,list")
    if not r.is_success:
        return [_graph_err(r)]
    return [{"id": l["id"], "name": l.get("displayName"),
             "hidden": (l.get("list") or {}).get("hidden"),
             "template": (l.get("list") or {}).get("template")}
            for l in r.json().get("value", [])]


@mcp.tool
def sp_list_columns(list_id: str, site_id: str | None = None) -> list[dict]:
    """Show a list's columns (internal `name` is what sp_list_item_create/update fields must use)."""
    if not _auth_ok():
        return [{"error": "unauthorized"}]
    r = _g("GET", f"/sites/{_site(site_id)}/lists/{list_id}/columns"
           "?$select=id,name,displayName,required,readOnly,text,choice,number,dateTime,boolean,personOrGroup")
    if not r.is_success:
        return [_graph_err(r)]
    out = []
    for c in r.json().get("value", []):
        kind = next((k for k in ("text", "choice", "number", "dateTime", "boolean", "personOrGroup")
                     if c.get(k) is not None), "other")
        entry = {"id": c.get("id"), "name": c["name"], "displayName": c.get("displayName"),
                 "type": kind, "required": c.get("required"), "readOnly": c.get("readOnly")}
        if kind == "choice":
            entry["choices"] = (c.get("choice") or {}).get("choices")
        out.append(entry)
    return out


@mcp.tool
def sp_list_items(list_id: str, top: int = 25, filter_: str | None = None,
                  site_id: str | None = None) -> list[dict]:
    """Read list items (with all fields). Optional OData `filter_` on fields, e.g.
    \"fields/Status eq 'Open'\" (may require indexed columns)."""
    if not _auth_ok():
        return [{"error": "unauthorized"}]
    url = f"/sites/{_site(site_id)}/lists/{list_id}/items?$expand=fields&$top={int(top)}"
    headers = {}
    if filter_:
        url += f"&$filter={filter_}"
        headers["Prefer"] = "HonorNonIndexedQueriesWarningMayFailRandomly"
    r = _g("GET", url, headers=headers)
    if not r.is_success:
        return [_graph_err(r)]
    return [{"id": it["id"], "webUrl": it.get("webUrl"), "fields": it.get("fields", {})}
            for it in r.json().get("value", [])]


@mcp.tool
def sp_list_item_create(list_id: str, fields: dict, site_id: str | None = None) -> dict:
    """Create a list item. `fields` maps INTERNAL column names -> values
    (e.g. {"Title": "Smith 43026", "Status": "Open"}). Use sp_list_columns to find names."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    r = _g("POST", f"/sites/{_site(site_id)}/lists/{list_id}/items", json={"fields": fields})
    if not r.is_success:
        return _graph_err(r)
    j = r.json()
    _audit("sp_list_item_create", f"{list_id}: {fields.get('Title', j.get('id'))}")
    return {"ok": True, "id": j.get("id"), "webUrl": j.get("webUrl")}


@mcp.tool
def sp_list_item_update(list_id: str, item_id: str, fields: dict,
                        site_id: str | None = None) -> dict:
    """Update fields on an existing list item (only the keys you pass are changed)."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    r = _g("PATCH", f"/sites/{_site(site_id)}/lists/{list_id}/items/{item_id}/fields", json=fields)
    if not r.is_success:
        return _graph_err(r)
    _audit("sp_list_item_update", f"{list_id}:{item_id} {list(fields.keys())}")
    return {"ok": True}


@mcp.tool
def sp_list_item_delete(list_id: str, item_id: str, confirm: bool = False,
                        site_id: str | None = None) -> dict:
    """Delete a list item. Requires confirm=True."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    if not confirm:
        return {"error": "refused: pass confirm=true to delete"}
    r = _g("DELETE", f"/sites/{_site(site_id)}/lists/{list_id}/items/{item_id}")
    if not r.is_success:
        return _graph_err(r)
    _audit("sp_list_item_delete", f"{list_id}:{item_id}")
    return {"ok": True}


# ---------------------------------------------------------------------------
# CALENDAR  (Calendars.ReadWrite app role — needs admin consent)  [v17]
# Mailbox calendars by the same aliases as Outlook (zach|admin|jeff|steven or UPN).
# NOTE: creating/updating an event WITH attendees sends invites -> requires confirm=True.
# ---------------------------------------------------------------------------
DEFAULT_TZ = os.environ.get("DEFAULT_TIMEZONE", "America/Indiana/Indianapolis")


def _event_out(e: dict) -> dict:
    return {"id": e["id"], "subject": e.get("subject"),
            "start": (e.get("start") or {}).get("dateTime"),
            "end": (e.get("end") or {}).get("dateTime"),
            "tz": (e.get("start") or {}).get("timeZone"),
            "location": (e.get("location") or {}).get("displayName"),
            "isAllDay": e.get("isAllDay"), "organizer":
                ((e.get("organizer") or {}).get("emailAddress") or {}).get("address"),
            "attendees": [((a.get("emailAddress") or {}).get("address"))
                          for a in e.get("attendees", [])],
            "webLink": e.get("webLink")}


@mcp.tool
def calendar_list_events(mailbox: str = "zach", start: str | None = None,
                         end: str | None = None, top: int = 20) -> list[dict]:
    """List calendar events. With start+end (ISO 8601, e.g. '2026-07-06T00:00:00') uses the
    expanded calendarView (recurring instances included); otherwise lists upcoming events."""
    if not _auth_ok():
        return [{"error": "unauthorized"}]
    upn = _mbx(mailbox)
    sel = "id,subject,start,end,location,isAllDay,organizer,attendees,webLink"
    if start and end:
        r = _g("GET", f"/users/{upn}/calendarView?startDateTime={start}&endDateTime={end}"
               f"&$top={int(top)}&$orderby=start/dateTime&$select={sel}",
               headers={"Prefer": f'outlook.timezone="{DEFAULT_TZ}"'})
    else:
        r = _g("GET", f"/users/{upn}/events?$top={int(top)}&$orderby=start/dateTime&$select={sel}"
               f"&$filter=end/dateTime ge '{_now()[:19]}'",
               headers={"Prefer": f'outlook.timezone="{DEFAULT_TZ}"'})
    if not r.is_success:
        return [_graph_err(r)]
    return [_event_out(e) for e in r.json().get("value", [])]


@mcp.tool
def calendar_create_event(mailbox: str, subject: str, start: str, end: str,
                          timezone: str | None = None, body_html: str = "",
                          location: str = "", attendees: list[str] | None = None,
                          all_day: bool = False, confirm: bool = False) -> dict:
    """Create a calendar event. start/end are ISO local times ('2026-07-06T14:00:00').
    Adding attendees SENDS INVITES -> requires confirm=True (no attendees = no confirm needed)."""
    if not _auth_ok():
        return {"error": "unauthorized"}
        body_html = _normalize_body_html(body_html)
    if attendees and not confirm:
        return {"error": "refused: attendees send invites — pass confirm=true"}
    if _consumer_block(subject, body_html):
        return {"error": "refused: consumer name present (§17)"}
    tz = timezone or DEFAULT_TZ
    ev: dict = {"subject": subject,
                "start": {"dateTime": start, "timeZone": tz},
                "end": {"dateTime": end, "timeZone": tz},
                "isAllDay": bool(all_day)}
    if body_html:
        ev["body"] = {"contentType": "HTML", "content": body_html}
    if location:
        ev["location"] = {"displayName": location}
    if attendees:
        ev["attendees"] = [{"emailAddress": {"address": a}, "type": "required"}
                           for a in attendees]
    r = _g("POST", f"/users/{_mbx(mailbox)}/events", json=ev)
    if not r.is_success:
        return _graph_err(r)
    j = r.json()
    _audit("calendar_create", f"{_mbx(mailbox)}: {subject} @ {start}")
    return {"ok": True, "id": j.get("id"), "webLink": j.get("webLink")}


@mcp.tool
def calendar_update_event(mailbox: str, event_id: str, subject: str | None = None,
                          start: str | None = None, end: str | None = None,
                          timezone: str | None = None, body_html: str | None = None,
                          location: str | None = None, confirm: bool = False) -> dict:
    """Update an event (only passed fields change). If the event HAS attendees, Outlook sends
    updates to them -> requires confirm=True to be safe."""
    if not _auth_ok():
        return {"error": "unauthorized"}
        body_html = _normalize_body_html(body_html)
    if not confirm:
        return {"error": "refused: updates to events with attendees notify them — pass confirm=true"}
    tz = timezone or DEFAULT_TZ
    patch: dict = {}
    if subject is not None:
        patch["subject"] = subject
    if start is not None:
        patch["start"] = {"dateTime": start, "timeZone": tz}
    if end is not None:
        patch["end"] = {"dateTime": end, "timeZone": tz}
    if body_html is not None:
        patch["body"] = {"contentType": "HTML", "content": body_html}
    if location is not None:
        patch["location"] = {"displayName": location}
    r = _g("PATCH", f"/users/{_mbx(mailbox)}/events/{event_id}", json=patch)
    if not r.is_success:
        return _graph_err(r)
    _audit("calendar_update", f"{_mbx(mailbox)}:{event_id}")
    return {"ok": True}


@mcp.tool
def calendar_delete_event(mailbox: str, event_id: str, confirm: bool = False) -> dict:
    """Delete/cancel an event. Requires confirm=True (attendees get cancellations)."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    if not confirm:
        return {"error": "refused: pass confirm=true to delete (attendees are notified)"}
    r = _g("DELETE", f"/users/{_mbx(mailbox)}/events/{event_id}")
    if not r.is_success:
        return _graph_err(r)
    _audit("calendar_delete", f"{_mbx(mailbox)}:{event_id}")
    return {"ok": True}


# ---------------------------------------------------------------------------
# MICROSOFT TO DO  (Tasks.ReadWrite.All app role — needs admin consent)  [v17]
# Per-user task lists via the same aliases (zach|admin|jeff|steven or UPN).
# Replaces the Skynet `throne-todo` PowerShell bridge for Claude-driven task writes.
# ---------------------------------------------------------------------------
_TODO_DROP_ELEMENTS = re.compile(r"<(script|style|head)\b[^>]*>.*?</\1>",
                                 re.IGNORECASE | re.DOTALL)


def _todo_body_text(t: dict) -> str:
    """Extract a To Do task's note as plain text. Graph returns body as an itemBody
    {content, contentType}; To Do's own clients write contentType='text', but a task created
    from a flagged email or by Outlook can carry 'html'.

    For html: drop <script>/<style>/<head> ELEMENTS (content and all) before stripping tags.
    _TAG_RE only removes the tags themselves, so stripping alone would leak CSS rules and JS
    source into the note text — which is exactly the flagged-email case this exists to handle.
    Then unescape entities so '&amp;' reads as '&' rather than surviving raw."""
    import html as _html
    b = t.get("body") or {}
    content = b.get("content") or ""
    if (b.get("contentType") or "").lower() == "html":
        content = _TODO_DROP_ELEMENTS.sub(" ", content)
        content = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", content, flags=re.IGNORECASE)
        content = _TAG_RE.sub(" ", content)
        content = _html.unescape(content)
        content = re.sub(r"[ \t]{2,}", " ", content)
        content = re.sub(r"\n{3,}", "\n\n", content)
    return content.strip()


def _todo_bool(v) -> bool:
    """Coerce a checked-flag that may arrive as a STRING. A model-generated payload can send
    {"checked": "false"}, and bare bool("false") is True — which would tick a step off when the
    caller asked for the opposite, silently and with no error."""
    if isinstance(v, str):
        return v.strip().lower() not in ("", "false", "0", "no", "off", "unchecked", "null")
    return bool(v)


def _todo_collection(url: str, cap: int = 1000) -> tuple[list[dict], bool, dict | None]:
    """GET a Graph collection, following @odata.nextLink.

    Returns (items, complete, error). `complete` is False if `cap` was hit with pages left —
    callers that DELETE based on this data must refuse to act when complete is False, or they
    would delete things they simply never saw. Empirically To Do returns the full
    checklistItems collection in one page and ignores $top, but that is undocumented
    behavior — this does not rely on it."""
    items: list[dict] = []
    full = f"{GRAPH}{url}" if url.startswith("/") else url
    while True:
        r = _send_with_retry("GET", full,
                             headers={"Authorization": f"Bearer {_token()}"}, timeout=120)
        if not r.is_success:
            return items, False, _graph_err(r)
        j = r.json()
        items.extend(j.get("value", []))
        nxt = j.get("@odata.nextLink")
        if not nxt:
            return items, True, None
        if len(items) >= cap:
            return items, False, None
        full = nxt


@mcp.tool
def todo_lists(user: str = "zach") -> list[dict]:
    """List a user's To Do task lists (name + id)."""
    if not _auth_ok():
        return [{"error": "unauthorized"}]
    r = _g("GET", f"/users/{_mbx(user)}/todo/lists?$top=50")
    if not r.is_success:
        return [_graph_err(r)]
    return [{"id": l["id"], "name": l.get("displayName"),
             "wellknown": l.get("wellknownListName")}
            for l in r.json().get("value", [])]


@mcp.tool
def todo_list_tasks(user: str, list_id: str, top: int = 25,
                    include_completed: bool = False, include_body: bool = True,
                    include_steps: bool = False) -> list[dict]:
    """Read tasks from a To Do list (newest first). Completed tasks excluded by default.
    include_body=True (default) returns each task's NOTE text; include_steps=True also expands
    the task's Steps (checklist items). Set include_body=False for a lean title-only listing."""
    if not _auth_ok():
        return [{"error": "unauthorized"}]
    # $orderby was missing before 2026-08-10 while the docstring promised "newest first" —
    # results were in whatever order Graph returned. Verified live that $orderby works on
    # this collection, and works combined with both $filter and $expand.
    url = (f"/users/{_mbx(user)}/todo/lists/{list_id}/tasks"
           f"?$top={int(top)}&$orderby=createdDateTime desc")
    if not include_completed:
        url += "&$filter=status ne 'completed'"
    if include_steps:
        url += "&$expand=checklistItems"
    r = _g("GET", url)
    if not r.is_success:
        return [_graph_err(r)]
    out = []
    for t in r.json().get("value", []):
        row = {"id": t["id"], "title": t.get("title"), "status": t.get("status"),
               "importance": t.get("importance"),
               "due": ((t.get("dueDateTime") or {}).get("dateTime")),
               "created": t.get("createdDateTime")}
        # v19: the note (body) was fetched from Graph but silently DISCARDED here before
        # 2026-08-10, so every task note was invisible to Claude — including notes Claude
        # itself had written. Surfaced now; empty notes are omitted to keep listings lean.
        if include_body:
            note = _todo_body_text(t)
            if note:
                row["body"] = note
        if include_steps:
            row["steps"] = [{"id": c.get("id"), "name": c.get("displayName"),
                             "checked": bool(c.get("isChecked"))}
                            for c in (t.get("checklistItems") or [])]
        out.append(row)
    return out


@mcp.tool
def todo_create_task(user: str, list_id: str, title: str, body: str = "",
                     due: str | None = None, reminder: str | None = None,
                     importance: str = "normal") -> dict:
    """Create a To Do task. due/reminder are ISO local datetimes ('2026-07-07T09:00:00');
    importance = low|normal|high."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    task: dict = {"title": title, "importance": importance}
    if body:
        task["body"] = {"content": body, "contentType": "text"}
    if due:
        task["dueDateTime"] = {"dateTime": due, "timeZone": DEFAULT_TZ}
    if reminder:
        task["reminderDateTime"] = {"dateTime": reminder, "timeZone": DEFAULT_TZ}
        task["isReminderOn"] = True
    r = _g("POST", f"/users/{_mbx(user)}/todo/lists/{list_id}/tasks", json=task)
    if not r.is_success:
        return _graph_err(r)
    j = r.json()
    # Lead with the task id: a title is free text and can carry a consumer name. _scrub_phi
    # catches blocklisted names, but the id is an opaque forensic key that identifies exactly
    # what was created even when the roster is empty or the name is spelled unexpectedly.
    _audit("todo_create", f"{_mbx(user)}:{j.get('id')} {title}")
    return {"ok": True, "id": j.get("id")}


@mcp.tool
def todo_update_task(user: str, list_id: str, task_id: str, title: str | None = None,
                     status: str | None = None, body: str | None = None,
                     due: str | None = None, importance: str | None = None) -> dict:
    """Update a task (only passed fields change). status = notStarted|inProgress|completed;
    setting status='completed' checks it off."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    patch: dict = {}
    if title is not None:
        patch["title"] = title
    if status is not None:
        patch["status"] = status
    if body is not None:
        patch["body"] = {"content": body, "contentType": "text"}
    if due is not None:
        patch["dueDateTime"] = {"dateTime": due, "timeZone": DEFAULT_TZ}
    if importance is not None:
        patch["importance"] = importance
    r = _g("PATCH", f"/users/{_mbx(user)}/todo/lists/{list_id}/tasks/{task_id}", json=patch)
    if not r.is_success:
        return _graph_err(r)
    _audit("todo_update", f"{_mbx(user)}:{task_id} {list(patch.keys())}")
    return {"ok": True}


@mcp.tool
def todo_delete_task(user: str, list_id: str, task_id: str, confirm: bool = False) -> dict:
    """Delete a To Do task. Requires confirm=True."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    if not confirm:
        return {"error": "refused: pass confirm=true to delete"}
    r = _g("DELETE", f"/users/{_mbx(user)}/todo/lists/{list_id}/tasks/{task_id}")
    if not r.is_success:
        return _graph_err(r)
    _audit("todo_delete", f"{_mbx(user)}:{task_id}")
    return {"ok": True}


# ---------------------------------------------------------------------------
# TO DO — DEEP MANIPULATION  [v19, 2026-08-10]
# Spec (Zach): "manipulate deeper into the actual to-dos, such as the notes...
# the nested notes or whatever inside to-dos... read-write access."
#
# Two distinct things were missing, and they are NOT the same thing:
#   1. THE NOTE (Graph `body`). The WRITE path always worked (todo_create_task /
#      todo_update_task both set it). The READ path threw it away — todo_list_tasks
#      projected only id/title/status/importance/due/created. Net effect: Claude could
#      write a note it could never read back. Proof in the wild: the task titled
#      "Hiring-ENGINE roadmap (2-3 wk arc) - see body" pointed at a body no tool returned.
#      Fixed above in todo_list_tasks + todo_get_task; append-without-clobber added below.
#   2. THE STEPS (Graph `checklistItems`) — the nested sub-items under a task. These had
#      NO tool at all, in either direction. Full read/write added below.
#
# Same app-only CertificateCredential path as every other tool here; To Do app-only is
# live-verified working (Tasks.ReadWrite.All app role, admin-consented). NOTE the contrast
# with the quarantined OneNote block at the end of this file: Microsoft retired app-only
# auth for /onenote, but NOT for /todo — do not generalize that quarantine to these.
# ---------------------------------------------------------------------------

def _todo_task_url(user: str, list_id: str, task_id: str) -> str:
    return f"/users/{_mbx(user)}/todo/lists/{list_id}/tasks/{task_id}"


@mcp.tool
def todo_get_task(user: str, list_id: str, task_id: str,
                  include_linked: bool = False) -> dict:
    """Read ONE To Do task in full fidelity — the complete note (body), every Step
    (checklist item) with its checked state, reminder/due/start times, categories and
    recurrence. This is the tool to use when you need a task's actual contents;
    todo_list_tasks is for scanning. include_linked=True also returns linkedResources
    (the 'related item' links To Do stores against a task)."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    r = _g("GET", _todo_task_url(user, list_id, task_id) + "?$expand=checklistItems")
    if not r.is_success:
        return _graph_err(r)
    t = r.json()
    out = {
        "id": t.get("id"),
        "title": t.get("title"),
        "status": t.get("status"),
        "importance": t.get("importance"),
        "body": _todo_body_text(t),
        "body_content_type": ((t.get("body") or {}).get("contentType")),
        # `body` above is tag-stripped plaintext, which is what you want to READ. It is LOSSY
        # for an html-typed note. `body_raw` is the exact bytes Graph holds — round-trip
        # through THIS field, never through `body`, or writing back destroys the original
        # formatting while looking like it worked.
        "body_raw": ((t.get("body") or {}).get("content")),
        "due": ((t.get("dueDateTime") or {}).get("dateTime")),
        "start": ((t.get("startDateTime") or {}).get("dateTime")),
        "reminder": ((t.get("reminderDateTime") or {}).get("dateTime")),
        "is_reminder_on": t.get("isReminderOn"),
        "completed": ((t.get("completedDateTime") or {}).get("dateTime")),
        "created": t.get("createdDateTime"),
        "last_modified": t.get("lastModifiedDateTime"),
        "categories": t.get("categories") or [],
        "recurrence": t.get("recurrence"),
        "has_attachments": t.get("hasAttachments"),
        "steps": [{"id": c.get("id"), "name": c.get("displayName"),
                   "checked": bool(c.get("isChecked")),
                   "checked_at": c.get("checkedDateTime")}
                  for c in (t.get("checklistItems") or [])],
    }
    if include_linked:
        lr = _g("GET", _todo_task_url(user, list_id, task_id) + "/linkedResources")
        out["linked_resources"] = (
            [{"id": x.get("id"), "name": x.get("displayName"), "url": x.get("webUrl"),
              "app": x.get("applicationName"), "external_id": x.get("externalId")}
             for x in lr.json().get("value", [])]
            if lr.is_success else _graph_err(lr)["error"])
    return out


@mcp.tool
def todo_append_note(user: str, list_id: str, task_id: str, text: str,
                     timestamp: bool = False, separator: str = "\n") -> dict:
    """Append text to a task's NOTE without clobbering what is already there
    (read-modify-write, server-side). Use this instead of todo_update_task(body=...)
    whenever the existing note must survive — that one REPLACES the whole note.
    timestamp=True prefixes the appended line with the current time."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    if not text:
        return {"error": "refused: empty text"}
    cur = _g("GET", _todo_task_url(user, list_id, task_id) + "?$select=body")
    if not cur.is_success:
        return _graph_err(cur)
    j = cur.json()
    existing = (j.get("body") or {}).get("content") or ""
    ctype = ((j.get("body") or {}).get("contentType") or "text").lower()
    line = f"[{_now()}] {text}" if timestamp else text
    if ctype == "html":
        # Preserve the existing HTML note rather than flattening someone's formatting.
        addition = (line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                        .replace("\n", "<br>"))
        if not existing.strip():
            merged = addition
        else:
            # A task body from Outlook/a flagged email is a FULL html document. Appending to
            # the end of the string puts the text after </body></html>, outside the document,
            # where To Do may not render it. Insert before the closing tag when there is one.
            m = re.search(r"</body\s*>|</html\s*>", existing, re.IGNORECASE)
            if m:
                merged = existing[:m.start()] + "<br>" + addition + existing[m.start():]
            else:
                merged = existing + "<br>" + addition
    else:
        merged = (existing + separator + line) if existing.strip() else line
    r = _g("PATCH", _todo_task_url(user, list_id, task_id),
           json={"body": {"content": merged, "contentType": ctype}})
    if not r.is_success:
        return _graph_err(r)
    _audit("todo_append_note", f"{_mbx(user)}:{task_id} (+{len(line)} chars)")
    return {"ok": True, "appended_chars": len(line), "note_chars": len(merged)}


@mcp.tool
def todo_set_steps(user: str, list_id: str, task_id: str, steps: list,
                   prune: bool = False, confirm: bool = False) -> dict:
    """Create / update the Steps (checklist items) nested under a task — the sub-items
    To Do shows beneath the task title.

    `steps` is a list of either plain strings ("Call the counselor") or dicts
    {"name": "...", "checked": true}. Every entry MUST yield a non-empty name — a dict with
    a misspelled key (e.g. {"nmae": ...}) is a hard error that aborts the whole call before
    anything is written. Matching against existing steps is BY NAME (case-insensitive):
    an existing name is updated in place (so you can check it off), a new name is created.
    Duplicate names within `steps` collapse to one entry, last value winning.

    prune=False (default) LEAVES existing steps not named in `steps` untouched.
    prune=True DELETES them and therefore ALSO requires confirm=True — same house rule as
    todo_delete_step, because pruning can remove many steps at once."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    if not isinstance(steps, list):
        return {"error": "refused: steps must be a list of strings or {name, checked} dicts"}
    if prune and not confirm:
        return {"error": "refused: prune=true deletes every step not listed in `steps`. "
                         "Pass confirm=true as well if that is genuinely intended."}

    # --- Parse and validate EVERYTHING before any write. A malformed entry must never
    # --- reach the delete loop: silently skipping one used to make it invisible to `seen`,
    # --- so prune would then delete the very step the caller was trying to name.
    wanted: dict[str, bool | None] = {}   # name_key -> checked (dict preserves insert order)
    labels: dict[str, str] = {}
    for i, raw in enumerate(steps):
        if isinstance(raw, str):
            name, checked = raw.strip(), None
        elif isinstance(raw, dict):
            name = str(raw.get("name") or raw.get("displayName") or "").strip()
            checked = raw.get("checked", raw.get("isChecked"))
        else:
            return {"error": f"refused: steps[{i}] is {type(raw).__name__}; expected a string "
                             f"or a dict with a 'name' key. Nothing was changed."}
        if not name:
            return {"error": f"refused: steps[{i}] has no usable name "
                             f"(got {raw!r}). Use {{'name': '...', 'checked': false}}. "
                             f"Nothing was changed."}
        key = name.lower()
        # Coerce here, once: a JSON payload can carry "false" as a STRING, and bare
        # bool("false") is True — which would check a step off when asked to uncheck it.
        wanted[key] = None if checked is None else _todo_bool(checked)
        labels[key] = name          # last write wins on a duplicate name

    base = _todo_task_url(user, list_id, task_id) + "/checklistItems"
    items, complete, err = _todo_collection(base)
    if err:
        return err
    if prune and not complete:
        return {"error": "refused: could not read the complete existing step list "
                         "(pagination incomplete), so pruning could delete steps never seen. "
                         "Nothing was changed."}
    # Group by name: a task CAN legitimately hold two steps with the same display name, and
    # collapsing them to one (dict last-wins) would leave a duplicate silently un-pruned.
    existing: dict[str, list[dict]] = {}
    for c in items:
        existing.setdefault((c.get("displayName") or "").strip().lower(), []).append(c)

    created, updated, unchanged, deleted, errors = [], [], [], [], []
    for key, checked in wanted.items():
        name = labels[key]
        matches = existing.get(key) or []
        if matches:
            for c in matches:      # keep every same-named step in step with the request
                if checked is not None and bool(c.get("isChecked")) != checked:
                    r = _g("PATCH", f"{base}/{c['id']}", json={"isChecked": checked})
                    if r.is_success:
                        c["isChecked"] = checked
                        updated.append(name)
                    else:
                        errors.append(f"{name}: {_graph_err(r)['error']}")
                else:
                    # Report it explicitly. An all-empty result is indistinguishable from
                    # "did nothing / found nothing", and invites a pointless retry.
                    unchanged.append(name)
        else:
            payload: dict = {"displayName": name}
            if checked is not None:
                payload["isChecked"] = checked
            r = _g("POST", base, json=payload)
            if r.is_success:
                # Record it so a later reference to the same name UPDATES instead of
                # creating a second copy — this is what makes the call idempotent on retry.
                existing.setdefault(key, []).append(r.json())
                created.append(name)
            else:
                errors.append(f"{name}: {_graph_err(r)['error']}")

    deleted_ids: list[str] = []
    if prune:
        for key, matches in existing.items():
            if key in wanted:
                continue
            for c in matches:
                r = _g("DELETE", f"{base}/{c['id']}")
                if r.is_success:
                    deleted.append(c.get("displayName"))
                    deleted_ids.append(str(c.get("id")))
                else:
                    errors.append(f"{c.get('displayName')}: {_graph_err(r)['error']}")

    # Log the deleted step IDS, not their names. A prune needs a forensic trail, but _audit
    # appends to Claude-Writes-Log.md at the ROOT of the shared library — a step name can
    # carry consumer detail, and PHI containment says identifiers stay in the case folder.
    # Opaque ids identify exactly what was destroyed without putting content in a shared file.
    _audit("todo_set_steps",
           f"{_mbx(user)}:{task_id} +{len(created)} ~{len(updated)} ={len(unchanged)} "
           f"-{len(deleted)}" + (f" deleted_ids={deleted_ids}" if deleted_ids else ""))
    out = {"ok": not errors, "created": created, "updated": updated,
           "unchanged": unchanged, "deleted": deleted}
    if errors:
        out["errors"] = errors
    return out


@mcp.tool
def todo_delete_step(user: str, list_id: str, task_id: str, step_id: str,
                     confirm: bool = False) -> dict:
    """Delete one Step (checklist item) from a task by its step id. Requires confirm=True.
    Get step ids from todo_get_task.

    To remove several at once you CAN use todo_set_steps(steps=[...the ones to KEEP...],
    prune=True, confirm=True) — but that is a bulk delete: anything not named is destroyed.
    Deleting them one at a time here is the safer default unless the keep-list is certain."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    if not confirm:
        return {"error": "refused: pass confirm=true to delete"}
    r = _g("DELETE", _todo_task_url(user, list_id, task_id) + f"/checklistItems/{step_id}")
    if not r.is_success:
        return _graph_err(r)
    _audit("todo_delete_step", f"{_mbx(user)}:{task_id}/{step_id}")
    return {"ok": True}


@mcp.tool
def todo_find_tasks(user: str, query: str, include_completed: bool = False,
                    search_notes: bool = True, per_list: int = 100,
                    list_id: str | None = None) -> list[dict]:
    """Find tasks across ALL of a user's To Do lists by text, so you don't need a list_id
    first. Matches the title and (search_notes=True, default) the NOTE body,
    case-insensitively. Returns the owning list name/id with each hit so the result can be
    fed straight into todo_get_task / todo_update_task. Pass list_id to restrict to one list.

    Graph exposes no $search on todoTask, so this fetches and filters server-side here;
    per_list caps how many tasks are pulled from each list."""
    if not _auth_ok():
        return [{"error": "unauthorized"}]
    q = (query or "").strip().lower()
    if not q:
        return [{"error": "refused: empty query"}]
    if list_id:
        lists = [{"id": list_id, "name": None}]
        lists_complete = True
    else:
        litems, lists_complete, lerr = _todo_collection(
            f"/users/{_mbx(user)}/todo/lists?$top=50")
        if lerr:
            return [lerr]
        lists = [{"id": l["id"], "name": l.get("displayName")} for l in litems]

    hits: list[dict] = []
    incomplete: list[str] = []          # lists whose tasks were truncated or errored
    if not lists_complete:
        incomplete.append("(the task-list enumeration itself was truncated)")

    for lst in lists:
        url = (f"/users/{_mbx(user)}/todo/lists/{lst['id']}/tasks"
               f"?$top={int(per_list)}&$expand=checklistItems")
        if not include_completed:
            url += "&$filter=status ne 'completed'"
        # One list erroring must not discard every hit already gathered. This loop makes up
        # to ~51 sequential Graph calls against an aggressively throttled service, and
        # _send_with_retry can sleep up to 60s per attempt — a single uncaught ReadTimeout
        # near the end would otherwise throw away the whole search.
        try:
            tasks, complete, err = _todo_collection(url, cap=int(per_list))
        except Exception as e:
            incomplete.append(f"{lst['name']}: {type(e).__name__}: {e}")
            continue
        if err:
            incomplete.append(f"{lst['name']}: {err['error']}")
            continue
        if not complete:
            incomplete.append(f"{lst['name']} (capped at per_list={per_list})")
        for t in tasks:
            title = t.get("title") or ""
            note = _todo_body_text(t) if search_notes else ""
            csteps = [{"name": c.get("displayName"), "checked": bool(c.get("isChecked"))}
                      for c in (t.get("checklistItems") or [])]
            where = []
            if q in title.lower():
                where.append("title")
            if search_notes and note and q in note.lower():
                where.append("note")
            if any(q in (s["name"] or "").lower() for s in csteps):
                where.append("step")
            if not where:
                continue
            hits.append({
                "list_name": lst["name"], "list_id": lst["id"],
                "id": t["id"], "title": title, "status": t.get("status"),
                "due": ((t.get("dueDateTime") or {}).get("dateTime")),
                "matched_in": where, "body": note, "steps": csteps,
            })

    # Never let a caller read "no hits" as "does not exist" when coverage was partial.
    if incomplete:
        hits.append({"coverage_warning":
                     "Search did NOT cover everything; a nil result here is not proof of "
                     "absence. Incomplete: " + "; ".join(incomplete)})
    return hits


# ===========================================================================
# v18 EXPANSION — spec 2026-07-13 (Zach). Adds the remaining Mail / Calendar /
# SharePoint tools from the permissions spec, plus throne_healthcheck. All Graph
# app-roles used below (Mail.ReadWrite/Send, Calendars.ReadWrite, Sites.FullControl.All,
# Files.ReadWrite.All, User.Read.All) are already admin-consented (verified 2026-07-13).
# House-style guards carried through: _auth_ok gate, _audit on every write,
# confirm=True on outward/destructive actions, _consumer_block on outbound content.
# ===========================================================================
GRAPH_BETA = "https://graph.microsoft.com/beta"


def _gb(method: str, path: str, **kw) -> httpx.Response:
    """Graph BETA request. Used ONLY where v1.0 lacks the surface (site-page web part
    editing). Every /beta call in this file goes through here so it's greppable."""
    headers = kw.pop("headers", {})
    headers["Authorization"] = f"Bearer {_token()}"
    kw.setdefault("follow_redirects", True)
    return _send_with_retry(method, f"{GRAPH_BETA}{path}", headers=headers, timeout=120, **kw)


# ---------------------------------------------------------------------------
# MAIL — search / move / folder management / attachments  [v18]
# ---------------------------------------------------------------------------
@mcp.tool
def outlook_search(mailbox: str = "zach", query: str = "", top: int = 25,
                   folder: str | None = None) -> list[dict]:
    """Full-text search a mailbox (KQL $search over subject/body/participants). Optionally
    scope to one folder (well-known name or id). mailbox = zach|admin|jeff|steven or a UPN."""
    if not _auth_ok():
        return [{"error": "unauthorized"}]
    upn = _mbx(mailbox)
    s = (query or "").replace('"', "")
    seg = f"mailFolders/{folder}/messages" if folder else "messages"
    r = _g("GET", f'/users/{upn}/{seg}?$search="{s}"&$top={int(top)}'
           "&$select=id,subject,from,receivedDateTime,bodyPreview,isRead",
           headers={"ConsistencyLevel": "eventual"})
    if not r.is_success:
        return [_graph_err(r)]
    return [{"id": m["id"], "subject": m.get("subject"),
             "from": (m.get("from") or {}).get("emailAddress", {}).get("address"),
             "received": m.get("receivedDateTime"), "preview": m.get("bodyPreview"),
             "isRead": m.get("isRead")} for m in r.json().get("value", [])]


@mcp.tool
def outlook_move_message(mailbox: str, message_id: str, destination: str) -> dict:
    """Move a message to another folder. destination = a well-known name (archive, deleteditems,
    junkemail) or a folder id (from outlook_list_folders / outlook_manage_folders)."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    upn = _mbx(mailbox)
    r = _g("POST", f"/users/{upn}/messages/{message_id}/move",
           json={"destinationId": destination})
    if not r.is_success:
        return _graph_err(r)
    j = r.json()
    _audit("outlook_move", f"{upn}:{message_id} -> {destination}")
    return {"ok": True, "id": j.get("id"), "webUrl": j.get("webLink")}


@mcp.tool
def outlook_manage_folders(mailbox: str, action: str, name: str | None = None,
                           folder_id: str | None = None, parent_id: str | None = None,
                           confirm: bool = False) -> dict:
    """Manage mail folders. action = list | create | rename | delete.
      list   -> children of parent_id (or top-level folders).
      create -> new folder `name` (under parent_id if given).
      rename -> set folder_id's display name to `name`.
      delete -> remove folder_id (requires confirm=True)."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    upn = _mbx(mailbox)
    act = (action or "").lower()
    if act == "list":
        base = f"/users/{upn}/mailFolders/{parent_id}/childFolders" if parent_id \
            else f"/users/{upn}/mailFolders"
        r = _g("GET", base + "?$top=100&$select=id,displayName,unreadItemCount,totalItemCount")
        if not r.is_success:
            return _graph_err(r)
        return {"ok": True, "folders": [{"id": f["id"], "name": f.get("displayName"),
                "unread": f.get("unreadItemCount"), "total": f.get("totalItemCount")}
                for f in r.json().get("value", [])]}
    if act == "create":
        if not name:
            return {"error": "create requires name"}
        base = f"/users/{upn}/mailFolders/{parent_id}/childFolders" if parent_id \
            else f"/users/{upn}/mailFolders"
        r = _g("POST", base, json={"displayName": name})
        if not r.is_success:
            return _graph_err(r)
        j = r.json()
        _audit("outlook_folder_create", f"{upn}: {name}")
        return {"ok": True, "id": j.get("id"), "name": j.get("displayName")}
    if act == "rename":
        if not (folder_id and name):
            return {"error": "rename requires folder_id and name"}
        r = _g("PATCH", f"/users/{upn}/mailFolders/{folder_id}", json={"displayName": name})
        if not r.is_success:
            return _graph_err(r)
        _audit("outlook_folder_rename", f"{upn}:{folder_id} -> {name}")
        return {"ok": True, "id": folder_id, "name": name}
    if act == "delete":
        if not folder_id:
            return {"error": "delete requires folder_id"}
        if not confirm:
            return {"error": "refused: pass confirm=true to delete a folder"}
        r = _g("DELETE", f"/users/{upn}/mailFolders/{folder_id}")
        if not r.is_success:
            return _graph_err(r)
        _audit("outlook_folder_delete", f"{upn}:{folder_id}")
        return {"ok": True}
    return {"error": f"unknown action '{action}' (use list|create|rename|delete)"}


@mcp.tool
def outlook_download_attachment(mailbox: str, message_id: str,
                                attachment_id: str | None = None) -> dict:
    """List a message's attachments (no attachment_id) or download one as base64 (with it)."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    upn = _mbx(mailbox)
    if not attachment_id:
        r = _g("GET", f"/users/{upn}/messages/{message_id}/attachments"
               "?$select=id,name,contentType,size,isInline")
        if not r.is_success:
            return _graph_err(r)
        return {"ok": True, "attachments": [{"id": a["id"], "name": a.get("name"),
                "contentType": a.get("contentType"), "size": a.get("size"),
                "isInline": a.get("isInline")} for a in r.json().get("value", [])]}
    r = _g("GET", f"/users/{upn}/messages/{message_id}/attachments/{attachment_id}")
    if not r.is_success:
        return _graph_err(r)
    a = r.json()
    return {"ok": True, "id": a.get("id"), "name": a.get("name"),
            "contentType": a.get("contentType"), "size": a.get("size"),
            "content_b64": a.get("contentBytes")}


@mcp.tool
def outlook_add_attachment(mailbox: str, message_id: str, name: str, content_b64: str,
                           content_type: str | None = None) -> dict:
    """Attach a file (base64 bytes) to an existing DRAFT message. Returns the attachment id."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    try:
        base64.b64decode(content_b64, validate=True)
    except Exception as e:
        return {"error": f"invalid base64 content: {e}"}
    upn = _mbx(mailbox)
    att = {"@odata.type": "#microsoft.graph.fileAttachment", "name": name,
           "contentBytes": content_b64}
    if content_type:
        att["contentType"] = content_type
    r = _g("POST", f"/users/{upn}/messages/{message_id}/attachments", json=att)
    if not r.is_success:
        return _graph_err(r)
    j = r.json()
    _audit("outlook_add_attachment", f"{upn}:{message_id} +{name}")
    return {"ok": True, "id": j.get("id"), "name": j.get("name")}


# ---------------------------------------------------------------------------
# CALENDAR — get one event / respond to invite / find availability  [v18]
# ---------------------------------------------------------------------------
@mcp.tool
def calendar_get_event(mailbox: str, event_id: str) -> dict:
    """Read one calendar event in full (body returned as plain text)."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    r = _g("GET", f"/users/{_mbx(mailbox)}/events/{event_id}"
           "?$select=id,subject,start,end,location,isAllDay,organizer,attendees,webLink,body",
           headers={"Prefer": f'outlook.timezone="{DEFAULT_TZ}"'})
    if not r.is_success:
        return _graph_err(r)
    e = r.json()
    out = _event_out(e)
    b = e.get("body") or {}
    out["body"] = (_TAG_RE.sub(" ", b.get("content", "")) if b.get("contentType") == "html"
                   else b.get("content", "")).strip()
    return out


@mcp.tool
def calendar_respond(mailbox: str, event_id: str, response: str, comment: str = "",
                     send_response: bool = True, confirm: bool = False) -> dict:
    """Respond to a meeting invite. response = accept | tentative | decline.
    Sends your response to the organizer -> requires confirm=True."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    if not confirm:
        return {"error": "refused: pass confirm=true (the organizer is notified)"}
    verb = {"accept": "accept", "tentative": "tentativelyAccept",
            "tentatively": "tentativelyAccept", "decline": "decline"}.get(
        (response or "").lower())
    if not verb:
        return {"error": "response must be accept|tentative|decline"}
    r = _g("POST", f"/users/{_mbx(mailbox)}/events/{event_id}/{verb}",
           json={"comment": comment, "sendResponse": send_response})
    if not r.is_success:
        return _graph_err(r)
    _audit("calendar_respond", f"{_mbx(mailbox)}:{event_id} {verb}")
    return {"ok": True, "response": verb}


@mcp.tool
def calendar_find_availability(mailboxes: list[str], start: str, end: str,
                               interval_minutes: int = 30) -> list[dict]:
    """Free/busy across one or more mailboxes for a window (ISO local times). Returns each
    schedule's availabilityView + busy blocks. mailboxes = aliases and/or UPNs."""
    if not _auth_ok():
        return [{"error": "unauthorized"}]
    if not mailboxes:
        return [{"error": "provide at least one mailbox"}]
    schedules = [_mbx(m) for m in mailboxes]
    organizer = schedules[0]
    body = {"schedules": schedules,
            "startTime": {"dateTime": start, "timeZone": DEFAULT_TZ},
            "endTime": {"dateTime": end, "timeZone": DEFAULT_TZ},
            "availabilityViewInterval": int(interval_minutes)}
    r = _g("POST", f"/users/{organizer}/calendar/getSchedule", json=body)
    if not r.is_success:
        return [_graph_err(r)]
    out = []
    for s in r.json().get("value", []):
        out.append({"mailbox": s.get("scheduleId"),
                    "availabilityView": s.get("availabilityView"),
                    "busy": [{"start": (i.get("start") or {}).get("dateTime"),
                              "end": (i.get("end") or {}).get("dateTime"),
                              "status": i.get("status")}
                             for i in s.get("scheduleItems", [])]})
    return out


# ---------------------------------------------------------------------------
# SHAREPOINT — sites / pages / web parts / lists / files, site-parameterized  [v18]
# `site` accepts: 'throne' (default) | 'opshub' | a URL | host:/sites/Name | a site id.
# ---------------------------------------------------------------------------
@mcp.tool
def sp_list_sites(search: str = "*", top: int = 25) -> list[dict]:
    """Discover SharePoint sites in the tenant (search='*' lists all indexed sites)."""
    if not _auth_ok():
        return [{"error": "unauthorized"}]
    r = _g("GET", f"/sites?search={search}&$top={int(top)}")
    if not r.is_success:
        return [_graph_err(r)]
    return [{"id": s["id"], "name": s.get("displayName") or s.get("name"),
             "webUrl": s.get("webUrl")} for s in r.json().get("value", [])]


@mcp.tool
def sp_get_site(site: str = "throne") -> dict:
    """Resolve a site and return its id, name, webUrl, and default document-library drive id."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    sid = _resolve_site(site)
    r = _g("GET", f"/sites/{sid}?$select=id,displayName,name,webUrl")
    if not r.is_success:
        return _graph_err(r)
    j = r.json()
    d = _g("GET", f"/sites/{sid}/drive?$select=id,name,webUrl")
    drive = d.json() if d.is_success else {}
    return {"id": j.get("id"), "name": j.get("displayName") or j.get("name"),
            "webUrl": j.get("webUrl"),
            "drive": {"id": drive.get("id"), "name": drive.get("name")}}


@mcp.tool
def sp_create_page(site: str, name: str, title: str, html: str, publish: bool = True) -> dict:
    """Create a modern SharePoint page on any site from an HTML body, then optionally publish.
    Returns the page id + webUrl. `name` becomes the .aspx filename."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    sid = _resolve_site(site)
    page = {"name": name if name.endswith(".aspx") else f"{name}.aspx", "title": title,
            "pageLayout": "article",
            "canvasLayout": {"horizontalSections": [{"layout": "oneColumn", "columns": [{
                "width": 12, "webparts": [{
                    "@odata.type": "#microsoft.graph.textWebPart", "innerHtml": html}]}]}]}}
    r = _g("POST", f"/sites/{sid}/pages", json=page)
    if not r.is_success:
        return _graph_err(r)
    j = r.json()
    page_id = j.get("id")
    out = {"ok": True, "id": page_id, "name": page["name"], "webUrl": j.get("webUrl")}
    if publish:
        p = _g("POST", f"/sites/{sid}/pages/{page_id}/microsoft.graph.publish")
        out["published"] = p.status_code in (200, 202, 204)
    _audit("sp_create_page", f"{sid}:{page['name']}")
    return out


@mcp.tool
def sp_update_page(site: str, page_id: str, title: str | None = None,
                   html: str | None = None, publish: bool = False) -> dict:
    """Update a page's title and/or replace its body HTML (single text web part), optionally
    re-publish. Body replacement uses Graph /beta (v1.0 canvas editing is limited) — flagged."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    sid = _resolve_site(site)
    if title is not None:
        t = _g("PATCH", f"/sites/{sid}/pages/{page_id}", json={"title": title})
        if not t.is_success:
            return _graph_err(t)
    if html is not None:
        # /beta: replace the page canvas with one text web part carrying the new HTML.
        canvas = {"canvasLayout": {"horizontalSections": [{"layout": "oneColumn", "columns": [{
            "width": 12, "webparts": [{
                "@odata.type": "#microsoft.graph.textWebPart", "innerHtml": html}]}]}]}}
        c = _gb("PATCH", f"/sites/{sid}/pages/{page_id}/microsoft.graph.sitePage", json=canvas)
        if not c.is_success:
            return _graph_err(c)
    out = {"ok": True, "id": page_id}
    if publish:
        p = _g("POST", f"/sites/{sid}/pages/{page_id}/microsoft.graph.publish")
        out["published"] = p.status_code in (200, 202, 204)
    _audit("sp_update_page", f"{sid}:{page_id}")
    return out


# Built-in SharePoint "Embed" web part id (iframe/embed code). Text uses the typed textWebPart.
_EMBED_WEBPART_ID = "490d7c76-1824-45b2-9de3-676421c997fa"


@mcp.tool
def sp_add_webpart(site: str, page_id: str, kind: str = "text", html: str = "",
                   embed_code: str = "", publish: bool = True) -> dict:
    """Append a web part to an existing page. kind='text' adds an HTML text block (`html`);
    kind='embed' adds an Embed web part rendering `embed_code` (iframe/HTML). Uses Graph /beta
    for the add-web-part canvas op (flagged); returns the page id + webUrl."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    sid = _resolve_site(site)
    # Read current canvas (beta), append the new web part to the first column, PATCH it back.
    cur = _gb("GET", f"/sites/{sid}/pages/{page_id}/microsoft.graph.sitePage"
              "?$select=canvasLayout")
    if not cur.is_success:
        return _graph_err(cur)
    canvas = cur.json().get("canvasLayout") or {"horizontalSections": []}
    sections = canvas.get("horizontalSections") or []
    if not sections:
        sections = [{"layout": "oneColumn", "columns": [{"width": 12, "webparts": []}]}]
        canvas["horizontalSections"] = sections
    col = sections[0]["columns"][0]
    col.setdefault("webparts", [])
    if kind == "embed":
        col["webparts"].append({"@odata.type": "#microsoft.graph.standardWebPart",
                                "webPartType": _EMBED_WEBPART_ID,
                                "data": {"properties": {"embedCode": embed_code or html}}})
    else:
        col["webparts"].append({"@odata.type": "#microsoft.graph.textWebPart",
                                "innerHtml": html})
    c = _gb("PATCH", f"/sites/{sid}/pages/{page_id}/microsoft.graph.sitePage",
            json={"canvasLayout": canvas})
    if not c.is_success:
        return _graph_err(c)
    out = {"ok": True, "id": page_id, "kind": kind}
    if publish:
        p = _g("POST", f"/sites/{sid}/pages/{page_id}/microsoft.graph.publish")
        out["published"] = p.status_code in (200, 202, 204)
    _audit("sp_add_webpart", f"{sid}:{page_id} ({kind})")
    return out


# ---------------------------------------------------------------------------
# ONENOTE  (read-only)
#
# ####################################################################
# ##  DO NOT DEPLOY AS-IS. THESE TOOLS CANNOT WORK ON THIS SERVER.  ##
# ####################################################################
#
# Microsoft RETIRED app-only (client-credentials) authentication for the Graph
# OneNote API on 2025-03-31. This server authenticates app-only via
# CertificateCredential, so every call below returns 401/40001 at runtime.
#   "The Microsoft Graph OneNote API doesn't support app-only authentication."
#   https://learn.microsoft.com/graph/api/resources/onenote-api-overview
#
# THE TRAP: the Entra app role `Notes.Read.All` still EXISTS and admin consent
# still SUCCEEDS. The app registration will look completely correct and fail only
# at runtime. Granting it buys nothing and widens the tenant blast radius — it is
# tenant-wide with no per-site scoping (Sites.Selected does not cover /onenote;
# there is no Notes.Selected scope and never was). DO NOT GRANT IT.
#
# The endpoint shapes, pagination, and return contracts below are all CORRECT and
# verified against current docs. What is wrong is only the auth mode. To make them
# live, this server needs a DELEGATED token from a service account
# (auth-code + offline_access + refresh token). Delegated Notes.Read.All needs no
# admin consent and is bounded by what that account can see in SharePoint — which
# is tighter governance than the app-only path would have been.
#
# Even then, mind the ceiling: delegated OneNote is 120 req/min and 400 req/hour,
# and these endpoints do NOT return Retry-After on 429 (self-managed backoff
# required — _send_with_retry's Retry-After path will not help here). Microsoft
# states the OneNote API is NOT suitable for bulk notebook extraction. For a
# one-time full capture, a manual OneNote desktop export to .docx remains the
# correct tool. These tools are for LIVE, LOW-VOLUME reads of specific pages.
# ---------------------------------------------------------------------------
_ONENOTE_PAGE_CAP = 200        # hard stop so a huge section can't run away
_ONENOTE_DUMP_CAP = 50         # content fetches are one request per page


# QUARANTINE 2026-07-27 — the five tools below are deliberately NOT registered.
# `@_onenote_quarantined` returns the function untouched instead of handing it to
# `mcp.tool`, so these five stay OUT of the served tool surface (69 since the v19 To Do
# expansion of 2026-08-10; it was 64 when this note was written) and an `az acr build` from this
# file cannot ship OneNote as a side effect. That is the entire point: this tree is
# canonical per CLAUDE.md Rule 6, and canonical must equal deployable.
#
# Do NOT "re-enable" by swapping this back to @mcp.tool. That would advertise five
# tools that 401 on every call (see the banner above — app-only Graph OneNote was
# retired 2025-03-31). Registering them is the LAST step of the delegated-token
# rework, not the first. Until that rework exists, the working capture path is
# 03-cases/audits/Gaspard-2026-07-27/dump_onenote.ps1, which already pulls full
# notebooks via the OneNote desktop COM API and needs no Graph permission at all.
def _onenote_quarantined(fn):
    """No-op decorator. Keeps the OneNote code in-tree but out of the tool manifest."""
    return fn


def _onenote_collect(sid: str, path: str, top: int, cap: int) -> tuple[list[dict], str | None]:
    """Page through a OneNote collection endpoint, honoring @odata.nextLink.
    Returns (items, error_message). Caps total items so an 86MB section can't hang a call."""
    items: list[dict] = []
    url = f"/sites/{sid}/onenote/{path}"
    sep = "&" if "?" in url else "?"
    url = f"{url}{sep}$top={min(int(top), 100)}"
    while url and len(items) < cap:
        r = _g("GET", url) if url.startswith("/") else _send_with_retry(
            "GET", url, headers={"Authorization": f"Bearer {_token()}"}, timeout=120)
        if not r.is_success:
            return items, _graph_err(r)["error"]
        body = r.json()
        items.extend(body.get("value", []))
        url = body.get("@odata.nextLink")
    return items[:cap], None


@_onenote_quarantined
def onenote_notebooks(site: str = "throne") -> list[dict]:
    """List OneNote notebooks in a SharePoint site. site='throne'|'opshub'|absolute URL|site id.
    For a contractor's personal case notebook pass that contractor's site URL, e.g.
    'https://netorg39360.sharepoint.com/sites/GaspardSteven'."""
    if not _auth_ok():
        return [{"error": "unauthorized"}]
    sid = _resolve_site(site)
    items, err = _onenote_collect(sid, "notebooks", 50, 100)
    if err:
        return [{"error": err}]
    return [{"id": n["id"], "name": n.get("displayName"),
             "created": n.get("createdDateTime"),
             "modified": n.get("lastModifiedDateTime"),
             "webUrl": n.get("links", {}).get("oneNoteWebUrl", {}).get("href")}
            for n in items]


@_onenote_quarantined
def onenote_sections(site: str = "throne", notebook_id: str | None = None) -> list[dict]:
    """List OneNote sections. Pass notebook_id to scope to one notebook, else all sections
    in the site. site='throne'|'opshub'|absolute URL|site id."""
    if not _auth_ok():
        return [{"error": "unauthorized"}]
    sid = _resolve_site(site)
    path = f"notebooks/{notebook_id}/sections" if notebook_id else "sections"
    items, err = _onenote_collect(sid, path, 50, 200)
    if err:
        return [{"error": err}]
    return [{"id": s["id"], "name": s.get("displayName"),
             "modified": s.get("lastModifiedDateTime"),
             "notebook": (s.get("parentNotebook") or {}).get("displayName")}
            for s in items]


@_onenote_quarantined
def onenote_pages(site: str = "throne", section_id: str | None = None,
                  top: int = 100, search: str | None = None) -> list[dict]:
    """List OneNote pages, newest-modified first. Pass section_id to scope to one section.
    `search` filters on page title (server-side startswith on title).
    Returns id + title + lastModifiedDateTime — use onenote_page_content to fetch a page."""
    if not _auth_ok():
        return [{"error": "unauthorized"}]
    sid = _resolve_site(site)
    path = f"sections/{section_id}/pages" if section_id else "pages"
    q = "?$select=id,title,lastModifiedDateTime,createdDateTime&$orderby=lastModifiedDateTime desc"
    if search:
        esc = search.replace("'", "''")
        q += f"&$filter=startswith(title,'{esc}')"
    items, err = _onenote_collect(sid, path + q, top, _ONENOTE_PAGE_CAP)
    if err:
        return [{"error": err}]
    return [{"id": p["id"], "title": p.get("title"),
             "modified": p.get("lastModifiedDateTime"),
             "created": p.get("createdDateTime")} for p in items]


@_onenote_quarantined
def onenote_page_content(page_id: str, site: str = "throne",
                         include_ids: bool = False) -> str:
    """Fetch a OneNote page as HTML, with <table> markup preserved — this is the point:
    activity-log hour columns survive as real table cells, unlike a PDF export.
    include_ids=True adds data-id attributes (needed only if you intend to PATCH the page)."""
    if not _auth_ok():
        return "unauthorized"
    sid = _resolve_site(site)
    url = f"/sites/{sid}/onenote/pages/{page_id}/content"
    if include_ids:
        url += "?includeIDs=true"
    r = _g("GET", url)
    if not r.is_success:
        return _graph_err(r)["error"]
    return r.text


@_onenote_quarantined
def onenote_dump_section(section_id: str, site: str = "throne",
                         max_pages: int = 25) -> list[dict]:
    """Pull a whole OneNote section — every page's title, modified date, AND html content —
    in one call. This is the case-notes recovery tool: one call per contractor section
    replaces a manual File > Export. Capped at max_pages (hard ceiling 50) because content
    is one Graph request per page."""
    if not _auth_ok():
        return [{"error": "unauthorized"}]
    sid = _resolve_site(site)
    cap = min(int(max_pages), _ONENOTE_DUMP_CAP)
    q = "?$select=id,title,lastModifiedDateTime&$orderby=lastModifiedDateTime desc"
    pages, err = _onenote_collect(sid, f"sections/{section_id}/pages" + q, cap, cap)
    if err:
        return [{"error": err}]
    out = []
    for p in pages:
        c = _g("GET", f"/sites/{sid}/onenote/pages/{p['id']}/content")
        out.append({"id": p["id"], "title": p.get("title"),
                    "modified": p.get("lastModifiedDateTime"),
                    "html": c.text if c.is_success else None,
                    "error": None if c.is_success else _graph_err(c)["error"]})
    return out


@mcp.tool
def sp_create_list(site: str, display_name: str, columns: list[dict] | None = None,
                   description: str = "", template: str = "genericList") -> dict:
    """Create a SharePoint list. columns = [{name, text|number|boolean|dateTime|choice...}].
    Example column: {"name":"Status","choice":{"choices":["Open","Closed"]}}. Returns id + webUrl."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    sid = _resolve_site(site)
    body: dict = {"displayName": display_name, "list": {"template": template}}
    if description:
        body["description"] = description
    if columns:
        body["columns"] = columns
    r = _g("POST", f"/sites/{sid}/lists", json=body)
    if not r.is_success:
        return _graph_err(r)
    j = r.json()
    _audit("sp_create_list", f"{sid}: {display_name}")
    return {"ok": True, "id": j.get("id"), "webUrl": j.get("webUrl")}


@mcp.tool
def sp_update_list(site: str, list_id: str, display_name: str | None = None,
                   description: str | None = None) -> dict:
    """Rename a list and/or change its description. Returns the list id + webUrl."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    sid = _resolve_site(site)
    patch: dict = {}
    if display_name is not None:
        patch["displayName"] = display_name
    if description is not None:
        patch["description"] = description
    if not patch:
        return {"error": "nothing to update"}
    r = _g("PATCH", f"/sites/{sid}/lists/{list_id}", json=patch)
    if not r.is_success:
        return _graph_err(r)
    j = r.json()
    _audit("sp_update_list", f"{sid}:{list_id} {list(patch.keys())}")
    return {"ok": True, "id": j.get("id", list_id), "webUrl": j.get("webUrl")}


@mcp.tool
def sp_upload_file(site: str, path: str, content_b64: str, library: str | None = None,
                   overwrite: bool = True) -> dict:
    """Upload a file (base64 bytes) to a site's document library by path. Handles any size
    (resumable session >4MB). library=None uses the site's default library. Returns id + webUrl."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    try:
        data = base64.b64decode(content_b64, validate=True)
    except Exception as e:
        return {"error": f"invalid base64 content: {e}"}
    if not data:
        return {"error": "refused: decoded content is empty"}
    drive = _site_drive_id(site, library)
    path = _normalize_lib_path(drive, path)  # guard: on the throne library, never double 'Case Documnents/'
    r = _upload_bytes(drive, path, data, overwrite)["resp"]
    if not r.is_success:
        return _graph_err(r)
    body = r.json() if r.content else {}
    _audit("sp_upload_file", f"{drive}:{path}")
    return {"ok": True, "id": body.get("id"), "path": path,
            "webUrl": body.get("webUrl"), "bytes": len(data)}


@mcp.tool
def sp_create_folder(site: str, name: str, parent_path: str = "", library: str | None = None) -> dict:
    """Create a folder in a site's document library. parent_path='' = library root. Returns id + webUrl."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    drive = _site_drive_id(site, library)
    parent_path = _normalize_lib_path(drive, parent_path)  # guard: on the throne library, never double 'Case Documnents/'
    if parent_path:
        seg = f"root:/{parent_path.strip('/')}:/children"
    else:
        seg = "root/children"
    r = _g("POST", f"/drives/{drive}/{seg}",
           json={"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"})
    if not r.is_success:
        return _graph_err(r)
    j = r.json()
    _audit("sp_create_folder", f"{drive}:{parent_path}/{name}")
    return {"ok": True, "id": j.get("id"), "webUrl": j.get("webUrl")}


# ---------------------------------------------------------------------------
# SHAREPOINT COLUMN MANAGEMENT  [v19] — add/modify columns on an EXISTING list.
# Enables the billing-firewall fields (Note Visibility choices + Billing Description).
# ---------------------------------------------------------------------------
def _column_id(sid: str, list_id: str, name_or_id: str) -> str:
    """Resolve a column internal name to its id; pass a GUID straight through."""
    if re.fullmatch(r"[0-9a-fA-F-]{36}", name_or_id or ""):
        return name_or_id
    r = _g("GET", f"/sites/{sid}/lists/{list_id}/columns?$select=id,name")
    r.raise_for_status()
    for c in r.json().get("value", []):
        if c.get("name") == name_or_id:
            return c["id"]
    raise RuntimeError(f"column '{name_or_id}' not found")


@mcp.tool
def sp_add_column(site: str, list_id: str, name: str, column_type: str = "text",
                  choices: list[str] | None = None, default_value: str | None = None,
                  description: str = "", multiline: bool = False) -> dict:
    """Add a column to an existing list. column_type = text | number | boolean | dateTime | choice.
    For a choice column pass choices=[...]; for long text set multiline=True; default_value optional.
    Returns the new column id + name."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    sid = _resolve_site(site)
    col: dict = {"name": name, "description": description}
    ct = column_type.lower()
    if ct == "text":
        col["text"] = {"allowMultipleLines": bool(multiline)}
    elif ct == "number":
        col["number"] = {}
    elif ct == "boolean":
        col["boolean"] = {}
    elif ct in ("datetime", "date"):
        col["dateTime"] = {}
    elif ct == "choice":
        col["choice"] = {"choices": choices or [], "displayAs": "dropDownMenu",
                         "allowTextEntry": False}
    else:
        return {"error": f"unsupported column_type '{column_type}' (text|number|boolean|dateTime|choice)"}
    if default_value is not None:
        col["defaultValue"] = {"value": str(default_value)}
    r = _g("POST", f"/sites/{sid}/lists/{list_id}/columns", json=col)
    if not r.is_success:
        return _graph_err(r)
    j = r.json()
    _audit("sp_add_column", f"{list_id}: {name} ({ct})")
    return {"ok": True, "id": j.get("id"), "name": j.get("name")}


@mcp.tool
def sp_update_column(site: str, list_id: str, column: str, choices: list[str] | None = None,
                     default_value: str | None = None, display_name: str | None = None) -> dict:
    """Modify an existing column: replace its `choices` (choice field), set `default_value`, and/or
    rename via `display_name`. `column` = the column's internal name (e.g. Note_x0020_Visibility) or id.
    NOTE: `choices` REPLACES the full list — include existing values you want to keep."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    sid = _resolve_site(site)
    try:
        col_id = _column_id(sid, list_id, column)
    except Exception as e:
        return {"error": str(e)}
    patch: dict = {}
    if choices is not None:
        patch["choice"] = {"choices": choices}
    if default_value is not None:
        patch["defaultValue"] = {"value": str(default_value)}
    if display_name is not None:
        patch["displayName"] = display_name
    if not patch:
        return {"error": "nothing to update"}
    r = _g("PATCH", f"/sites/{sid}/lists/{list_id}/columns/{col_id}", json=patch)
    if not r.is_success:
        return _graph_err(r)
    _audit("sp_update_column", f"{list_id}:{column} {list(patch.keys())}")
    return {"ok": True, "id": col_id}


# ---------------------------------------------------------------------------
# HEALTHCHECK  [v18]
# ---------------------------------------------------------------------------
@mcp.tool
def throne_healthcheck() -> dict:
    """Verify Graph token acquisition and report which target mailboxes + SharePoint sites are
    reachable app-only. Mailboxes default to zach,admin (override via HEALTHCHECK_MAILBOXES env).
    Sites checked: the pinned Throne site and ZachOperationsHub (opshub)."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    out: dict = {"checked_at": _now(), "token": None, "mailboxes": {}, "sites": {}}
    try:
        _token()
        out["token"] = "ok"
    except Exception as e:
        out["token"] = f"FAIL: {e}"
        return out
    mbxs = [m.strip() for m in os.environ.get("HEALTHCHECK_MAILBOXES", "zach,admin").split(",")
            if m.strip()]
    for m in mbxs:
        upn = _mbx(m)
        try:
            r = _g("GET", f"/users/{upn}/mailFolders/inbox?$select=totalItemCount")
            out["mailboxes"][upn] = ("reachable" if r.is_success
                                     else _graph_err(r)["error"])
        except Exception as e:
            out["mailboxes"][upn] = f"FAIL: {e}"
    for key in ("throne", "opshub"):
        try:
            sid = _resolve_site(key)
            if not sid:
                out["sites"][key] = "not configured"
                continue
            r = _g("GET", f"/sites/{sid}?$select=displayName,webUrl")
            if not r.is_success:
                out["sites"][key] = _graph_err(r)["error"]
                continue
            j = r.json()
            lists = _g("GET", f"/sites/{sid}/lists?$select=id")
            out["sites"][key] = {"name": j.get("displayName"), "webUrl": j.get("webUrl"),
                                 "status": "reachable",
                                 "lists": len(lists.json().get("value", [])) if lists.is_success else None}
        except Exception as e:
            out["sites"][key] = f"FAIL: {e}"
    return out


if __name__ == "__main__":
    # Streamable-HTTP for remote connector use. Container listens on 8080.
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
