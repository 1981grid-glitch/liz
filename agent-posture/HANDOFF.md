# Handoff — running one posture across every assistant

`POSTURE.md` is the source of truth. Everything else in this folder is a rendering of it
for a surface that accepts instructions in a particular format and length. This document
says where each rendering goes and how to confirm it actually took effect.

**Nothing here was deployed.** These are artifacts plus placement steps — the session that
produced them is an ephemeral cloud container with no reach into the Skynet workstation or
the Microsoft 365 tenant. Every step below has to be run by a human on a machine that is
signed in.

---

## The surfaces

| Surface | Where the file goes | Scope | Gets |
|---|---|---|---|
| Claude Code | `~/.claude/CLAUDE.md` | every project on that machine | `claude/CLAUDE.md` |
| Claude Code | `<repo>/CLAUDE.md` | one repo, layers over the global | repo-specific, see this repo's own |
| GitHub Copilot | `%USERPROFILE%/copilot-instructions.md` | every Copilot session | `github-copilot/copilot-instructions.md` |
| GitHub Copilot | `<repo>/.github/copilot-instructions.md` | one repo, applies alongside user-level | same file |
| M365 Copilot Chat | Settings → Personalization → Custom instructions | all Copilot chat, no agent needed | `m365-copilot/custom-instructions.txt` |
| M365 Copilot agent | Copilot Studio, or an uploaded app package | one named agent you select | `m365-copilot/declarativeAgent.json` |

---

## 1. Claude Code

Copy `claude/CLAUDE.md` to `~/.claude/CLAUDE.md` on each machine you code from. It applies
to every project. A project's own `CLAUDE.md` layers on top and wins on specifics — that
is why this repo's root `CLAUDE.md` carries Throne architecture and deploy invariants
while the global one carries only temperament.

Cloud sessions (claude.ai/code) start from a fresh container each time, so a file written
into `~/.claude` there does not survive. Only two things persist into a cloud session:
what is **committed to the repo**, and what your account **syncs** (skills and plugins,
which land under `~/.claude/skills/synced/`). So the repo-level `CLAUDE.md` is the copy
that actually reaches a cloud session — keep it current.

**Verify:** start a session and ask it to state its posture rules without reading any
file. If it can, the global file loaded.

## 2. GitHub Copilot

Two placements, and they stack:

- `%USERPROFILE%/copilot-instructions.md` — user-level, every session, no repo required.
  Create the file if it does not exist.
- `.github/copilot-instructions.md` — per repository, committed, applies to everyone
  working in that repo alongside their own user-level file.

In Visual Studio and SSMS this must be switched on: **Tools → Options → GitHub → Copilot →
Copilot Chat → "Enable custom instructions to be loaded from .github/copilot-instructions.md
files and added to requests."**

**Verify:** custom instructions are not visible in the chat view. When Copilot actually
uses the file, it lists it in the **References** section of its response. If References
never names it, it is not loading.

## 3. Microsoft 365 Copilot — the quick path

Settings → **Personalization** → **Custom instructions**. Paste
`m365-copilot/custom-instructions.txt`. It applies to Copilot Chat from then on with no
agent to select and no packaging.

Three things to know before relying on it:

- **A tenant admin can switch it off for you.** The **Enhanced personalization** control
  governs it. It is on by default. If an admin turns it off, the Custom instructions,
  Saved memories, and Chat history toggles all show as off at the user level and cannot be
  turned back on from your side.
- **Personalization and memory are in preview** (Frontier program) and subject to change.
- Custom instructions, saved memories, and inferred chat-history details are stored in a
  hidden folder in your Exchange mailbox, so they inherit mailbox security and compliance —
  encryption at rest, Customer Lockbox. Note that Purview retention policies for Copilot
  Chat do **not** apply to memory, and memory actions do not produce audit log entries.

This field is one free-text box, which is why the rendering is trimmed. Long entries
dilute. Use the agent below when you want the whole posture.

## 4. Microsoft 365 Copilot — the declarative agent

This is the real "give it a personality" mechanism: a named agent you pick from the agent
list, carrying up to 8,000 characters of instructions plus grounding sources and
conversation starters.

`m365-copilot/declarativeAgent.json` is generated — edit `m365-copilot/instructions.md`,
then:

```bash
python build-agent.py            # re-render declarativeAgent.json
python build-agent.py --check    # validate limits only, exit 1 if over
```

Current headroom: instructions **5,420 / 8,000**.

### Path A — Copilot Studio agent builder (no packaging)

Fastest for a solo operator. Create an agent in Copilot Studio and paste the contents of
`instructions.md` into the instructions field, then add the SharePoint knowledge sources
you want it grounded on. Copilot Studio generates the manifest and its `id` for you. No
zip, no icons, no admin upload.

### Path B — app package (version-controlled)

Use when you want the agent's definition to live in git alongside this repo. The
declarative agent manifest is referenced from the app manifest:

```json
"copilotAgents": {
    "declarativeAgents": [
        { "id": "adaptiveOps", "file": "declarativeAgent.json" }
    ]
}
```

Only **one** declarative agent definition is supported per app manifest. Building outside
Copilot Studio means you assign the `id` yourself. Package `manifest.json`,
`declarativeAgent.json`, and both icons (`color.png`, `outline.png`) into a zip, then
upload via **Microsoft 365 admin center → Settings → Integrated apps → Upload custom app**.

The icons are not in this repo — Path B needs them created before the package will
validate.

### Licensing

An agent using only the `WebSearch` capability is available broadly. Any other capability —
including the `OneDriveAndSharePoint` grounding this agent declares — requires a
Microsoft 365 Copilot license or a tenant that allows metered usage. A Copilot Premium
seat covers it.

### Capability names

`build-agent.py` emits only the two capability identifiers confirmed against the published
schema: `OneDriveAndSharePoint` and `WebSearch`. Others exist — Email, Teams messages,
People, Meetings, Code interpreter, Dataverse, Graphic art, Copilot connectors, Embedded
knowledge — but copy their exact identifier strings from the schema reference rather than
guessing, because a wrong one fails at upload:

<https://learn.microsoft.com/microsoft-365/copilot/extensibility/declarative-agent-manifest-1.8>

**Verify:** ask the agent something whose honest answer is "I don't know" — a hours-remaining
question for a Client ID with no authorization document in its grounded sources. A correct
deployment says the record does not show it and names what would settle it. A deployment
that produces a confident number has not picked up the instructions, and that is the exact
failure the posture exists to prevent.

---

## Schema limits

Enforced by `build-agent.py`; exceeding one is rejected at upload, not at runtime.

| Field | Limit |
|---|---|
| `name` | 100 characters |
| `description` | 1,000 characters |
| `instructions` | 8,000 characters |
| `conversation_starters` | 12 entries |
| `actions` | 1–10 plugins |
| declarative agents per app manifest | 1 |

## Maintenance

Edit `POSTURE.md` first, then re-render the surfaces that changed. Renderings that drift
apart are worse than no posture at all, because each tool then enforces a slightly
different set of rules and none of them is the one you think you wrote.

When a rule earns its place because something went wrong, add the incident to it. A rule
that carries the incident that produced it survives editing; a rule that reads as generic
advice gets trimmed by the next person who needs the space.
