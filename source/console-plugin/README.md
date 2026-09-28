# POC OpenShift Console plugin

Minimal customer-owned dynamic plugin for the OpenShift web console. It adds
one nav item (**Home → POC Plugin**) and a page that says the plugin loaded.
Nothing else.

This is the same extension mechanism ACM uses: a webpack module-federation
bundle, a `ConsolePlugin` CR, and an enablement entry on
`consoles.operator.openshift.io/cluster`.

Custom plugin code is not supported by Red Hat. Cluster-admin is required to
enable it.

## Prerequisites

- Node.js 18+ (to build)
- `oc` logged into an OpenShift 4.12+ cluster
- A container registry the cluster can pull from
- `podman` or `docker`

## Local development (no in-cluster deploy)

Terminal 1:

```bash
cd source/console-plugin
npm install
npm start
```

Terminal 2 (requires `oc login`):

```bash
npm run start-console
```

Open http://localhost:9000 and look under **Home → POC Plugin**.

## Deploy to a cluster

1. Build and push the image, then apply manifests and enable the plugin:

   ```bash
   cd source/console-plugin
   REGISTRY=registry.example.com:5000 ./deploy.sh
   ```

   Or set a full image reference:

   ```bash
   IMAGE=registry.example.com:5000/poc-plugin:v0.0.1 ./deploy.sh
   ```

2. Hard-refresh the OpenShift web console. The nav item is **Home → POC Plugin**,
   or open `/poc-plugin` on the console URL.

To enable by hand instead of using the script:

```bash
oc apply -k source/console-plugin/manifests
oc -n poc-console-plugin set image deployment/poc-plugin poc-plugin=YOUR_IMAGE
oc patch consoles.operator.openshift.io cluster --type=json \
  -p '[{"op":"add","path":"/spec/plugins/-","value":"poc-plugin"}]'
```

If `spec.plugins` is still empty, merge works:

```bash
oc patch consoles.operator.openshift.io cluster --type=merge \
  -p '{"spec":{"plugins":["poc-plugin"]}}'
```

## What gets created

| Object | Purpose |
| --- | --- |
| Namespace `poc-console-plugin` | Plugin workload |
| Service (HTTPS :9443) | Console fetches plugin assets; serving cert from the service CA |
| Deployment | nginx serving the webpack bundle |
| `ConsolePlugin/poc-plugin` | Registers the plugin with the console |
| `consoles.operator.openshift.io/cluster` `.spec.plugins` | Cluster-admin enablement |

## PatternFly (OEM look) and airgap

Use PatternFly so the plugin matches the OpenShift console. This cluster is
4.16, so use **PatternFly 5** (`@patternfly/react-core` / `react-icons` /
`react-table` major 5). Do not import `@patternfly/patternfly` CSS or
`@patternfly/react-styles/**/*.css` — the console already loads PF styles.
Prefix plugin-only CSS (`poc-plugin__…`) so it cannot override `.pf-` classes.

PatternFly does **not** need to be installed on the cluster. Webpack federates
the console’s PF at runtime. You only need the npm packages on the **build**
machine.

On a connected host (tarball is hundreds of MB, well under 10GB):

```bash
cd source/console-plugin
chmod +x download-npm-offline.sh
./download-npm-offline.sh
# produces ocp-console-plugin-npm-offline.tgz
```

Copy that file into the airgap, then:

```bash
tar -xzf ocp-console-plugin-npm-offline.tgz
npm ci --offline --ignore-scripts --legacy-peer-deps --cache ./npm-cache
# or use the node_modules/ from the tarball and skip npm
npm run build
```

Ship only the built `dist/` (or the plugin image) to the cluster, not the npm
tarball. For OpenShift 4.19+ set `PF_MAJOR=6` when running the download script.

## Remove

```bash
oc delete consoleplugin poc-plugin
oc delete namespace poc-console-plugin
```

Then remove `poc-plugin` from `spec.plugins` on `consoles.operator.openshift.io/cluster`.
