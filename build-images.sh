#!/bin/bash
# Build script for migration dashboard container images
# Usage: ./build-images.sh [registry] [tag]
# Example: ./build-images.sh registry.example.com:5000 v1.0.0

set -e

REGISTRY="${1:-iherman-bastion-01.ocp.lab:5000}"
TAG="${2:-latest}"
NAMESPACE="${3:-applications}"

echo "Building images with registry: ${REGISTRY}, tag: ${TAG}, namespace: ${NAMESPACE}"

# Build peer-app image
echo "Building peer-app image..."
cd source/ocp-peer
podman build -t ${REGISTRY}/${NAMESPACE}/peer-app:${TAG} -f Containerfile .
echo "Built: ${REGISTRY}/${NAMESPACE}/peer-app:${TAG}"

# Build bastion-client image
echo "Building bastion-client image..."
cd ../bastion-peer
podman build -t ${REGISTRY}/${NAMESPACE}/bastion-client:${TAG} -f Containerfile .
echo "Built: ${REGISTRY}/${NAMESPACE}/bastion-client:${TAG}"

# Build dashboard image (optional)
echo "Building dashboard image..."
cd ../dashboard
podman build -t ${REGISTRY}/${NAMESPACE}/dashboard:${TAG} -f Containerfile .
echo "Built: ${REGISTRY}/${NAMESPACE}/dashboard:${TAG}"

cd ../..

echo ""
echo "Build complete! Images:"
echo "  - ${REGISTRY}/${NAMESPACE}/peer-app:${TAG}"
echo "  - ${REGISTRY}/${NAMESPACE}/bastion-client:${TAG}"
echo "  - ${REGISTRY}/${NAMESPACE}/dashboard:${TAG}"
echo ""
echo "To push images to registry:"
echo "  podman push ${REGISTRY}/${NAMESPACE}/peer-app:${TAG}"
echo "  podman push ${REGISTRY}/${NAMESPACE}/bastion-client:${TAG}"
echo "  podman push ${REGISTRY}/${NAMESPACE}/dashboard:${TAG}"

