# Magnum Fixes & Maintenance

The quirks that **recur on a fresh deployment** of the Bobcat-era magnum charm, the
canonical steps to fix them, and what survives what. The golden template +
`flannel-service.sh` patch eliminate the node-level fixes for new clusters; the
remaining unit-level fixes are small and well understood.

> For reboot/outage behaviour, see [`disaster-recovery.md`](disaster-recovery.md).

---

## 1. Template/unit-level fixes (magnum/0 unit)

| # | Issue | Trigger | Fix | Re-triggered by |
|---|---|---|---|---|
| 1 | Missing `vault:certificates` relation → no CA installed; keystone TLS fails (503) and clusters boot without the OpenStack CA | deploy omission | `juju integrate vault:certificates magnum:certificates` | never (relation persists) |
| 2 | Keystone auth uses `v2.0` paths | deploy (charm default) | see canonical block below | charm refresh, **reboot** |
| 3 | `cluster-user-trust` defaults to false | deploy | `juju config magnum cluster-user-trust=true` | not re-triggered once set |
| 4 | `heat_stack_user` role missing | first cluster ever (one-time) | `openstack role create heat_stack_user` | never (additive) |
| 5 | All mysql-routers stale → read-only DB errors (500) on any write | deploy / DB failover / **reboot** | restart every `*-mysql-router.service` | DB primary change, **reboot** (see DR doc) |

### Canonical fix block (issue 2)

```bash
juju exec -m kis --unit magnum/0 -- sudo sed -i s/v2.0/v3/g /etc/magnum/magnum.conf
juju exec -m kis --unit magnum/0 -- sudo systemctl restart magnum-api magnum-conductor
```

`cluster-user-trust` (issue 3) does not revert once set; the v2.0→v3 editor does not
revert either. (The old pre-TLS haproxy-backend bug is resolved by the TLS topology
the certificates relation enables — see [`disaster-recovery.md`](disaster-recovery.md).)

---

## 2. Node-level fixes (ALL eliminated by the golden template + flannel patch)

| # | Issue | Status | How eliminated |
|---|---|---|---|
| 6 | Kubelet legacy `--network-plugin`, `--cni-*` flags | **ELIMINATED** | `container_runtime=containerd` in golden template |
| 7 | Kubelet missing `--container-runtime-endpoint` | **ELIMINATED** | `container_runtime=containerd` in golden template |
| 8 | Standalone containerd not running | **ELIMINATED** | `container_runtime=containerd` in golden template |
| 9 | kubelet restart after all patches | **ELIMINATED** | no patches needed |
| 10 | containerd `bin_dir` wrong | **ELIMINATED** | `container_runtime=containerd` in golden template |
| 11 | Flannel CNI plugins dead quay tag | **ELIMINATED** | `install-cni-plugins` uses rancher image + wget |
| 12 | Missing `flannel` binary in `/opt/cni/bin/` | **ELIMINATED** | `install-cni-plugins` copies `/flannel` from rancher image |

The `flannel-service.sh` patch lives on the magnum unit (template fragment
`.../magnum/drivers/common/templates/kubernetes/fragments/flannel-service.sh`).
Re-apply after `juju refresh`/`juju upgrade-charm` with:

```bash
juju ssh magnum/0 -- python3 fix-flannel-final.py   # script in Magnum/ folder
```

> The driver source path lives in the magnum package at
> `/usr/lib/python3/dist-packages/magnum/`.

### What survives what

| Fix | Survives reboot | Survives node replace | Survives `juju refresh` |
|---|---|---|---|
| Golden template labels | N/A (template) | N/A | yes (template) |
| Flannel init container (rancher + wget) | ✓ (etcd) | ✓ (re-scheduled) | **re-patch** |
| Flannel binary on nodes | ✓ (init container re-runs) | ✓ (init container re-runs) | ✓ (if template patched) |
| `cluster-user-trust=true` (juju config) | ✓ | ✓ | ✓ |
| `heat_stack_user` role | ✓ | ✓ | ✓ |
| Vault CA via `vault:certificates` relation | ✓ | ✓ | ✓ |
| Golden cluster template (magnum DB) | ✓ | ✓ | ✓ |

---

## 3. Remaining manual work after `juju refresh`

```bash
# 1. re-patch flannel-service.sh on magnum/0 (unless already patched via action)
juju ssh magnum/0 -- python3 fix-flannel-final.py

# 2. re-apply the canonical v2.0→v3 fix block (§1)
# 3. verify cluster-user-trust persisted:
juju config magnum | grep cluster-user-trust   # expect: cluster-user-trust: "true"
```

---

## 4. Skyline Container Infra fixes (two upstream bugs)

**Fix A — nginx config missing `container_infra` proxy location.** Skyline's nginx
is generated from the keystone catalog at charm render time. After deploying any new
service, regenerate:

```bash
juju run skyline/leader regenerate-nginx
# hard-refresh the browser after
```

**Fix B — upstream skyline-console JS bug (Create Cluster page error).**
`checkVolumeQuota()` destructures `cinderQuota` without a fallback; when Cinder is
absent the destructuring `{left:l=0}=r` throws (surfaces as *"Error, Unable to get
Data"*):

```javascript
// Before (broken):  {left:l=0}=r;
// After (fixed):    {left:l=0}=r||{};
```

Apply to all occurrences in the minified JS on each Skyline unit, then remove stale
`.gz` companions so nginx serves the patched files.

---

## 5. Upgrading the Fedora CoreOS image (security updates)

New clusters can boot a newer FCOS image; existing clusters are **not affected** —
only new clusters use the updated template. No CA baking is involved (the CA comes
from the `vault:certificates` relation and is injected via Heat at boot).

```bash
# 1. download the new FCOS image (check https://builds.coreos.fedoraproject.org/)
wget https://builds.coreos.fedoraproject.org/prod/streams/stable/builds/39.XXXXX.X.X/x86_64/fedora-coreos-39.XXXXX.X.X-openstack.x86_64.qcow2.xz
unxz -T0 fedora-coreos-39.XXXXX.X.X-openstack.x86_64.qcow2.xz

# 2. upload it to Glance
openstack image create fedora-coreos-39.XXXXX.X.X \
  --file fedora-coreos-39.XXXXX.X.X-openstack.x86_64.qcow2 \
  --disk-format qcow2 --container-format bare \
  --property os_distro=fedora-coreos

# 3. point the golden template at it (magnumclient uses JSON-patch ops)
openstack coe cluster template update k8s-ct-golden replace /image_id=<new-image-id>

# 4. verify
openstack coe cluster template show k8s-ct-golden -f value -c image_id
```

**What stays the same:** all cluster template labels, the flannel patch, the golden
template itself, the haproxy/keystone config. **What to verify after:** the new FCOS
works with k8s 1.26 (kernel/ignition compatibility), the containerd version
interacts correctly with `containerd_version=1.6.20`, `heat-container-agent` boots;
create a test cluster before making it the default.

> Existing clusters keep their original image and continue running.

---

## 6. Lessons learned

1. **Bobcat magnum + k8s 1.26 is a broken combination** — the Heat templates
   predate the 1.24 kubelet flag removal and don't configure CRI. `kube_tag`
   controls the k8s version but the templates assume pre-1.24 kubelet regardless.
2. **A missing `vault:certificates` relation silently breaks keystone TLS** — the
   charm installs no CA on the unit and `[drivers] openstack_ca_file` stays unset, so
   clusters boot with an empty `openstack_ca` and the in-guest agent fails TLS to
   keystone. Relate vault from the start; never hand-copy the CA or bake it into an
   image.
3. **`containerd` caches CNI config at startup** — changing `bin_dir` requires a full
   restart, not just file replacement.
4. **The standard CNI plugins tarball does NOT include `flannel`** — it's a separate
   binary shipped in `flannel-cni-plugin` images. The rancher-mirror init container
   copies `/flannel` and downloads the standard plugins in one step.
5. **Juju config changes re-render unit files** — always re-check `haproxy.cfg` and
   `magnum.conf` after `juju config` / `juju integrate` on the magnum app.
6. **Skyline nginx must be regenerated** after deploying any new service
   (catalog-based proxy routes are baked at charm render time).
7. **When patching minified JS, remove `.gz` companions** — nginx serves gzip
   versions when available, bypassing edits to the uncompressed `.js`.
8. **Skyline frontend silently swallows errors** — the generic "Unable to get Data"
   wraps any JS exception; check DevTools for the real TypeError.
9. **The charm-era `containerd_version` default is too old for k8s ≥1.26** — k8s 1.26
   requires CRI v1 (containerd ≥1.6.x); always set it via template label.
10. **Upstream template defaults are intentional** — `container_runtime=host-docker`
    and the pre-1.6 containerd default target pre-1.24. For k8s ≥1.24 you MUST override
    via template labels.
11. **Flannel image `quay.io/coreos/flannel-cni:v0.3.0` is gone** — patch
    `flannel-service.sh` to the rancher mirror (survives everything except
    `juju refresh`).

**Permanent-fix recommendations (not done, noted for future work):**

* Upstream the flannel image fix to `charm-magnum` so `install-cni-plugins` doesn't
  need re-patching after refresh.
* Upstream the haproxy backend fix (LP #1943385 / #2058474), or switch to direct
  bind by setting `[api] host = 0.0.0.0`.
* Add a charm action (e.g. `apply-template-fixes`) that re-applies all three fixes
  after `juju refresh`: `juju run magnum/0 apply-template-fixes`.