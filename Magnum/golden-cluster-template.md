# Golden Cluster Template & Onboarding

The **golden template** is a cloud-wide (public) cluster template that bakes in every
known fix, so users can create working Kubernetes clusters with zero SSH and zero
manual node surgery. This document is the canonical reference for the template, for
onboarding new projects/users, and for the permission model behind kubeconfig access.

> Companion docs: [`first-cluster-build-log.md`](first-cluster-build-log.md) explains
> *why* each label exists; [`../Kubernetes/k8s-cluster-usage.md`](../Kubernetes/k8s-cluster-usage.md)
> covers using a finished cluster.

---

## 1. The golden template (canonical create command)

```bash
openstack coe cluster template create k8s-ct-golden \
  --image fedora-coreos-38.20230806.3.0-ca --external-network ext-net \
  --dns-nameserver 203.0.113.53 --keypair magnum-k8s \
  --master-flavor 2c2r20d --flavor 2c2r20d \
  --network-driver flannel --coe kubernetes \
  --labels kube_tag=v1.26.8-rancher1,container_runtime=containerd,containerd_version=1.6.20,containerd_tarball_sha256=1d86b534c7bba51b78a7eeb1b67dd2ac6c0edeb01c034cc5f590d5ccd824b416 \
  --public
```

`--public` makes it visible to **all projects**. Drop it for project-scoped use.

### Key labels explained

| Label | Value | Why |
|---|---|---|
| `kube_tag` | `v1.26.8-rancher1` | Controls the k8s version |
| `container_runtime` | `containerd` | Bypasses the `host-docker` default (pre-1.24 only) |
| `containerd_version` | `1.6.20` | Required for k8s 1.26 (CRI v1 support; the default 1.4.4 is too old) |
| `containerd_tarball_sha256` | `1d86b534…d824b416` | Integrity check for the containerd tarball |

The flannel CNI fix lives in the `flannel-service.sh` template fragment on the
magnum unit; see [`magnum-fixes-and-maintenance.md`](../OpenStack/magnum-fixes-and-maintenance.md)
and [`fix-flannel-final.py`](fix-flannel-final.py).

### Minimum flavors

Master and worker must be **`2c2r20d` or larger**. A `1c1r10d` master fails
mid-deploy: the kube-apiserver's resource-quota evaluator times out under memory
pressure (etcd + apiserver + scheduler crammed onto 1024 MB). Failure signature:

```
kubectl apply --validate=false -f /srv/magnum/kubernetes/kubernetes-dashboard.yaml
Error from server (InternalError): ... Internal error occurred: resource quota evaluation timed out
status_reason: deploy_status_code : Deployment exited with non-zero status code: 1
```

---

## 2. New project/user onboarding checklist

| Step | Command | Notes |
|---|---|---|
| 1. Create project | `openstack project create <project>` | One-time |
| 2. Create user | `openstack user create --project <project> --domain admin_domain <user>` | One-time |
| 3. Add roles | `openstack role add --project <project> --user <user> member` **and** `openstack role add --project <project> --user <user> load-balancer_member` | Both **required**: `member` for the cluster itself, `load-balancer_member` so cluster **delete** works (Octavia pre-delete 403 otherwise — see §4). Add `admin` too if they must download kubeconfig for clusters **they didn't create** (§3) |
| 4. Create keypair | `openstack keypair create --user <user> --domain <domain> <keypair-name>` | Per-user; keypairs are user-scoped |
| 5. Source credentials | `source <project>-openrc.sh` | Must match the project |
| 6. Create cluster | `openstack coe cluster create <name> --cluster-template k8s-ct-golden --master-count 1 --node-count 1` | Template must be `--public` or in the same project |
| 7. Get kubeconfig | `openstack coe cluster config <name> --dir ~/kubeconfig` | Or via Skyline: cluster detail → **Download kubeconfig** |
| 8. Use cluster | `export KUBECONFIG=~/kubeconfig/config; kubectl get nodes` | Minimum: install `kubectl`, have the master FIP routable |

**Key gotcha:** keypairs are **per-USER** in this Nova version, not per-project.
Each user must create their own keypair and their cluster template must point at it
(`--keypair <their-keypair>`).

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