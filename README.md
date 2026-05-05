# OVN Migration Dashboard

A comprehensive monitoring dashboard for tracking connectivity during OVN (Open Virtual Network) migrations in OpenShift/Kubernetes environments. The dashboard monitors connectivity between bastion hosts and cluster peers, as well as pod-to-pod connectivity within the cluster.

## Architecture

The system consists of three main components:

1. **Bastion Client** (`bastion-peer/`) - Runs outside the cluster, polls per-node status and optional external tests
2. **Peer Application** (`ocp-peer/`) - Runs as a DaemonSet (one pod per node): TCP mesh, HTTP :8082, WebSocket :8080 for bastion external probes
3. **Dashboard** (`dashboard/`) - Alternative simpler dashboard (optional)

## Components

### Bastion Client
- **Primary**: Polls **NODE_STATUS_ENDPOINTS** (one URL per node via NodePort with `externalTrafficPolicy: Local`) for `/status` and `/history`; builds a dynamic N×N connectivity matrix and optional **Mermaid connectivity graph**
- **Optional**: **ROUTE_STATUS_ENDPOINTS** to test OpenShift router per node
- **Optional**: MetalLB / NodePort / Route external tests (legacy)
- Clear history fans out to all NODE_STATUS_ENDPOINTS
- Dashboard on port 9091

### Peer Application
- Runs as a **DaemonSet** (one pod per node); discovers peers via **headless Service** DNS
- **TCP** pod-to-pod mesh (configurable interval); **HTTP** on 8082; **WebSocket** on 8080 (for bastion external “LoadBalancer / NodePort” WS probes)
- HTTP server for `/status`, `/history`, `/ping`, `/admin/clear_history`
- `/status` returns `self` (pod_name, pod_ip, node_name) and `connections` keyed by peer IP
- **NodePort Service** publishes 8080/8081/8082. Optional **per-node LoadBalancers** (`migration-peer-lb-<node>`): set `APPLY_LOADBALANCER=yes` in `deploy.conf` if the cluster has MetalLB (or cloud LB); `deploy.sh` then prints `METALLB_PEERS` JSON for the bastion

## Prerequisites

- Python 3.11+
- Podman or Docker for building containers
- Access to a container registry (for airgapped: local registry)
- OpenShift/Kubernetes cluster
- MetalLB (if using LoadBalancer services)
- Routes configured (if using OpenShift Routes)

## Airgapped Deployment

This project is designed to work in disconnected/airgapped environments. Follow these steps:

### 1. Prepare Dependencies

#### Option A: Download Python Wheels (Recommended)
```bash
./download-dependencies.sh ./wheels
```

This creates a `wheels/` directory with all Python dependencies that can be copied to the airgapped environment.

#### Option B: Use Local PyPI Mirror
Set up a local PyPI mirror and configure pip to use it.

### 2. Build Container Images

#### On Connected System (to prepare images)
```bash
# Build images
./build-images.sh [registry] [tag] [namespace]
# Example:
./build-images.sh registry.example.com:5000 v1.0.0 applications

# Save images for transfer
podman save -o migration-dashboard-images.tar \
  registry.example.com:5000/applications/peer-app:v1.0.0 \
  registry.example.com:5000/applications/bastion-client:v1.0.0 \
  registry.example.com:5000/applications/dashboard:v1.0.0
```

#### Transfer to Airgapped System
Copy the following to your airgapped environment:
- `migration-dashboard-images.tar` (or individual image files)
- Entire `source/` directory
- `wheels/` directory (if using offline wheels)

#### On Airgapped System
```bash
# Load images
podman load -i migration-dashboard-images.tar

# Or build directly (if wheels are available)
./build-images.sh [local-registry] [tag] [namespace]
```

### 3. Configure for Your Environment

#### Deploy with script (recommended)

1. Copy and edit deploy config:
   ```bash
   cp scripts/deploy.conf.example deploy.conf
   # Set REGISTRY= (e.g. local registry for airgap), NAMESPACE=migration-test-system
   ```

2. Deploy (substitutes REGISTRY/NAMESPACE in manifests, applies DaemonSet + headless + NodePort, syncs Routes per node):
   ```bash
   ./scripts/deploy.sh -c deploy.conf
   # Or override: ./scripts/deploy.sh -r my-registry:5000 -n migration-test-system
   ```

3. Paste the script output (`NODE_STATUS_ENDPOINTS` and optionally `ROUTE_STATUS_ENDPOINTS`) into bastion `config.env`.

#### Configure Bastion Client

1. Copy example config:
   ```bash
   cp source/bastion-peer/config.example.env source/bastion-peer/config.env
   ```

2. Set **NODE_STATUS_ENDPOINTS** (and optionally **ROUTE_STATUS_ENDPOINTS**) from the deploy script output. Optionally set MetalLB/NodePort/Route for external tests.
   - Test intervals

3. Use configuration:
   ```bash
   export $(cat source/bastion-peer/config.env | xargs)
   python source/bastion-peer/bastion-client.py
   ```

Or set environment variables directly:
```bash
export METALLB_PEERS='{"peer-1-lb": "10.0.0.1", ...}'
export ROUTE_PEERS="http://peer-1-route.example.com,..."
python source/bastion-peer/bastion-client.py
```

### 4. Deploy to Cluster

Use the deploy script (config file + CLI overrides for REGISTRY and NAMESPACE):

```bash
oc create namespace migration-test-system   # if needed
cp scripts/deploy.conf.example deploy.conf
# Edit deploy.conf: REGISTRY=..., NAMESPACE=migration-test-system

./scripts/deploy.sh -c deploy.conf
# Or: ./scripts/deploy.sh -r my-registry:5000 -n migration-test-system
```

The script applies the DaemonSet, headless Service, NodePort Service, waits for pods, labels pods with `node-name`, creates one Service and one Route per node (for router test), then prints **NODE_STATUS_ENDPOINTS** and **ROUTE_STATUS_ENDPOINTS**. Paste those into bastion `config.env`.

**When nodes are added or removed:** Re-run `./scripts/deploy.sh -c deploy.conf` (or `--sync-routes` only to update Routes), then update NODE_STATUS_ENDPOINTS and ROUTE_STATUS_ENDPOINTS in bastion config and restart the bastion. The in-cluster mesh self-heals via DNS.

### 5. Run Bastion Client

```bash
# Using environment file
export $(cat source/bastion-peer/config.env | xargs)
python source/bastion-peer/bastion-client.py

# Or using container (set NODE_STATUS_ENDPOINTS from deploy script output)
podman run -d \
  --name migration-dashboard \
  -p 9091:9091 \
  -e NODE_STATUS_ENDPOINTS="http://node1:30082,http://node2:30082,..." \
  your-registry.example.com:5000/applications/bastion-client:latest
```

If you use `--env-file`, it must be **`KEY=value` lines only** (see `source/bastion-peer/config.podman.example.env`). Files with `export KEY=value` break Podman: variables become named `export KEY`, so the app ignores them.

Access dashboard at: `http://localhost:9091`. The dashboard shows a **dynamic N×N** connectivity matrix (by node) and a **Mermaid connectivity graph** that updates with the data.

## Configuration Reference

### Bastion Client Environment Variables

| Variable | Description | Format |
|----------|-------------|--------|
| `NODE_STATUS_ENDPOINTS` | Per-node URLs (NodePort Local); required for internal status | Comma-separated |
| `ROUTE_STATUS_ENDPOINTS` | Per-node URLs via Route (router test); optional | Comma-separated |
| `METALLB_PEERS` | MetalLB IPs (optional external test) | JSON object |
| `NODEPORT_PEERS` | NodePort config (optional) | JSON object |
| `ROUTE_PEERS` | Legacy route URLs (optional) | Comma-separated |
| `POLL_INTERVAL` | Status poll interval (seconds) | Float |
| `DASHBOARD_PORT` | Dashboard web port | Integer |
| `MAX_HISTORY` | Max history entries | Integer |

### Peer Application (DaemonSet) Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `PEER_SERVICE` | Headless service name for peer discovery | `migration-peer-svc` |
| `NAMESPACE` | Namespace (downward API) | - |
| `POD_IP`, `NODE_NAME`, `HOSTNAME` | Set from downward API | - |
| `CHECK_INTERVAL` | TCP check interval (seconds) | 10.0 |
| `PEER_RESOLVE_INTERVAL` | DNS re-resolve interval (seconds) | 60.0 |

## Building Images with Offline Dependencies

If you've downloaded wheels to `./wheels/`, modify Containerfiles to use them:

```dockerfile
# In Containerfile
COPY wheels/ /app/wheels/
COPY requirements.txt /app/
RUN pip install --no-index --find-links /app/wheels -r requirements.txt
```

## Troubleshooting

### Images won't pull
- Verify registry is accessible from cluster nodes
- Check image pull secrets if registry requires authentication
- Verify image tags match deployment manifests

### Bastion client can't connect
- Verify network connectivity from bastion to cluster
- Check firewall rules
- Verify IPs/URLs in configuration are correct
- Check MetalLB/NodePort/Routes are properly configured

### Pods not connecting to each other
- Verify services are created and endpoints exist
- Check service DNS resolution: `nslookup peer-1-svc.migration-test-system.svc.cluster.local`
- Verify pods can reach each other: `oc exec -it <pod> -- ping <peer-svc>`

### Dashboard not showing data
- Check bastion client logs
- Verify peer `/status` endpoints are accessible
- Check browser console for JavaScript errors

## Project Structure

```
migration-dashboard/
├── README.md                    # This file
├── build-images.sh              # Build script for container images
├── push-images.sh               # Push script for container images
├── download-dependencies.sh     # Download Python dependencies
└── source/
    ├── bastion-peer/
    │   ├── bastion-client.py    # Main bastion client application
    │   ├── Containerfile        # Container build file
    │   ├── requirements.txt      # Python dependencies
    │   └── config.example.env   # Example configuration
    ├── ocp-peer/
    │   ├── app.py               # Peer application
    │   ├── Containerfile        # Container build file
    │   ├── requirements.txt     # Python dependencies
    │   ├── deployment-peer-*.yaml  # Kubernetes deployments
    │   └── service-peer-*.yaml     # Kubernetes services
    └── dashboard/
        ├── dashboard.py         # Alternative dashboard (optional)
        ├── Containerfile        # Container build file
        └── requirements.txt     # Python dependencies
```
