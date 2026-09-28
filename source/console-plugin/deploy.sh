#!/usr/bin/env bash
# Build, push, and enable the POC OpenShift console plugin.
#
# Usage:
#   REGISTRY=registry.example.com:5000 ./deploy.sh
#   IMAGE=my.registry/poc-plugin:v0.0.1 ./deploy.sh
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REGISTRY=${REGISTRY:-registry.example.com:5000}
IMAGE=${IMAGE:-${REGISTRY}/poc-plugin:latest}

if ! command -v oc >/dev/null 2>&1; then
  echo "oc is required" >&2
  exit 1
fi

if ! oc whoami >/dev/null 2>&1; then
  echo "Not logged in. Run: oc login" >&2
  exit 1
fi

echo "Building ${IMAGE}"
if command -v podman >/dev/null 2>&1; then
  podman build --platform linux/amd64 -t "${IMAGE}" -f "${SCRIPT_DIR}/Containerfile" "${SCRIPT_DIR}"
  podman push "${IMAGE}"
else
  docker build --platform linux/amd64 -t "${IMAGE}" -f "${SCRIPT_DIR}/Containerfile" "${SCRIPT_DIR}"
  docker push "${IMAGE}"
fi

echo "Applying manifests with image ${IMAGE}"
oc apply -k "${SCRIPT_DIR}/manifests"
oc -n poc-console-plugin set image deployment/poc-plugin poc-plugin="${IMAGE}"
oc -n poc-console-plugin rollout status deployment/poc-plugin --timeout=120s

if oc get consoles.operator.openshift.io cluster >/dev/null 2>&1; then
  plugins=$(oc get consoles.operator.openshift.io cluster -o jsonpath='{.spec.plugins}' 2>/dev/null || true)
  if echo "${plugins}" | grep -q 'poc-plugin'; then
    echo "poc-plugin is already enabled on the cluster console"
  elif [ -z "${plugins}" ] || [ "${plugins}" = "null" ]; then
    echo "Enabling poc-plugin on the cluster console"
    oc patch consoles.operator.openshift.io cluster --type=merge \
      -p '{"spec":{"plugins":["poc-plugin"]}}'
  else
    echo "Enabling poc-plugin on the cluster console"
    oc patch consoles.operator.openshift.io cluster --type=json \
      -p '[{"op":"add","path":"/spec/plugins/-","value":"poc-plugin"}]'
  fi
fi

CONSOLE_URL=$(oc whoami --show-console 2>/dev/null || true)
echo
echo "Plugin deployed. After a console refresh, open Home → POC Plugin"
if [ -n "${CONSOLE_URL}" ]; then
  echo "  ${CONSOLE_URL}/poc-plugin"
fi
