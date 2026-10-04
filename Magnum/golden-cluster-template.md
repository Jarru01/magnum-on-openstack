# Golden Cluster Template & Onboarding

The **golden template** is a cloud-wide (public) cluster template that bakes in every
known fix, so users can create working Kubernetes clusters with zero SSH and zero
manual node surgery. This document is the canonical reference for the template, for
onboarding new projects/users, and for the permission model behind kubeconfig access.

> Companion docs: [`first-cluster-build-log.md`](first-cluster-build-log.md) explains
> *why* each label exists; [`../Kubernetes/k8s-cluster-usage.md`](../Kubernetes/k8s-cluster-usage.md)
> covers using a finished cluster; [`../OpenStack/cloud-prerequisites.md`](../OpenStack/cloud-prerequisites.md)
> lists what the cloud itself must provide.

---

## 1. The golden template (canonical create command)

### Cloud without Cinder (reference cloud)

```bash
openstack coe cluster template create k8s-ct-golden \
  --image <fcos-image> --external-network <external-network> \
  --dns-nameserver <dns-resolver> --keypair <keypair> \
  --master-flavor <flavor> --flavor <flavor> \
  --network-driver flannel --coe kubernetes \
  --labels kube_tag=v1.26.8-rancher1,container_runtime=containerd,containerd_version=1.6.20,containerd_tarball_sha256=1d86b534c7bba51b78a7eeb1b67dd2ac6c0edeb01c034cc5f590d5ccd824b416 \
  --public
```

### Cloud with Cinder (persistent volumes)

Add `--volume-driver cinder` to deploy the Cinder CSI driver, and optionally
`etcd_volume_size` to put etcd on a Cinder volume:

```bash
openstack coe cluster template create k8s-ct-golden-cinder \
  --image <fcos-image> --external-network <external-network> \
  --dns-nameserver <dns-resolver> --keypair <keypair> \
  --master-flavor <flavor> --flavor <flavor> \
  --network-driver flannel --coe kubernetes \
  --volume-driver cinder \
  --labels kube_tag=v1.26.8-rancher1,container_runtime=containerd,containerd_version=1.6.20,containerd_tarball_sha256=1d86b534c7bba51b78a7eeb1b67dd2ac6c0edeb01c034cc5f590d5ccd824b416,etcd_volume_size=10 \
  --public
```

Notes:

* Only use `--volume-driver cinder` on a cloud that actually has the Cinder v3 API —
  otherwise the CSI driver deploys but cannot work. CSI additionally needs a
  **StorageClass per cluster** (Magnum ships none) — see
  [`../Kubernetes/k8s-cluster-usage.md`](../Kubernetes/k8s-cluster-usage.md) →
  Persistent storage.
* `etcd_volume_size` is optional; 5–10 GB is plenty for etcd.

`--public` makes it visible to **all projects**. Drop it for project-scoped use.

### Replace the placeholders

| Placeholder | Replace with | Reference cloud |
|---|---|---|
| `<fcos-image>` | Fedora CoreOS image in Glance (**stock** — no CA bake needed; the certificates relation injects the CA at boot) | `fedora-coreos-38.20230806.3.0` |
| `<external-network>` | the cloud's external (public) network name | `ext-net-154` |
| `<dns-resolver>` | DNS resolver reachable from the cluster nodes | the DNS your cloud's instances use |
| `<keypair>` | keypair **owned by the account that creates the cluster** (keypairs are per-user) | `magnum-k8s` |
| `<flavor>` | master and worker flavor, `2c2r20d`-or-larger | `2c2r20d` |

> Keep the `--labels` value exactly as shown — those labels are the fixes that make
> k8s 1.26 work on this driver.

### Key labels explained

| Label | Value | Why |
|---|---|---|
| `kube_tag` | `v1.26.8-rancher1` | Controls the k8s version |
| `container_runtime` | `containerd` | Bypasses the `host-docker` default (pre-1.24 only) |
| `containerd_version` | `1.6.20` | Required for k8s 1.26 (CRI v1 support; the charm-era default is too old — always set it via template label) |
| `containerd_tarball_sha256` | `1d86b534…d824b416` | Integrity check for the containerd tarball |

The flannel CNI fix lives in the `flannel-service.sh` template fragment on the
magnum unit; see [`magnum-fixes-and-maintenance.md`](../OpenStack/magnum-fixes-and-maintenance.md)
and [`fix-flannel-final.py`](fix-flannel-final.py).

### Storage & capacity options

| Option | Kind | What it does | Notes |
|---|---|---|---|
| `--volume-driver cinder` | template field | Deploys the Cinder CSI driver (`cinder.csi.openstack.org`); gated by `cinder_csi_enabled` (label, default `true`) | Required for PVCs; needs Cinder v3 + a per-cluster StorageClass. Omit on clouds without Cinder |
| `cinder_csi_enabled` | label | Gate for the CSI driver | Only effective together with `volume_driver=cinder` |
| `etcd_volume_size` / `etcd_volume_type` | cluster labels | Cinder volume per master for etcd, mounted at `/var/lib/etcd` | Create-time only (recreate to change); the volume is **deleted with the cluster** → backups still required |
| `--docker-volume-size` | template field | Cinder volume per node for container/image storage | Local disk is used when unset |
| `boot_volume_size` | label | Boot node root from a Cinder volume | Default `0` = image-backed (ephemeral) root |

Node root disks are **ephemeral by design** (nodes are rebuilt from the image +
Heat config); only PVCs and — optionally — the etcd volume provide durability. Full
storage model: [`../OpenStack/architecture-overview.md`](../OpenStack/architecture-overview.md)
→ Storage model.

### Minimum flavors

Master and worker must be **`2c2r20d` or larger**. A `1c1r10d` master fails
mid-deploy: the kube-apiserver's resource-quota evaluator times out under memory
pressure (etcd + apiserver + scheduler crammed onto 1024 MB). Failure signature:

```
kubectl apply --validate=false -f /srv/magnum/kubernetes/kubernetes-dashboard.yaml
Error from server (InternalError): ... Internal error occurred: resource quota evaluation timed out
status_reason: deploy_status_code : Deployment exited with non-zero status code: 1
```

### HA masters

Use **1 or 3 masters, never 2** (quorum). Magnum **requires `--master-lb-enabled`
when `master_count > 1`**, and the master LB needs **Octavia** in the cloud — without
it the create is rejected (`master_count must be 1 when master_lb_enabled is False`),
and the legacy neutron-lbaas path is gone from modern OpenStack. Multi-master
checklist:

* Octavia present (`openstack endpoint list --service octavia`) and with capacity.
* `--master-count 3 --master-lb-enabled` at cluster create (master count is
  immutable afterwards; node count can be scaled later).
* Allow a longer build: `--timeout 90`.
* The kubeconfig/API endpoint becomes the LB's floating IP.
* With `etcd_volume_size`, **each master gets its own etcd volume** (3× the storage;
  volumes are deleted with the cluster — backups still required).

**Verified 2026-10-04:** a 3-master / 2-worker cluster built with
`--master-lb-enabled` produced `api_lb` (VIP + FIP, listener 6443) and `etcd_lb`
(internal VIP, listener 2379), both `ACTIVE`/`ONLINE`, with all 3 masters `ONLINE`
as pool members; `curl -k https://<lb-fip>:6443/healthz` returned `ok`, and etcd
formed 3 members. Details + commands:
`../Kubernetes/k8s-cluster-usage.md` → Master API/etcd load balancer.

### Auto-healing & auto-scaling (leave off)

Both are template flags, **off by default** — keep them off on this deployment:

* **Auto-scaling** (`auto_scaling_enabled`) deploys
  `openstackmagnum/cluster-autoscaler:v1.18.1` — a k8s-1.18-era autoscaler against
  this cloud's k8s 1.26 — and scales the `default-worker` nodegroup through the
  **Magnum API** (the Magnum public endpoint must be reachable from the cluster, plus
  trust credentials). Defaults are risky: `min_node_count=0` (it can scale workers
  down to **zero** under low load) and `max_node_count` falls back to `node_count + 1`.
* **Auto-healing** (`auto_healing_enabled`) with the default controller `draino`
  only cordons/drains unhealthy nodes — it does **not** repair/replace them, and the
  draino path additionally deploys the same old autoscaler. True replacement needs
  `auto_healing_controller=magnum-auto-healer`: a DaemonSet on the **masters** that
  deletes unhealthy nodes via the Magnum API — useful mainly with 3 masters, since a
  single master cannot heal itself.

**Observed 2026-10-04** (enabled on a 3-master cluster): `draino` deployed and ran
(workers get the `draino-enabled=true` label), and `cluster-autoscaler` reached the
Magnum API (it found the stack) — but it **crash-looped**:
`Could not parse node group spec 0:3:default-worker: invalid node group spec: min
size must be >= 1`. Cause: `min_node_count` was unset, so it defaults to **0**,
which the autoscaler's Magnum provider rejects. If you ever enable autoscaling,
set `min_node_count` to at least **1** (plus an explicit `max_node_count`) at
cluster create.

Neither is recommended in this repository. Use the manual path (watch → `kubectl
drain` → replace/scale via Magnum; mind the +1-node rule). If you must experiment,
use a disposable cluster with `min_node_count=1`, an explicit `max_node_count`, the
Magnum API allowed from the cluster network, and expect to bump `autoscaler_tag` /
`magnum_auto_healer_tag`.

---

## 2. New project/user onboarding checklist

| Step | Command | Notes |
|---|---|---|
| 1. Create project | `openstack project create <project>` | One-time |
| 2. Create user | `openstack user create --project <project> --domain admin_domain <user>` | One-time |
| 3. Add roles | `openstack role add --project <project> --user <user> member` **and** `openstack role add --project <project> --user <user> load-balancer_member` | Both **required**: `member` for the cluster itself, `load-balancer_member` so cluster **delete** works (Octavia pre-delete 403 otherwise — see §4). Add `admin` too if they must download kubeconfig for clusters **they didn't create** (§3) |
| 4. *Skip for the common case* | see note below | Golden clusters embed the operator's `magnum-k8s` keypair — users get **no SSH**. A personal keypair only matters if the operator provisions a per-user template copy |
| 5. Source credentials | `source <project>-openrc.sh` | Must match the project |
| 6. Create cluster | `openstack coe cluster create <name> --cluster-template k8s-ct-golden --master-count 1 --node-count 1` | Template must be `--public` or in the same project |
| 7. Get kubeconfig | `openstack coe cluster config <name> --dir ~/kubeconfig` | Or via Skyline: cluster detail → **Download kubeconfig** |
| 8. Use cluster | `export KUBECONFIG=~/kubeconfig/config; kubectl get nodes` | Minimum: install `kubectl`, have the master FIP routable |

**Key gotcha: keypairs are per-USER in this Nova version, not per-project.** The
public golden template carries the operator's `magnum-k8s` keypair, so clusters
created from it are **not SSH-able by the creating user** — kubeconfig/kubectl is
the access path. If a user needs node SSH, the operator must create a per-user
template copy (private or project-scoped) with `--keypair <their-keypair>`; a
plain `member` cannot fork a public template themselves. `magnum-k8s.pem` stays
operator-only in all cases.

> **Creating the keypair:** either flow works — the CLI (`openssl`/`ssh-keygen` +
> `openstack keypair create`, as in the build log) or Skyline's *Keypair → Create*
> (save the private key when it is shown; it is not retrievable later). The CLI keeps
> the private key local to your machine. Either way the keypair must be **owned by
> the account that creates the cluster**, and the template's `--keypair` must
> reference it.

### Cluster visibility vs. keypair scoping

| Resource | Scope | Who can see/use it |
|---|---|---|
| Cluster | **Project** | Any user in the project (view/list) |
| Cluster template | **Project** (or public) | Any user in the project (or all projects if `--public`) |
| Keypair | **User** | Only the user who created it |
| Kubeconfig (`cluster config`) | Project to view; **creator/admin to fetch** | Owner (creator) or any project `admin` — see §3 |
| Node SSH access | **User** (needs keypair) | Only the user who owns the keypair |

---

## 3. Who can download a kubeconfig (the 403 people stumble on)

`openstack coe cluster config` (CLI) and Skyline's **Download kubeconfig** both call
magnum's `GET /v1/certificates/<cluster>`, gated by magnum policy
(`/etc/magnum/policy.json`):

```
"admin_or_user":   "is_admin:True or user_id:%(user_id)s"
"cluster_user":    "user_id:%(trustee_user_id)s"
"certificate:get": "rule:admin_or_user or rule:cluster_user"
```

A project member who did **not** create the cluster gets this exact error:

```
Failed to fetch CA certificate: {"errors": [{"code": "client", "status": 403,
"title": "Policy doesn't allow certificate:get to be performed", ... }]}
```

This is **by design**: the kubeconfig ships a client cert `CN=admin, O=system:masters`
— full `cluster-admin` in k8s — so OpenStack restricts who can pull it.

### Options for granting kubeconfig access (pick one)

| # | Grant | Works for | Notes |
|---|---|---|---|
| **1. Creator/owner — recommended, least-privilege** | `member` role; the user **creates their own cluster** | only clusters that user created | The self-service model the golden template enables |
| **2. Project `admin` role** | `member` + `admin` in the project | **any** cluster in the project | Verified working (2026-08-29). Note: `admin` is full project-admin (also satisfies `cluster:delete_all_projects` / `clustertemplate:delete_all_projects`) — more than kubeconfig access. Fine for a trusted single-project cloud |
| **3. Relax magnum policy** | patch `certificate:get` in `/etc/magnum/policy.json`, e.g. to `rule:admin_or_owner` | any member of the project for any cluster in it | Lets any project member grab cluster-admin creds → only for trusted clouds. The charm **re-renders** `policy.json` on refresh/reboot, so the patch reverts |

### Practical model

- **Creator** can get kubeconfig for their own cluster ✅ and use kubectl ✅
- **Project admin** can get kubeconfig for any cluster in the project ✅
- **Other project members** can see/list clusters but get the 403 above on kubeconfig ❌
- **Nobody can SSH** into cluster nodes without a keypair ❌

> **Access level:** whoever fetches a kubeconfig — creator or admin — receives a
> `CN=admin, O=system:masters` certificate, i.e. full cluster-admin. There is no RBAC
> scoping in the download; if you need per-user k8s RBAC, create ServiceAccounts +
> Roles inside the cluster instead of sharing the kubeconfig.

---

## 4. Cluster create/delete failure modes

### Delete requires the Octavia role

Teardown runs magnum's `pre_delete_cluster` → `octavia.delete_loadbalancers`, which
lists the cluster's load balancers using the **user's** token. Octavia policy
(`/etc/octavia/policy.json`) allows that list only for admins or `load-balancer_member`
**and** `member`, both in the LB's owning project:

```
os_load-balancer_api:loadbalancer:get_all  →  rule:load-balancer:read
load-balancer:read  →  ... or rule:load-balancer:member_and_owner or rule:load-balancer:admin
load-balancer:member_and_owner  →  role:load-balancer_member AND rule:project-member
project-member  →  role:member AND project_id:%(project_id)s
```

A user with only `member` hits this at delete time (create goes through Heat, so
only teardown trips, leaving the cluster at `DELETE_FAILED`):

```
Failed to pre-delete resources for cluster <uuid>, error: Policy does not allow this request
to be performed. (HTTP 403)
```

Fix (verified 2026-08-29 on exactly this 403; the fixed user went on to **create**
and **delete** clusters end-to-end):

```bash
openstack role add --project <project> --user <user> load-balancer_member
```

`load-balancer_observer` is optional extra.

### Cleaning up a failed cluster

A cluster stuck at `DELETE_FAILED` must be deleted by a principal that has the
Octavia roles — add `load-balancer_member` to the owner first, or delete as the
identity holding `load-balancer_admin`.
