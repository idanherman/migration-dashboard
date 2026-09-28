#!/usr/bin/env bash
# Print NODE_STATUS_ENDPOINTS and NODEPORT_PEERS exactly as deploy.sh does,
# using live cluster state. Run anywhere `oc` points at the target cluster.
# Usage: ./scripts/print-expected-bastion-endpoints.sh -n migration-test-system

set -euo pipefail

NAMESPACE=""
NODE_PORT_SVC_NAME="migration-peer-nodeport"
ROUTE_PROBE_NAME="${ROUTE_PROBE_NAME:-migration-peer-route-probe}"
ROUTE_PROBE_SCHEME="${ROUTE_PROBE_SCHEME:-http}"

usage() {
  echo "Usage: $0 -n <namespace> [-s <nodeport-svc-name>]"
  echo "  Prints KEY=value lines for bastion config.podman.env (no export prefix)."
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -n) NAMESPACE="$2"; shift 2 ;;
    -s) NODE_PORT_SVC_NAME="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1"; usage; exit 1 ;;
  esac
done

if [[ -z "$NAMESPACE" ]]; then
  echo "NAMESPACE is required (-n)"
  usage
  exit 1
fi

echo "=== Bastion config (paste into config.podman.env) ==="
NODEPORT=$(oc get svc -n "$NAMESPACE" "$NODE_PORT_SVC_NAME" -o jsonpath='{.spec.ports[?(@.port==8082)].nodePort}' 2>/dev/null || true)
if [[ -z "$NODEPORT" ]]; then
  echo "# Could not get NodePort for svc/$NODE_PORT_SVC_NAME (is it deployed?)"
else
  NODE_URLS=()
  while read -r ip _node; do
    [[ -n "$ip" ]] && NODE_URLS+=("http://${ip}:${NODEPORT}")
  done < <(oc get pods -n "$NAMESPACE" -l app=migration-peer -o json \
    | jq -r '.items | sort_by(.spec.nodeName)[] | "\(.status.hostIP) \(.spec.nodeName)"')
  if [[ ${#NODE_URLS[@]} -eq 0 ]]; then
    echo "# No migration-peer pods found"
  else
    NODE_STATUS_ENDPOINTS=$(IFS=,; echo "${NODE_URLS[*]}")
    echo "NODE_STATUS_ENDPOINTS=$NODE_STATUS_ENDPOINTS"
    echo ""
    echo "# Quick probe from this host:"
    echo "# curl -sS \"${NODE_URLS[0]}/status\" | head -c 200; echo"
  fi

  NP_WS=$(oc get svc -n "$NAMESPACE" "$NODE_PORT_SVC_NAME" -o jsonpath='{.spec.ports[?(@.name=="ws")].nodePort}' 2>/dev/null || true)
  NP_TCP=$(oc get svc -n "$NAMESPACE" "$NODE_PORT_SVC_NAME" -o jsonpath='{.spec.ports[?(@.name=="tcp")].nodePort}' 2>/dev/null || true)
  NP_HTTP=$(oc get svc -n "$NAMESPACE" "$NODE_PORT_SVC_NAME" -o jsonpath='{.spec.ports[?(@.name=="http")].nodePort}' 2>/dev/null || true)
  if [[ -n "$NP_WS" && -n "$NP_TCP" && -n "$NP_HTTP" ]]; then
    NODEPORT_PEERS_JSON=$(oc get pods -n "$NAMESPACE" -l app=migration-peer -o json | \
      NP_WS="$NP_WS" NP_TCP="$NP_TCP" NP_HTTP="$NP_HTTP" python3 -c '
import json, os, sys
pods = json.load(sys.stdin).get("items", [])
pods.sort(key=lambda p: p["spec"]["nodeName"])
out = {}
for i, p in enumerate(pods, 1):
    ip = (p.get("status") or {}).get("hostIP")
    if not ip:
        continue
    out[f"peer-{i}-np"] = {
        "host": ip,
        "ws_port": int(os.environ["NP_WS"]),
        "tcp_port": int(os.environ["NP_TCP"]),
        "http_port": int(os.environ["NP_HTTP"]),
    }
print(json.dumps(out))
')
    [[ -n "$NODEPORT_PEERS_JSON" && "$NODEPORT_PEERS_JSON" != "{}" ]] && echo "NODEPORT_PEERS=$NODEPORT_PEERS_JSON"
  fi
fi

ROUTE_HOST=$(oc get route -n "$NAMESPACE" "$ROUTE_PROBE_NAME" -o jsonpath='{.spec.host}' 2>/dev/null || true)
if [[ -n "$ROUTE_HOST" ]]; then
  echo "ROUTE_PROBE_URL=${ROUTE_PROBE_SCHEME}://${ROUTE_HOST}"
else
  echo "# ROUTE_PROBE_URL: no route $ROUTE_PROBE_NAME (run deploy with APPLY_ROUTE_PROBE=yes)"
fi

echo ""
echo "SSL_VERIFY=false"
echo "DASHBOARD_PORT=9091"
