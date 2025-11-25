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
oc apply -f source/ocp-peer/service-peer-*.yaml
oc apply -f source/ocp-peer/deployment-peer-*.yaml
```

### 3. Create Routes
```bash
oc expose svc peer-1-svc -n migration-test-system --name=peer-1-route
oc expose svc peer-2-svc -n migration-test-system --name=peer-2-route
oc expose svc peer-3-svc -n migration-test-system --name=peer-3-route
```

### 4. Configure and Run Bastion Client
```bash
cp source/bastion-peer/config.example.env source/bastion-peer/config.env
# Edit config.env with your values
export $(cat source/bastion-peer/config.env | xargs)
python source/bastion-peer/bastion-client.py
```

Access dashboard at: http://localhost:9091

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

### 4. Update Manifests
- Update image registry in `deployment-peer-*.yaml`
- Update namespace if needed

### 5. Deploy and Configure
Follow steps 2-4 from "For Connected Environments" above.

## Configuration Quick Reference

### Bastion Client Environment Variables
```bash
export METALLB_PEERS='{"peer-1-lb": "IP1", "peer-2-lb": "IP2", "peer-3-lb": "IP3"}'
export ROUTE_PEERS="http://route1,http://route2,http://route3"
export DASHBOARD_PORT=9091
```

### Get Values from Cluster
```bash
# MetalLB IPs
oc get svc -n migration-test-system -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.loadBalancer.ingress[0].ip}{"\n"}{end}'

# Route URLs
oc get routes -n migration-test-system -o jsonpath='{range .items[*]}{.spec.host}{"\n"}{end}'
```

## Common Commands

```bash
# Check pods
oc get pods -n migration-test-system

# Check services
oc get svc -n migration-test-system

# View logs
oc logs -f deployment/peer-1 -n migration-test-system

# Restart deployment
oc rollout restart deployment/peer-1 -n migration-test-system

# Scale deployment
oc scale deployment/peer-1 --replicas=5 -n migration-test-system
```

