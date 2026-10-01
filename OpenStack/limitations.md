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

Runs reliably, but no upstream security fixes. Upgrading means uploading a newer
FCOS image and repointing the golden template (`magnum-fixes-and-maintenance.md`
§5) plus matching `kube_tag` / `containerd_version` labels — plan this
periodically.

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

**Cloud prerequisite:** this needs Octavia in the catalog. On clouds without Octavia,
`LoadBalancer` Services stay `<pending>` (see `cloud-prerequisites.md` §3).

## 5. etcd durability (no backup procedure provided)

etcd holds all cluster state. In this deployment it runs on the **master's local
disk** by default; on Cinder-capable clouds the `etcd_volume_size` cluster label can
put it on a Cinder volume (survives a master VM/hypervisor loss, but the volume is
**deleted with the cluster**, and the label is create-time only — enabling it means
recreating the cluster). Neither option protects against logical corruption or
mistakes, and **no etcd snapshot/backup procedure is documented yet** — treat this as
the main durability gap for long-lived clusters.

Availability note: use **1 or 3 masters, never 2** (quorum). With 3 masters etcd is
replicated and tolerates one failure; the Cinder etcd volume then becomes optional
rather than required. Multi-master additionally requires Octavia (Magnum mandates a
master LB for `master_count > 1`) — see `cloud-prerequisites.md` §3.

---

## Not deployed (reference cloud — context)

* **Cinder / Swift** — no block/object storage; drives #3 above.
* **Designate zones** — service idle; the cluster's `discovery_url` uses the public
  etcd discovery service instead.
