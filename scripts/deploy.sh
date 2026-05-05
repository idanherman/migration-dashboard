#!/usr/bin/env bash
# Deploy migration-test-system: DaemonSet + headless + NodePort, then sync Routes per node.
# Uses config file (deploy.conf) and CLI overrides (-r REGISTRY, -n NAMESPACE).
# Outputs NODE_STATUS_ENDPOINTS and ROUTE_STATUS_ENDPOINTS for bastion config.

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
SYNC_ROUTES_ONLY=""

usage() {
  echo "Usage: $0 [OPTIONS]"
  echo "  -c, --config FILE   Config file (key=value per line)"
  echo "  -r, --registry URL  Image registry (overrides config)"
  echo "  -n, --namespace NS Namespace (overrides config)"
  echo "  --sync-routes       Only run router sync (label pods, create Service+Route per node)"
  echo "  -h, --help          This help"
}

while [[ $# -gt 0 ]]; do
  case $1 in
    -c|--config) CONFIG_FILE="$2"; shift 2 ;;
    -r|--registry) REGISTRY="$2"; shift 2 ;;
    -n|--namespace) NAMESPACE="$2"; shift 2 ;;
    --sync-routes) SYNC_ROUTES_ONLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1"; usage; exit 1 ;;
  esac
done

# Load config file
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

# CLI overrides
[[ -n "$REGISTRY" ]] && export REGISTRY
[[ -n "$NAMESPACE" ]] && export NAMESPACE
IMAGE_TAG="${IMAGE_TAG:-latest}"
PEER_APP_IMAGE="${PEER_APP_IMAGE:-applications/peer-app}"
NODE_PORT_SVC_NAME="${NODE_PORT_SVC_NAME:-migration-peer-nodeport}"
# Bastion HTTP probes to OpenShift routes: http = :80 (avoids TLS verify and many edge 503 on :443 from lab networks)
ROUTE_STATUS_SCHEME="${ROUTE_STATUS_SCHEME:-http}"

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

if [[ -n "$SYNC_ROUTES_ONLY" ]]; then
  echo "=== Sync routes only (label pods, Service+Route per node) ==="
  # List DaemonSet pods and their node names
  PODS_JSON=$(oc get pods -n "$NAMESPACE" -l app=migration-peer -o json 2>/dev/null || true)
  if [[ -z "$PODS_JSON" ]] || [[ "$PODS_JSON" == *"items":[]* ]]; then
    echo "No migration-peer pods found in namespace $NAMESPACE"
    exit 1
  fi

  # Label each pod with node-name
  while read -r pod_name node_name; do
    [[ -z "$pod_name" ]] && continue
    oc label pod -n "$NAMESPACE" "$pod_name" node-name="$node_name" --overwrite 2>/dev/null || true
  done < <(echo "$PODS_JSON" | jq -r '.items[] | "\(.metadata.name) \(.spec.nodeName)"')

  # Get unique node names that have a pod
  NODES=()
  while read -r n; do [[ -n "$n" ]] && NODES+=("$n"); done < <(echo "$PODS_JSON" | jq -r '.items[].spec.nodeName' | sort -u)

  # Create Service + Route per node
  for node in "${NODES[@]}"; do
    node_safe=$(echo "$node" | tr '.' '-' | tr '[:upper:]' '[:lower:]' | sed 's/^[-]*//')
    svc_name="migration-peer-${node_safe}"
    oc get svc -n "$NAMESPACE" "$svc_name" &>/dev/null || oc create service clusterip "$svc_name" -n "$NAMESPACE" --tcp=8082 2>/dev/null || true
    oc patch svc -n "$NAMESPACE" "$svc_name" -p "{\"spec\":{\"selector\":{\"app\":\"migration-peer\",\"node-name\":\"$node\"}}}" --type=merge 2>/dev/null || true
    oc get route -n "$NAMESPACE" "$svc_name" &>/dev/null || oc expose svc -n "$NAMESPACE" "$svc_name" --name="$svc_name" 2>/dev/null || true
  done

  _sync_per_node_lb_svcs "$NAMESPACE" "${NODES[@]}"

  # Delete Services/Routes for nodes that no longer have a pod
  current_safes=()
  for node in "${NODES[@]}"; do
    current_safes+=("$(echo "$node" | tr '.' '-' | tr '[:upper:]' '[:lower:]' | sed 's/^[-]*//')")
  done
  for rname in $(oc get routes -n "$NAMESPACE" -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null); do
    [[ "$rname" != migration-peer-* ]] && continue
    node_safe="${rname#migration-peer-}"
    if [[ ! " ${current_safes[*]} " =~ " ${node_safe} " ]]; then
      oc delete route -n "$NAMESPACE" "$rname" --ignore-not-found 2>/dev/null || true
      oc delete svc -n "$NAMESPACE" "$rname" --ignore-not-found 2>/dev/null || true
    fi
  done
  _delete_stale_per_node_lb_svcs "$NAMESPACE" "${current_safes[@]}"

  echo "Router sync done."
fi

if [[ -z "$SYNC_ROUTES_ONLY" ]]; then
  echo "=== Deploying manifests (namespace=$NAMESPACE, image=$FULL_IMAGE) ==="
  for f in daemonset-migration-peer.yaml service-migration-peer-headless.yaml service-migration-peer-nodeport.yaml; do
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
  oc rollout status daemonset/migration-peer -n "$NAMESPACE" --timeout=120s 2>/dev/null || true

  echo "=== Syncing routes (label pods, Service+Route per node) ==="
  PODS_JSON=$(oc get pods -n "$NAMESPACE" -l app=migration-peer -o json 2>/dev/null || true)
  if [[ -n "$PODS_JSON" ]] && [[ "$PODS_JSON" != *'"items":[]'* ]]; then
    while read -r pod_name node_name; do
      [[ -z "$pod_name" ]] && continue
      oc label pod -n "$NAMESPACE" "$pod_name" node-name="$node_name" --overwrite 2>/dev/null || true
    done < <(echo "$PODS_JSON" | jq -r '.items[] | "\(.metadata.name) \(.spec.nodeName)"')
    NODES=()
    while read -r n; do [[ -n "$n" ]] && NODES+=("$n"); done < <(echo "$PODS_JSON" | jq -r '.items[].spec.nodeName' | sort -u)
    for node in "${NODES[@]}"; do
      node_safe=$(echo "$node" | tr '.' '-' | tr '[:upper:]' '[:lower:]' | sed 's/^[-]*//')
      svc_name="migration-peer-${node_safe}"
      oc get svc -n "$NAMESPACE" "$svc_name" &>/dev/null || oc create service clusterip "$svc_name" -n "$NAMESPACE" --tcp=8082 2>/dev/null || true
      oc patch svc -n "$NAMESPACE" "$svc_name" -p "{\"spec\":{\"selector\":{\"app\":\"migration-peer\",\"node-name\":\"$node\"}}}" --type=merge 2>/dev/null || true
      oc get route -n "$NAMESPACE" "$svc_name" &>/dev/null || oc expose svc -n "$NAMESPACE" "$svc_name" --name="$svc_name" 2>/dev/null || true
    done
    _sync_per_node_lb_svcs "$NAMESPACE" "${NODES[@]}"
    current_safes=()
    for node in "${NODES[@]}"; do current_safes+=("$(echo "$node" | tr '.' '-' | tr '[:upper:]' '[:lower:]' | sed 's/^[-]*//')"); done
    for rname in $(oc get routes -n "$NAMESPACE" -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null); do
      [[ "$rname" != migration-peer-* ]] && continue
      node_safe="${rname#migration-peer-}"
      if [[ ! " ${current_safes[*]} " =~ " ${node_safe} " ]]; then
        oc delete route -n "$NAMESPACE" "$rname" --ignore-not-found 2>/dev/null || true
        oc delete svc -n "$NAMESPACE" "$rname" --ignore-not-found 2>/dev/null || true
      fi
    done
    _delete_stale_per_node_lb_svcs "$NAMESPACE" "${current_safes[@]}"
  fi
fi

echo ""
echo "=== Bastion config (paste into config.env or set env) ==="
NODEPORT=$(oc get svc -n "$NAMESPACE" "$NODE_PORT_SVC_NAME" -o jsonpath='{.spec.ports[?(@.port==8082)].nodePort}' 2>/dev/null || true)
if [[ -z "$NODEPORT" ]]; then
  echo "Could not get NodePort for $NODE_PORT_SVC_NAME"
else
  NODE_IPS=()
  while read -r ip; do [[ -n "$ip" ]] && NODE_IPS+=("$ip"); done < <(oc get nodes -o jsonpath='{range .items[*]}{.status.addresses[?(@.type=="InternalIP")].address}{"\n"}{end}' 2>/dev/null)
  NODE_URLS=()
  for ip in "${NODE_IPS[@]}"; do
    NODE_URLS+=("http://${ip}:${NODEPORT}")
  done
  NODE_STATUS_ENDPOINTS=$(IFS=,; echo "${NODE_URLS[*]}")
  echo "export NODE_STATUS_ENDPOINTS=\"$NODE_STATUS_ENDPOINTS\""
fi

ROUTE_URLS=()
while read -r name host; do
  [[ "$name" == migration-peer-* ]] && [[ -n "$host" ]] && ROUTE_URLS+=("${ROUTE_STATUS_SCHEME}://${host}")
done < <(oc get routes -n "$NAMESPACE" -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.host}{"\n"}{end}' 2>/dev/null)
if [[ ${#ROUTE_URLS[@]} -gt 0 ]]; then
  ROUTE_STATUS_ENDPOINTS=$(IFS=,; echo "${ROUTE_URLS[*]}")
  echo "export ROUTE_STATUS_ENDPOINTS=\"$ROUTE_STATUS_ENDPOINTS\""
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
    echo "export METALLB_PEERS='$METALLB_JSON'"
  else
    echo "# METALLB_PEERS: no external IPs yet on migration-peer-lb-* (oc get svc -n $NAMESPACE; wait for LoadBalancer)"
  fi
fi

echo ""
