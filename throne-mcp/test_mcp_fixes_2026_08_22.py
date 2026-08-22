"""
Proves the 2026-08-22 fixes, against the REAL code.

Same discipline as test_todo_v19.py / test_phi_audit.py: every function under test is
extracted out of throne_mcp_server.py via `ast` and exec'd, never retyped, so this file
cannot drift from what ships.

    python test_mcp_fixes_2026_08_22.py     # exit 0 = green
"""
import ast
import base64
import html
import os
import re
import sys

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "throne_mcp_server.py")
source = open(SRC, encoding="utf-8").read()
tree = ast.parse(source)

WANT_FUNCS = {"_normalize_body_html", "calendar_create_event", "outlook_add_attachment",
              "onedrive_list", "onedrive_delete", "_mbx"}
WANT_ASSIGNS = {"_ATTACH_SIMPLE_MAX", "_ATTACH_MAX", "_UPLOAD_CHUNK",
                "_REAL_TAG_RE", "_ESCAPED_TAG_RE", "_MAILBOX_ALIASES"}

func_src, assign_src = {}, {}
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in WANT_FUNCS:
        seg = ast.get_source_segment(source, node)
        func_src[node.name] = "\n".join(
            l for l in seg.splitlines() if not l.strip().startswith("@"))
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id in WANT_ASSIGNS:
                assign_src[t.id] = ast.get_source_segment(source, node)

missing = (WANT_FUNCS - set(func_src)) | (WANT_ASSIGNS - set(assign_src))
if missing:
    print(f"FATAL: could not extract from source: {sorted(missing)}")
    sys.exit(1)
print(f"extracted {len(func_src)} functions + {len(assign_src)} constants from the real source\n")

STATE = {"graph": [], "puts": [], "audit": []}


class R:
    def __init__(self, code=200, js=None, headers=None):
        self.status_code, self._j = code, (js or {})
        self.headers = headers or {}
        self.is_success = 200 <= code < 300
        self.text = ""
    def json(self): return self._j
    def raise_for_status(self):
        if not self.is_success:
            raise AssertionError(f"HTTP {self.status_code}")


GRAPH_REPLY = {"next": None}


def _g(method, path, **kw):
    STATE["graph"].append((method, path, kw.get("json")))
    if GRAPH_REPLY["next"] is not None:
        r, GRAPH_REPLY["next"] = GRAPH_REPLY["next"], None
        return r
    return R(200, {"id": "evt1", "webLink": "https://outlook/evt1"})


class _FakeHttpx:
    @staticmethod
    def put(url, content=None, timeout=None, headers=None):
        STATE["puts"].append((url, len(content), headers.get("Content-Range")))
        span, total_s = headers["Content-Range"].split()[1].split("/")
        total = int(total_s)
        end = int(span.split("-")[1])
        if end + 1 >= total:
            return R(201, {}, {"Location": "https://o/Messages('m')/Attachments('ATT-9')"})
        return R(202)


def build():
    ns = {"re": re, "html": html, "base64": base64, "httpx": _FakeHttpx,
          "DEFAULT_TZ": "America/Indiana/Indianapolis",
          "_auth_ok": lambda: True,
          "_consumer_block": lambda *a, **k: None,
          "_graph_err": lambda r: {"error": f"graph {r.status_code}"},
          "_audit": lambda a, t: STATE["audit"].append((a, t)),
          "_g": _g, "os": os}
    for name in ("_REAL_TAG_RE", "_ESCAPED_TAG_RE", "_ATTACH_SIMPLE_MAX",
                 "_ATTACH_MAX", "_UPLOAD_CHUNK", "_MAILBOX_ALIASES"):
        exec(assign_src[name], ns)
    for name in ("_normalize_body_html", "_mbx", "calendar_create_event",
                 "outlook_add_attachment", "onedrive_list", "onedrive_delete"):
        exec(func_src[name], ns)
    return ns


ns = build()
passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1; print(f"  PASS  {label}")
    else:
        failed += 1; print(f"  FAIL  {label}")
    if detail:
        print(f"        {detail}")


def reset():
    STATE["graph"].clear(); STATE["puts"].clear(); STATE["audit"].clear()


# ---------------------------------------------------------------------------
print("=== FIX 1: _normalize_body_html is REACHABLE (was dead code after `return`) ===")
# The regression: the call sat one indent too deep, inside `if not _auth_ok():` and
# AFTER its return, so an entity-escaped body shipped literal <p> text to vendors.
reset()
escaped = "&lt;p&gt;Quote for Client ID 357257&lt;/p&gt;"
ns["calendar_create_event"]("zach", "Vendor call", "2026-09-01T10:00:00",
                            "2026-09-01T11:00:00", body_html=escaped)
sent = STATE["graph"][-1][2]
body_sent = (sent.get("body") or {}).get("content", "")
check("escaped body was UNESCAPED before reaching Graph",
      body_sent == "<p>Quote for Client ID 357257</p>", repr(body_sent))
check("literal '&lt;p&gt;' no longer ships to the recipient", "&lt;p&gt;" not in body_sent)

# The conservative half of the contract: a body that already has real markup is untouched.
reset()
real = "<p>Real markup quoting &lt;p&gt; on purpose</p>"
ns["calendar_create_event"]("zach", "s", "2026-09-01T10:00:00", "2026-09-01T11:00:00",
                            body_html=real)
check("a body with REAL markup is passed through byte-for-byte",
      (STATE["graph"][-1][2].get("body") or {}).get("content") == real)

# ---------------------------------------------------------------------------
print("\n=== FIX 2: calendar_create_event can make a TEAMS meeting ===")
reset()
GRAPH_REPLY["next"] = R(200, {"id": "e2", "webLink": "https://outlook/e2",
                              "isOnlineMeeting": True,
                              "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/x"}})
out = ns["calendar_create_event"]("zach", "Teams sync", "2026-09-01T10:00:00",
                                  "2026-09-01T11:00:00", is_online_meeting=True)
sent = STATE["graph"][-1][2]
check("isOnlineMeeting set on the Graph payload", sent.get("isOnlineMeeting") is True)
check("onlineMeetingProvider = teamsForBusiness",
      sent.get("onlineMeetingProvider") == "teamsForBusiness", str(sent.get("onlineMeetingProvider")))
check("joinUrl returned to the caller",
      out.get("joinUrl") == "https://teams.microsoft.com/l/x", str(out))
check("no false warning when the meeting really provisioned", "warning" not in out)

reset()
GRAPH_REPLY["next"] = R(200, {"id": "e3", "webLink": "w", "isOnlineMeeting": True})
out = ns["calendar_create_event"]("zach", "s", "2026-09-01T10:00:00",
                                  "2026-09-01T11:00:00", is_online_meeting=True)
check("Graph 201 with NO joinUrl is reported, not silently called ok",
      "warning" in out and out.get("joinUrl") is None, str(out.get("warning"))[:70])

reset()
ns["calendar_create_event"]("zach", "s", "2026-09-01T10:00:00", "2026-09-01T11:00:00")
check("default (no flag) sends NO online-meeting fields - old behavior intact",
      "isOnlineMeeting" not in STATE["graph"][-1][2])

# ---------------------------------------------------------------------------
print("\n=== FIX 3: outlook_add_attachment handles BIG files ===")
small = base64.b64encode(b"x" * 1024).decode()
reset()
GRAPH_REPLY["next"] = R(200, {"id": "A1", "name": "small.pdf"})
out = ns["outlook_add_attachment"]("zach", "msg1", "small.pdf", small)
check("<=3MB still uses the plain POST (one Graph call, no chunks)",
      out.get("upload") == "simple" and not STATE["puts"], str(out))
check("plain POST targets /attachments", STATE["graph"][-1][1].endswith("/attachments"))

big_len = 8 * 1024 * 1024                      # 8MB - the old code hard-failed here
big = base64.b64encode(b"y" * big_len).decode()
reset()
GRAPH_REPLY["next"] = R(200, {"uploadUrl": "https://upload/session/1"})
out = ns["outlook_add_attachment"]("zach", "msg1", "big.pdf", big, content_type="application/pdf")
check("8MB attachment SUCCEEDS (was a hard Graph failure before)", out.get("ok") is True, str(out))
check("routed through an attachment upload SESSION", out.get("upload") == "session")
check("createUploadSession was called",
      STATE["graph"][-1][1].endswith("/attachments/createUploadSession"))
item = STATE["graph"][-1][2]["AttachmentItem"]
check("session declares attachmentType/name/size",
      item["attachmentType"] == "file" and item["size"] == big_len, str(item))
check("contentType forwarded", item.get("contentType") == "application/pdf")

chunk = ns["_UPLOAD_CHUNK"]
check(f"chunked into {len(STATE['puts'])} PUTs of <= {chunk}B",
      all(n <= chunk for _, n, _ in STATE["puts"]) and len(STATE["puts"]) > 1)
check("every chunk is a multiple of 320KiB except the last",
      all(n % (320 * 1024) == 0 for _, n, _ in STATE["puts"][:-1]),
      str([n for _, n, _ in STATE["puts"]]))
first, last = STATE["puts"][0][2], STATE["puts"][-1][2]
last_end = last.split()[1].split("/")[0].split("-")[1]
check("Content-Range starts at 0 and ends at total-1",
      first.startswith("bytes 0-") and last.endswith(f"/{big_len}")
      and last_end == str(big_len - 1), f"{first} .. {last}")
check("byte coverage is exactly the payload, no gaps/overlap",
      sum(n for _, n, _ in STATE["puts"]) == big_len)
check("attachment id recovered from the Location header (body is empty on 201)",
      out.get("id") == "ATT-9", str(out.get("id")))

reset()
over = base64.b64encode(b"z" * 16).decode()
saved = ns["_ATTACH_MAX"]
ns["_ATTACH_MAX"] = 8                          # shrink the ceiling rather than build 150MB
out = ns["outlook_add_attachment"]("zach", "m", "huge.bin", over)
ns["_ATTACH_MAX"] = saved
check("over-ceiling payload refused BEFORE any Graph call",
      "error" in out and not STATE["graph"], str(out))

reset()
out = ns["outlook_add_attachment"]("zach", "m", "empty.bin", "")
check("empty payload refused", "error" in out and not STATE["graph"])
out = ns["outlook_add_attachment"]("zach", "m", "bad.bin", "!!!not-base64!!!")
check("invalid base64 refused", "error" in out and "base64" in out["error"])

# ---------------------------------------------------------------------------
print("\n=== FIX 4: onedrive_* accept the short aliases (were 404 'User not found') ===")
reset()
ns["onedrive_list"]("zach")
check("onedrive_list('zach') resolves to the full UPN",
      "/users/zach.eltzroth@adaptiveenterprisesllc.com/drive" in STATE["graph"][-1][1],
      STATE["graph"][-1][1])
reset()
ns["onedrive_delete"]("jeff", "item1", confirm=True)
check("onedrive_delete('jeff') resolves too",
      "/users/jeff.price@adaptiveenterprisesllc.com/drive" in STATE["graph"][-1][1],
      STATE["graph"][-1][1])
reset()
ns["onedrive_list"]("zach.eltzroth@adaptiveenterprisesllc.com")
check("a full UPN still passes through unchanged (no double-resolution)",
      "/users/zach.eltzroth@adaptiveenterprisesllc.com/drive" in STATE["graph"][-1][1])

# ---------------------------------------------------------------------------
print("\n=== FIX 5: careers@ alias ===")
check("'careers' resolves to the careers@ mailbox",
      ns["_mbx"]("careers") == "careers@adaptiveenterprisesllc.com", ns["_mbx"]("careers"))
check("existing aliases unchanged",
      ns["_mbx"]("zach") == "zach.eltzroth@adaptiveenterprisesllc.com"
      and ns["_mbx"]("admin") == "admin@adaptiveenterprisesllc.com"
      and ns["_mbx"]("jeff") == "jeff.price@adaptiveenterprisesllc.com")
check("unknown value still passes through untouched",
      ns["_mbx"]("someone@else.com") == "someone@else.com")

print("\n" + "=" * 60)
print(f"2026-08-22 fix totals: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
