#!/usr/bin/env python3
"""Render declarativeAgent.json from instructions.md.

The declarative agent manifest carries the personality in a single `instructions`
string with a hard 8,000-character ceiling. Hand-maintaining that string inside JSON
means escaping every newline and re-counting by eye on each edit, so instead the prose
lives in instructions.md and this script renders and validates it.

    python build-agent.py            # write declarativeAgent.json
    python build-agent.py --check    # validate only, exit 1 if a limit is exceeded

Limits enforced below are from the published schema:
https://learn.microsoft.com/microsoft-365/copilot/extensibility/declarative-agent-manifest-1.8
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "instructions.md")
OUT = os.path.join(HERE, "declarativeAgent.json")

# Schema ceilings. Exceeding any of these is rejected at upload, not at runtime.
MAX_NAME = 100
MAX_DESCRIPTION = 1000
MAX_INSTRUCTIONS = 8000
MAX_STARTERS = 12

NAME = "Adaptive Ops"
DESCRIPTION = (
    "Working assistant for Adaptive Enterprises VR casework. Answers from the case "
    "record rather than from inference, labels every claim as verified or assumed, "
    "cites the file it came from, and refuses to put a consumer name in external mail."
)

# Only capability names confirmed against the published schema are emitted here.
# Others (Email, TeamsMessages, People, Meetings, CodeInterpreter, ...) exist but their
# exact identifier strings should be copied from the schema doc, not guessed.
#
# GROUNDING IS SCOPED ON PURPOSE. Per the schema, omitting BOTH items_by_url and
# items_by_sharepoint_ids lets the agent reach "all OneDrive and SharePoint sources in
# the organization" — the tenant, not just this site. Naming the Throne site keeps
# retrieval on the canonical case record instead of every draft that ever lived in a
# personal OneDrive, which also makes "cite the file" answers less likely to surface a
# superseded document.
#
# READ THIS BEFORE ASSUMING THE SCOPE IS A PRIVACY CONTROL. It is not, on its own.
# Claude-Writes-Log.md sits at the ROOT of the Case Documnents library, i.e. INSIDE this
# scope, and as measured 2026-08-10 carried consumer names on 324 of 1,031 lines because
# PHI_SCRUB_NAMES was empty in production. Narrowing grounding does not exclude it — only
# arming the roster on the connector does. See FINDING-audit-log-phi-2026-08-24.md.
THRONE_SITE_URL = "https://netorg39360.sharepoint.com/sites/MasterChiefsThrone"

CAPABILITIES = [
    {
        "name": "OneDriveAndSharePoint",
        "items_by_url": [{"url": THRONE_SITE_URL}],
    },
    {"name": "WebSearch"},
]

CONVERSATION_STARTERS = [
    {
        "title": "Case status",
        "text": "What is the current authorization status for Client ID [ID]? Cite the file for each fact and mark anything you could not verify.",
    },
    {
        "title": "Draft vendor RFQ",
        "text": "Draft an equipment RFQ for Client ID [ID]. Client ID only, no consumer name anywhere in it.",
    },
    {
        "title": "Hours remaining",
        "text": "How many authorized hours remain for Client ID [ID]? Show the source document and the date it was last updated.",
    },
    {
        "title": "What is unresolved",
        "text": "Review this case folder and list only the open questions the record does not answer.",
    },
]


def fail(msg):
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def main():
    check_only = "--check" in sys.argv

    if not os.path.exists(SRC):
        fail(f"missing {SRC}")
    with open(SRC, encoding="utf-8") as f:
        instructions = f.read().strip()

    if not instructions:
        fail("instructions.md is empty")

    checks = [
        ("name", len(NAME), MAX_NAME),
        ("description", len(DESCRIPTION), MAX_DESCRIPTION),
        ("instructions", len(instructions), MAX_INSTRUCTIONS),
        ("conversation_starters", len(CONVERSATION_STARTERS), MAX_STARTERS),
    ]
    over = [(k, n, lim) for k, n, lim in checks if n > lim]
    for k, n, lim in checks:
        headroom = lim - n
        flag = "OVER" if headroom < 0 else "ok"
        print(f"  {k:<22} {n:>5} / {lim:<5} ({headroom:+d})  {flag}")
    if over:
        fail("; ".join(f"{k} is {n - lim} over its {lim} limit" for k, n, lim in over))

    manifest = {
        "$schema": "https://developer.microsoft.com/json-schemas/copilot/declarative-agent/v1.8/schema.json",
        "version": "v1.8",
        "name": NAME,
        "description": DESCRIPTION,
        "instructions": instructions,
        "capabilities": CAPABILITIES,
        "conversation_starters": CONVERSATION_STARTERS,
    }

    if check_only:
        print("check passed; nothing written")
        return

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
