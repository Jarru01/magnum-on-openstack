# Using a Cluster (kubectl + kubeconfig)

How users actually *work with* a finished cluster: fetch the kubeconfig (CLI or
Skyline), install kubectl, wire it up on Windows/Linux, and (when the single master
FIP isn't routable) reach the cluster through a DNAT. The **default and only
required** access path is: Skyline → download kubeconfig → local `kubectl`. No
OpenStack CLI, no SSH, no manual fixes.

> Companion: [`kubernetes-dashboard.md`](kubernetes-dashboard.md) for the web UI.

---

## 1. Getting the kubeconfig (two equivalent paths)

### Path A — Skyline dashboard (recommended for users)

Skyline's Container Infra UI is the main access path: cluster management **and**
kubeconfig retrieval are both supported from the dashboard.

| Action | Supported |
|---|---|
| Create cluster | ✅ |
| List/view clusters | ✅ |
| Resize cluster | ✅ |
| Delete cluster | ✅ |
| **Download kubeconfig** | ✅ |
| **Deploy workloads (kubectl)** | ❌ use the downloaded kubeconfig with local `kubectl` |

Full flow: create the cluster under **Container Infra** (uses the public golden
template), open its detail view, click **Download kubeconfig**, then use the file
locally with kubectl:

```bash
# Option A — point kubectl at the downloaded file directly (any filename works):
kubectl --kubeconfig C:\Users\<you>\Downloads\<cluster>-kubeconfig.yaml get nodes

# Option B — make it the default so plain `kubectl` works (kubectl auto-loads ~/.kube/config):
Copy-Item C:\Users\<you>\Downloads\<cluster>-kubeconfig.yaml $HOME\.kube\config
kubectl get nodes
```

The download already *is* a kubeconfig (`kind: Config`) — no conversion needed; the
filename doesn't matter.

> **Gotcha (Option B):** the file **must** be named exactly `config` in `~/.kube\`.
> kubectl silently ignores e.g. `config.yaml` there (`kubectl config get-contexts`
> shows empty, `kubectl config current-context` errors "not set"). Rename and it
> works immediately.

### Path B — OpenStack CLI

```bash
source <your-project>-openrc.sh
openstack coe cluster config <cluster> --dir ~/kubeconfig
export KUBECONFIG=~/kubeconfig/config
kubectl get nodes
```

Requires `python-openstackclient` + `python-magnumclient` + credentials, and
produces the same kind of kubeconfig the dashboard download returns.

### Who is allowed to download

Both paths call magnum's `GET /v1/certificates/<cluster>` — gated to the cluster
**creator** or a project **`admin`** (a plain `member` gets a 403 "Policy doesn't
allow certificate:get"). Workflow + options: see the permission model in
[`golden-cluster-template.md`](../Magnum/golden-cluster-template.md).

### Key properties of the kubeconfig users get

* The artifact embeds the cluster CA + a client certificate — kubectl validates TLS
  as-is against the master FIP, and cert-mode auth grants `CN=admin, O=system:masters`
  (full cluster-admin).
* Users **never** receive the node SSH key (`magnum-k8s.pem`) — node-level access
  stays admin/operator-only.
* For per-user RBAC later: `openstack coe cluster config --use-keystone` produces a
  token-authenticated kubeconfig backed by the in-cluster `k8s-keystone-auth`
  webhook *(untested on this cloud — the webhook DaemonSet runs, but the flag's
  token flow has not been verified end-to-end)*. Default remains the admin cert.

---

## 2. Installing kubectl

**Linux (apt, pinned to cluster version e.g. v1.26.8):**

```bash
sudo apt-get update && sudo apt-get install -y apt-transport-https ca-certificates curl
curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.26.8/deb/Release.key | sudo gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.26.8/deb/ /" | sudo tee /etc/apt/sources.list.d/kubernetes.list
sudo apt-get update && sudo apt-get install -y kubectl
```

**Windows (winget or scoop):**

```powershell
winget install Kubernetes.kubectl
# or: scoop install kubectl
```

---

## 3. Wiring it up (no env var needed)

kubectl auto-checks `~/.kube/config`:

**Linux:**
```bash
mkdir -p ~/.kube
cp ~/magnum-work/kubeconfig/config ~/.kube/config
```

**Windows (PowerShell):**
```powershell
mkdir -Force $HOME\.kube
cp C:\Users\<you>\.kube\config-current $HOME\.kube\config   # or from Downloads
```

> The `KUBECONFIG` env var is only needed to switch between multiple clusters; for
> a single cluster `~/.kube/config` is all you need.

### Quick usage examples

```bash
kubectl run test-nginx --image=nginx:alpine
kubectl get pods -o wide           # shows node + IP
kubectl describe pod test-nginx    # full lifecycle events
kubectl exec test-nginx -- cat /etc/os-release
kubectl delete pod test-nginx
```

---

## 4. SSH to cluster nodes (operator only)

```bash
ssh -i magnum-k8s.pem -o StrictHostKeyChecking=no core@<master-fip>
ssh -i magnum-k8s.pem -o StrictHostKeyChecking=no core@<worker-fip>
```

The private key (`magnum-k8s.pem`) is the only way in — do not lose it.

---

## 5. Reaching the cluster when the FIP is NOT routable (maas DNAT)

If the cluster master FIP can't be reached directly from the user's PC/VPN, expose
the API through the maas host's public IP with an nft DNAT rule. **maas uses nft,
not iptables.**

```bash
# on the maas host:
#   tcp dport 6767 dnat to <masterFIP>:6443
echo '<maas-password>' | sudo -S nft add rule inet nat PREROUTING tcp dport 6767 dnat to 198.51.100.10:6443
```

Then take the downloaded kubeconfig and edit only this part:

```yaml
clusters:
- cluster:
    # NO certificate-authority-data line here — kubectl ≥1.28 rejects "root
    # certificates file with the insecure flag"
    server: https://203.0.113.137:6767
    insecure-skip-tls-verify: true
  name: k8s-test
```

> **Faster** — do the same edit with two `kubectl config` commands instead of
> hand-editing the YAML (the downloaded cluster name is `k8s-test`):
> ```powershell
> $k = "$HOME\Downloads\<cluster>-kubeconfig.yaml"
> kubectl --kubeconfig $k config set-cluster k8s-test --server=https://203.0.113.137:6767 --insecure-skip-tls-verify=true
> kubectl --kubeconfig $k config unset clusters.k8s-test.certificate-authority-data
> ```
> `kubectl config` only edits the file locally (no cluster access).

Two gotchas that will otherwise bite:

1. **Filename:** kubectl only reads `~/.kube\config` (exact name). `config.yaml` is
   silently ignored — rename it to `config`.
2. **`insecure-skip-tls-verify: true` + a CA entry cannot coexist** — kubectl errors
   "specifying a root certificates file with the insecure flag is not allowed". Drop
   the CA line and keep the insecure flag; your **client cert/key still authenticate**
   (mutual TLS from your side), only the server identity check is waived.

Security caveat: `insecure-skip-tls-verify` means anyone who can MITM the path to
maas could impersonate the API server. Acceptable for a test setup; for production
prefer routing the real FIP over the VPN so the kubeconfig stays unmodified.

---

## 6. Production access model (user PCs, no SSH anywhere)

**The main access path — Skyline only:**

| Step | Tool | Requires |
|---|---|---|
| 1. Create/resize/delete cluster | Skyline → Container Infra | Skyline web URL (VPN reachable) |
| 2. Download kubeconfig | Skyline cluster detail view | Same |
| 3. Use the cluster | `kubectl` with the downloaded kubeconfig | Master FIP routable over VPN + `kubectl` installed |

Users need two things on their PC and nothing else: `kubectl` (a single binary,
match the cluster version) and routing to the cluster master FIP. No OpenStack CLI,
no `clouds.yaml`, no CA file copy.

**The OpenStack CLI is optional (admin/advanced use only):**

| Task | Talks to | Reachability needed |
|---|---|---|
| `kubectl` (work on a cluster) | master API `https://<FIP>:6443` | Cluster master FIP routable |
| `openstack coe cluster config` (manual kubeconfig) | keystone + **magnum** | Public endpoints for both routable |
| `openstack server/image/network ...` | catalog URLs | Matching public endpoints |

The cluster FIPs being routable **alone does not make the CLI work** — `openstack`
still needs to reach the control plane. This is only relevant if you choose to use
the CLI; the Skyline download path has no such dependency.

**What stays admin-only (correctly never on user PCs):**

* maas/controller SSH access
* Node SSH key `magnum-k8s.pem`
* Template/unit fixes (charm quirks — see `OpenStack/magnum-fixes-and-maintenance.md`)
* Octavia / cloud internals debugging