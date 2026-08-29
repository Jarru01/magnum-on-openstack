#!/usr/bin/env bash
# Bake the Vault root CA into a Fedora CoreOS QCOW2 image.
#
# virt-customize is broken on Ubuntu (supermin), so we mount the image via qemu-nbd
# and edit the FCOS ostree root directly (FCOS 38 layout; partition may differ on
# other FCOS majors — check with `sudo fdisk -l <image>`).
#
# Usage:
#   IMAGE=fedora-coreos-38.20230806.3.0-openstack.x86_64.qcow2 \
#   CA=~/kis-ca/kisroot-ca.crt \
#   OUT=fcos-ca.qcow2 \
#   sudo -E bash fcos-bake-ca.sh
set -euo pipefail

IMAGE="${IMAGE:?IMAGE is required}"
CA="${CA:?CA is required (Vault root CA: e.g. kisroot-ca.crt)}"
OUT="${OUT:-fcos-ca.qcow2}"
MNT="${MNT:-/mnt/fcos}"
DEV="${DEV:-/dev/nbd0}"

modprobe nbd max_part=8
qemu-nbd --connect="$DEV" "$IMAGE"
mount "${DEV}p4" "$MNT"        # p4 = FCOS root (ostree) on FCOS 38

CERT_DIR="$MNT/ostree/deploy/fedora-coreos/deploy/"
DST="$CERT_DIR/$(ls -1 "$CERT_DIR" | grep -v ostree).0"
cp "$CA" "$DST/etc/pki/ca-trust/source/anchors/"

# ca-bundle.crt is a DANGLING symlink in a never-booted image -> materialize a real bundle
BUNDLE=/etc/ssl/certs/ca-certificates.crt
cat "$BUNDLE" "$CA" > "$MNT/combined-bundle.pem"

rm "$DST/etc/pki/tls/certs/ca-bundle.crt"
mkdir -p "$DST/etc/pki/ca-trust/extracted/pem" "$DST/etc/pki/ca-trust/extracted/openssl"
cp "$MNT/combined-bundle.pem" "$DST/etc/pki/tls/certs/ca-bundle.crt"
cp "$MNT/combined-bundle.pem" "$DST/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem"
cp "$MNT/combined-bundle.pem" "$DST/etc/pki/ca-trust/extracted/openssl/ca-bundle.trust.crt"
rm "$MNT/combined-bundle.pem"

umount "$MNT"
qemu-nbd --disconnect "$DEV"

echo "Patched image written to $OUT — upload to Glance as <name>-ca"
echo "  openstack image create <name>-ca --file $OUT --disk-format qcow2 --container-format bare --property os_distro=fedora-coreos"