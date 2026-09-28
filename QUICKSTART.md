# Quick Start Guide

## For Connected Environments

### 1. Build and Push Images
```bash
./build-images.sh registry.example.com:5000 v1.0.2 applications
./push-images.sh registry.example.com:5000 v1.0.2 applications
```

### 2. Deploy to Cluster
```bash
oc create namespace migration-test-system
cp scripts/deploy.conf.example deploy.conf
# Edit deploy.conf: REGISTRY=..., NAMESPACE=migration-test-system, IMAGE_TAG=v1.0.2
./scripts/deploy.sh -c deploy.conf
```
Copy the printed **NODE_STATUS_ENDPOINTS** and **NODEPORT_PEERS** into `source/bastion-peer/config.podman.env` (KEY=value, no `export`).

### 3. Configure and Run Bastion Client
```bash
cp source/bastion-peer/config.podman.example.env source/bastion-peer/config.podman.env
# Paste deploy.sh output into config.podman.env
podman run -d --name migration-dashboard --network host \
  --env-file source/bastion-peer/config.podman.env \
  registry.example.com:5000/applications/bastion-client:v1.0.2
```

Access dashboard at: http://localhost:9091 (dynamic N×N matrix and Mermaid connectivity graph).

## For Airgapped Environments

### 1. On Connected System: Prepare
```bash
./download-dependencies.sh ./wheels
./build-images.sh registry.example.com:5000 v1.0.2 applications
podman save -o images.tar <all-images>
```

### 2. Transfer to Airgapped System
- Copy `images.tar`
- Copy entire `source/` directory
- Copy scripts and documentation

### 3. On Airgapped System: Load Images
```bash
podman load -i images.tar
./push-images.sh local-registry.example.com:5000 v1.0.2 applications
```

### 4. Deploy and Configure
- Set REGISTRY and NAMESPACE in `deploy.conf` (or use `-r` / `-n` when running the script).
- Run `./scripts/deploy.sh -c deploy.conf`; paste output into `config.podman.env`.
- Run the bastion container with `--network host` (see step 3 above).

## Configuration Quick Reference

### Bastion Client (required)
```bash
NODE_STATUS_ENDPOINTS=http://node1:30082,http://node2:30082,...
NODEPORT_PEERS={"peer-1-np":{"host":"node1","ws_port":30080,"tcp_port":30081,"http_port":30082},...}
SSL_VERIFY=false
DASHBOARD_PORT=9091
```

### Node add/remove
Re-run `./scripts/deploy.sh -c deploy.conf`, update `config.podman.env`, restart the dashboard container. The in-cluster mesh self-heals via DNS.

## Common Commands

```bash
# Check DaemonSet pods
oc get pods -n migration-test-system -l app=migration-peer

# Check NodePort service
oc get svc migration-peer-nodeport -n migration-test-system

# View logs (any peer pod)
oc logs -f -l app=migration-peer -n migration-test-system

# Re-print bastion config from live cluster
./scripts/print-expected-bastion-endpoints.sh -n migration-test-system
```
