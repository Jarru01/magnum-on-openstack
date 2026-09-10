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
  --image fedora-coreos-38.20230806.3.0 --external-network ext-net \
  --dns-nameserver 203.0.113.53 --keypair magnum-k8s \
  --master-flavor 2c2r20d --flavor 2c2r20d \
  --network-driver flannel --coe kubernetes \
  --labels kube_tag=v1.26.8-rancher1
openstack coe cluster create k8s-test --cluster-template k8s-ct --master-count 1 --node-count 1
```

Final state (original build): **CREATE_COMPLETE / HEALTHY**, both nodes `Ready`
(v1.26.8, containerd 1.6.19), all kube-system pods Running, test deployment
(`nginx:alpine`) Running.

> Note: the original build used the interim `k8s-ct` template and ran containerd
> 1.6.19 (charm default). The current `k8s-test` was recreated from the **golden**
> template on 2026-08-28 and runs **containerd 1.6.20** with the same kube/flannel
> stack — version numbers above are historical.

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

# ⚠ config change re-renders unit files → re-apply the v2.0→v3 fix afterwards.
#   (Historical: the pre-TLS haproxy-backend fix was also re-rendered; it is obsolete
#   now that the certificates relation puts magnum in the TLS topology. Canonical
#   block: ../OpenStack/magnum-fixes-and-maintenance.md)
```

### 1.3 Vault CA injection (now handled by the `vault:certificates` relation)

The in-guest `heat-container-agent` (podman) failed TLS to keystone because the
cluster had no Vault root CA. **Root cause: magnum was deployed without relating
its `certificates` endpoint to vault**, so `[drivers] openstack_ca_file` stayed
unset and the Heat `openstack_ca` parameter — written to each node's user-data as
`/etc/pki/ca-trust/source/anchors/openstack-ca.pem` — was empty.

The interim workaround was baking the CA into a custom FCOS image. **That is no
longer needed**: with `juju integrate vault:certificates magnum:certificates` the
charm installs the CA and magnum injects it into every new cluster at boot, so a
**stock** Fedora CoreOS image works (verified 2026-09-10 — a stock-image cluster
reached `CREATE_COMPLETE / HEALTHY`). See
[`magnum-deployment-guide.md`](magnum-deployment-guide.md) → Certificates.

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

1. `quay.io/coreos/flannel-cni:v0.3.0` → **401** (repo gone from quay). The fragment
   is label-driven (`flannel_cni_tag`, with prefix `container_infra_prefix` or
   `quay.io/coreos/`), but no reachable image matches that name/layout today (see
   [`../OpenStack/magnum-fixes-and-maintenance.md`](../OpenStack/magnum-fixes-and-maintenance.md)
   §2), so the fragment was patched in place. The first iteration used
   `busybox:1.36` + `wget` of the standard plugins — which exposed issue 3 — and the
   final patch uses the rancher mirror image plus `cp /flannel`.
2. containerd looked for plugins in `/usr/libexec/cni/` (its config.toml) while
   magnum installs to `/opt/cni/bin`; a bind mount alone is not enough — containerd
   caches the dir at startup (must restart containerd **and** kubelet).
3. The standard plugins bundle lacks the `flannel` binary itself — it ships in the
   flannel-cni-plugin image, so the init container copies `/flannel` from the rancher
   mirror to `/opt/cni/bin/flannel`.

The patch has three recognised states (PRISTINE / BUSYBOX / FINAL) and the current
[`fix-flannel-final.py`](fix-flannel-final.py) handles all of them idempotently.

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
no `container_infra` proxy location. Regenerate it on **every** Skyline unit (the
config is per-unit; the leader-only action leaves the others stale) and hard-refresh
the browser:

```bash
for u in skyline/72 skyline/73 skyline/74; do juju run "$u" regenerate-nginx; done
# (adjust unit numbers to match `juju status skyline`)
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
| `kisroot-ca.crt` | Vault root CA — working copy at `~/magnum-work/kisroot-ca.crt`; retained anchor `~/snap/openstackclients/common/` (from the initial cloud deploy, Apr) |
