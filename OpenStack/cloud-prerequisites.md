# Cloud Prerequisites (what Magnum clusters need from the cloud)

Magnum can only build and operate Kubernetes clusters when the cloud underneath
provides a small set of things. This document is a portable checklist: what each
requirement is for, what breaks when it is missing, and how to verify it. It applies
to any target cloud — the reference cloud here, or a fresh deployment elsewhere.

> Companion docs: [`architecture-overview.md`](architecture-overview.md) (how the
> build flow works), [`limitations.md`](limitations.md) (what the reference cloud
> does and does not provide), [`../Magnum/golden-cluster-template.md`](../Magnum/golden-cluster-template.md)
> (template options per cloud capability).

---

## 1. Node → API reachability (Keystone + Heat)

Every cluster node runs `heat-container-agent`, whose `os-collect-config` does:

1. authenticate to **Keystone** using the `auth_url` baked into the node at stack
   creation (stored in `/var/lib/cloud/data/cfn-init-data`; Magnum fills it from the
   catalog's **public** identity endpoint via `trustee_keystone_interface`, default
   `public`), then
2. resolve the **Heat** service from the catalog with `endpoint_type: publicURL`
   (hardcoded in `os_collect_config/heat.py`) and poll the stack resource metadata
   for its deployment configs.

Magnum itself also talks to Heat via the catalog `publicURL`
(`[heat_client] endpoint_type` default `publicURL`).

**Consequence:** the cloud's **public** Keystone and Heat endpoints must be reachable
from the tenant/cluster networks. If they only exist on a management network, that
network (or the specific API addresses/ports) must be routed/firewalled to the
tenant networks.

Typical API ports:

| Service | Service type | Port |
|---|---|---|
| Keystone | `identity` | 5000 (v3) |
| Heat (orchestration) | `orchestration` | 8000 or 8004, depending on registration |
| Heat (CloudFormation) | `cloudformation` | the other of 8000/8004 |

Publish **both** Heat ports — which one the agent uses depends on the Heat template
and the `heat-container-agent` image version.

### Verify

From a tenant VM (the cluster master works fine), TCP must succeed — the HTTP status
does not matter:

```bash
curl -m 5 -sv https://<keystone-public>:5000/v3
curl -m 5 -sv http://<heat-public>:8004/v1
curl -m 5 -sv http://<heat-public>:8000/v1
```

Do **not** use `ping` to test this: firewalls and port-forwarding commonly drop ICMP
while allowing the TCP API ports, so ICMP failures say nothing. Also mind the scheme:
speaking plain HTTP to a TLS port (or vice versa) returns a misleading but harmless
error — it still proves TCP reachability.

## 2. Internet egress from cluster nodes

New nodes download during bootstrap (existing clusters keep working without it):

* the `heat-container-agent` container image (`docker.io/openstackmagnum/...`)
* Kubernetes/containerd binaries, flannel + CNI images, CSI sidecar images
* `https://discovery.etcd.io/<id>` for etcd formation (when discovery is used)

**Verify:** from a tenant VM, `curl -m 5 -sv https://registry-1.docker.io/v2/`
(HTTP 401 is the expected success response).

**Symptom when missing:** `heat-container-agent.service` fails and no container is
created — the journal shows `pinging container registry registry-1.docker.io: i/o
timeout`. The unit has **no `Restart=`**, so it stays failed even after egress is
restored: restart it manually (`sudo systemctl restart heat-container-agent`) or
delete and recreate the cluster.

## 3. Optional services (per capability)

| Service | Unlocks | If missing |
|---|---|---|
| **Cinder v3** | Persistent volumes (PVCs) via the Cinder CSI driver, optional per-node container volumes, optional etcd volumes | No PVCs. Do **not** set `--volume-driver cinder` — the CSI driver would deploy but cannot work. See [`../Kubernetes/k8s-cluster-usage.md`](../Kubernetes/k8s-cluster-usage.md) → Persistent storage |
| **Octavia** | `type: LoadBalancer` Services (OCCM) **and multi-master clusters** (Magnum requires a master LB when `master_count > 1`) | `LoadBalancer` Services stay `<pending>` (OCCM logs `Claiming to support LoadBalancer` but has no endpoint to use), and `master_count > 1` creates are rejected: `master_count must be 1 when master_lb_enabled is False` |
| **Barbican** | OCCM secret features (e.g. LB TLS secrets); Magnum's default `cert_manager_type=barbican` | OCCM logs `Failed to create an OpenStack Secret client ... No suitable endpoint` — benign unless those features are needed. On clouds without Barbican, deploy Magnum with `cert_manager_type=x509keypair` |

Cinder CSI additionally needs a StorageClass **per cluster** — Magnum ships none (see
[`../Kubernetes/k8s-cluster-usage.md`](../Kubernetes/k8s-cluster-usage.md)).

## 4. Endpoint exposure notes

* "Public" in the catalog only means "the interface clients are expected to use".
  Whether it is reachable from tenant VMs is a cloud-topology question (routing,
  NAT, firewall, or a public VIP). The build depends on it (§1).
* Prefer exposing only the required API addresses/ports to tenant networks rather
  than the whole management network.
* Verified 2026-09 on a second OpenStack cloud (Cinder present; no Octavia/Barbican):
  after the public Keystone + Heat endpoints were made reachable from the cluster
  network, clusters built and reached `CREATE_COMPLETE / HEALTHY`.

## 5. Symptom → cause table

| Symptom | Likely cause | Check |
|---|---|---|
| Cluster stuck at `master_config_deployment`; agent logs `Source [heat] Unavailable` | Public Heat endpoint(s) not reachable from the tenant VM network | §1 curls from the master |
| Agent fails to authenticate (401 / TLS error) | Public Keystone not reachable, or the node does not trust the API CA | `curl https://<keystone-public>:5000/v3` from the master; CA is injected by the `vault:certificates` relation |
| `heat-container-agent.service` failed, no container | Image-pull egress missing (or an earlier pull failed — no auto-restart) | §2 curl; `journalctl -u heat-container-agent` |
| `LoadBalancer` Service stays `<pending>` | No Octavia in the catalog | `openstack endpoint list --service octavia` |
| PVC stays `Pending` | Cluster built without `--volume-driver cinder`, or no StorageClass exists | `kubectl get csidrivers`; `kubectl get sc` |
| Cluster create fails with `401 Unauthorized` while validating a nested resource (e.g. a network) | Transient trust/token rejection (Keystone restart or key rotation mid-create), or session/project mismatch | delete + recreate with a fresh session; if it repeats, check `heat-engine` logs and whether Keystone was restarted/rotated keys |
| Nodes `NotReady` after bootstrap | CNI/flannel images or plugin binaries | [`../OpenStack/magnum-fixes-and-maintenance.md`](magnum-fixes-and-maintenance.md) §2 |

## 6. Quick verification checklist

Run from the cloud (OpenStack CLI):

```bash
openstack endpoint list --interface public
openstack endpoint list --service octavia       # for LoadBalancer Services
openstack volume service list                   # for PVCs / etcd volumes
```

Run from a tenant VM (the cluster master works):

```bash
curl -m 5 -sv https://<keystone-public>:5000/v3
curl -m 5 -sv http://<heat-public>:8004/v1
curl -m 5 -sv https://registry-1.docker.io/v2/
```
