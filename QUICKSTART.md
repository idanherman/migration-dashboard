# Quick Start Guide

## For Connected Environments

### 1. Build and Push Images
```bash
./build-images.sh registry.example.com:5000 latest applications
./push-images.sh registry.example.com:5000 latest applications
```

### 2. Deploy to Cluster
```bash
oc create namespace migration-test-system
cp scripts/deploy.conf.example deploy.conf
# Edit deploy.conf: REGISTRY=..., NAMESPACE=migration-test-system
./scripts/deploy.sh -c deploy.conf
```
Copy the printed **NODE_STATUS_ENDPOINTS** (and **ROUTE_STATUS_ENDPOINTS**) into bastion config.

### 3. Configure and Run Bastion Client
```bash
cp source/bastion-peer/config.example.env source/bastion-peer/config.env
# Set NODE_STATUS_ENDPOINTS (and optionally ROUTE_STATUS_ENDPOINTS) from deploy script output
export $(cat source/bastion-peer/config.env | xargs)
python source/bastion-peer/bastion-client.py
```

Access dashboard at: http://localhost:9091 (dynamic N×N matrix and Mermaid connectivity graph).

## For Airgapped Environments

### 1. On Connected System: Prepare
```bash
./download-dependencies.sh ./wheels
./build-images.sh registry.example.com:5000 v1.0.0 applications
podman save -o images.tar <all-images>
```

### 2. Transfer to Airgapped System
- Copy `images.tar`
- Copy entire `source/` directory
- Copy scripts and documentation

### 3. On Airgapped System: Load Images
```bash
podman load -i images.tar
./push-images.sh local-registry.example.com:5000 v1.0.0 applications
```

### 4. Deploy and Configure
- Set REGISTRY and NAMESPACE in `deploy.conf` (or use `-r` / `-n` when running the script).
- Run `./scripts/deploy.sh -c deploy.conf`; paste NODE_STATUS_ENDPOINTS and ROUTE_STATUS_ENDPOINTS into bastion config.
- Follow step 3 from "For Connected Environments" to run the bastion.

## Configuration Quick Reference

### Bastion Client (required for internal status)
```bash
# From deploy script output
export NODE_STATUS_ENDPOINTS="http://node1:30082,http://node2:30082,..."
export ROUTE_STATUS_ENDPOINTS="https://migration-peer-node1-....apps...,..."   # optional, router test
export DASHBOARD_PORT=9091
```

### Node add/remove
After adding or removing cluster nodes, re-run `./scripts/deploy.sh -c deploy.conf` (or `--sync-routes`), update NODE_STATUS_ENDPOINTS and ROUTE_STATUS_ENDPOINTS in bastion config, then restart the bastion. The in-cluster mesh self-heals via DNS.

## Common Commands

```bash
# Check DaemonSet pods
oc get pods -n migration-test-system -l app=migration-peer

# Check services
oc get svc -n migration-test-system

# View logs (any peer pod)
oc logs -f -l app=migration-peer -n migration-test-system

# Re-sync routes only (e.g. after node add/remove)
./scripts/deploy.sh -c deploy.conf --sync-routes
```

