# Magnum Deployment Guide (MaaS + Juju)

Deploying the OpenStack **Magnum** (container orchestration / COE) service on a
Juju-charmed OpenStack cloud, up to the point where the Magnum API is fully
functional. Cluster build-out from this point is covered in
[`golden-cluster-template.md`](golden-cluster-template.md) and
[`first-cluster-build-log.md`](first-cluster-build-log.md).

> All addresses in this documentation use reserved documentation ranges
> (RFC 5737) / placeholders, not the live cloud's values.

---

## 1. Deploy the service

```bash
juju deploy magnum --channel 2023.2/stable \
  --config openstack-origin=cloud:jammy-bobcat --to lxd:0 --bind openstack-mgmt
juju deploy mysql-router --channel 8.0/stable magnum-mysql-router --to lxd:0

juju integrate magnum-mysql-router:db-router mysql-innodb-cluster:db-router
juju integrate magnum-mysql-router:shared-db magnum:shared-db
juju integrate rabbitmq-server:amqp magnum:amqp
juju integrate keystone:identity-service magnum:identity-service
```

Result on the reference deployment: magnum rev **126** (magnum 17.0.1),
mysql-router rev **1154**, unit `magnum/0`, converged to `active` in ~8 minutes.

### Verification

| Check | Result |
|---|---|
| `juju status` app/unit state | magnum + magnum-mysql-router `active/idle`, port 9511 open |
| systemd inside unit | `magnum-api`, `magnum-conductor` both `active` |
| DB provisioning | `magnum` schema present in the cluster DB (replicated) |
| Keystone catalog | service `magnum` (`container-infra`) + admin/public/internal endpoints |
| Service user | `magnum@service_domain` exists and issues tokens |
| Rendered config sanity | `[trust] trustee_domain_name=magnum`, `cert_manager_type=barbican` |
| Whole-stack regression | all applications still `active`; zero changes outside magnum |

---

## 2. Post-deploy fixes required (charm quirks)

### Fix 1 — haproxy backend targets the wrong address (upstream LP #1943385 / #2058474)

The charm fronts magnum-api with a local haproxy (`*:9511` → backend), but renders
the backend as `server magnum-0 192.0.2.11:9501` while magnum binds `127.0.0.1:9501`
only (upstream default `[api] host = 127.0.0.1`). Every request dies with an empty
reply (exit code 52).

```bash
juju exec -m kis --unit magnum/0 -- sudo sed -i s/192.0.2.11:9501/127.0.0.1:9501/ /etc/haproxy/haproxy.cfg
juju exec -m kis --unit magnum/0 -- sudo systemctl restart haproxy
```

> Re-apply after any event that re-renders `haproxy.cfg` (config/relation changes,
> charm refresh, reboot). This is the **canonical** fix — other documents
> cross-reference it instead of repeating it.

### Fix 2 — Vault CA not trusted + stale v2.0 URIs

This charm line has no `certificates` endpoint, so nothing installs the Vault root
CA into the unit's trust store. Token validation against the HTTPS keystone
endpoint then fails (SSL `CERTIFICATE_VERIFY_FAILED` → API returns 503
"Keystone service is temporarily unavailable"). The **canonical** fix:

```bash
# 1. copy the Vault root CA into a plain-path temp dir (juju cannot read ~/snap/... paths):
cp ~/kis-ca/kisroot-ca.crt ~/magnum-work/kisroot-ca.crt
juju scp -m kis ~/magnum-work/kisroot-ca.crt magnum/0:/tmp/kisroot-ca.crt

# 2. install into the unit's trust store:
juju exec -m kis --unit magnum/0 -- sudo cp /tmp/kisroot-ca.crt /usr/local/share/ca-certificates/kisroot-ca.crt
juju exec -m kis --unit magnum/0 -- sudo update-ca-certificates

# 3. correct the legacy v2.0 references the template renders:
juju exec -m kis --unit magnum/0 -- sudo sed -i s/v2.0/v3/g /etc/magnum/magnum.conf
juju exec -m kis --unit magnum/0 -- sudo systemctl restart magnum-api magnum-conductor
```

> NOTE: juju (snap) cannot read files under `~/snap/...` or `/tmp` of other
> snaps/users — stage copies in plain `$HOME`. After `update-ca-certificates`,
> `curl https://192.0.2.1:5000/v3` from the unit returns 200.

---

## 3. Trustee domain setup

```bash
juju run magnum/leader domain-setup --wait 5m
```

The action creates (its trailing "No domain ... exists" lines are its own
pre-creation existence checks, not errors):

* domain **`magnum`** — "Magnum trustee domain"
* user **`magnum_domain_admin`** in that domain

### Verification

| Check | Result |
|---|---|
| `openstack domain show magnum` | enabled, correct ID |
| `openstack role assignment list --domain magnum --names` | `admin` → `magnum_domain_admin@magnum` |
| `openstack coe cluster template list` | empty table, **no error** (pre-domain-setup 403 resolved) |
| `openstack coe cluster list` | empty table, no error |
| Whole-stack regression | `0` blocked / `0` error / `0` maintenance units model-wide |

Magnum is now fully functional as a service: auth → catalog → haproxy → API →
trust machinery → DB.

---

## 4. Project layout clarifications

### Two projects named `admin`

The cloud contains **two projects named `admin`**:

| ID | Domain | Role |
|---|---|---|
| (operator's working project) | **admin_domain** | The project all cluster work happens in; every command in this documentation scopes here |
| (other) | default | Separate/unused |

During investigation a temporary `admin` role was added to user `skyline` on the
*default-domain* admin project and **immediately reverted** once it proved
unnecessary. Net IAM change: none.

### Keypair visibility (important gotcha)

This Nova version treats keypairs as **per-USER resources**. Consequences:

* A keypair is only visible via API/GUI to the user that owns it.
* Cluster templates must reference a keypair owned by the **cluster creator**.
* To make a public key visible to another account, re-register it while
  authenticated as that account:
  `openstack keypair create --public-key <pub> <name>`
* Debugging tip: inspect ground truth via the `nova_api.key_pairs` table on the
  nova-cloud-controller unit (the plain `nova` schema's `key_pairs` is unused
  legacy here).

---

## Working OpenStack CLI credentials

The stock `exportCred.sh` can pull a stale admin password from the keystone leader.
A working admin context used throughout this documentation (replace the
placeholders):

```bash
export OS_AUTH_URL=https://192.0.2.1:5000/v3
export OS_USERNAME=<admin-user>
export OS_PASSWORD=<admin-password>
export OS_USER_DOMAIN_NAME=admin_domain
export OS_PROJECT_NAME=admin
export OS_PROJECT_DOMAIN_NAME=admin_domain
export OS_REGION_NAME=RegionOne
export OS_INTERFACE=public
export OS_IDENTITY_API_VERSION=3
export OS_CACERT=<path-to-kisroot-ca.crt>
```

> **Note:** use `source admin-openrc.sh`, **not** `./admin-openrc.sh`. Running it
> with `./` executes in a subprocess — the `export` variables vanish as soon as the
> script exits.