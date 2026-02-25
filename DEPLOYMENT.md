# Deployment Guide for Airgapped Environments

This guide provides step-by-step instructions for deploying the migration dashboard in an airgapped/disconnected environment.

## Pre-Deployment Checklist

- [ ] Container registry accessible from cluster nodes
- [ ] Python 3.11+ available for building (or pre-built images)
- [ ] Network connectivity from bastion to cluster (node IPs + NodePort)
- [ ] OpenShift/Kubernetes cluster access
- [ ] Namespace created (default: `migration-test-system`)
- [ ] Deploy script config (REGISTRY, NAMESPACE) prepared (e.g. `deploy.conf`)

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

## Step 5: Deploy to Cluster (Script)

The deploy script applies the DaemonSet, headless Service, NodePort Service (with image/namespace substitution), waits for pods, then runs **router sync**: labels pods with `node-name`, creates one Service and one Route per node, and prints **NODE_STATUS_ENDPOINTS** and **ROUTE_STATUS_ENDPOINTS**.

### Config file

```bash
cp scripts/deploy.conf.example deploy.conf
# Edit: REGISTRY=local-registry.example.com:5000, NAMESPACE=migration-test-system
# Optional: IMAGE_TAG, PEER_APP_IMAGE, NODE_PORT_SVC_NAME
```

### Deploy

```bash
oc create namespace migration-test-system   # if needed
./scripts/deploy.sh -c deploy.conf
# Or override: ./scripts/deploy.sh -r local-registry.example.com:5000 -n migration-test-system
```

Copy the printed NODE_STATUS_ENDPOINTS and ROUTE_STATUS_ENDPOINTS for bastion config.

### Verify

```bash
oc get pods -n migration-test-system -l app=migration-peer
oc get svc -n migration-test-system
oc get routes -n migration-test-system
```

## Step 6: Configure Bastion Client

```bash
cp source/bastion-peer/config.example.env source/bastion-peer/config.env
```

Set **NODE_STATUS_ENDPOINTS** (and optionally **ROUTE_STATUS_ENDPOINTS**) from the deploy script output. Optionally set METALLB_PEERS, NODEPORT_PEERS, ROUTE_PEERS for external tests.

## Step 7: Run Bastion Client

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
  -e NODE_STATUS_ENDPOINTS="http://node1:30082,http://node2:30082,..." \
  local-registry.example.com:5000/applications/bastion-client:latest
```

## Step 8: Access Dashboard

Open browser to: `http://localhost:9091`. The dashboard shows a **dynamic N×N** connectivity matrix (by node) and a **Mermaid connectivity graph** that updates with polled data.

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

1. Test headless service DNS (peer discovery):
   ```bash
   oc exec -it <pod-name> -n migration-test-system -- nslookup migration-peer-svc
   ```

2. Test pod HTTP from another pod:
   ```bash
   oc exec -it <pod-name> -n migration-test-system -- curl http://migration-peer-svc:8082/ping
   ```

3. Check firewall rules between bastion and cluster (node IP + NodePort)

4. Verify NODE_STATUS_ENDPOINTS use node InternalIPs and the NodePort from `migration-peer-nodeport`

### Dashboard Not Showing Data

1. Check bastion client logs:
   ```bash
   podman logs migration-dashboard
   ```

2. Test a node endpoint manually (use a URL from NODE_STATUS_ENDPOINTS):
   ```bash
   curl http://<node-ip>:<NodePort>/status
   ```

3. Check browser console for JavaScript errors

4. Verify NODE_STATUS_ENDPOINTS and (if used) ROUTE_STATUS_ENDPOINTS are correct

## Post-Deployment Verification

1. **Pods Running**: DaemonSet pods (one per node) in Running state
2. **Services**: Headless + NodePort + one Service per node (for Routes) created
3. **Routes**: One Route per node if router sync was run
4. **Dashboard**: Loads at port 9091; dynamic N×N matrix and Mermaid connectivity graph show data
5. **History**: Disconnection history tracked and clear fans out to all nodes

## Node add/remove

When cluster nodes are added or removed:

1. Re-run the deploy script: `./scripts/deploy.sh -c deploy.conf` (or `--sync-routes` only to update Services/Routes).
2. Update **NODE_STATUS_ENDPOINTS** and **ROUTE_STATUS_ENDPOINTS** in bastion config from the new script output.
3. Restart the bastion (or reload config if supported). The in-cluster TCP mesh self-heals via headless Service DNS.

## Maintenance

### Updating Images

1. Build new images with updated tag
2. Set IMAGE_TAG (or REGISTRY/PEER_APP_IMAGE) in deploy.conf and re-run:
   ```bash
   ./scripts/deploy.sh -c deploy.conf
   ```
3. Or apply DaemonSet with new image and let pods roll:
   ```bash
   oc set image daemonset/migration-peer peer-app=... -n migration-test-system
   ```

### Scaling

The DaemonSet runs one pod per node; scaling is by adding/removing cluster nodes. Re-run the deploy script and refresh bastion config after node changes (see Node add/remove).

### Monitoring

Check pod resource usage:

```bash
oc top pods -n migration-test-system
```

Check service endpoints:

```bash
oc get endpoints -n migration-test-system
```

