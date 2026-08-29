# First Cluster Build Log (how the golden template was derived)

Forensic record of building the **first working** Kubernetes cluster on this
Magnum deployment — the five consecutive defects that had to be fixed, in order,
to get from "Magnum API works" to "a cluster is CREATE_COMPLETE / HEALTHY". The
fixes themselves are baked into the golden template so *new* clusters skip all of
this.

> Historical context (dated). Addresses use the documentation placeholders. Read
> [`golden-cluster-template.md`](golden-cluster-template.md) for the current truth.

---

## 0. Cluster prerequisites (keypair + image)

### SSH keypair `magnum-k8s`

```bash
cd ~/magnum-work
openssl genrsa -out magnum-k8s.pem 2048 && chmod 600 magnum-k8s.pem
ssh-keygen -y -f magnum-k8s.pem > magnum-k8s.pub
openstack keypair create --public-key magnum-k8s.pub magnum-k8s
```

Private key stays on the account that owns it (no other way into the nodes). SSH to
nodes as user `core`.

### Fedora CoreOS image

```bash
wget https://builds.coreos.fedoraproject.org/prod/streams/stable/builds/38.20230806.3.0/x86_64/fedora-coreos-38.20230806.3.0-openstack.x86_64.qcow2.xz
unxz -T0 fedora-coreos-38.20230806.3.0-openstack.x86_64.qcow2.xz
openstack image create fedora-coreos-38.20230806.3.0 \
  --file fedora-coreos-38.20230806.3.0-openstack.x86_64.qcow2 \
  --disk-format qcow2 --container-format bare \
  --property os_distro=fedora-coreos
rm fedora-coreos-38.20230806.3.0-openstack.x86_64.qcow2   # reclaim space after 'active'
```

---

## 1. The five defects

```bash
openstack coe cluster template create k8s-ct \
  --image fedora-coreos-38.20230806.3.0-ca --external-network ext-net \
  --dns-nameserver 203.0.113.53 --keypair magnum-k8s \
  --master-flavor 2c2r20d --flavor 2c2r20d \
  --network-driver flannel --coe kubernetes \
  --labels kube_tag=v1.26.8-rancher1
openstack coe cluster create k8s-test --cluster-template k8s-ct --master-count 1 --node-count 1
```

Final state (original build): **CREATE_COMPLETE / HEALTHY**, both nodes `Ready`
(v1.26.8, containerd 1.6.19), all kube-system pods Running, test deployment
(`nginx:alpine`) Running.

Getting there required fixing five separate defects — each verified before moving on:

### 1.1 `heat_stack_user` role missing (pre-existing cloud gap)

First create attempt failed instantly: `Can't find role heat_stack_user`. Repaired
once, additively:

```bash
openstack role create heat_stack_user
```

### 1.2 Trust ID blanked by default (`cluster-user-trust`)

Guest scripts died building a trust-scoped token — `/etc/sysconfig/heat-params`
had `TRUST_ID=""` although the trust existed in the magnum DB. Fix (charm-exposed):

```bash
juju config magnum cluster-user-trust=true

# ⚠ config change re-renders unit files → re-apply the haproxy backend and
#   v2.0→v3 fixes afterwards (canonical block: ../OpenStack/magnum-fixes-and-maintenance.md)
```

### 1.3 Vault CA baked into a custom FCOS image

The in-guest `heat-container-agent` (podman) failed TLS to keystone — stock FCOS
lacks the Vault root CA. `virt-customize` is broken on Ubuntu (supermin), so the
image was customized via `qemu-nbd` (baking the CA into the FCOS ostree root). The
repeatable steps live in [`fcos-bake-ca.sh`](../OpenStack/fcos-bake-ca.sh).

### 1.4 Kubelet legacy flags (kube ≥1.24 removed them)

Control plane containers started, but kubelet crash-looped:
`unknown flag: --network-plugin` (then `--cni-conf-dir`/`--cni-bin-dir`, then a
missing runtime endpoint). Bobcat-era templates still render flags removed in k8s
1.24+, and the default `host-docker` runtime can't work with ≥1.24.

Manually per node: enable containerd (CRI), strip the legacy flags from
`/etc/kubernetes/kubelet`, append `--container-runtime-endpoint=unix:///run/containerd/containerd.sock`,
restart kubelet. **All of this is eliminated today** by `container_runtime=containerd`
in the golden template.

### 1.5 Flannel images/binary + containerd CNI dir

Nodes registered but stayed NotReady; three stacked issues:

1. `quay.io/coreos/flannel-cni:v0.3.0` → **401** (tag effectively gone from quay).
   The `install-cni-plugins` initContainer was replaced with a busybox fetch of the
   official plugins — see [`flannel-patch.json`](../Kubernetes/flannel-patch.json).
2. containerd looked for plugins in `/usr/libexec/cni/` (its config.toml) while
   magnum installs to `/opt/cni/bin`; a bind mount alone is not enough — containerd
   caches the dir at startup (must restart containerd **and** kubelet).
3. The standard plugins bundle lacks the `flannel` binary itself — extracted it on
   each node from a rancher mirror image.

After kicking stuck pods (`kubectl delete pod --force --grace-period=0`) everything
converged.

### Verification matrix (original build)

| Check | Result |
|---|---|
| `coe cluster show` | CREATE_COMPLETE, **HEALTHY** |
| `kubectl get nodes` | 2/2 Ready, v1.26.8, containerd://1.6.19 |
| `kubectl get pods -n kube-system` | coredns×2, dashboard, npd, keystone-auth, OCCM, flannel×2 — all Running |
| Workload | `test-nginx` deployment Running on the flannel overlay |

---

## 2. Skyline Container Infra fixes (two upstream bugs)

### Fix A — nginx config missing `container_infra` proxy location

Skyline's nginx is generated from the keystone catalog **before magnum existed** —
no `container_infra` proxy location. Regenerate it (and hard-refresh the browser):

```bash
juju run skyline/leader regenerate-nginx
```

### Fix B — upstream skyline-console JS bug (Create Cluster page error)

`checkVolumeQuota()` destructures `cinderQuota` without a fallback. When Cinder is
absent, the destructuring `{left:l=0}=r` throws because `r` is undefined, surfacing
as *"Error, Unable to get Data"*:

```javascript
// Before (broken):  {left:l=0}=r;
// After (fixed):    {left:l=0}=r||{};
```

Applied to all occurrences in the minified JS files on each Skyline unit; stale
`.gz` companions removed so nginx serves the patched `.js`. The patch marker ensures
idempotency.

---

## 3. Cleanup (done)

| Item | Action |
|---|---|
| `fcos-base.qcow2` / `fcos-ca.qcow2` temp files | Deleted (uploaded images persist in Glance) |
| `combined-bundle.pem` temp cert bundle | Deleted |
| `test-nginx` deployment | Deleted (cluster clean, system pods only) |

**Retained for future use:**

| File | Purpose |
|---|---|
| `magnum-k8s.pem` | Private SSH key for cluster nodes — **no other way in** |
| `kubeconfig/config` | kubectl credentials |
| `kubectl` (v1.26.8) | Matching kubectl binary |
| `kisroot-ca.crt` | Vault root CA |