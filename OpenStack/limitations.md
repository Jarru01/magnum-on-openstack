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

Runs reliably, but no upstream security fixes. Upgrading requires the manual FCOS
re-bake (`magnum-fixes-and-maintenance.md` §5) plus a new `kube_tag` /
`containerd_version` in the golden template — plan this periodically.

## 3. No persistent storage (k8s PVCs unavailable)

No Cinder/Swift is deployed and the template has no `volume_driver=cinder`, so the
Cinder CSI driver is not installed. **Kubernetes PVCs have no storage class/provider.**
Workloads can only use `emptyDir`/`hostPath`. Do not promise PV-backed storage to
users.

## 4. `type: LoadBalancer` services may fail

`openstack-cloud-controller-manager` runs and would provision Octavia LBs, but a
pre-existing Octavia LB can be stuck in `ERROR`. LBaaS-backed services are possible
but should be tested per-cluster before relying on them (relevant to the dashboard
Option 2a — see `Kubernetes/kubernetes-dashboard.md`).

---

## Not deployed (context)

* **Cinder / Swift** — no block/object storage; drives #3 above.
* **Designate zones** — service idle; the cluster's `discovery_url` uses the public
  etcd discovery service instead.