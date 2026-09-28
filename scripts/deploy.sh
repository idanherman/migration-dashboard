#!/usr/bin/env bash
# Deploy migration-test-system: DaemonSet + headless + NodePort.
# Outputs NODE_STATUS_ENDPOINTS and NODEPORT_PEERS for bastion config (per-node via NodePort + Local).

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MANIFESTS_DIR="${REPO_ROOT}/source/ocp-peer"

CONFIG_FILE=""
REGISTRY=""
NAMESPACE=""
IMAGE_TAG="latest"
PEER_APP_IMAGE="applications/peer-app"
NODE_PORT_SVC_NAME="migration-peer-nodeport"
ROUTE_PROBE_NAME="migration-peer-route-probe"
ROUTE_PROBE_SCHEME="${ROUTE_PROBE_SCHEME:-http}"

usage() {
  echo "Usage: $0 [OPTIONS]"
  echo "  -c, --config FILE   Config file (key=value per line)"
  echo "  -r, --registry URL  Image registry (overrides config)"
  echo "  -n, --namespace NS Namespace (overrides config)"
  echo "  -h, --help          This help"
}

while [[ $# -gt 0 ]]; do
  case $1 in
    -c|--config) CONFIG_FILE="$2"; shift 2 ;;
    -r|--registry) REGISTRY="$2"; shift 2 ;;
    -n|--namespace) NAMESPACE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1"; usage; exit 1 ;;
  esac
done

if [[ -n "$CONFIG_FILE" ]]; then
  if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "Config file not found: $CONFIG_FILE"
    exit 1
  fi
  set -a
  # shellcheck source=/dev/null
  source "$CONFIG_FILE" 2>/dev/null || true
  set +a
elif [[ -f "$REPO_ROOT/deploy.conf" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$REPO_ROOT/deploy.conf" 2>/dev/null || true
  set +a
fi

[[ -n "$REGISTRY" ]] && export REGISTRY
[[ -n "$NAMESPACE" ]] && export NAMESPACE
IMAGE_TAG="${IMAGE_TAG:-latest}"
PEER_APP_IMAGE="${PEER_APP_IMAGE:-applications/peer-app}"
NODE_PORT_SVC_NAME="${NODE_PORT_SVC_NAME:-migration-peer-nodeport}"
ROUTE_PROBE_NAME="${ROUTE_PROBE_NAME:-migration-peer-route-probe}"
ROUTE_PROBE_SCHEME="${ROUTE_PROBE_SCHEME:-http}"

if [[ -z "$NAMESPACE" ]]; then
  echo "NAMESPACE is required (set in config or -n)"
  exit 1
fi

if [[ -z "$REGISTRY" ]]; then
  echo "REGISTRY is required (set in config or -r)"
  exit 1
fi

FULL_IMAGE="${REGISTRY}/${PEER_APP_IMAGE}:${IMAGE_TAG}"

_sync_per_node_lb_svcs() {
  local ns="$1"
  shift
  [[ "${APPLY_LOADBALANCER:-}" == "yes" || "${APPLY_LOADBALANCER:-}" == "1" ]] || return 0
  local template="$MANIFESTS_DIR/service-migration-peer-lb-per-node.yaml"
  if [[ ! -f "$template" ]]; then
    echo "[WARN] $template not found; skipping LoadBalancer sync"
    return 0
  fi
  echo "=== Per-node LoadBalancer services (APPLY_LOADBALANCER) ==="
  local node node_safe lbsvc
  for node in "$@"; do
    node_safe=$(echo "$node" | tr '.' '-' | tr '[:upper:]' '[:lower:]' | sed 's/^[-]*//')
    lbsvc="migration-peer-lb-${node_safe}"
    sed -e "s/REPLACE_NS/$ns/" -e "s/REPLACE_NAME/$lbsvc/" -e "s/REPLACE_NODE/$node/" "$template" | oc apply -f -
  done
}

_delete_stale_per_node_lb_svcs() {
  local ns="$1"
  shift
  [[ $# -gt 0 ]] || return 0
  local joined=" $* "
  local lbsvc node_safe
  while read -r lbsvc; do
    [[ -z "$lbsvc" ]] && continue
    node_safe="${lbsvc#migration-peer-lb-}"
    if [[ "$joined" != *" ${node_safe} "* ]]; then
      oc delete svc -n "$ns" "$lbsvc" --ignore-not-found 2>/dev/null || true
    fi
  done < <(oc get svc -n "$ns" -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null | grep '^migration-peer-lb-' || true)
}

echo "=== Deploying manifests (namespace=$NAMESPACE, image=$FULL_IMAGE) ==="
for f in daemonset-migration-peer.yaml service-migration-peer-headless.yaml service-migration-peer-nodeport.yaml servicemonitor-migration-peer.yaml; do
  path="$MANIFESTS_DIR/$f"
  if [[ ! -f "$path" ]]; then
    echo "Manifest not found: $path"
    exit 1
  fi
  sed -e "s|namespace:.*# Override.*|namespace: $NAMESPACE|" \
      -e "s|registry.example.com/applications/peer-app:latest|$FULL_IMAGE|g" \
      -e "s|migration-test-system|$NAMESPACE|g" \
      "$path" | oc apply -f -
done

echo "Waiting for DaemonSet migration-peer..."
oc rollout status daemonset/migration-peer -n "$NAMESPACE" --timeout=300s 2>/dev/null || true

# Optional MetalLB (requires node-name label on pods; only if APPLY_LOADBALANCER=yes)
PODS_JSON=$(oc get pods -n "$NAMESPACE" -l app=migration-peer -o json 2>/dev/null || true)
if [[ -n "$PODS_JSON" ]] && [[ "$PODS_JSON" != *'"items":[]'* ]]; then
  NODES=()
  while read -r n; do [[ -n "$n" ]] && NODES+=("$n"); done < <(echo "$PODS_JSON" | jq -r '.items[].spec.nodeName' | sort -u)
  if [[ "${APPLY_LOADBALANCER:-}" == "yes" || "${APPLY_LOADBALANCER:-}" == "1" ]]; then
  while read -r pod_name node_name; do
    [[ -z "$pod_name" ]] && continue
    oc label pod -n "$NAMESPACE" "$pod_name" node-name="$node_name" --overwrite 2>/dev/null || true
  done < <(echo "$PODS_JSON" | jq -r '.items[] | "\(.metadata.name) \(.spec.nodeName)"')
    _sync_per_node_lb_svcs "$NAMESPACE" "${NODES[@]}"
    _delete_stale_per_node_lb_svcs "$NAMESPACE" "${NODES[@]}"
  fi
fi

if [[ "${APPLY_ROUTE_PROBE:-}" == "yes" || "${APPLY_ROUTE_PROBE:-}" == "1" ]]; then
  echo "=== Shared OpenShift Route (router probe) ==="
  if ! oc get route -n "$NAMESPACE" "$ROUTE_PROBE_NAME" &>/dev/null; then
    oc expose svc -n "$NAMESPACE" "$NODE_PORT_SVC_NAME" --name="$ROUTE_PROBE_NAME" --port=8082
  fi
fi

echo ""
echo "=== Bastion config (paste into config.podman.env) ==="
NODEPORT=$(oc get svc -n "$NAMESPACE" "$NODE_PORT_SVC_NAME" -o jsonpath='{.spec.ports[?(@.port==8082)].nodePort}' 2>/dev/null || true)
if [[ -z "$NODEPORT" ]]; then
  echo "Could not get NodePort for $NODE_PORT_SVC_NAME"
else
  NODE_URLS=()
  while read -r ip _node; do
    [[ -n "$ip" ]] && NODE_URLS+=("http://${ip}:${NODEPORT}")
  done < <(oc get pods -n "$NAMESPACE" -l app=migration-peer -o json \
    | jq -r '.items | sort_by(.spec.nodeName)[] | "\(.status.hostIP) \(.spec.nodeName)"')
  if [[ ${#NODE_URLS[@]} -eq 0 ]]; then
    echo "# No migration-peer pods found; check DaemonSet"
  else
    NODE_STATUS_ENDPOINTS=$(IFS=,; echo "${NODE_URLS[*]}")
    echo "NODE_STATUS_ENDPOINTS=$NODE_STATUS_ENDPOINTS"
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

if [[ "${APPLY_LOADBALANCER:-}" == "yes" || "${APPLY_LOADBALANCER:-}" == "1" ]]; then
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
")
  if [[ -n "$METALLB_JSON" && "$METALLB_JSON" != "{}" ]]; then
    echo "METALLB_PEERS=$METALLB_JSON"
  fi
fi

if [[ "${APPLY_ROUTE_PROBE:-}" == "yes" || "${APPLY_ROUTE_PROBE:-}" == "1" ]]; then
  ROUTE_HOST=$(oc get route -n "$NAMESPACE" "$ROUTE_PROBE_NAME" -o jsonpath='{.spec.host}' 2>/dev/null || true)
  if [[ -n "$ROUTE_HOST" ]]; then
    echo "ROUTE_PROBE_URL=${ROUTE_PROBE_SCHEME}://${ROUTE_HOST}"
  else
    echo "# ROUTE_PROBE_URL: route $ROUTE_PROBE_NAME not found in $NAMESPACE"
  fi
fi

echo ""
