# Deploying cartographer — the short version

Three pastes, two browser tabs. No workstation, no Docker, no local `az` or
`gcloud`. Works from a phone.

Everything that *can* be automated is. What's left needs you because Google and
Anthropic require an authenticated human for it — see "Why isn't this one paste"
at the bottom.

---

## 1. Azure Cloud Shell — get the egress IPs

Open **https://shell.azure.com** (or the Cloud Shell button in the Azure mobile
app), pick **Bash**, paste:

```bash
git clone --depth 1 https://github.com/1981grid-glitch/liz.git ~/liz 2>/dev/null || git -C ~/liz pull -q
bash ~/liz/cartographer-mcp/scripts/01-azure-egress.sh
```

Reads public metadata, changes nothing. Prints the egress IPs and the exact next
command with them already filled in. **Copy that command.**

## 2. Google Cloud Shell — provision everything

Open **https://shell.cloud.google.com**. `gcloud` is already signed in as you.
Paste the command step 1 printed. It:

- creates the `adaptive-maps-mcp` project (or reuses it)
- links billing
- enables exactly three APIs — Places (New), Routes, Geocoding
- mints one API key, **restricted to those APIs and those IPs at creation** —
  never unrestricted, not even briefly
- prints the key and the next command

**If it stops asking for a billing account:** that's the one thing no script can
do. Create one at https://console.cloud.google.com/billing and re-run — Google
requires an authenticated human to accept terms and attach a payment method.

## 3. Azure Cloud Shell — build and deploy

Back in the first tab, paste the command step 2 printed. It:

- builds the image server-side with `az acr build` (no Docker daemon needed —
  this is what makes a browser sufficient)
- deploys into Throne's existing Container Apps environment
- injects the key as a Container App **secret**
- creates the Entra app registration and wires up OAuth, mirroring Throne
- polls `/healthz` and tells you whether the rollout actually worked

## 4. claude.ai — connect it

Settings → Connectors → Add custom connector. Paste the MCP URL step 3 printed
(`https://<fqdn>/mcp`). **Leave the OAuth client fields blank** — the server is
its own OAuth proxy, same as Throne.

Then in a **new** conversation: `maps_healthcheck`. Expect `key_status: "ok"`.

Test from the phone **and** desktop. Desktop-only is a failed build.

---

## Worth 2 more minutes: quota caps

Script 2 prints direct links. Set each API to ~500 requests/day.

Quotas are the only guard Google actually enforces — a cap refuses the call and
nothing bills. The $10 budget alert just tells you after the money is gone.
Expected real usage is $0/month (see `../COST-MODEL.md`), so a 500/day cap is
pure headroom.

## If Maps starts returning REQUEST_DENIED later

Container Apps egress IPs are only guaranteed stable behind a NAT gateway.
Without one they can rotate when the environment restarts. Re-run scripts 1 and
2 — script 2 is idempotent and just refreshes the existing key's allowed IPs.

## Why isn't this one paste?

Three things genuinely need a human, and no amount of automation changes that:

| Step | Why |
|---|---|
| Billing account | Google requires an authenticated human to accept terms and attach a payment method |
| The two Cloud Shells | Azure and Google each authenticate you separately; neither can reach the other |
| Connector registration | claude.ai's connector UI has no API |

The chicken-and-egg between them is real, not incidental: the key must be
IP-restricted at creation, so Azure has to be asked for the IPs *before* Google
can mint the key — and the key has to exist before Azure can deploy with it.
