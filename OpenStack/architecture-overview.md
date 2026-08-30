# Architecture Overview

How Magnum creates a Kubernetes cluster on this cloud, and which components are
involved.

---

## How Magnum creates a k8s cluster

Magnum doesn't directly provision anything — it delegates to **Heat** (OpenStack's
orchestration engine), which creates the VMs, networking, and bootstraps k8s on each
node.

**The flow:**

```
openstack coe cluster create
  → Magnum reads your cluster-template params (image, flavor, network, labels)
  → template_def.py maps params → Heat template variables
  → Heat creates a stack (Nova VMs + Neutron networking)
  → Each VM boots FCOS, runs user_data (Ignition config)
  → heat-container-agent on each node configures kubelet, containerd, flannel
  → Nodes discover each other and form a k8s cluster
```

## Heat templates (on the magnum unit)

The templates are part of the magnum package at
`/usr/lib/python3/dist-packages/magnum/drivers/k8s_fedora_coreos_v1/`:

```
template_def.py              # Python: maps cluster-template params → Heat variables
templates/
├── kubecluster.yaml         # Top-level stack (orchestrates master + worker count)
├── kubemaster.yaml          # Master node: API server, scheduler, controller-manager
├── kubeminion.yaml          # Worker node: kubelet, kube-proxy
├── fcct-config.yaml         # Fedora CoreOS Ignition config (cloud-init equivalent)
└── user_data.json           # Writes /etc/sysconfig/heat-params on each node at boot
```

`user_data.json` is the bridge between Heat and the running node — it writes
`heat-params` (flavor, image, trust_id, network, etc.) to each VM, which the
`heat-container-agent` (podman container on the node) picks up to configure kubelet,
containerd, and flannel at runtime.

This is also where the `cluster_user_trust` bug lived: `template_def.py` only passed
`trust_id` to Heat when the config flag was set — otherwise it was blank.

## Infrastructure components

| Component | What it does |
|---|---|
| **MAAS** | Bare-metal provisioning — owns the physical machines everything runs on |
| **Juju** | Service orchestration — deploys and manages all OpenStack charms |
| **LXD** | Linux container hypervisor — runs the OpenStack control plane inside containers on bare metal |
| **Nova** | Compute service — runs the actual k8s master/worker VMs |
| **Neutron** | Networking — FIPs, flat networks, security groups for k8s nodes |
| **OVN** | SDN for OpenStack (replaces legacy Neutron agents) |
| **Glance** | Image service — stores the FCOS image k8s nodes boot from |
| **Keystone** | Identity service — authentication/authorization for everything |
| **Heat** | Orchestration engine — Magnum delegates VM creation to Heat stacks |
| **Magnum** | Manages the lifecycle of container orchestration clusters (k8s) via Heat |
| **Barbican** | Secret management — stores the TLS certificates Magnum uses |
| **Vault** | PKI/CA — issues TLS certificates for all OpenStack services |
| **HAProxy** | Load balancer — fronts service APIs |
| **Keepalived** | VIP failover — HA across the proxy nodes |
| **MySQL InnoDB Cluster** | Database — every service has its own replicated schema |
| **RabbitMQ** | Message bus — async service communication |
| **Octavia** | LBaaS — provisions `LoadBalancer`-type k8s services (OCCM) and the cluster's API/etc. LBs |
| **Designate** | DNS-as-a-service (deployed, zones not configured) |
| **Skyline** | Web dashboard (alternative to Horizon) |

## Kubernetes cluster components (inside the cluster VMs)

| Component | What it does |
|---|---|
| **kubelet** | Node agent — manages pod lifecycle on each master/worker |
| **containerd** | Container runtime — actually starts/stops containers (replaced Docker) |
| **Flannel** | CNI network plugin — assigns pod IPs and routes traffic between nodes |
| **CNI plugins** | Standard binaries (bridge, host-local, portmap) Flannel relies on |
| **CoreDNS** | Cluster DNS — service-name resolution |
| **kube-dns-autoscaler** | Scales CoreDNS replicas with cluster size |
| **etcd** | Distributed KV store — all k8s cluster state |
| **k8s-keystone-auth** | Authenticates k8s API requests against OpenStack Keystone |
| **OCCM** | OpenStack Cloud Controller Manager — bridges k8s to OpenStack (FIPs, LBaaS, storage) |
| **NPD** | Node Problem Detector — monitors nodes, reports as events |
| **Kubernetes Dashboard** | Web UI for cluster resources |
| **FCOS** | Fedora CoreOS — the OS inside each node VM |
| **podman** | Container engine inside FCOS nodes (runs heat-container-agent) |

## Constraints that shaped the design

* **No Cinder/Swift** → cluster templates must NOT use `--docker-volume-size` (needs
  Cinder volumes) and `registry_enabled` stays off.
* Charm channel must match the cloud: `magnum --channel 2023.2/stable`
  (`magnum-k8s` is the Kubernetes-hosted variant and NOT applicable here).
* On MAAS clouds every deploy needs explicit placement (`--to lxd:N`), otherwise
  Juju asks MAAS for brand-new machines and hangs.
* Capacity plan: minimal clusters are 1 master + 1 worker on flavor `2c2r20d`
  (smaller masters fail — see `golden-cluster-template.md` §1).

---

## Intended multi-tenant model & upstream direction

Magnum's stated job is **Containers-as-a-Service** — Kubernetes as a first-class
OpenStack resource. The **primary** multi-tenancy model is drawn **around each
cluster, not inside it**: every user provisions their **own** cluster in their
**own** project, isolated by Keystone roles + per-cluster private Neutron
networks ("the COE is not multi-tenant; isolation happens at the networking
layer"). That's the intended "big multi-user" use — many users × many private
clusters. It's also why the default kubeconfig model assumes one owner/creator
per cluster (see the membership discussion in
`../Kubernetes/k8s-cluster-usage.md`).

**Sharing one cluster is possible but secondary.** Magnum also ships a documented
in-cluster RBAC path for the same project to share a cluster: the
`keystone_auth_enabled` label (default `true` since Rocky) deploys the
`k8s-keystone-auth` webhook, OpenStack roles `k8s_admin`/`k8s_developer`/
`k8s_viewer` map to Kubernetes permissions, and the `k8s-keystone-auth-policy` +
`keystone-sync-policy` ConfigMaps grant users in the project access to that
cluster. It's real but the looser/more manual model — and the kubeconfig side of
it is where the friction lives: any user allowed through `certificate:get`
(creator/admin) still receives the full admin client cert, and letting a plain
`member` fetch the CA to build a token kubeconfig is blocked by the same gate
(no "CA only" response). So sharing is workable, but "proper, fully-automatic
per-user kubeconfig issuance" is a gap (see `k8s-cluster-usage.md` — Future
work).

**Scale is proven** at the cluster level: CERN ran Magnum in production and tested
bays up to ~1,000 nodes (2M req/s on a 200-node bay). Documented scale costs:
- Many clusters → Magnum's periodic stack-get per cluster loads Heat (a global
  stack-list optimization exists but is security-sensitive, so off by default).
- Multi-AZ HA is still listed "work in progress" in the heat-driver docs.

**The Heat driver — this deployment's `k8s_fedora_coreos_v1` — is being removed
upstream.** Magnum 2025.1 deprecates the heat driver in favor of the Cluster API
drivers (`k8s_capi_helm`, `k8s_cluster_api`); newer releases drop
`k8s_fedora_coreos_v1` entirely (along with the Keystone trust manager it was the
only consumer of). The CAPI drivers are production-ready per StackHPC/Azimuth and
modernize the credential/CA story, but require a CAPI **management cluster** plus
Barbican and Octavia, and still serve per-cluster kubeconfigs (the certificate
model survives because the tenant boundary remains *between* clusters).

**Conclusion:** Magnum is the right tool for "many users, each with their own
cluster" (most future-proofly on a CAPI driver). For "one big shared cluster with
per-user RBAC inside" it *can* work via the keystone webhook path above, but it's
the secondary, manual model with clunky kubeconfig issuance — if shared-cluster
RBAC were the *primary* goal, managed-k8s territory (Rancher/RKE, OpenShift)
would serve it better; fittingly this cloud's k8s binaries are already the
`-rancher1` distro builds.