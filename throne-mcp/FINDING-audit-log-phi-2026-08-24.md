# Finding — Claude-Writes-Log.md carries consumer names in production

**Raised 2026-08-24** while scoping a Microsoft 365 Copilot agent's SharePoint grounding.
Not fixed here. This is a note, not a patch — the fix is an env change plus remediation of
an existing file, neither of which belongs in a documentation PR.

## What

`_audit()` mirrors every write to `Claude-Writes-Log.md` at the **root of the shared
"Case Documnents" library**, readable by any staff member with library access. Names are
supposed to be replaced with opaque references by `_scrub_phi()` at that choke point.

`_scrub_phi` is a no-op when the roster is empty:

```python
if not _BLOCKLIST_RE or not text:
    return text
```

and `_BLOCKLIST_RE` is `None` when `_BLOCKLIST_INDEX` is empty, which it is when
`PHI_SCRUB_NAMES` and `CONSUMER_NAME_BLOCKLIST` are both unset — both default to `""`.

The source already says this happened, in its own words at `throne_mcp_server.py:321-324`:

> NOTE: this is only as good as the roster. With PHI_SCRUB_NAMES and
> CONSUMER_NAME_BLOCKLIST both empty the scrub is a no-op — measured 2026-08-10, that is
> production's state, and it is why 324 of the 1,031 lines then in Claude-Writes-Log.md
> carried a consumer name. Arming PHI_SCRUB_NAMES is what turns this on; it does NOT
> affect mail refusal.

So the control is implemented correctly and is switched off. Rule 8 says a consumer's
identifiers live only in that consumer's own case folder; a root-level shared log naming
324 lines' worth of consumers is the opposite of that.

## Why it is being raised now

A declarative agent grounded on this site would index that file and quote from it on
request. That converts a passive exposure — a log nobody opens — into an actively
retrievable one, surfaced by an assistant whose own instructions forbid putting a consumer
name in external mail. The agent would be following every instruction correctly and still
handing back names, because the log is a legitimate grounded source.

**Scoping the agent does not mitigate this.** The log lives inside the Case Documnents
library, which is inside the Throne site — the very scope you would ground on. There is no
grounding configuration that includes the case record and excludes a file at its root.
The agent scope was narrowed anyway, on separate merits (see `build-agent.py`), but it
should not be mistaken for a control over this.

## Status — CONFIRMED live in production, verified 2026-08-24

Verified from Skynet, signed in, via `az containerapp show -g rg-throne-mcp -n throne-mcp`:

```
PHI_SCRUB_NAMES         : ABSENT
CONSUMER_NAME_BLOCKLIST : ABSENT
AUDIT_SCRUB_STDOUT      : ABSENT
image                   : thronemcpe9ebfc.azurecr.io/throne-mcp:3.2
revision                : throne-mcp--0000020
```

All three are **absent**, not merely empty — the distinction matters because an empty
`--query ...value` alone can't rule out a value delivered via `secretRef`, which is why the
check was widened to list every env var name first. `_scrub_phi()` has been a no-op since
before the 2026-08-10 measurement and still is today. Every line appended to
`Claude-Writes-Log.md` since then, including whatever was written this week, carries
consumer names in plain text at the root of a library any staff member with access can
open.

Also confirmed, incidentally, from the same query: the deployed image is still `3.2`
(`throne-mcp--0000020`). The 2026-08-22 fixes have not shipped — independent confirmation
of what `DEPLOY-NOTES-2026-08-22.md` already said, not a new problem.

What remains open is not whether the scrub is off — that's settled — but **where the
current roster lives.** `PHI_SCRUB_NAMES` wants every consumer who has ever appeared in a
path or subject (~84 names as of 2026-08-10). This session cannot locate that roster: it
has no Azure CLI, and the Throne MCP connector it was using to read the site disconnected
mid-session and needs reauthorization before it can be queried again. Whoever arms this
should pull the roster from wherever it is canonically maintained, not retype it from
memory.

## Fix, when someone picks this up

Two parts. The first without the second stops the bleeding but leaves the wound.

1. **Arm the roster.** `PHI_SCRUB_NAMES` takes the broad list — every consumer who has ever
   appeared in a path or subject, ~84 names as of the 2026-08-10 count. Env-var change, no
   image rebuild:

   ```bash
   az containerapp update -g rg-throne-mcp -n throne-mcp \
     --set-env-vars PHI_SCRUB_NAMES="<broad roster>"
   ```

   Keep it separate from `CONSUMER_NAME_BLOCKLIST`, which is curated and narrow *because it
   also refuses mail*. The source is explicit that merging them would redact the log
   correctly and simultaneously start refusing routine internal mail. This is already
   documented at `throne_mcp_server.py:69-77`; do not "simplify" the two lists into one.

2. **Remediate the existing file.** Arming the roster only affects future writes. The lines
   already in `Claude-Writes-Log.md` stay exactly as they are. Rewriting it in place through
   `_audit`'s own path is not appropriate — decide deliberately whether to scrub in place,
   archive to a permission-restricted location, or truncate, and record which was chosen.

## Worth considering separately

The stdout/Log Analytics trail keeps full fidelity by design, on the reasoning that
Container Apps logs are gated by Azure RBAC rather than library permissions. That reasoning
is sound and is not what this finding is about. But it does mean a complete audit trail
already exists somewhere access-controlled — which raises the question of whether the
SharePoint mirror needs to carry identifiers at all, or whether it should be reference-only
regardless of roster state. That would make the control independent of an env var somebody
has to remember to set, which is the actual failure mode here.
