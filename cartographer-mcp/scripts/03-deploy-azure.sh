#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 03 — Build + deploy cartographer.  RUN IN: Azure Cloud Shell (Bash)
#
#   usage:  bash 03-deploy-azure.sh "<API_KEY_FROM_SCRIPT_02>"
#
# Builds the image server-side with `az acr build` (no Docker daemon, which is
# what makes this work from a browser), deploys into Throne's existing Container
# Apps environment, injects the Maps key as a Container App SECRET (never an
# image layer, never a repo file), and verifies the key from inside the running
# container -- the only place an IP-restricted key can succeed.
#
# Then tries to mirror Throne's Entra OAuth so claude.ai can connect. If your
# account cannot create app registrations, the deploy still completes with a
# generated static bearer and prints the exact remaining step. It never falls
# back to anonymous.
#
# Idempotent: re-running rebuilds at the next free tag and rolls the app.
# ---------------------------------------------------------------------------
set -euo pipefail

MAPS_KEY="${1:-}"
RG="${RG:-rg-throne-mcp}"
ACR="${ACR:-thronemcpe9ebfc}"
REF_APP="${REF_APP:-throne-mcp}"
APP="${APP:-cartographer-mcp}"
REPO="cartographer-mcp"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -z "$MAPS_KEY" ]]; then
  echo "usage: bash 03-deploy-azure.sh \"<API_KEY>\"" >&2
  echo "Get the key by running 02-provision-google.sh in Google Cloud Shell first." >&2
  exit 1
fi
[[ -f "$SRC/Dockerfile" ]] || { echo "Dockerfile not found in $SRC" >&2; exit 1; }

echo "== Target =="
az account show --query "{sub:name, id:id}" -o tsv

ENV_ID="$(az containerapp show -g "$RG" -n "$REF_APP" --query "properties.environmentId" -o tsv)"
[[ -n "$ENV_ID" ]] || { echo "Could not read Throne's environment id." >&2; exit 1; }
echo "Environment: ${ENV_ID##*/}"

# --- tag: computed, never hardcoded ----------------------------------------
# A hardcoded tag is a guard that expires, and tag reuse already corrupted this
# registry's history once (1.8 and 2.2 share a digest). Compute it.
LAST="$(az acr repository show-tags -n "$ACR" --repository "$REPO" -o tsv 2>/dev/null \
        | sort -V | tail -1 || true)"
if [[ -z "$LAST" ]]; then TAG="1.0"; else
  TAG="$(awk -F. '{printf "%d.%d", $1, $2+1}' <<<"$LAST")"
fi
echo "Image tag: $REPO:$TAG  (previous: ${LAST:-none})"

echo
echo "== Building server-side in ACR =="
az acr build -r "$ACR" -t "$REPO:$TAG" "$SRC" >/dev/null
IMAGE="$ACR.azurecr.io/$REPO:$TAG"
echo "   built $IMAGE"

# --- deploy ----------------------------------------------------------------
BEARER="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 48)"
TENANT="$(az account show --query tenantId -o tsv)"

echo
if az containerapp show -g "$RG" -n "$APP" >/dev/null 2>&1; then
  echo "== Updating existing app '$APP' =="
  az containerapp secret set -g "$RG" -n "$APP" \
    --secrets "google-maps-api-key=$MAPS_KEY" >/dev/null
  az containerapp update -g "$RG" -n "$APP" --image "$IMAGE" >/dev/null
else
  echo "== Creating app '$APP' =="
  # max-replicas 1: the cache is per-replica and this is a personal-volume
  # service; a second replica would only halve cache hits, not add capacity.
  az containerapp create -g "$RG" -n "$APP" \
    --environment "$ENV_ID" \
    --image "$IMAGE" \
    --target-port 8080 --ingress external --transport auto \
    --min-replicas 0 --max-replicas 1 \
    --secrets "google-maps-api-key=$MAPS_KEY" "mcp-bearer=$BEARER" \
    --env-vars "GOOGLE_MAPS_API_KEY=secretref:google-maps-api-key" \
               "MCP_BEARER=secretref:mcp-bearer" \
               "AZURE_TENANT_ID=$TENANT" \
    >/dev/null
fi

FQDN="$(az containerapp show -g "$RG" -n "$APP" \
         --query "properties.configuration.ingress.fqdn" -o tsv)"
echo "   https://$FQDN"

# --- verify the key from INSIDE the container ------------------------------
echo
echo "== Verifying the Maps key from inside the container =="
echo "   (the key is IP-restricted; this is the only place it can succeed)"
# The server exposes an unauthenticated /healthz that makes no outbound call,
# so this is free, needs no TTY, and cannot be abused to burn quota.
echo "   waiting for the revision to come up..."
OK=0
for i in $(seq 1 20); do
  if BODY="$(curl -fsS --max-time 10 "https://$FQDN/healthz" 2>/dev/null)"; then
    echo "   $BODY"
    OK=1; break
  fi
  sleep 6
done

if [[ "$OK" != "1" ]]; then
  echo "   /healthz did not answer after ~2 min."
  echo "   Check:  az containerapp logs show -g $RG -n $APP --tail 50"
elif ! grep -q '"api_key_configured": *true' <<<"$BODY"; then
  echo "   WARNING: the container is up but no Maps key is injected."
elif grep -q '"auth_mode": *"locked"' <<<"$BODY"; then
  echo "   WARNING: no auth armed -- every tool call will be refused."
else
  echo "   container healthy, key injected, auth armed."
fi

# /healthz proves the key is PRESENT, not that Google accepts it -- validating
# that costs a real call, so it lives behind auth in maps_healthcheck. Run that
# from claude.ai once connected; REQUEST_DENIED there means the egress IP moved
# (re-run 01 and 02) or an API is missing from the key's restriction list.

# --- OAuth, mirroring Throne ------------------------------------------------
echo
echo "== Entra OAuth (so claude.ai can connect) =="
REDIRECT="https://$FQDN/auth/callback"

# Prefer Throne's actual redirect path over an assumed one: read its app
# registration and reuse whatever path it really uses.
THRONE_CID="$(az containerapp show -g "$RG" -n "$REF_APP" \
  --query "properties.template.containers[0].env[?name=='OAUTH_CLIENT_ID'].value | [0]" -o tsv 2>/dev/null || true)"
if [[ -n "${THRONE_CID:-}" && "$THRONE_CID" != "None" ]]; then
  TPATH="$(az ad app show --id "$THRONE_CID" --query "web.redirectUris[0]" -o tsv 2>/dev/null || true)"
  if [[ -n "${TPATH:-}" && "$TPATH" == https://* ]]; then
    REDIRECT="https://$FQDN/$(sed -E 's#^https://[^/]+/##' <<<"$TPATH")"
    echo "   mirroring Throne's redirect path -> $REDIRECT"
  fi
fi

APP_ID="$(az ad app list --display-name "$APP" --query "[0].appId" -o tsv 2>/dev/null || true)"
if [[ -z "${APP_ID:-}" || "$APP_ID" == "None" ]]; then
  APP_ID="$(az ad app create --display-name "$APP" \
              --sign-in-audience AzureADMyOrg \
              --web-redirect-uris "$REDIRECT" \
              --query appId -o tsv 2>/dev/null || true)"
else
  az ad app update --id "$APP_ID" --web-redirect-uris "$REDIRECT" >/dev/null 2>&1 || true
fi

if [[ -n "${APP_ID:-}" && "$APP_ID" != "None" ]]; then
  SECRET="$(az ad app credential reset --id "$APP_ID" --append --years 2 \
              --query password -o tsv 2>/dev/null || true)"
  if [[ -n "${SECRET:-}" ]]; then
    az containerapp secret set -g "$RG" -n "$APP" \
      --secrets "oauth-client-secret=$SECRET" >/dev/null
    az containerapp update -g "$RG" -n "$APP" \
      --set-env-vars "OAUTH_CLIENT_ID=$APP_ID" \
                     "OAUTH_CLIENT_SECRET=secretref:oauth-client-secret" \
                     "PUBLIC_BASE_URL=https://$FQDN" \
                     "AZURE_TENANT_ID=$TENANT" >/dev/null
    OAUTH_OK=1
    echo "   app registration $APP_ID wired up"
  fi
fi

# --- report ----------------------------------------------------------------
cat <<REPORT

=============================================================================
 DEPLOYED : https://$FQDN
 MCP URL  : https://$FQDN/mcp
 IMAGE    : $IMAGE
=============================================================================
REPORT

if [[ "${OAUTH_OK:-0}" == "1" ]]; then
  cat <<'REPORT'
Auth: Entra OAuth (same pattern as Throne). Tenant-restricted.

LAST STEP -- in claude.ai:
  Settings -> Connectors -> Add custom connector
  Paste the MCP URL above. Leave the OAuth client fields BLANK: the server acts
  as its own OAuth proxy, which is why Throne connects that way too.

Then, in a NEW conversation, run:  maps_healthcheck
Expect key_status "ok". Test from the phone AND desktop -- desktop-only is a
failed build.
REPORT
else
  cat <<REPORT
Auth: static bearer (OAuth auto-config did not complete -- your account likely
cannot create Entra app registrations). The server is NOT anonymous; it fails
closed on every call without this bearer:

  $BEARER

claude.ai's custom-connector UI expects OAuth, so to finish, either grant your
account Application Developer in Entra and re-run this script, or create the
app registration by hand:

  az ad app create --display-name $APP --sign-in-audience AzureADMyOrg \\
    --web-redirect-uris "$REDIRECT"

then re-run this script -- it will pick the registration up and wire it in.
REPORT
fi
