#!/usr/bin/env python3
"""Patch magnum's flannel-service.sh so the install-cni-plugins init container
uses the rancher flannel mirror (which ships the flannel binary at /flannel) and
downloads the standard CNI plugins instead of the retired quay.io/coreos image.

Run once on the magnum unit after deploy (re-run after `juju refresh`):

    juju ssh magnum/0 -- python3 fix-flannel-final.py

Ref: https://github.com/openstack/charm-magnum (Bobcat era) template fragment
    magnum/drivers/common/templates/kubernetes/fragments/flannel-service.sh
"""
import os
import sys

PATH = "/usr/lib/python3/dist-packages/magnum/drivers/common/templates/kubernetes/fragments/flannel-service.sh"

with open(PATH, "r") as f:
    content = f.read()

# Replace the install-cni-plugins init container
old_init = """      - name: install-cni-plugins
        image: docker.io/rancher/mirrored-flannelcni-flannel-cni-plugin:v1.1.2
        command:
        - sh
        args:
        - /etc/kube-flannel/magnum-install-cni.sh"""
new_init = """      - name: install-cni-plugins
        image: docker.io/rancher/mirrored-flannelcni-flannel-cni-plugin:v1.1.2
        command:
        - sh
        args:
        - -c
        - "cp /flannel /host/opt/cni/bin/flannel && chmod 755 /host/opt/cni/bin/flannel && cd /host/opt/cni/bin/ && wget -qO- https://github.com/containernetworking/plugins/releases/download/v1.3.0/cni-plugins-linux-amd64-v1.3.0.tgz | tar xz && ls" """

if old_init in content:
    content = content.replace(old_init, new_init)
    print("Patched install-cni-plugins: rancher image + flannel binary + CNI plugins")
else:
    print("Old init container not found (already patched or version differs)")

# Also remove the unused magnum-install-cni.sh from the ConfigMap
old_section = """  magnum-install-cni.sh: |
    #!/bin/sh
    set -e -x;
    if [ -w "/host/opt/cni/bin/" ]; then
      cp /opt/cni/bin/* /host/opt/cni/bin/;
      echo "Wrote CNI binaries to /host/opt/cni/bin/";
    fi;"""

if old_section in content:
    content = content.replace(old_section, "")
    print("Removed magnum-install-cni.sh from ConfigMap")
else:
    print("magnum-install-cni.sh section not found (already removed)")

with open(PATH, "w") as f:
    f.write(content)

# Fail loudly if nothing was changed
sys.exit(0 if ("cp /flannel" in content) else 1)