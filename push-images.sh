#!/bin/bash
# Push script for migration dashboard container images
# Usage: ./push-images.sh [registry] [tag]
# Example: ./push-images.sh registry.example.com:5000 v1.0.0

set -e

REGISTRY="${1:-iherman-bastion-01.ocp.lab:5000}"
TAG="${2:-latest}"
NAMESPACE="${3:-applications}"

echo "Pushing images to registry: ${REGISTRY}, tag: ${TAG}, namespace: ${NAMESPACE}"

# Push peer-app image
echo "Pushing peer-app image..."
podman push ${REGISTRY}/${NAMESPACE}/peer-app:${TAG}

# Push bastion-client image
echo "Pushing bastion-client image..."
podman push ${REGISTRY}/${NAMESPACE}/bastion-client:${TAG}

# Push dashboard image
echo "Pushing dashboard image..."
podman push ${REGISTRY}/${NAMESPACE}/dashboard:${TAG}

echo ""
echo "All images pushed successfully!"

