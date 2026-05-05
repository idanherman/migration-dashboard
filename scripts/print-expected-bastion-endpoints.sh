#!/usr/bin/env bash
# Print NODE_STATUS_ENDPOINTS and ROUTE_STATUS_ENDPOINTS exactly as deploy.sh does,
# using live cluster state. Run anywhere `oc` points at the target cluster.
# Usage: ./scripts/print-expected-bastion-endpoints.sh -n migration-test-system
# Optional: -s migration-peer-nodeport

set -euo pipefail

NAMESPACE=""
NODE_PORT_SVC_NAME="migration-peer-nodeport"
# Match deploy.sh: http avoids TLS issues from bastion; set ROUTE_STATUS_SCHEME=https to test :443
ROUTE_STATUS_SCHEME="${ROUTE_STATUS_SCHEME:-http}"

usage() {
  echo "Usage: $0 -n <namespace> [-s <nodeport-svc-name>]"
  echo "  Prints export lines for bastion config (no registry/image changes)."
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

echo "=== Cluster expects (paste into bastion config.env) ==="
NODEPORT=$(oc get svc -n "$NAMESPACE" "$NODE_PORT_SVC_NAME" -o jsonpath='{.spec.ports[?(@.port==8082)].nodePort}' 2>/dev/null || true)
if [[ -z "$NODEPORT" ]]; then
  echo "# Could not get NodePort for svc/$NODE_PORT_SVC_NAME (is it deployed?)"
else
  NODE_IPS=()
  while read -r ip; do [[ -n "$ip" ]] && NODE_IPS+=("$ip"); done < <(oc get nodes -o jsonpath='{range .items[*]}{.status.addresses[?(@.type=="InternalIP")].address}{"\n"}{end}' 2>/dev/null)
  NODE_URLS=()
  for ip in "${NODE_IPS[@]}"; do
    NODE_URLS+=("http://${ip}:${NODEPORT}")
  done
  NODE_STATUS_ENDPOINTS=$(IFS=,; echo "${NODE_URLS[*]}")
  echo "export NODE_STATUS_ENDPOINTS=\"$NODE_STATUS_ENDPOINTS\""
  echo ""
  echo "# Quick probe from this host (same path bastion uses):"
  first="${NODE_URLS[0]}"
  echo "# curl -sS \"${first}/status\" | head -c 200; echo"
fi

ROUTE_URLS=()
while read -r name host; do
  [[ "$name" == migration-peer-* ]] && [[ -n "$host" ]] && ROUTE_URLS+=("${ROUTE_STATUS_SCHEME}://${host}")
done < <(oc get routes -n "$NAMESPACE" -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.host}{"\n"}{end}' 2>/dev/null)
if [[ ${#ROUTE_URLS[@]} -gt 0 ]]; then
  ROUTE_STATUS_ENDPOINTS=$(IFS=,; echo "${ROUTE_URLS[*]}")
  echo "export ROUTE_STATUS_ENDPOINTS=\"$ROUTE_STATUS_ENDPOINTS\""
else
  echo "# No migration-peer-* routes in namespace $NAMESPACE"
fi

NP_WS=$(oc get svc -n "$NAMESPACE" "$NODE_PORT_SVC_NAME" -o jsonpath='{.spec.ports[?(@.name=="ws")].nodePort}' 2>/dev/null || true)
NP_TCP=$(oc get svc -n "$NAMESPACE" "$NODE_PORT_SVC_NAME" -o jsonpath='{.spec.ports[?(@.name=="tcp")].nodePort}' 2>/dev/null || true)
NP_HTTP=$(oc get svc -n "$NAMESPACE" "$NODE_PORT_SVC_NAME" -o jsonpath='{.spec.ports[?(@.name=="http")].nodePort}' 2>/dev/null || true)
if [[ -n "$NP_WS" && -n "$NP_TCP" && -n "$NP_HTTP" ]]; then
  IPS_LINE=$(oc get nodes -o jsonpath='{range .items[*]}{.status.addresses[?(@.type=="InternalIP")].address}{" "}{end}' 2>/dev/null | sed 's/[[:space:]]*$//')
  NODEPORT_PEERS_JSON=$(
    IPS_LINE="$IPS_LINE" NP_WS="$NP_WS" NP_TCP="$NP_TCP" NP_HTTP="$NP_HTTP" python3 - <<'PY'
import json, os
ips = [x for x in os.environ.get("IPS_LINE", "").split() if x]
try:
    ws, tcp, http = int(os.environ["NP_WS"]), int(os.environ["NP_TCP"]), int(os.environ["NP_HTTP"])
except (KeyError, ValueError):
    print("{}")
else:
    out = {}
    for i, ip in enumerate(ips, 1):
        out[f"peer-{i}-np"] = {"host": ip, "ws_port": ws, "tcp_port": tcp, "http_port": http}
    print(json.dumps(out))
PY
  )
  echo "export NODEPORT_PEERS='$NODEPORT_PEERS_JSON'"
fi

METALLB_JSON=$(oc get svc -n "$NAMESPACE" -o json 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
out = {}
i = 1
for item in sorted(data.get('items', []), key=lambda x: x['metadata']['name']):
    name = item['metadata']['name']
    if not name.startswith('migration-peer-lb-'):
        continue
    ing = (item.get('status') or {}).get('loadBalancer') or {}
    ingress = ing.get('ingress') or []
    if not ingress:
        continue
    ip = ingress[0].get('ip') or ingress[0].get('hostname')
    if not ip:
        continue
    out[f'peer-{i}-lb'] = ip
    i += 1
print(json.dumps(out))
" 2>/dev/null || echo "{}")
if [[ -n "$METALLB_JSON" && "$METALLB_JSON" != "{}" ]]; then
  echo "export METALLB_PEERS='$METALLB_JSON'"
else
  echo "# METALLB_PEERS: (optional) set APPLY_LOADBALANCER=yes and re-run deploy to create migration-peer-lb-* LoadBalancers"
fi

echo ""
echo "=== Optional: disable unused external matrices ==="
echo "# export METALLB_PEERS='{}' NODEPORT_PEERS='{}'"
