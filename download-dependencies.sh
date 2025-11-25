#!/bin/bash
# Download Python dependencies for offline/airgapped installation
# Usage: ./download-dependencies.sh [output-dir]
# Example: ./download-dependencies.sh ./wheels

set -e

OUTPUT_DIR="${1:-./wheels}"
mkdir -p ${OUTPUT_DIR}

echo "Downloading Python dependencies to ${OUTPUT_DIR}..."

# Download dependencies for all components
pip download -r source/bastion-peer/requirements.txt -d ${OUTPUT_DIR}
pip download -r source/ocp-peer/requirements.txt -d ${OUTPUT_DIR}
pip download -r source/dashboard/requirements.txt -d ${OUTPUT_DIR}

echo ""
echo "Dependencies downloaded to ${OUTPUT_DIR}"
echo ""
echo "To use in Containerfiles, copy wheels directory and modify Containerfile:"
echo "  COPY wheels/ /app/wheels/"
echo "  RUN pip install --no-index --find-links /app/wheels -r requirements.txt"

