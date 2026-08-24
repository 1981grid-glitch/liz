#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 02 — Provision Google Cloud.  RUN IN: Google Cloud Shell (Bash)
#      https://shell.cloud.google.com    (gcloud is already signed in as you)
#
#   usage:  bash 02-provision-google.sh "<EGRESS_IPS_FROM_SCRIPT_01>"
#
# Creates the project, enables exactly three APIs, and mints ONE API key that is
# restricted two ways at creation time (never unrestricted, not even briefly):
#   - API restriction  -> only Places (New), Routes, Geocoding
#   - IP restriction   -> only the Container Apps egress IPs
#
# Idempotent: re-running reuses the existing project and key, and refreshes the
# key's allowed IPs. Safe to run again after an egress IP change.
# ---------------------------------------------------------------------------
set -euo pipefail

ALLOWED_IPS="${1:-}"
PROJECT_ID="${PROJECT_ID:-adaptive-maps-mcp}"
KEY_NAME="${KEY_NAME:-cartographer-mcp}"

if [[ -z "$ALLOWED_IPS" ]]; then
  echo "usage: bash 02-provision-google.sh \"<EGRESS_IPS>\"" >&2
  echo "Get the IPs by running 01-azure-egress.sh in Azure Cloud Shell first." >&2
  exit 1
fi

# Places API (NEW) is places.googleapis.com.  places-backend.googleapis.com is
# the LEGACY Places API -- frozen since 2025-03 and unavailable to new projects.
# Enabling the wrong one yields REQUEST_DENIED on every search, so this list is
# deliberately explicit rather than inferred.
SERVICES=(
  places.googleapis.com
  routes.googleapis.com
  geocoding-backend.googleapis.com
)

echo "== Signed in as =="
gcloud config get-value account 2>/dev/null || true

# --- billing account -------------------------------------------------------
echo
echo "== Billing accounts =="
mapfile -t BA < <(gcloud billing accounts list --filter="open=true" --format="value(name)" 2>/dev/null || true)
if (( ${#BA[@]} == 0 )); then
  cat >&2 <<'MSG'
No open billing account found.

This is the one step that cannot be scripted: Google requires an authenticated
human to accept the billing terms and attach a payment method. Create one at
  https://console.cloud.google.com/billing
then re-run this script. Nothing else here needs your attention.
MSG
  exit 1
elif (( ${#BA[@]} == 1 )); then
  BILLING="${BA[0]}"
  echo "Using the only open billing account: $BILLING"
else
  echo "More than one open billing account:"
  gcloud billing accounts list --filter="open=true" --format="table(name,displayName)"
  echo
  echo "Re-run pinning one explicitly, e.g.:"
  echo "  BILLING=billingAccounts/XXXXXX-XXXXXX-XXXXXX bash $0 '$ALLOWED_IPS'"
  BILLING="${BILLING:-}"
  [[ -z "$BILLING" ]] && exit 1
fi

# --- project ---------------------------------------------------------------
echo
if gcloud projects describe "$PROJECT_ID" >/dev/null 2>&1; then
  echo "== Project '$PROJECT_ID' already exists, reusing =="
else
  echo "== Creating project '$PROJECT_ID' =="
  if ! gcloud projects create "$PROJECT_ID" --name="Adaptive Maps MCP" 2>/dev/null; then
    # Project IDs are globally unique across all of Google Cloud, so the plain
    # name may be taken by a stranger. Fall back to a suffixed id and carry it
    # forward rather than failing.
    PROJECT_ID="${PROJECT_ID}-$(tr -dc 'a-z0-9' </dev/urandom | head -c6)"
    echo "   name was taken; using '$PROJECT_ID' instead"
    gcloud projects create "$PROJECT_ID" --name="Adaptive Maps MCP"
  fi
fi

echo
echo "== Linking billing =="
gcloud billing projects link "$PROJECT_ID" --billing-account="$BILLING" >/dev/null
echo "   linked to $BILLING"

# --- APIs ------------------------------------------------------------------
echo
echo "== Enabling APIs (exactly three, no others) =="
for s in "${SERVICES[@]}"; do
  printf '   %-40s ' "$s"
  if gcloud services enable "$s" --project="$PROJECT_ID" 2>/dev/null; then
    echo "enabled"
  else
    echo "FAILED"; FAILED=1
  fi
done
[[ "${FAILED:-0}" == "1" ]] && { echo "One or more APIs failed to enable. Stopping." >&2; exit 1; }

# --- API key ---------------------------------------------------------------
API_TARGETS=()
for s in "${SERVICES[@]}"; do API_TARGETS+=(--api-target="service=$s"); done

echo
EXISTING="$(gcloud services api-keys list --project="$PROJECT_ID" \
             --filter="displayName='$KEY_NAME'" --format="value(name)" 2>/dev/null | head -1)"

if [[ -n "$EXISTING" ]]; then
  echo "== Key '$KEY_NAME' exists; refreshing its restrictions =="
  gcloud services api-keys update "$EXISTING" \
    --allowed-ips="$ALLOWED_IPS" "${API_TARGETS[@]}" >/dev/null
  KEY_RES="$EXISTING"
else
  echo "== Creating restricted key '$KEY_NAME' =="
  gcloud services api-keys create \
    --display-name="$KEY_NAME" \
    --allowed-ips="$ALLOWED_IPS" \
    "${API_TARGETS[@]}" \
    --project="$PROJECT_ID" >/dev/null
  KEY_RES="$(gcloud services api-keys list --project="$PROJECT_ID" \
              --filter="displayName='$KEY_NAME'" --format="value(name)" | head -1)"
fi

KEY_STRING="$(gcloud services api-keys get-key-string "$KEY_RES" --format="value(keyString)")"

# --- report ----------------------------------------------------------------
cat <<REPORT

=============================================================================
 PROJECT : $PROJECT_ID
 KEY ID  : ${KEY_RES##*/}
 API KEY : $KEY_STRING
=============================================================================

Restricted to: ${SERVICES[*]}
Allowed IPs  : $ALLOWED_IPS

NOT smoke-tested here on purpose. The key only works from the Container Apps
egress IPs, and Cloud Shell is not one of them -- a test from this terminal
would return REQUEST_DENIED and look like a failure when it is the restriction
working correctly. Script 03 tests it from inside the container, which is the
only place it can succeed.

NEXT -- back in Azure Cloud Shell:

  git clone --depth 1 https://github.com/1981grid-glitch/liz.git ~/liz 2>/dev/null || git -C ~/liz pull -q
  bash ~/liz/cartographer-mcp/scripts/03-deploy-azure.sh '$KEY_STRING'

STILL WORTH DOING (2 min, console only -- quota caps are the only guard Google
actually enforces; the budget alert just tells you after the money is gone):

  Quotas : https://console.cloud.google.com/apis/api/places.googleapis.com/quotas?project=$PROJECT_ID
           https://console.cloud.google.com/apis/api/routes.googleapis.com/quotas?project=$PROJECT_ID
           https://console.cloud.google.com/apis/api/geocoding-backend.googleapis.com/quotas?project=$PROJECT_ID
           Set each to ~500 requests/day. Expected real usage is far below that.

  Budget : https://console.cloud.google.com/billing/budgets?project=$PROJECT_ID
           \$10/month alert.
REPORT
