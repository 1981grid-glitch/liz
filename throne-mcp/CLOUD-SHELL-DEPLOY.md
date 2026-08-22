# Deploying Throne MCP from a browser (Azure Cloud Shell)

For when you are away from Skynet. Needs only a browser and your Azure login —
no workstation, no local `az`, no OneDrive sync.

This is an alternative to `push-through-2026-08-22.ps1`, which does the same
work but requires the Skynet workstation. Everything else in `DEPLOY_V18.md`
still applies; this only replaces *where* the commands run.

---

## 0. Open Cloud Shell

Go to **https://shell.azure.com** and pick **Bash**. It comes with `az` already
signed in as you, so there is no `az login` step. (First use may ask to create a
storage account — that is a one-time Cloud Shell setup, unrelated to this deploy.
The Azure mobile app has the same Cloud Shell if you are on a phone.)

Confirm you are in the right place:

```bash
az account show --query "{sub:name, id:id}" -o table
az containerapp show -g rg-throne-mcp -n throne-mcp \
  --query "properties.template.containers[0].image" -o tsv
```

That second command is **step zero from the runbook**: no file is authoritative
about what is live — ask Azure. Expect `throne-mcp:3.2` if nothing has shipped
since 2026-08-10.

---

## 1. Get the build context

```bash
rm -rf ~/liz && git clone --depth 1 https://github.com/1981grid-glitch/liz.git ~/liz
cd ~/liz/throne-mcp
```

## 2. Verify what you are about to ship — do not skip

The image is whatever `throne_mcp_server.py` is in this folder, so check it
*before* building rather than diagnosing a bad image afterwards.

```bash
grep -c '^@mcp.tool' throne_mcp_server.py        # expect 69
grep -c 'onenote_' throne_mcp_server.py          # quarantined; must NOT appear as @mcp.tool
grep -c 'is_online_meeting' throne_mcp_server.py # expect >= 4  (Teams fix)
grep -c '_ATTACH_SIMPLE_MAX' throne_mcp_server.py # expect 3    (big-attachment fix)
grep -c '_mbx(user_id)' throne_mcp_server.py     # expect 6    (onedrive alias fix)
grep -c '"careers"' throne_mcp_server.py         # expect 1    (careers alias)
```

The dead-normalizer fix is the one that matters most and is the easiest to
misread, so check it positionally — this must print **0**:

```bash
grep -A1 'return {"error": "unauthorized"}' throne_mcp_server.py \
  | grep -c '^-\s\+body_html = _normalize_body_html'
```

Zero means no `_normalize_body_html` call is still stranded after a `return`.
Seven live calls should exist at the correct indent:

```bash
grep -cE '^    body_html = _normalize_body_html\(body_html\)' throne_mcp_server.py  # expect 7
```

Optional byte check against the tested build:

```bash
sha256sum throne_mcp_server.py
# 71b7c6f9764811743d0e33e9152af5e1193be535271b183911d4fb6b74c0543f
```

If the hash differs but every grep above is correct, it is almost certainly git
normalizing CRLF to LF on clone — harmless to Python and to the image. If a
**grep** is wrong, stop and re-check the source; do not build.

## 3. Pick the next free tag — never hardcode one

Section 10 of `DEPLOY_V18.md` was written because a hardcoded tag is a guard
that expires: a parallel session shipped a version and the script aborted.
Compute it instead.

```bash
az acr repository show-tags -n thronemcpe9ebfc --repository throne-mcp -o tsv | sort -V | tail -5
```

Take the highest and add one. As of 2026-08-10 the top tag was `3.2`, so the
next free is `3.3`.

```bash
TAG=3.3   # <-- set from the command above, do not assume
```

**Never reuse an existing tag.** Tag reuse is what corrupted this registry's
history (`1.8` and `2.2` share a digest). If `show-tags` lists it, pick higher.

Record what is live now, so rollback is a copy-paste rather than a scramble:

```bash
PREV=$(az containerapp show -g rg-throne-mcp -n throne-mcp \
  --query "properties.template.containers[0].image" -o tsv)
echo "rollback with: az containerapp update -g rg-throne-mcp -n throne-mcp --image $PREV"
```

## 4. Build and roll

```bash
az acr build -r thronemcpe9ebfc -t throne-mcp:$TAG .
```

`az acr build` builds **and** pushes server-side — there is no separate
`docker push`, and Cloud Shell does not need a Docker daemon. This is why the
whole thing works from a browser.

```bash
az containerapp update -g rg-throne-mcp -n throne-mcp \
  --image thronemcpe9ebfc.azurecr.io/throne-mcp:$TAG
```

Zero-downtime revision swap; `min-replicas` carries over. No env-var changes
are needed for this release.

## 5. Confirm

```bash
az containerapp show -g rg-throne-mcp -n throne-mcp \
  --query "{image:properties.template.containers[0].image, \
            revision:properties.latestRevisionName, \
            health:properties.runningStatus}" -o table
```

Wait for the new revision to report healthy. If it does not, roll back with the
line step 3 printed.

Then, in claude.ai: toggle the Throne connector **off and back on**, and start a
**new conversation**. `calendar_create_event` gained a parameter, and the tool
manifest is cached per connection — a reconnect alone will not refresh it.

## 6. Prove it actually took

The cheapest end-to-end check, because it exercises a code path that only exists
in the new image and has no side effects:

```
onedrive_list(user_id: "zach")
```

- **Before:** `404 ResourceNotFound: User not found` — the alias was passed to
  Graph unresolved.
- **After:** a folder listing.

Then confirm the rest of the surface is intact:

```
throne_healthcheck()      -> token ok, zach + admin reachable, both sites reachable
```

Confirm the tool count is **69** and, per section 7, disambiguate **by name**:
`todo_set_steps` present and **no** `onenote_*` means the manifest is correct.

---

## What this deploy carries

More than the five fixes. The canonical source was last written **2026-08-20**
but the live image was built **2026-08-10**, so this also ships the undeployed
work from that window — including `_normalize_body_html` itself, not just the
indent fix that makes it reachable. Expect a larger diff than the five items.
