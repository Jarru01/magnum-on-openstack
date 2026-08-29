# Kubernetes Dashboard (web UI)

The dashboard is deployed by default in `kube-system` but ships as `ClusterIP` — not
reachable from outside the cluster. Using it involves two independent decisions:

1. **How to log in** (Step 1) — the dashboard always asks for credentials first.
2. **How to reach it** (Step 2) — expose the service from outside the cluster.

Pick any login method with any exposure method; they don't interact.

**At a glance (`✓` = operator-free — works for any cluster admin with only kubectl
+ kubeconfig):**

| | Option | Needs maas? | Set-up | Security |
|---|---|---|---|---|
| Auth | 1a — admin token SA | no ✓ | snippet, once per cluster | cluster-admin token |
| Auth | 1b — skip login | no ✓ | one deployment patch | **unauthenticated** |
| Expose | 2a — Octavia `LoadBalancer` | no ✓ | one svc patch | public HTTPS |
| Expose | 2b — `kubectl proxy` / port-forward | no ✓ | command per use | localhost only |
| Expose | 2c — NodePort + maas nft DNAT | **yes** | maas rule per cluster | public HTTPS |

The route verified on the reference cluster is **1a + 2c**. `2a` (Octavia) is the
preferred operator-free exposure for future clusters but must be tested against the
cloud's Octavia first (see `OpenStack/limitations.md` #4). All dashboard sessions run
over the pod's self-signed TLS cert — the browser will warn; accept it.

---

## Step 1 — Choose a login method

The login screen has two tabs: **Token** (paste a token) and **kubeconfig** (upload a
file). Both need a *bearer token*. The "kubeconfig" tab only accepts a `token` (or
username/password) inside the file — it **cannot** use client-certificate auth, so
the magnum kubeconfig download (`client-certificate-data`/`client-key-data`) fails
with 500 *"Not enough data to create auth info structure"* (it works fine for
kubectl, just not for the dashboard).

### Option 1a — admin token service account (recommended, secure)

One-time per cluster, run from any machine with `kubectl` + the cluster kubeconfig.
On k8s ≥1.24 `kubectl create token` works but mints a shorter-lived token; for a
durable one create a dedicated admin SA, bind `cluster-admin`, and mint a long-lived
token via a Secret:

```bash
# 1. admin service account + cluster-admin binding (one-time)
kubectl apply -f - <<EOF
apiVersion: v1
kind: ServiceAccount
metadata:
  name: admin-user
  namespace: kube-system
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: admin-user
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: cluster-admin
subjects:
- kind: ServiceAccount
  name: admin-user
  namespace: kube-system
EOF

# 2. k8s >=1.24 does NOT auto-create a token Secret for the SA — create one explicitly:
kubectl apply -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: admin-user-token
  namespace: kube-system
  annotations:
    kubernetes.io/service-account.name: admin-user
type: kubernetes.io/service-account-token
EOF

# 3. print the long-lived token (no re-run needed)
kubectl get secret admin-user-token -n kube-system -o jsonpath='{.data.token}' | base64 -d
```

Log in by pasting it into the **Token** field, or build the optional kubeconfig file
below.

### Option 1b — skip login (zero set-up; NOT for production)

```bash
kubectl -n kube-system patch deployment kubernetes-dashboard --type=json -p \
  '[{"op":"replace","path":"/spec/template/spec/containers/0/args","value":["--auto-generate-certificates","--namespace=kube-system","--enable-skip-login=true"]}]'
```

The login screen gets a **Skip** button that yields full, unauthenticated access.
Only for trusted/lab clouds.

### Optional — token-based kubeconfig for the "kubeconfig" tab

Package the 1a token as a file: take the kubeconfig from `kubectl config view` (or
the magnum download) and replace **only the user entry** with the token — keep
`certificate-authority-data` and `server` untouched. See the ready template in
[`kubeconfig-dashboard-token.example.yaml`](kubeconfig-dashboard-token.example.yaml).

> The dashboard pod connects to `server` from **inside** the cluster, so the API
> address must be the in-cluster master FIP (e.g. `198.51.100.10:6443`) — NOT a maas
> DNAT address, which is only reachable from outside.

---

## Step 2 — Choose how to reach it

### Option 2a — `type: LoadBalancer` via Octavia (self-service, no maas)

Patch the service and `openstack-cloud-controller-manager` provisions an Octavia
amphora LB: VIP on the cluster internal subnet plus a floating IP on the external
network:

```bash
kubectl patch svc kubernetes-dashboard -n kube-system -p '{"spec":{"type":"LoadBalancer"}}'
kubectl get svc kubernetes-dashboard -n kube-system   # watch the EXTERNAL-IP column
# browse: https://<EXTERNAL-IP>   (accept the self-signed cert)
```

Pros: one patch, persistent URL, no maas access required. Caveat: Octavia may have a
pre-existing LB in `ERROR` (see `OpenStack/limitations.md` #4) — test LB creation on
a fresh cluster before relying on it.

### Option 2b — `kubectl proxy` / `port-forward` (zero infra, single-user, per use)

```bash
kubectl proxy
# open: http://localhost:8001/api/v1/namespaces/kube-system/services/https:kubernetes-dashboard:/proxy/
```

or:

```bash
kubectl -n kube-system port-forward svc/kubernetes-dashboard 8443:443
# open: https://localhost:8443
```

Needs the API address to be reachable from wherever you run it.

### Option 2c — NodePort + maas nft DNAT (legacy; operator-only)

Requires SSH access to the maas host, so it only suits the cloud operator.

*Step 2c.1 — change the dashboard service to NodePort:*

```bash
kubectl patch svc kubernetes-dashboard -n kube-system -p '{"spec":{"type":"NodePort"}}'
kubectl get svc kubernetes-dashboard -n kube-system
```

`PORT(S)` shows e.g. `443:31127/TCP`; the number after the colon is the NodePort.
For a fixed port instead of the random one:

```bash
kubectl patch svc kubernetes-dashboard -n kube-system --type=json \
  -p '[{"op":"replace","path":"/spec/ports/0/nodePort","value":30443}]'
```

*Step 2c.2 — add the nftables DNAT on the maas host* (nft, not iptables):

```bash
# get the worker FIP:
kubectl get nodes -o wide    # EXTERNAL-IP of the non-master node

echo '<maas-password>' | sudo -S nft add rule ip nat prerouting tcp dport <PORT> dnat to <WORKER_FIP>:<PORT>
```

This forwards traffic hitting the maas host's external IP on that port to the
worker's FIP. Known-working example: NodePort `31127` on worker `198.51.100.11`,
public port `32439`:
`sudo nft add rule ip nat prerouting tcp dport 32439 dnat to 198.51.100.11:31127`

To move an existing rule to a new cluster/node (the old entry goes stale after a
recreate):

```bash
sudo nft -a list chain ip nat prerouting | grep dport 32439   # note the "handle N"
sudo nft delete rule ip nat prerouting handle N
sudo nft add rule ip nat prerouting tcp dport 32439 dnat to <WORKER_FIP>:<NEW_NODEPORT>
```

The nft rules are **not persistent** — re-add after a maas reboot
(see `OpenStack/disaster-recovery.md`).

---

## Step 3 — Log in

- 1a → browse to the exposed URL, choose **Token**, paste the token, **Sign in**; or
  use the **kubeconfig** tab with the token-based file above.
- 1b → click **Skip**.