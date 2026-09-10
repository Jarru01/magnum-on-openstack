#!/usr/bin/env python3
"""Repair the flannel CNI init container in magnum's flannel-service.sh.

The Bobcat-era template fragment renders an ``install-cni-plugins`` init
container that pulls a retired image (``${_prefix}flannel-cni:${FLANNEL_CNI_TAG}``
-> ``quay.io/coreos/flannel-cni:v0.3.0``, now gone).  This script moves that
init container to the known-good state:

    image: docker.io/rancher/mirrored-flannelcni-flannel-cni-plugin:v1.1.2
    args : cp /flannel -> /opt/cni/bin/flannel
           + download standard CNI plugins

Recognised states (classified from the current init container block):

  * PRISTINE       ${_prefix}flannel-cni:${FLANNEL_CNI_TAG} + magnum-install-cni.sh
  * BUSYBOX        busybox:1.36 + inline wget (plugins only, no flannel binary)
  * RANCHER_OLDARG rancher mirror image + magnum-install-cni.sh
  * FINAL          rancher mirror image + cp /flannel + wget  -> no-op

Safety:

  * writes only when the block is one of the known pre-states;
  * refuses (exit 2) and prints the block when it cannot classify it;
  * backs the file up (``<file>.bak.<timestamp>``) before writing;
  * validates after writing (bash syntax, final markers, DaemonSet intact)
    and restores the backup if anything fails (exit 1).

Usage:

    python3 fix-flannel-final.py [--path PATH] [--dry-run] [--no-backup]
"""

import argparse
import difflib
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_PATH = (
    "/usr/lib/python3/dist-packages/magnum/drivers/common/templates/"
    "kubernetes/fragments/flannel-service.sh"
)

FINAL_BLOCK = """      - name: install-cni-plugins
        image: docker.io/rancher/mirrored-flannelcni-flannel-cni-plugin:v1.1.2
        command:
        - sh
        args:
        - -c
        - "cp /flannel /host/opt/cni/bin/flannel && chmod 755 /host/opt/cni/bin/flannel && cd /host/opt/cni/bin/ && wget -qO- https://github.com/containernetworking/plugins/releases/download/v1.3.0/cni-plugins-linux-amd64-v1.3.0.tgz | tar xz && ls"
        volumeMounts:
        - name: host-cni-bin
          mountPath: /host/opt/cni/bin/
        - name: flannel-cfg
          mountPath: /etc/kube-flannel/
"""

OLD_CNI_SCRIPT = """  magnum-install-cni.sh: |
    #!/bin/sh
    set -e -x;
    if [ -w "/host/opt/cni/bin/" ]; then
      cp /opt/cni/bin/* /host/opt/cni/bin/;
      echo "Wrote CNI binaries to /host/opt/cni/bin/";
    fi;
"""

BLOCK_RE = re.compile(
    r"(?ms)^      - name: install-cni-plugins\n.*?(?=^      - name: install-cni\n)"
)

FINAL_MARKER = "cp /flannel /host/opt/cni/bin/flannel"


def classify(block: str) -> str:
    if FINAL_MARKER in block:
        return "FINAL"
    if "busybox" in block:
        return "BUSYBOX"
    if "flannel-cni" in block and "FLANNEL_CNI_TAG" in block:
        return "PRISTINE"
    if "rancher" in block and "magnum-install-cni.sh" in block:
        return "RANCHER_OLDARG"
    return "UNKNOWN"


def bash_syntax_ok(path: Path):
    """Return (checked, ok). Skipped (checked=False) when bash is unavailable."""
    if sys.platform == "win32":
        return False, True
    bash = shutil.which("bash")
    if not bash:
        return False, True
    result = subprocess.run(
        [bash, "-n", str(path)], capture_output=True, text=True
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
    return True, result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default=DEFAULT_PATH, help="fragment to patch")
    parser.add_argument("--dry-run", action="store_true", help="show diff only")
    parser.add_argument("--no-backup", action="store_true", help="skip backup")
    args = parser.parse_args()

    path = Path(args.path)
    if not path.is_file():
        print(f"ERROR: fragment not found: {path}")
        return 2

    original = path.read_text(encoding="utf-8")
    match = BLOCK_RE.search(original)
    if not match:
        print(
            "ERROR: could not locate the install-cni-plugins init container "
            "block; refusing to write."
        )
        return 2

    state = classify(match.group(0))
    print(f"Target : {path}")
    print(f"State  : {state}")

    if state == "FINAL":
        print("Already in the final state - nothing to do (no-op).")
        return 0

    if state == "UNKNOWN":
        print("ERROR: unrecognised init container content; refusing to write.")
        print("--- current block ---")
        print(match.group(0), end="")
        print("---------------------")
        return 2

    new_content = original[: match.start()] + FINAL_BLOCK + original[match.end() :]

    removed_cni_script = False
    if OLD_CNI_SCRIPT in new_content:
        new_content = new_content.replace(OLD_CNI_SCRIPT, "", 1)
        removed_cni_script = True

    if new_content == original:
        print("No content change produced - nothing to do (no-op).")
        return 0

    if args.dry_run:
        print(f"Would patch {state} -> FINAL"
              + (" and remove the unused magnum-install-cni.sh ConfigMap entry"
                 if removed_cni_script else ""))
        diff = difflib.unified_diff(
            original.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=str(path),
            tofile=str(path) + " (patched)",
        )
        sys.stdout.writelines(diff)
        return 0

    backup = None
    if not args.no_backup:
        backup = path.with_name(
            path.name + ".bak." + datetime.now().strftime("%Y%m%d-%H%M%S")
        )
        shutil.copy2(path, backup)

    path.write_text(new_content, encoding="utf-8")

    # ---- validation -------------------------------------------------------
    problems = []
    reread = path.read_text(encoding="utf-8")
    new_match = BLOCK_RE.search(reread)
    if not new_match:
        problems.append("init container block not found after write")
    elif classify(new_match.group(0)) != "FINAL":
        problems.append("block is not in the final state after write")
    if FINAL_MARKER not in reread:
        problems.append("final marker missing after write")
    if reread.count("kind: DaemonSet") != original.count("kind: DaemonSet"):
        problems.append("kind: DaemonSet count changed")
    checked, ok = bash_syntax_ok(path)
    if checked and not ok:
        problems.append("bash syntax check failed")
    if not checked:
        print("WARN   : bash not available; skipped syntax check")

    if problems:
        for problem in problems:
            print(f"ERROR  : {problem}")
        if backup:
            shutil.copy2(backup, path)
            print(f"Restored backup: {backup}")
        else:
            path.write_text(original, encoding="utf-8")
            print("Restored original content (backup disabled)")
        return 1

    print(f"Patched: {state} -> FINAL"
          + (" (removed unused magnum-install-cni.sh ConfigMap entry)"
             if removed_cni_script else ""))
    if backup:
        print(f"Backup : {backup}")
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
