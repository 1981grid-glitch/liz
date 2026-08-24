#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 01 — Read the Container Apps egress IPs.  RUN IN: Azure Cloud Shell (Bash)
#      https://shell.azure.com   (or the Cloud Shell button in the Azure app)
#
# Reads nothing but public resource metadata. Changes nothing. Safe to re-run.
#
# Why this runs first: the Maps API key must be IP-restricted at creation time,
# and an unrestricted Maps key is a live financial liability. Cartographer
# deploys into Throne's existing Container Apps environment, so it inherits
# Throne's egress set -- which means we can read it before cartographer exists.
# ---------------------------------------------------------------------------
set -euo pipefail

RG="${RG:-rg-throne-mcp}"
REF_APP="${REF_APP:-throne-mcp}"

echo "== Subscription =="
az account show --query "{sub:name, id:id}" -o tsv

echo
echo "== Reading egress IPs from '$REF_APP' in '$RG' =="
IPS="$(az containerapp show -g "$RG" -n "$REF_APP" \
        --query "properties.outboundIpAddresses" -o tsv 2>/dev/null | tr '\t' ',' | tr -d '\r')"

if [[ -z "${IPS:-}" ]]; then
  echo "FAILED to read outbound IPs." >&2
  echo "Check the app name and resource group:" >&2
  echo "  az containerapp list -o table" >&2
  exit 1
fi

ENV_ID="$(az containerapp show -g "$RG" -n "$REF_APP" \
           --query "properties.environmentId" -o tsv)"

echo
echo "-------------------------------------------------------------"
echo "EGRESS IPS: $IPS"
echo "-------------------------------------------------------------"
echo "Environment: ${ENV_ID##*/}"
echo
echo "NEXT: open Google Cloud Shell -> https://shell.cloud.google.com"
echo "Then paste:"
echo
echo "  git clone --depth 1 https://github.com/1981grid-glitch/liz.git ~/liz 2>/dev/null || git -C ~/liz pull -q"
echo "  bash ~/liz/cartographer-mcp/scripts/02-provision-google.sh '$IPS'"
echo
echo "NOTE: Container Apps egress IPs are only guaranteed stable behind a NAT"
echo "gateway. Without one they can rotate when the environment is restarted or"
echo "rebuilt. If Maps calls start returning REQUEST_DENIED later, re-run this"
echo "script and update the key's allowed IPs with:"
echo "  gcloud services api-keys update KEY_ID --allowed-ips=NEW_IPS --project=adaptive-maps-mcp"
