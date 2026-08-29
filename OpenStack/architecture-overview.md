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