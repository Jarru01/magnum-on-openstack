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
juju integrate vault:certificates magnum:certificates
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

### Fix 1 — API topology (haproxy → apache TLS → magnum-api)

With the `vault:certificates` relation (added in §1) magnum runs in the charm's TLS
topology: haproxy binds the public port `9511` and TCP-passes to an apache2 TLS vhost
on `9501`, which proxies to magnum-api on `9491`. Verify with `sudo ss -tlnp` on the
unit.

> **Historic (pre-TLS) bug:** without the relation, the charm rendered haproxy's
> backend as the unit's IP while magnum-api bound `127.0.0.1:9501` only → every
> request failed with an empty reply (upstream LP #1943385 / #2058474). The workaround
> was `sed 's/[0-9.:]*:9501/127.0.0.1:9501/'` + restart haproxy. In the TLS topology
> the backend targets apache on the unit IP:9501 and works, so this sed is **not**
> needed — apply it only if a 502 actually recurs.

### Fix 2 — keystone auth renders legacy `v2.0` paths (upstream charm bug)

Keystone publishes `api_version` as an **integer** (`3`). Magnum's bundled keystone
interface reads relation data through `charms.reactive`'s `JSONUnitDataView`, which
JSON-decodes the value back to the integer `3`; the render template compares it to the
string `"3"` (`{% if identity_service.api_version == "3" %}`), which is always `False`
— so `magnum.conf` gets the legacy `v2.0` auth paths regardless of the connected
keystone version.

**A. Patch the render template once (recommended — survives reboots and re-renders):**

```bash
juju exec -m kis --unit magnum/0 -- sudo sed -i \
  's/identity_service.api_version == "3"/identity_service.api_version|string == "3"/' \
  /var/lib/juju/agents/unit-magnum-0/charm/templates/parts/keystone-authtoken
```

The next config render then emits `auth_version = v3` automatically (verified: the
patched template renders `v3` for both int `3` and string `"3"`). The patch is lost
on `juju refresh` / `upgrade-charm`, so re-apply it afterwards (same as the flannel
patch — see `../OpenStack/magnum-fixes-and-maintenance.md` §3). It does not rewrite
the already-rendered file until the next relation change/reboot.

**B. Correct the rendered file (immediate, but re-apply after every re-render):**

```bash
juju exec -m kis --unit magnum/0 -- sudo sed -i s/v2.0/v3/g /etc/magnum/magnum.conf
juju exec -m kis --unit magnum/0 -- sudo systemctl restart magnum-api magnum-conductor
```

### Certificates / OpenStack CA — via the `vault:certificates` relation

Magnum's `certificates` endpoint (interface `tls-certificates`) must be related to
the cloud's Vault PKI. This is **required** and is the only place the CA comes
from — there is no manual step and no custom image:

```bash
juju integrate vault:certificates magnum:certificates
```

Once related, the charm automatically:

* installs the Vault root CA into the unit's trust store
  (`/usr/local/share/ca-certificates/magnum.crt` + `update-ca-certificates`), so
  magnum-api can validate the HTTPS keystone endpoint. Without it, token validation
  fails (`CERTIFICATE_VERIFY_FAILED` → API returns 503 "Keystone service is
  temporarily unavailable");
* renders `[drivers] openstack_ca_file` in `magnum.conf`.

The magnum conductor passes that CA to every new cluster as the Heat
`openstack_ca` parameter; the node user-data writes it to
`/etc/pki/ca-trust/source/anchors/openstack-ca.pem` and trusts it via
`update-ca-trust`. **A stock Fedora CoreOS image therefore works — no CA-baked
image is needed** (verified 2026-09-10: a cluster built from the stock image was
`CREATE_COMPLETE / HEALTHY`, with the Vault root CA present in the node's trust
anchors).

> **Enabling TLS also changes the API topology** (standard for the OpenStack
> charms): `https()` flips true, so the charm shifts ports — haproxy keeps the
> public `9511`, terminating TLS at an apache2 vhost on `9501`, which proxies to
> magnum-api on `9491` (public port −10 for apache, −20 for the API). The keystone
> catalog endpoints are re-registered as `https://<unit>:9511/v1`. After enabling
> it, regenerate Skyline's nginx **on every Skyline unit** (the config is per-unit):
> `for u in skyline/72 skyline/73 skyline/74; do juju run "$u" regenerate-nginx; done`.
> Then hard-refresh the browser.

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
