# Magnum Cluster Guides on OpenStack (MaaS + Juju)

Guides, configuration files and notes on deploying the OpenStack **Magnum**
(container orchestration / COE) service on a private OpenStack cloud managed via
**MaaS** and **Juju** — up to creating fully working Kubernetes clusters.

> Tested on a mini 2-node OpenStack cloud deployed via **MaaS + Juju** (Bobcat).
> The golden cluster template bakes in every known fix, so users create clusters
> with zero SSH and zero manual node surgery.

> **Note on addresses:** values in these guides use reserved documentation ranges
> (RFC 5737) and `<placeholders>` — not the live cloud's addresses or passwords.

> **Topology note:** this is a **reference/lab cloud** — its cluster FIPs are not
> routable from outside, so some guides include MAAS `nft` DNAT workarounds.
> On production OpenStack with routable FIPs those steps do not apply.

## 📚 Documents in this repository

Document | Description
--- | ---
[Magnum deployment guide](Magnum/magnum-deployment-guide.md) | Deploying the magnum service + trustee domain setup, charm quirks, project-layout gotchas
[Golden cluster template](Magnum/golden-cluster-template.md) | The canonical template, onboarding a new project/user, the kubeconfig permission model and create/delete failure modes
[First cluster build log](Magnum/first-cluster-build-log.md) | How the first working cluster was built (the five defects behind the golden template)
[Cluster usage (kubectl)](Kubernetes/k8s-cluster-usage.md) | Getting the kubeconfig (CLI & Skyline), installing kubectl, reachability workarounds (lab topology), production access model
[Kubernetes dashboard](Kubernetes/kubernetes-dashboard.md) | Logging in (token / skip) and exposing the dashboard (Octavia LB / proxy / NodePort+DNAT)
[Magnum fixes & maintenance](OpenStack/magnum-fixes-and-maintenance.md) | Recurring charm quirks, the flannel fix, Skyline Container Infra fixes, FCOS upgrades, lessons learned
[Disaster recovery](OpenStack/disaster-recovery.md) | Power outage / full reboot procedure
[Architecture overview](OpenStack/architecture-overview.md) | How Magnum → Heat creates clusters, component maps
[Known limitations](OpenStack/limitations.md) | Internet dependency, EOL stack, no PVCs, LBaaS caveats
[Flannel patch](Magnum/fix-flannel-final.py) | The patched `flannel-service.sh` — init container copies `/flannel` from the rancher mirror and fetches the standard CNI plugins, eliminating node-level CNI fixes
[FCOS CA bake script](OpenStack/fcos-bake-ca.sh) | Re-baking the Vault root CA into a Fedora CoreOS image (`qemu-nbd`, no `virt-customize`)
[Kubeconfig examples](Kubernetes/kubeconfig-dashboard-token.example.yaml) / [DNAT](Kubernetes/kubeconfig-dnat.example.yaml) | Ready templates for dashboard and DNAT kubeconfig files

## 🧯 Troubleshooting

### Issue 1: Kubeconfig download fails with 403

`openstack coe cluster config` / Skyline **Download kubeconfig** → 403 *"Policy
doesn't allow certificate:get to be performed"*. The caller must be the cluster
creator or a project `admin` (the kubeconfig embeds `CN=admin, O=system:masters`
= cluster-admin, so OpenStack gates it).

#### Solution

Let the user create their own cluster (self-service), or grant the `admin` role in
the project. Full permission model + options:
[`Magnum/golden-cluster-template.md`](Magnum/golden-cluster-template.md).

### Issue 2: Cluster delete fails for a `member` user

Delete → *"Failed to pre-delete resources for cluster … Policy does not allow this
request to be performed. (HTTP 403)"*. Teardown lists the cluster's Octavia load
balancers with the user's token, which requires `load-balancer_member` **and**
`member`.

#### Solution

```bash
openstack role add --project <project> --user <user> load-balancer_member
```

Cluster then goes to `DELETE_FAILED` until deleted by a principal with the role
(same document: permission + failure modes).

### Issue 3: Cluster create fails mid-deploy (deploy timeout)

`CREATE_FAILED` with `status_reason: deploy_status_code : Deployment exited with
non-zero status code: 1`, and the deployment logs show the kube-apiserver
`resource quota evaluation timed out`. The master is under-resourced (e.g. a
`1c1r10d` flavor).

#### Solution

Use flavor **≥ `2c2r20d`** (golden template default). See
[`Magnum/golden-cluster-template.md`](Magnum/golden-cluster-template.md).

### Issue 4: Skyline "Error, Unable to get Data" on Create Cluster

Upstream JS bug in `checkVolumeQuota()` — it destructures `cinderQuota` without a
fallback when Cinder is absent.

#### Solution

Patch `{left:l=0}=r` → `{left:l=0}=r||{}` in the minified console JS on each Skyline
unit, and also run `juju run skyline/leader regenerate-nginx` if Container Infra
tabs were rendered before magnum existed.
[`Magnum/first-cluster-build-log.md`](Magnum/first-cluster-build-log.md).

### Issue 5: `openstack coe ...` empty reply / Skyline 502

Every other API works, only magnum fails. The charm re-rendered haproxy backend
(and/or the v2.0 keystone paths) back to broken values after a config change,
refresh, or reboot.

#### Solution

Re-apply the canonical haproxy + v2.0→v3 fix block:
[`OpenStack/magnum-fixes-and-maintenance.md`](OpenStack/magnum-fixes-and-maintenance.md)
(and [`OpenStack/disaster-recovery.md`](OpenStack/disaster-recovery.md) after a reboot).

## About

Guides, configuration files and notes on deploying the Magnum
container-orchestration service on a private OpenStack cloud managed via MaaS and
Juju.