# Disaster Recovery (power outage / full reboot)

Everything that survives, everything that does not, and the recovery procedure.

---

## What survives

* OpenStack cloud — Juju manages service restarts automatically (keystone, nova,
  neutron, magnum, etc. all come back on their own).
* Magnum cluster definitions — stored in the magnum DB.
* Glance images — the stock FCOS image persists (the Vault CA is injected at boot
  from the `vault:certificates` relation, not baked into the image).
* k8s cluster VMs — shut down cleanly; power back on with `openstack server start`.
* Flannel DaemonSet init-container patch (rancher + wget) — persists in etcd, so it
  **does** come back automatically after nodes recover.

## What does NOT survive (re-apply after every outage/reboot)

### Stale mysql-routers after DB failover (ALL OpenStack services)

After a reboot/outage, `mysql-innodb-cluster` may promote a different member to R/W
primary. All the per-service `*-mysql-router` processes keep pointing at the *old*
primary (now read-only), so every write fails with:

```text
pymysql.err.OperationalError (1290, 'The MySQL server is running with the --read-only
option so it cannot execute this statement')
```

Surfaces as e.g. `Failed to synchronize the placement service ... ResourceProviderSyncFailed`
(code 500) on instance delete, and vague 500s on create/update across all APIs. Reads
still work (served by any member), which makes it easy to misdiagnose.

Fix — restart every router so it re-bootstraps to the current primary:

```bash
for svc in placement nova neutron glance keystone magnum barbican octavia dashboard; do
  juju ssh ${svc}/0 -- sudo systemctl restart ${svc}-mysql-router.service
done
# (unit names nova-cloud-controller/0, neutron-api/0, openstack-dashboard/0 use
#  nova-mysql-router / neutron-mysql-router / dashboard-mysql-router service names)
```

Verify: `openstack quota set --class default --instances 20` succeeds (a real write).

### Magnum keystone `v2.0` paths (charm re-render) + TLS topology

magnum runs in the OpenStack charm's TLS topology (enabled by the
`vault:certificates` relation): haproxy on the public port `9511` TCP-passes to an
apache2 TLS vhost on `9501`, which proxies to magnum-api on `9491`. The charm
re-renders `/etc/magnum/magnum.conf` on config/relation changes and reboots, and the
`keystone_authtoken` section reverts to the legacy `v2.0` paths, which breaks auth
(Skyline "Get clusters" → 502 / empty quota, `openstack coe ...` empty reply).
Symptom signature: every other API service works, only magnum fails. Re-apply:

```bash
juju ssh magnum/0 -- sudo sed -i 's/v2.0/v3/g' /etc/magnum/magnum.conf
juju ssh magnum/0 -- sudo systemctl restart magnum-api magnum-conductor
# verify: Skyline Container Infra lists clusters; openstack coe cluster list works
```

The Vault CA is installed automatically by the `vault:certificates` relation
(`/usr/local/share/ca-certificates/magnum.crt`) and survives reboots — there is no
manual CA copy.

> **Historic (pre-TLS) haproxy-backend bug:** before the certificates relation was
> added, the charm rendered haproxy's backend as the unit's IP while magnum-api bound
> loopback only → 502s; the workaround was
> `sed 's/[0-9.:]*:9501/127.0.0.1:9501/' /etc/haproxy/haproxy.cfg`. In the TLS
> topology the backend correctly targets apache on the unit IP:9501, so it is not
> needed — only re-apply if a 502 actually recurs, after checking the listeners
> (`ss -tlnp` should show haproxy :9511, apache :9501, magnum-api :9491).

### CNI plugins / flannel binary

They sit on the nodes' ephemeral filesystem, but the flannel DaemonSet init
container re-downloads them automatically on pod restart. No manual intervention
needed.

### maas nft DNAT rules for clusters (lab topology only)

The exposure rules added per [`k8s-cluster-usage.md`](../Kubernetes/k8s-cluster-usage.md)
§5 / [`kubernetes-dashboard.md`](../Kubernetes/kubernetes-dashboard.md) Option 2c
are **not persistent** — re-add them after a maas reboot. These rules exist only
because the reference cloud's FIPs are not routable from outside; on production
OpenStack with routable FIPs there are no such rules to re-add.

---

## Recovery procedure after full reboot

```bash
# 1. start the k8s cluster VMs
openstack server start k8s-test-<id>-master-0
openstack server start k8s-test-<id>-node-0

# 2. re-apply the magnum keystone v3 fix (charm re-render reverts it to v2.0)
juju ssh magnum/0 -- sudo sed -i 's/v2.0/v3/g' /etc/magnum/magnum.conf
juju ssh magnum/0 -- sudo systemctl restart magnum-api magnum-conductor

# 2b. restart all mysql-routers (DB failover left them read-only)
for svc in placement nova neutron glance keystone magnum barbican octavia dashboard; do
  juju ssh ${svc}/0 -- sudo systemctl restart ${svc}-mysql-router.service
done
# verify writes work: openstack quota set --class default --instances 20

# 3. wait for nodes to boot and pods to schedule
#    no SSH needed — the DaemonSet init container re-runs and restores
#    /opt/cni/bin (flannel binary + CNI plugins)

# 4. verify
kubectl get nodes                    # both Ready
kubectl get pods -A | grep -v Running  # empty = all green
openstack coe cluster list           # magnum API reachable again (no 502)
```

> **Note:** recovery is *not* fully automatic — the magnum keystone-v3 fix (step 2)
> and the mysql-router restarts (step 2b) must be re-applied after any
> reboot/outage. The k8s-side recovery (flannel init container) IS automatic. The
> `vault:certificates` relation, `cluster-user-trust` (juju config) and the
> `heat_stack_user` role survive reboots.