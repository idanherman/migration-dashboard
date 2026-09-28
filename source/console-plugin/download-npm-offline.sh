#!/usr/bin/env bash
# Build an offline npm bundle for OpenShift console plugin development.
# Run this on a machine with internet, then copy the tarball into the airgap.
#
# Usage:
#   ./download-npm-offline.sh
#   ./download-npm-offline.sh /path/to/ocp-console-plugin-npm-offline.tgz
#
# Target: OpenShift 4.15–4.18 (PatternFly 5). For 4.19+ : PF_MAJOR=6 ./download-npm-offline.sh
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
OUT="${1:-${SCRIPT_DIR}/ocp-console-plugin-npm-offline.tgz}"
WORK=$(mktemp -d)
PF_MAJOR="${PF_MAJOR:-5}"

cleanup() { rm -rf "${WORK}"; }
trap cleanup EXIT

echo "Staging npm cache in ${WORK} (PatternFly ${PF_MAJOR})"
cd "${WORK}"
cp "${SCRIPT_DIR}/package.json" .

# PatternFly is a build-time dependency. At runtime the OpenShift console
# already provides PF CSS and federated components — do not import
# @patternfly/patternfly or @patternfly/react-styles/**/*.css in plugin code.
npm install --ignore-scripts --no-audit --no-fund --legacy-peer-deps \
  --cache "${WORK}/npm-cache" \
  "@patternfly/react-core@${PF_MAJOR}" \
  "@patternfly/react-icons@${PF_MAJOR}" \
  "@patternfly/react-table@${PF_MAJOR}"

tar -C "${WORK}" -czf "${OUT}" \
  package.json \
  package-lock.json \
  npm-cache \
  node_modules

BYTES=$(wc -c < "${OUT}" | tr -d ' ')
echo
echo "Wrote ${OUT} (${BYTES} bytes)"
echo
echo "On the airgapped machine, next to this plugin tree:"
echo "  tar -xzf $(basename "${OUT}")"
echo "  npm ci --offline --ignore-scripts --legacy-peer-deps --cache ./npm-cache"
echo "  # or skip npm and use the included node_modules/"
echo
echo "Do not copy this tarball into the cluster. The console already has PatternFly."
echo "Build the plugin JS here, then ship only the webpack dist / plugin image."
