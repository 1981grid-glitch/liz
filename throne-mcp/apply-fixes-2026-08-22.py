"""
Apply the 2026-08-22 Throne MCP fixes to throne_mcp_server.py, in place.

Run it from the canonical folder:
    cd "C:\\Users\\1981g\\OneDrive - Adaptiveenterprisesllc\\Adaptive Business\\CLAUSE CODE DEFAULT\\04-automations\\connectors\\throne-mcp\\throne-mcp-v18-expanded"
    python apply-fixes-2026-08-22.py

Safe to run twice: it refuses unless the file is byte-for-byte the expected pre-patch
source, so a second run is a no-op with a clear message rather than a double-patch.

Fixes: (1) _normalize_body_html was dead code at all 7 sites  (2) big mail attachments
via upload session  (3) Teams meetings on calendar_create_event  (4) onedrive_* alias
resolution  (5) careers@ alias.  Tool count stays 69.
"""
import datetime, hashlib, io, os, py_compile, sys

TARGET = "throne_mcp_server.py"
PRE  = "d46925f2fcf2b9008c70fb5b9ee8f5b15767fc018a5a3d9e032134a9f7c2525a"
POST = "71b7c6f9764811743d0e33e9152af5e1193be535271b183911d4fb6b74c0543f"


def die(msg):
    print("ABORTED: " + msg)
    sys.exit(1)


if not os.path.exists(TARGET):
    die(f"{TARGET} not found. Run this from the canonical throne-mcp-v18-expanded folder.")

raw = io.open(TARGET, "rb").read()
got = hashlib.sha256(raw).hexdigest()
if got == POST:
    print("Already patched (sha256 matches the post-patch build). Nothing to do.")
    sys.exit(0)
if got != PRE:
    die(f"{TARGET} is not the expected pre-patch source.\n"
        f"  expected {PRE}\n  found    {got}\n"
        "Someone edited it after 2026-08-20 17:40Z. Re-diff before patching.")

src = raw.decode("utf-8").replace("\r\n", "\n")
done = []

# (1) the dead _normalize_body_html calls -----------------------------------
dead = '        return {"error": "unauthorized"}\n        body_html = _normalize_body_html(body_html)\n'
live = '        return {"error": "unauthorized"}\n    body_html = _normalize_body_html(body_html)\n'
if src.count(dead) != 7:
    die(f"expected 7 dead _normalize_body_html sites, found {src.count(dead)}")
src = src.replace(dead, live)
done.append("1. un-indented 7 dead _normalize_body_html calls")

# (2) attachment size constants ---------------------------------------------
old = "_UPLOAD_CHUNK = 5 * 320 * 1024         # 1.6MB; Graph requires multiples of 320KiB\n"
new = (old +
       "# Mail attachments are a SEPARATE Graph ceiling from drive uploads: the plain\n"
       "# POST /attachments endpoint caps at 3MB, and above that an attachment upload\n"
       "# session is required (Graph's own hard ceiling for those is 150MB).\n"
       "_ATTACH_SIMPLE_MAX = 3 * 1024 * 1024   # >3MB must use an attachment upload session\n"
       "_ATTACH_MAX = 150 * 1024 * 1024        # Graph ceiling for attachment upload sessions\n")
if src.count(old) != 1:
    die("upload-constant anchor not found")
src = src.replace(old, new)
done.append("2. added _ATTACH_SIMPLE_MAX / _ATTACH_MAX")

# (3) outlook_add_attachment -------------------------------------------------
old = '''    """Attach a file (base64 bytes) to an existing DRAFT message. Returns the attachment id."""
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
'''
new = '''    """Attach a file (base64 bytes) to an existing DRAFT message. Returns the attachment id.

    Handles any size up to Graph's 150MB attachment ceiling: a plain POST under 3MB,
    a resumable ATTACHMENT upload session above it. Note this is a different endpoint
    and a different limit from the drive uploads `_upload_bytes` handles - the plain
    /attachments endpoint rejects anything over 3MB, which is why large evaluation
    PDFs and scanned reports used to fail here."""
    if not _auth_ok():
        return {"error": "unauthorized"}
    try:
        data = base64.b64decode(content_b64, validate=True)
    except Exception as e:
        return {"error": f"invalid base64 content: {e}"}
    if not data:
        return {"error": "refused: decoded content is empty"}
    if len(data) > _ATTACH_MAX:
        return {"error": f"refused: {len(data)} bytes exceeds Graph's "
                         f"{_ATTACH_MAX} byte attachment ceiling"}
    upn = _mbx(mailbox)

    if len(data) <= _ATTACH_SIMPLE_MAX:
        att = {"@odata.type": "#microsoft.graph.fileAttachment", "name": name,
               "contentBytes": content_b64}
        if content_type:
            att["contentType"] = content_type
        r = _g("POST", f"/users/{upn}/messages/{message_id}/attachments", json=att)
        if not r.is_success:
            return _graph_err(r)
        j = r.json()
        _audit("outlook_add_attachment", f"{upn}:{message_id} +{name}")
        return {"ok": True, "id": j.get("id"), "name": j.get("name"),
                "bytes": len(data), "upload": "simple"}

    # --- resumable attachment upload session (>3MB) ---
    item = {"attachmentType": "file", "name": name, "size": len(data)}
    if content_type:
        item["contentType"] = content_type
    s = _g("POST", f"/users/{upn}/messages/{message_id}/attachments/createUploadSession",
           json={"AttachmentItem": item})
    if not s.is_success:
        return _graph_err(s)
    url = s.json()["uploadUrl"]
    total = len(data)
    last = None
    # The session URL carries its own pre-authorization - sending our bearer token
    # to it is both unnecessary and a token leak to a non-Graph host, so this goes
    # out through bare httpx exactly like _upload_bytes does.
    for start in range(0, total, _UPLOAD_CHUNK):
        chunk = data[start:start + _UPLOAD_CHUNK]
        end = start + len(chunk) - 1
        last = httpx.put(url, content=chunk, timeout=120, headers={
            "Content-Length": str(len(chunk)),
            "Content-Range": f"bytes {start}-{end}/{total}",
        })
        if last.status_code not in (200, 201, 202):
            return _graph_err(last)
    # The final PUT answers 201 with the attachment id in the Location header; the
    # body is often empty, so parsing it alone would drop the id on the floor.
    att_id = None
    try:
        att_id = (last.json() or {}).get("id")
    except Exception:
        att_id = None
    if not att_id:
        m = re.search(r"Attachments\\('([^']+)'\\)", last.headers.get("Location", ""))
        att_id = m.group(1) if m else None
    _audit("outlook_add_attachment", f"{upn}:{message_id} +{name}")
    return {"ok": True, "id": att_id, "name": name,
            "bytes": total, "upload": "session"}
'''
if src.count(old) != 1:
    die("outlook_add_attachment body not matched")
src = src.replace(old, new)
done.append("3. outlook_add_attachment: upload session >3MB (ceiling 150MB)")

# (4) Teams meetings ---------------------------------------------------------
old = '''                          all_day: bool = False, confirm: bool = False) -> dict:
    """Create a calendar event. start/end are ISO local times ('2026-07-06T14:00:00').
    Adding attendees SENDS INVITES -> requires confirm=True (no attendees = no confirm needed)."""'''
new = '''                          all_day: bool = False, is_online_meeting: bool = False,
                          confirm: bool = False) -> dict:
    """Create a calendar event. start/end are ISO local times ('2026-07-06T14:00:00').
    Adding attendees SENDS INVITES -> requires confirm=True (no attendees = no confirm needed).

    is_online_meeting=True makes it a TEAMS MEETING: Graph provisions the meeting and
    the join link is returned as `joinUrl` (and lands in the event body for attendees).
    Graph cannot remove online-meeting info from an event once set, so this is
    create-time only - it is deliberately not exposed on calendar_update_event."""'''
if src.count(old) != 1:
    die("calendar_create_event signature not matched")
src = src.replace(old, new)

old = '''    if attendees:
        ev["attendees"] = [{"emailAddress": {"address": a}, "type": "required"}
                           for a in attendees]
    r = _g("POST", f"/users/{_mbx(mailbox)}/events", json=ev)
    if not r.is_success:
        return _graph_err(r)
    j = r.json()
    _audit("calendar_create", f"{_mbx(mailbox)}: {subject} @ {start}")
    return {"ok": True, "id": j.get("id"), "webLink": j.get("webLink")}
'''
new = '''    if attendees:
        ev["attendees"] = [{"emailAddress": {"address": a}, "type": "required"}
                           for a in attendees]
    if is_online_meeting:
        ev["isOnlineMeeting"] = True
        ev["onlineMeetingProvider"] = "teamsForBusiness"
    r = _g("POST", f"/users/{_mbx(mailbox)}/events", json=ev)
    if not r.is_success:
        return _graph_err(r)
    j = r.json()
    _audit("calendar_create", f"{_mbx(mailbox)}: {subject} @ {start}")
    out = {"ok": True, "id": j.get("id"), "webLink": j.get("webLink")}
    if is_online_meeting:
        om = j.get("onlineMeeting") or {}
        out["isOnlineMeeting"] = bool(j.get("isOnlineMeeting"))
        out["joinUrl"] = om.get("joinUrl")
        if not om.get("joinUrl"):
            # Graph accepts isOnlineMeeting on an event it cannot actually provision
            # (Teams not licensed for the mailbox, or policy blocks it) and answers 201
            # with no join link. Reporting a bare ok here would hand back a "Teams
            # meeting" nobody can join, so say so explicitly.
            out["warning"] = ("event created but Graph returned no joinUrl - the mailbox "
                              "may not be Teams-licensed or policy blocks online meetings; "
                              "the event exists but is NOT a Teams meeting")
    return out
'''
if src.count(old) != 1:
    die("calendar_create_event body not matched")
src = src.replace(old, new)
done.append("4. calendar_create_event: is_online_meeting -> Teams + joinUrl")

# (5) onedrive alias resolution + careers ------------------------------------
if src.count("/users/{user_id}/drive") != 6:
    die(f"expected 6 onedrive user_id URLs, found {src.count('/users/{user_id}/drive')}")
src = src.replace("/users/{user_id}/drive", "/users/{_mbx(user_id)}/drive")
done.append("5. onedrive_*: 6 user_id URLs routed through _mbx()")

old = '''    "jeff":  "jeff.price@adaptiveenterprisesllc.com",
}'''
new = '''    "jeff":  "jeff.price@adaptiveenterprisesllc.com",
    # Added 2026-08-22. Verified reachable app-only BEFORE adding (mailFolders returned
    # 8 folders, Inbox 37 unread), so it is already in the Exchange scope group and this
    # alias cannot become one of the "resolves fine then 403s" entries warned about above.
    "careers": "careers@adaptiveenterprisesllc.com",
}'''
if src.count(old) != 1:
    die("_MAILBOX_ALIASES anchor not found")
src = src.replace(old, new)
done.append("6. careers@ alias added")

# --- write, verify ----------------------------------------------------------
out_bytes = src.replace("\n", "\r\n").encode("utf-8")
final = hashlib.sha256(out_bytes).hexdigest()
if final != POST:
    die(f"patched result does not match the tested build.\n"
        f"  expected {POST}\n  produced {final}\nNothing was written.")

stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
bak = f"{TARGET}.bak-{stamp}"
io.open(bak, "wb").write(raw)
io.open(TARGET, "wb").write(out_bytes)

try:
    py_compile.compile(TARGET, doraise=True)
except py_compile.PyCompileError as e:
    io.open(TARGET, "wb").write(raw)
    die(f"patched file failed to compile, ROLLED BACK: {e}")

print("Patched " + TARGET)
for d in done:
    print("  " + d)
print(f"\nBackup: {bak}")
print(f"sha256: {final}  (matches the tested build)")
print("\nNext:")
print("  python test_todo_v19.py    python test_phi_audit.py")
print("  .\\deploy-throne-mcp.ps1 -WhatIf   then   .\\deploy-throne-mcp.ps1")
