# Known Limitations & Future Risks

These do **not** block current functionality but will surface over time. Tracked
here so future operators know what to watch.

## 1. External internet dependency for new cluster creation

New cluster bootstrap depends on internet egress:

* `https://discovery.etcd.io/<id>` (CoreOS public discovery service) — used to form
  etcd during creation only
* Pulling k8s binaries, the flannel rancher image (`docker.io/...`), and CNI plugins

**Impact:** if the cloud loses internet, **new** cluster creation fails. Existing
clusters are unaffected (images cached, etcd bootstrapped). Scheduling pods with
*new* images also needs registry access.

## 2. Long-term stack is old (no security patches)

* Kubernetes **1.26** (EOL Feb 2024)
* containerd **1.6.20** (EOL)
* Fedora CoreOS **38.20230806.3.0** (Aug 2023)

Runs reliably, but no upstream security fixes.

**Version ceilings (2026-10):** the `k8s_fedora_coreos_v1` heat driver in this
Magnum release is Yoga-era and caps Kubernetes at **1.26** — k8s 1.27 removed
`--container-runtime`, which the templates pass unconditionally, and the
`kubelet_options` label can only append options, not remove them. The driver is
also deprecated and removed in newer Magnum releases, so no upstream fixes will
arrive. A newer FCOS image (44.20260913.3.2 was tested end-to-end through master
bootstrap) boots only with three package shims — the CA bundle path, containerd
tarball extraction through the `/usr/local` symlink, and curl's default CAfile —
which we deliberately chose **not** to maintain; FCOS stays 38. Treat this stack
as frozen: real modernization means a newer Magnum release + CAPI driver (or
different tooling), not an FCOS bump.

## 3. Persistent storage (PVCs) — cloud-dependent

* **Reference cloud:** no Cinder/Swift is deployed and the template has no
  `volume_driver=cinder`, so the Cinder CSI driver is not installed. **Kubernetes
  PVCs have no storage class/provider.** Workloads can only use `emptyDir`/`hostPath`.
  Do not promise PV-backed storage to users here.
* **Clouds with Cinder v3:** add `--volume-driver cinder` to the cluster template
  (plus a StorageClass **per cluster** — Magnum ships none). **Verified 2026-10** on
  a Cinder-capable cloud: PVC `Bound` → Pod mounted → volume attached in Cinder.
  Procedure: `../Kubernetes/k8s-cluster-usage.md` → Persistent storage.

## 4. `type: LoadBalancer` services (verified working)

`openstack-cloud-controller-manager` provisions Octavia LBs: a `type: LoadBalancer`
Service gets an `ACTIVE`/`ONLINE` amphora LB with a floating IP. **Verified
2026-09-10** on a fresh cluster (provisioned → served HTTP 200 → deleted cleanly with
the Service). Reproducible procedure + architecture:

See `../Kubernetes/k8s-cluster-usage.md` → External `LoadBalancer` services.

The **master LB** created by `--master-lb-enabled` (`api_lb` on 6443 with a
floating IP + internal `etcd_lb` on 2379) is also verified — 2026-10-04 on a
3-master cluster, all pool members `ONLINE`.

**Cloud prerequisite:** this needs Octavia in the catalog. On clouds without Octavia,
`LoadBalancer` Services stay `<pending>` (see `cloud-prerequisites.md` §3).

## 5. etcd durability (no backup procedure provided)

etcd holds all cluster state. In this deployment it runs on the **master's local
disk** by default; on Cinder-capable clouds the `etcd_volume_size` cluster label can
put it on a Cinder volume (survives a master VM/hypervisor loss, but the volume is
**deleted with the cluster**, and the label is create-time only — enabling it means
recreating the cluster). Recovery is manual: the Cinder volume survives an in-place
master VM rebuild (Heat keeps the volume and reattaches it), but there is no
automatic master repair — if the master is lost, the control plane stays down until
the VM is rebuilt/recovered by hand. Neither option protects against logical
corruption or mistakes, and **no etcd snapshot/backup procedure is documented yet** —
treat this as the main durability gap for long-lived clusters.

Availability note: use **1 or 3 masters, never 2** (quorum). With 3 masters etcd is
replicated and tolerates one failure; the Cinder etcd volume then becomes optional
rather than required. Multi-master additionally requires Octavia (Magnum mandates a
master LB for `master_count > 1`) — see `cloud-prerequisites.md` §3.

## 6. CSI image registry drift (future risk)

Cinder CSI works today (verified 2026-10), but its images are aging:
`k8scloudprovider/cinder-csi-plugin:v1.23.0` from Docker Hub and the sidecars
(`csi-attacher`, `csi-provisioner`, …) from **`k8s.gcr.io`**, which is deprecated
and only redirects to `registry.k8s.io`. If that redirect or the old tags
disappear, **new** cluster builds (and nodes rebuilt from scratch) would fail to
pull the CSI images; running clusters keep working until then.

Fix if it ever breaks: bump the CSI labels (`cinder_csi_plugin_tag`,
`csi_attacher_tag`, `csi_provisioner_tag`, …) and/or mirror the images to your own
registry and set `container_infra_prefix` in the cluster template, then recreate
the cluster — see `magnum-fixes-and-maintenance.md` §6 (lesson 12).

---

## Not deployed (reference cloud — context)

* **Cinder / Swift** — no block/object storage; drives #3 above.
* **Designate zones** — service idle; the cluster's `discovery_url` uses the public
  etcd discovery service instead.
