# Deployment Guide for Airgapped Environments

This guide provides step-by-step instructions for deploying the migration dashboard in an airgapped/disconnected environment.

## Pre-Deployment Checklist

- [ ] Container registry accessible from cluster nodes
- [ ] Python 3.11+ available for building (or pre-built images)
- [ ] Network connectivity from bastion to cluster
- [ ] OpenShift/Kubernetes cluster access
- [ ] Namespace created (default: `migration-test-system`)
- [ ] Routes configured (if using OpenShift Routes)
- [ ] MetalLB configured (if using LoadBalancer services)
- [ ] NodePort ports identified (if using NodePort services)

## Step 1: Prepare Dependencies (On Connected System)

### Download Python Dependencies

```bash
cd /path/to/migration-dashboard
./download-dependencies.sh ./wheels
```

This creates a `wheels/` directory containing all Python package wheels.

### Build Container Images

```bash
# Build all images
./build-images.sh your-registry.example.com:5000 v1.0.0 applications

# Save images for transfer
podman save -o migration-dashboard-images.tar \
  your-registry.example.com:5000/applications/peer-app:v1.0.0 \
  your-registry.example.com:5000/applications/bastion-client:v1.0.0 \
  your-registry.example.com:5000/applications/dashboard:v1.0.0
```

## Step 2: Transfer to Airgapped Environment

Copy the following to your airgapped environment:

1. **Container images**: `migration-dashboard-images.tar`
2. **Source code**: Entire `source/` directory
3. **Dependencies** (optional): `wheels/` directory
4. **Scripts**: `build-images.sh`, `push-images.sh`, `download-dependencies.sh`
5. **Documentation**: `README.md`, `DEPLOYMENT.md`

## Step 3: Load Images (On Airgapped System)

### Option A: Load Pre-built Images

```bash
podman load -i migration-dashboard-images.tar
```

### Option B: Build Images Locally

If you have the `wheels/` directory and want to rebuild:

1. Modify Containerfiles to use offline wheels (see README.md)
2. Build images:
   ```bash
   ./build-images.sh local-registry.example.com:5000 v1.0.0 applications
   ```

## Step 4: Push Images to Local Registry

```bash
# Tag images for local registry (if needed)
podman tag your-registry.example.com:5000/applications/peer-app:v1.0.0 \
  local-registry.example.com:5000/applications/peer-app:v1.0.0

# Push to local registry
./push-images.sh local-registry.example.com:5000 v1.0.0 applications
```

## Step 5: Configure Kubernetes Manifests

### Update Image Registry

Edit all deployment files (`deployment-peer-*.yaml`):

```yaml
containers:
  - name: peer-app
    image: local-registry.example.com:5000/applications/peer-app:latest
```

### Update Namespace

If using a different namespace, update all YAML files:

```yaml
metadata:
  namespace: your-namespace
```

Or remove namespace and apply to default namespace.

### Configure Node Selectors (Optional)

If you need pods on specific nodes, uncomment and update:

```yaml
spec:
  nodeSelector:
    kubernetes.io/hostname: your-worker-node
```

## Step 6: Deploy to Cluster

### Create Namespace

```bash
oc create namespace migration-test-system
# Or use existing namespace
```

### Deploy Services

```bash
oc apply -f source/ocp-peer/service-peer-1.yaml
oc apply -f source/ocp-peer/service-peer-2.yaml
oc apply -f source/ocp-peer/service-peer-3.yaml
```

### Deploy Peer Applications

```bash
oc apply -f source/ocp-peer/deployment-peer-1.yaml
oc apply -f source/ocp-peer/deployment-peer-2.yaml
oc apply -f source/ocp-peer/deployment-peer-3.yaml
```

### Verify Deployment

```bash
# Check pods are running
oc get pods -n migration-test-system

# Check services
oc get svc -n migration-test-system

# Check pod logs
oc logs -f deployment/peer-1 -n migration-test-system
```

## Step 7: Configure Routes (OpenShift)

If using OpenShift Routes:

```bash
# Create routes
oc expose svc peer-1-svc -n migration-test-system --name=peer-1-route
oc expose svc peer-2-svc -n migration-test-system --name=peer-2-route
oc expose svc peer-3-svc -n migration-test-system --name=peer-3-route

# Get route URLs
oc get routes -n migration-test-system -o jsonpath='{range .items[*]}{.spec.host}{"\n"}{end}'
```

## Step 8: Get Service Information

### MetalLB LoadBalancer IPs

```bash
oc get svc -n migration-test-system -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.loadBalancer.ingress[0].ip}{"\n"}{end}'
```

### NodePort Ports

```bash
oc get svc -n migration-test-system -o yaml | grep -A 5 nodePort
```

Note the node IPs and port numbers for each service.

## Step 9: Configure Bastion Client

### Create Configuration File

```bash
cp source/bastion-peer/config.example.env source/bastion-peer/config.env
```

### Edit Configuration

Update `source/bastion-peer/config.env` with your environment values:

```bash
# MetalLB IPs (from Step 8)
export METALLB_PEERS='{"peer-1-lb": "10.0.0.1", "peer-2-lb": "10.0.0.2", "peer-3-lb": "10.0.0.3"}'

# NodePort (from Step 8)
export NODEPORT_PEERS='{"peer-1-np": {"host": "node-ip", "ws_port": 30001, "tcp_port": 30002, "http_port": 30003}, ...}'

# Routes (from Step 7)
export ROUTE_PEERS="http://peer-1-route-migration-test-system.apps.example.com,http://peer-2-route-migration-test-system.apps.example.com,http://peer-3-route-migration-test-system.apps.example.com"
```

## Step 10: Run Bastion Client

### Option A: Run Directly

```bash
cd source/bastion-peer
export $(cat config.env | xargs)
python bastion-client.py
```

### Option B: Run in Container

```bash
# Build bastion client image (if not already built)
cd source/bastion-peer
podman build -t local-registry.example.com:5000/applications/bastion-client:latest -f Containerfile .

# Run container
podman run -d \
  --name migration-dashboard \
  -p 9091:9091 \
  --env-file config.env \
  local-registry.example.com:5000/applications/bastion-client:latest
```

### Option C: Run with Environment Variables

```bash
podman run -d \
  --name migration-dashboard \
  -p 9091:9091 \
  -e METALLB_PEERS='{"peer-1-lb": "10.0.0.1", ...}' \
  -e ROUTE_PEERS="http://peer-1-route.example.com,..." \
  local-registry.example.com:5000/applications/bastion-client:latest
```

## Step 11: Access Dashboard

Open browser to: `http://localhost:9091` (or bastion host IP:9091)

## Troubleshooting

### Images Not Pulling

1. Verify registry is accessible:
   ```bash
   curl -k https://local-registry.example.com:5000/v2/
   ```

2. Check image pull secrets:
   ```bash
   oc get secrets -n migration-test-system
   oc create secret docker-registry regcred \
     --docker-server=local-registry.example.com:5000 \
     --docker-username=user \
     --docker-password=pass \
     -n migration-test-system
   ```

3. Update deployment to use image pull secret:
   ```yaml
   spec:
     imagePullSecrets:
       - name: regcred
   ```

### Pods Not Starting

1. Check pod events:
   ```bash
   oc describe pod <pod-name> -n migration-test-system
   ```

2. Check pod logs:
   ```bash
   oc logs <pod-name> -n migration-test-system
   ```

3. Verify image exists in registry:
   ```bash
   podman pull local-registry.example.com:5000/applications/peer-app:latest
   ```

### Connectivity Issues

1. Test service DNS:
   ```bash
   oc exec -it <pod-name> -n migration-test-system -- nslookup peer-1-svc
   ```

2. Test pod-to-pod connectivity:
   ```bash
   oc exec -it <pod-name> -n migration-test-system -- curl http://peer-1-svc:8082/ping
   ```

3. Check firewall rules between bastion and cluster

4. Verify MetalLB/NodePort/Routes are configured correctly

### Dashboard Not Showing Data

1. Check bastion client logs:
   ```bash
   podman logs migration-dashboard
   ```

2. Test peer endpoints manually:
   ```bash
   curl http://peer-1-route.example.com/status
   ```

3. Check browser console for JavaScript errors

4. Verify configuration values are correct

## Post-Deployment Verification

1. **Pods Running**: All peer pods should be in Running state
2. **Services Created**: All three services should have endpoints
3. **Routes Accessible**: Routes should return HTTP 200
4. **Dashboard Accessible**: Dashboard should load at port 9091
5. **Data Showing**: Dashboard should show connection status for all peers
6. **History Working**: Disconnection history should be tracked

## Maintenance

### Updating Images

1. Build new images with updated tag
2. Update deployment manifests with new tag
3. Apply updated manifests:
   ```bash
   oc apply -f source/ocp-peer/deployment-peer-*.yaml
   ```
4. Restart pods if needed:
   ```bash
   oc rollout restart deployment/peer-1 -n migration-test-system
   ```

### Scaling

To change replica count:

```bash
oc scale deployment/peer-1 --replicas=5 -n migration-test-system
```

### Monitoring

Check pod resource usage:

```bash
oc top pods -n migration-test-system
```

Check service endpoints:

```bash
oc get endpoints -n migration-test-system
```

