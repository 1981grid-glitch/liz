"""PHI containment tests for the _audit trail (CLAUDE.md Rule 8) - the section 8 finding's fix.
Every consumer name below is FICTIONAL."""
import ast
import hashlib
import os
import re
import sys

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "throne_mcp_server.py")
source = open(SRC, encoding="utf-8").read()
tree = ast.parse(source)

WANT_FUNCS = {"_blocklist_ref", "_scrub_phi", "_audit"}
WANT_ASSIGNS = {"_BLOCKLIST_INDEX", "_BLOCKLIST_RE"}

func_src, assign_src = {}, {}
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in WANT_FUNCS:
        func_src[node.name] = ast.get_source_segment(source, node)
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id in WANT_ASSIGNS:
                assign_src[t.id] = ast.get_source_segment(source, node)

missing = (WANT_FUNCS - set(func_src)) | (WANT_ASSIGNS - set(assign_src))
if missing:
    print(f"FATAL: could not extract from source: {sorted(missing)}")
    sys.exit(1)
print(f"extracted {len(func_src)} functions + {len(assign_src)} module constants from the real source\n")

ROSTER = ["Fakename Alpha", "Fakename Alphabeta", "Zzyzx Quondam"]
STATE = {"slog": [], "put": []}


def _now():
    return "2026-08-10T00:00:00Z"


def _slog(action, target, **kw):
    STATE["slog"].append((action, target))


def _throne_drive_id():
    return "drive!fake"


class R:
    def __init__(self, code=200, text=""):
        self.status_code, self.text = code, text
        self.is_success = 200 <= code < 300


def _g(method, path, **kw):
    if method == "GET":
        return R(200, "# Claude Writes Log\n")
    STATE["put"].append(kw.get("content", b"").decode("utf-8"))
    return R(200)


def build(roster, scrub_stdout=False):
    ns = {"re": re, "hashlib": hashlib, "CONSUMER_NAME_BLOCKLIST": roster,
          "PHI_SCRUB_NAMES": roster, "AUDIT_SCRUB_STDOUT": scrub_stdout,
          "_now": _now, "_slog": _slog,
          "_throne_drive_id": _throne_drive_id, "_g": _g}
    for name in ("_BLOCKLIST_INDEX", "_BLOCKLIST_RE"):
        exec(assign_src[name], ns)
    for name in ("_blocklist_ref", "_scrub_phi", "_audit"):
        exec(func_src[name], ns)
    return ns


passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}")
    if detail:
        print(f"        {detail}")


ns = build(ROSTER)
scrub, ref, audit = ns["_scrub_phi"], ns["_blocklist_ref"], ns["_audit"]

print("=== 1: the section 17 refusal text must not carry the name into the log ===")
reason = ("blocked: consumer name 'Fakename Alpha' present in subject/body - "
          "vendor comms are Client-ID-only")
out = scrub(f"admin@ae.com -> vendor@example.com :: {reason}")
check("literal name is gone", "Fakename Alpha" not in out, out)
check("an opaque ref replaced it", "entry#0/" in out)
check("the external recipient is PRESERVED for incident review", "vendor@example.com" in out)
check("the reason/rule is PRESERVED", "Client-ID-only" in out)

print("\n=== 2: prefix trap ===")
out = scrub("blocked: consumer name 'Fakename Alphabeta' present")
check("longer entry matched whole, no stranded 'beta'", "beta" not in out, out)
check("resolved to entry#1, not entry#0", "entry#1/" in out)

print("\n=== 3: case-insensitive, and names inside PATHS are scrubbed too ===")
out = scrub("write_file | Cases/FAKENAME ALPHA/notes.md")
check("uppercase occurrence scrubbed", "FAKENAME" not in out.upper().replace("[CONSUMER", ""), out)
out = scrub("write_file | Cases/Fakename Alpha/Fakename Alpha - AT Evaluation.html")
check("both occurrences on one path line scrubbed",
      "Fakename Alpha" not in out and out.count("entry#0/") == 2, out)

print("\n=== 4: end-to-end - the bytes actually PUT to SharePoint ===")
STATE["put"].clear()
STATE["slog"].clear()
audit("send_mail_BLOCKED", f"admin@ae.com -> vendor@example.com :: {reason}")
body = STATE["put"][-1]
check("SharePoint mirror contains NO consumer name", "Fakename Alpha" not in body)
check("SharePoint mirror contains the ref", "entry#0/" in body, body.strip().splitlines()[-1])
check("line format preserved",
      re.search(r"^- \S+ \| send_mail_BLOCKED \| ", body.splitlines()[-1]) is not None)

print("\n=== 5: stdout keeps fidelity by default, scrubs when asked ===")
check("default: stdout trail RETAINS the name for incident review",
      "Fakename Alpha" in STATE["slog"][-1][1])
ns2 = build(ROSTER, scrub_stdout=True)
STATE["slog"].clear()
ns2["_audit"]("send_mail_BLOCKED", reason)
check("AUDIT_SCRUB_STDOUT=1: stdout is scrubbed too",
      "Fakename Alpha" not in STATE["slog"][-1][1], STATE["slog"][-1][1])

print("\n=== 6: empty roster (PRODUCTION'S CURRENT STATE) must be a safe no-op ===")
ns3 = build([])
check("no crash building an empty blocklist regex", ns3["_BLOCKLIST_RE"] is None)
check("passthrough unchanged", ns3["_scrub_phi"]("write_file | Cases/Anyone/x.md")
      == "write_file | Cases/Anyone/x.md")
check("empty/None target does not crash", ns3["_scrub_phi"]("") == ""
      and ns3["_scrub_phi"](None) is None)

print("\n=== 7: the ref is stable across roster edits ===")
h_before = ref("Zzyzx Quondam").split("/")[1]
reordered = build(["Zzyzx Quondam", "Fakename Alpha", "Fakename Alphabeta"])
after = reordered["_blocklist_ref"]("Zzyzx Quondam")
check("index moved with the roster", after.startswith("entry#0") and
      ref("Zzyzx Quondam").startswith("entry#2"), f"{ref('Zzyzx Quondam')} -> {after}")
check("hash survived the reorder", after.split("/")[1] == h_before)
check("a name NOT on the roster still yields a hash, never the name",
      ref("Someone Unlisted") == "entry#?/" +
      hashlib.sha256(b"someone unlisted").hexdigest()[:8])

print("\n=== 8: free-text vectors ===")
for label, action, target in [
        ("outlook_draft subject", "outlook_draft",
         "zach@ae.com: Re: Fakename Alpha accommodation plan"),
        ("calendar_create subject", "calendar_create",
         "zach@ae.com: Eval - Zzyzx Quondam @ 2026-08-12"),
        ("todo_create title", "todo_create",
         "zach@ae.com:AAMk123 Call Fakename Alphabeta re: worksite"),
        ("attachment filename", "outlook_add_attachment",
         "zach@ae.com:msg1 +Fakename Alpha - Eval.docx")]:
    STATE["put"].clear()
    audit(action, target)
    b = STATE["put"][-1]
    check(label, not any(n in b for n in ROSTER), b.strip().splitlines()[-1])

print("\n" + "=" * 60)
print(f"PHI audit totals: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
