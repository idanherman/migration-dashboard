# OVN Migration Dashboard

A comprehensive monitoring dashboard for tracking connectivity during OVN (Open Virtual Network) migrations in OpenShift/Kubernetes environments. The dashboard monitors connectivity between bastion hosts and cluster peers, as well as pod-to-pod connectivity within the cluster.

## Architecture

The system consists of three main components:

1. **Bastion Client** (`bastion-peer/`) - Runs outside the cluster, tests external connectivity
2. **Peer Application** (`ocp-peer/`) - Runs inside cluster pods, tests pod-to-pod connectivity
3. **Dashboard** (`dashboard/`) - Alternative simpler dashboard (optional)

## Components

### Bastion Client
- Tests connectivity from bastion host to cluster via:
  - MetalLB LoadBalancer IPs (HTTP, WebSocket, TCP)
  - NodePort services (HTTP, WebSocket, TCP)
  - OpenShift Routes (HTTP)
- Polls peer status endpoints
- Provides web dashboard on port 9091
- Tracks disconnection history

### Peer Application
- Runs inside cluster pods
- Implements WS/TCP/HTTP servers
- Connects to other peer services (pod-to-pod)
- Tracks connection state and outages
- Exposes `/status` and `/history` endpoints

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

#### Update Kubernetes Manifests

1. **Update image registry** in deployment files:
   ```yaml
   # In deployment-peer-*.yaml
   image: your-registry.example.com:5000/applications/peer-app:latest
   ```

2. **Update namespace** (if different):
   ```yaml
   metadata:
     namespace: your-namespace
   ```

3. **Configure node selectors** (if needed):
   ```yaml
   spec:
     nodeSelector:
       kubernetes.io/hostname: your-worker-node
   ```

#### Configure Bastion Client

1. Copy example config:
   ```bash
   cp source/bastion-peer/config.example.env source/bastion-peer/config.env
   ```

2. Edit `config.env` with your environment values:
   - MetalLB IPs
   - NodePort configurations
   - Route URLs
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

```bash
# Create namespace (if needed)
oc create namespace migration-test-system

# Deploy services
oc apply -f source/ocp-peer/service-peer-1.yaml
oc apply -f source/ocp-peer/service-peer-2.yaml
oc apply -f source/ocp-peer/service-peer-3.yaml

# Deploy peer applications
oc apply -f source/ocp-peer/deployment-peer-1.yaml
oc apply -f source/ocp-peer/deployment-peer-2.yaml
oc apply -f source/ocp-peer/deployment-peer-3.yaml

# Verify deployments
oc get pods -n migration-test-system
oc get svc -n migration-test-system
```

### 5. Configure Routes (OpenShift)

If using OpenShift Routes, create routes for each peer service:

```bash
oc expose svc peer-1-svc -n migration-test-system --name=peer-1-route
oc expose svc peer-2-svc -n migration-test-system --name=peer-2-route
oc expose svc peer-3-svc -n migration-test-system --name=peer-3-route
```

Get route URLs:
```bash
oc get routes -n migration-test-system
```

Update `ROUTE_PEERS` in bastion client configuration.

### 6. Configure MetalLB (if using)

1. Get LoadBalancer IPs:
   ```bash
   oc get svc -n migration-test-system
   ```

2. Update `METALLB_PEERS` in bastion client configuration.

### 7. Configure NodePort (if using)

1. Get NodePort ports:
   ```bash
   oc get svc -n migration-test-system -o yaml
   ```

2. Update `NODEPORT_PEERS` in bastion client configuration with:
   - Node IPs
   - NodePort numbers for each service

### 8. Run Bastion Client

```bash
# Using environment file
export $(cat source/bastion-peer/config.env | xargs)
python source/bastion-peer/bastion-client.py

# Or using container
podman run -d \
  --name migration-dashboard \
  -p 9091:9091 \
  -e METALLB_PEERS='{"peer-1-lb": "10.0.0.1", ...}' \
  -e ROUTE_PEERS="http://peer-1-route.example.com,..." \
  your-registry.example.com:5000/applications/bastion-client:latest
```

Access dashboard at: `http://localhost:9091`

## Configuration Reference

### Bastion Client Environment Variables

| Variable | Description | Default | Format |
|----------|-------------|---------|--------|
| `METALLB_PEERS` | MetalLB LoadBalancer IPs | See example | JSON object |
| `NODEPORT_PEERS` | NodePort configurations | See example | JSON object |
| `ROUTE_PEERS` | HTTP Route URLs | See example | Comma-separated |
| `HTTP_INTERVAL` | HTTP test interval (seconds) | 1.0 | Float |
| `WS_INTERVAL` | WebSocket test interval (seconds) | 0.5 | Float |
| `TCP_INTERVAL` | TCP test interval (seconds) | 0.5 | Float |
| `POLL_INTERVAL` | Status poll interval (seconds) | 1.0 | Float |
| `RECONNECT_DELAY` | Reconnect delay (seconds) | 1.0 | Float |
| `HTTP_TIMEOUT` | HTTP timeout (seconds) | 1.0 | Float |
| `WS_OPEN_TIMEOUT` | WebSocket open timeout (seconds) | 1.0 | Float |
| `TCP_CONNECT_TIMEOUT` | TCP connect timeout (seconds) | 1.0 | Float |
| `DASHBOARD_PORT` | Dashboard web port | 9091 | Integer |
| `MAX_HISTORY` | Max history entries | 200 | Integer |

### Peer Application Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `PEERS` | Comma-separated peer service names | `peer-1-svc,peer-2-svc,peer-3-svc` |
| `HOSTNAME` | Pod hostname (auto-set from metadata) | Pod name |

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
