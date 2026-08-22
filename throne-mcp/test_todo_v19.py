"""
Re-run the adversarial review's exact attacks against the FIXED code.

Extracts the real function source out of throne_mcp_server.py (no retyping) and runs it
against a simulated Graph, so every result below is observed behavior of the shipping code.
"""
import ast
import os
import re
import sys

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "throne_mcp_server.py")
source = open(SRC, encoding="utf-8").read()
tree = ast.parse(source)

WANT = {"_todo_body_text", "_todo_collection", "todo_set_steps", "todo_append_note",
        "_todo_task_url", "_todo_bool"}
segments = []
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in WANT:
        seg = ast.get_source_segment(source, node)
        seg = "\n".join(l for l in seg.splitlines() if not l.strip().startswith("@mcp.tool"))
        segments.append(seg)
print(f"extracted {len(segments)} functions from the real source\n")

GRAPH = "https://graph.microsoft.com/v1.0"
_TAG_RE = re.compile(r"<[^>]+>")
_TODO_DROP_ELEMENTS = re.compile(r"<(script|style|head)\b[^>]*>.*?</\1>",
                                 re.IGNORECASE | re.DOTALL)
STATE = {"steps": [], "calls": [], "next_id": 100, "body": None}


class R:
    def __init__(self, ok, js=None, code=200):
        self.is_success, self._j, self.status_code = ok, (js or {}), code
    def json(self): return self._j


def _auth_ok(): return True
def _audit(*a, **k): STATE["calls"].append(("audit",) + a)
def _mbx(u): return "zach@x"
def _now(): return "2026-08-10T00:00:00Z"
def _graph_err(r): return {"error": f"graph {r.status_code}"}
def _token(): return "t"


def _g(method, path, **kw):
    STATE["calls"].append((method, path.split("/")[-1], kw.get("json")))
    if method == "POST":
        STATE["next_id"] += 1
        item = {"id": str(STATE["next_id"]),
                "displayName": kw["json"]["displayName"],
                "isChecked": kw["json"].get("isChecked", False)}
        STATE["steps"].append(item)
        return R(True, item)
    if method == "PATCH":
        sid = path.rsplit("/", 1)[-1]
        for s in STATE["steps"]:
            if s["id"] == sid:
                s.update(kw.get("json") or {})
        if "checklistItems" not in path:
            STATE["body"] = (kw.get("json") or {}).get("body")
        return R(True, {})
    if method == "DELETE":
        sid = path.rsplit("/", 1)[-1]
        STATE["steps"][:] = [s for s in STATE["steps"] if s["id"] != sid]
        return R(True, {})
    if method == "GET":
        return R(True, {"body": STATE["body"]} if "checklistItems" not in path
                 else {"value": list(STATE["steps"])})
    return R(False, {}, 500)


def _send_with_retry(method, url, **kw):
    return R(True, {"value": list(STATE["steps"])})


ns = dict(globals())
exec("\n\n".join(segments), ns)
set_steps = ns["todo_set_steps"]
append_note = ns["todo_append_note"]
body_text = ns["_todo_body_text"]

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  PASS  {label}")
    else:
        FAIL += 1; print(f"  FAIL  {label}")
    if detail:
        print(f"        {detail}")


def seed(names):
    STATE["steps"][:] = [{"id": str(i), "displayName": n, "isChecked": False}
                         for i, n in enumerate(names)]
    STATE["calls"].clear()


print("=== SHIP-BLOCKER 1: malformed key + prune must NOT wipe the checklist ===")
seed(["Draft eval", "Send to VR", "Book travel"])
out = set_steps("zach", "L", "T", [{"nmae": "Draft eval"}], prune=True, confirm=True)
check("returns an error instead of ok:true", "error" in out, f"returned: {out}")
check("all 3 steps SURVIVE", len(STATE["steps"]) == 3,
      f"steps now: {[s['displayName'] for s in STATE['steps']]}")
check("zero DELETE calls issued",
      not [c for c in STATE["calls"] if c[0] == "DELETE"])

print("\n=== SHIP-BLOCKER 2: prune must require confirm ===")
seed(["a", "b"])
out = set_steps("zach", "L", "T", [], prune=True)
check("refused without confirm=True", "error" in out and "confirm" in out["error"])
check("nothing deleted", len(STATE["steps"]) == 2)
seed(["a", "b"])
out = set_steps("zach", "L", "T", [], prune=True, confirm=True)
check("WITH confirm=True it does prune (intended path)", len(STATE["steps"]) == 0,
      f"deleted={out.get('deleted')}")

print("\n=== SHIP-BLOCKER 3: duplicate names in one call must not double-create ===")
seed([])
out = set_steps("zach", "L", "T", ["Call counselor", "Call counselor"])
posts = [c for c in STATE["calls"] if c[0] == "POST"]
check("exactly ONE step created", len(STATE["steps"]) == 1, f"POSTs issued: {len(posts)}")
check("created list has no duplicate", out["created"] == ["Call counselor"],
      f"created={out['created']}")

print("\n=== SHOULD-FIX 5: last writer wins on conflicting checked values ===")
seed([])
set_steps("zach", "L", "T", [{"name": "Draft eval", "checked": False},
                             {"name": "Draft eval", "checked": True}])
check("one step, final state checked=True",
      len(STATE["steps"]) == 1 and STATE["steps"][0]["isChecked"] is True,
      f"final: {STATE['steps']}")

print("\n=== SHOULD-FIX 4: pre-existing duplicate names both prune ===")
seed(["Call counselor", "Call counselor", "Keep me"])
set_steps("zach", "L", "T", ["Keep me"], prune=True, confirm=True)
check("BOTH duplicates deleted, 'Keep me' survives",
      [s["displayName"] for s in STATE["steps"]] == ["Keep me"],
      f"remaining: {[s['displayName'] for s in STATE['steps']]}")

print("\n=== SHOULD-FIX 8: append into a full HTML document ===")
STATE["body"] = {"content": "<html><body><p>Existing note</p></body></html>",
                 "contentType": "html"}
append_note("zach", "L", "T", "Line one\nLine two")
merged = STATE["body"]["content"]
check("appended text is INSIDE </body>", merged.endswith("</body></html>"), merged)
check("newline became <br>", "Line one<br>Line two" in merged)

print("\n=== SHOULD-FIX 9: <style>/<script> content must not leak into note text ===")
leaky = {"body": {"contentType": "html",
                  "content": "<html><head><style>p{margin:0}</style></head><body>"
                             "<p>Call the counselor</p><script>var x=1;</script>"
                             "<p>Second &amp; final</p></body></html>"}}
txt = body_text(leaky)
check("no CSS leak", "margin:0" not in txt, repr(txt))
check("no JS leak", "var x=1" not in txt)
check("entity decoded", "Second & final" in txt)
check("real text preserved", "Call the counselor" in txt)

print("\n=== ROUND 2 (second reviewer's findings) ===")
print("--- bool('false') must not check a step ---")
seed([])
set_steps("zach", "L", "T", [{"name": "Draft eval", "checked": "false"}])
check("string 'false' -> step is NOT checked",
      STATE["steps"][0]["isChecked"] is False, f"{STATE['steps']}")
seed([])
set_steps("zach", "L", "T", [{"name": "Draft eval", "checked": "true"}])
check("string 'true' -> step IS checked", STATE["steps"][0]["isChecked"] is True)

print("--- already-correct state must be reported, not silently empty ---")
seed(["Draft eval"])
out = set_steps("zach", "L", "T", ["Draft eval"])
check("returns unchanged=['Draft eval'] rather than all-empty",
      out.get("unchanged") == ["Draft eval"], f"{out}")

print("--- prune audits deleted IDs (forensics) but NOT names (PHI) ---")
seed(["Secret step name", "Keep"])
STATE["calls"].clear()
set_steps("zach", "L", "T", ["Keep"], prune=True, confirm=True)
audits = [c for c in STATE["calls"] if c[0] == "audit"]
audit_txt = " ".join(str(a) for a in audits)
check("audit records deleted_ids", "deleted_ids" in audit_txt, audit_txt)
check("audit does NOT contain the step name", "Secret step name" not in audit_txt)

print(f"\n{'=' * 60}\nROUND 2 totals: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
