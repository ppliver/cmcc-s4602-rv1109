#!/bin/bash
# ============================================================================
# Build S4602 system_b.img (UBI/squashfs) from the Firefly rootfs artifact.
#
# Why this exists
# ---------------
# The GA job build-firefly-rootfs.yml produces a correct but FAT rootfs:
# rootfs.squashfs is ~137 MiB, while S4602's system_b MTD partition is only
# 44 MiB (0x2c00000). ~90% of the bulk is Firefly's generic BSP NPU/AI/GUI
# demo payload (rknn/OpenVX/Qt/OpenCV/CJK fonts/test models) which an IP
# camera does not use. This script trims that payload deterministically and
# repacks into the S4602 UBI geometry, yielding a flashable system_b.img.
#
# Since: 2026-11-20. Verified trimmed squashfs = 25.9 MiB (fits vol_size 40.2 MiB).
# Camera-critical libs (librkaiq / libgstrockchipmpp / startup_app_ipc) were
# checked with readelf -d: NONE depend on the trimmed OpenVX/Qt/OpenCV/GAL libs.
#
# Geometry — MUST match the factory / build_hook_image.sh (hard gates):
#   mksquashfs -comp xz -b 131072
#   ubinize -p 131072 -m 2048 -Q 1734217813, vol_id=0, vol_type=dynamic,
#            vol_name=rootfs, vol_alignment=1, vol_size=42156032 (332 LEB)
#   final size 46137344 (0x2C00000 = full mtd9), 0xFF tail padding.
# A reserved_pebs=352 variant is known to boot-panic — do not change geometry.
#
# Usage (inside WSL / Linux with squashfs-tools + mtd-utils):
#   bash tools/build_firefly_system_b.sh <path/to/rootfs.squashfs> <out/system_b.img>
# ============================================================================
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

usage() { echo "usage: $0 <rootfs.squashfs> <out/system_b.img>" >&2; exit 2; }
[ $# -eq 2 ] || usage
IN="$1"; OUT="$2"
[ -f "$IN" ] || { echo "FATAL: input squashfs not found: $IN" >&2; exit 1; }

VOL_SIZE=42156032      # 332 * 126976 (LEB)
FINAL_SIZE=46137344    # 0x2C00000 = full mtd9
SEQ=1734217813         # factory UBI sequence number

for t in unsquashfs mksquashfs ubinize; do
  command -v "$t" >/dev/null 2>&1 || { echo "FATAL: missing tool: $t (apt install squashfs-tools mtd-utils)" >&2; exit 1; }
done

WORK="$(mktemp -d /tmp/ffl-sysb.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
echo "== work: $WORK"

# ---- 1. unpack artifact squashfs ----
# unsquashfs as non-root cannot recreate static device nodes (dev/console etc.)
# and returns non-zero; those nodes are recreated by devtmpfs/mdev at boot, so
# tolerate it. Completeness is enforced by the camera-critical gates in step 3.
unsquashfs -d "$WORK/rootfs" -no-progress "$IN" >/dev/null 2>&1 || true
[ -d "$WORK/rootfs/usr/lib" ] || { echo "FATAL: unsquashfs failed to unpack tree" >&2; exit 1; }
echo "unpacked files: $(find "$WORK/rootfs" -type f | wc -l)"

# ---- 2. trim Firefly generic NPU/AI/GUI demo payload ----
# Deterministic denylist. Nothing camera-critical: rkaiq I(Q) + gst mpp plugin
# + startup_app_ipc + ffmpeg libs + IQ xml are all preserved (gated below).
rm -rf "$WORK/rootfs/npu_arc" \
       "$WORK/rootfs/rockchip_test" \
       "$WORK/rootfs/usr/share/fonts" \
       "$WORK/rootfs/usr/lib/qt"
rm -f  "$WORK/rootfs/usr/lib/libNNVXCBinary.so" \
       "$WORK/rootfs/usr/lib/libVSC.so" \
       "$WORK/rootfs/usr/lib/libNNGPUBinary.so" \
       "$WORK/rootfs/usr/lib/libOvx12VXCBinary.so" \
       "$WORK/rootfs/usr/lib"/libOpenVX.so* \
       "$WORK/rootfs/usr/lib/libneuralnetworks.so" \
       "$WORK/rootfs/usr/lib/librknn_runtime.so" \
       "$WORK/rootfs/usr/lib/libCLC.so" \
       "$WORK/rootfs/usr/lib/libGAL.so" \
       "$WORK/rootfs/usr/lib"/libopencv_*.so* \
       "$WORK/rootfs/usr/lib"/libQt5*.so* \
       "$WORK/rootfs/usr/bin/gdb"

# ---- 3. gates: camera-critical payload must survive the trim ----
for pat in 'librkaiq.so' 'libgstrockchipmpp.so' 'startup_app_ipc' 'libavcodec.so'; do
  find "$WORK/rootfs" -name "$pat" 2>/dev/null | grep -q . \
    || { echo "FATAL: trimmed away camera-critical: $pat" >&2; find "$WORK/rootfs" -name "$pat"; exit 1; }
  echo "  kept: $pat"
done
find "$WORK/rootfs/etc/iqfiles" -name 'sc5338*' 2>/dev/null | grep -q . \
  || { echo "FATAL: SC5338 IQ file missing after trim" >&2; exit 1; }
echo "  kept: SC5338 IQ xml"

# ---- 4. repack squashfs (factory geometry) ----
# NOTE: mksquashfs 4.x may return exit 2 on "create_inode: could not create
# character device dev/console (not superuser)" warnings even though the image
# is written correctly (device nodes are recreated by devtmpfs/mdev at boot).
# Do NOT let pipefail kill us on that warning: check the output file instead.
set +o pipefail
mksquashfs "$WORK/rootfs" "$WORK/rootfs.sqfs" \
  -comp xz -b 131072 -noappend -all-root -no-progress 2>&1 | tail -3
set -o pipefail
[ -s "$WORK/rootfs.sqfs" ] || { echo "FATAL: mksquashfs produced no output" >&2; exit 1; }
SQ=$(stat -c%s "$WORK/rootfs.sqfs")
echo "trimmed squashfs: $SQ bytes ($(awk "BEGIN{printf \"%.1f\",$SQ/1048576}") MiB)"
[ "$SQ" -le "$VOL_SIZE" ] \
  || { echo "FATAL: squashfs $SQ > vol_size $VOL_SIZE" >&2; exit 1; }

# ---- 5. ubinize (geometry is the hard gate) ----
cat > "$WORK/ubinize.cfg" <<EOF
[rootfs]
mode=ubi
image=rootfs.sqfs
vol_id=0
vol_type=dynamic
vol_name=rootfs
vol_alignment=1
vol_size=$VOL_SIZE
EOF
( cd "$WORK" && ubinize -o rootfs.ubi -p 131072 -m 2048 -Q "$SEQ" ubinize.cfg )
UBI=$(stat -c%s "$WORK/rootfs.ubi")
echo "ubi size: $UBI bytes"

# ---- 6. 0xFF tail pad to full mtd9 ----
[ "$UBI" -le "$FINAL_SIZE" ] || { echo "FATAL: ubi $UBI > mtd9 $FINAL_SIZE" >&2; exit 1; }
PAD=$((FINAL_SIZE - UBI))
if [ "$PAD" -gt 0 ]; then
  head -c "$PAD" /dev/zero | tr '\0' '\377' > "$WORK/pad.bin"
  cat "$WORK/rootfs.ubi" "$WORK/pad.bin" > "$WORK/system_b.img"
else
  cp "$WORK/rootfs.ubi" "$WORK/system_b.img"
fi

# ---- 7. output gates ----
F=$(stat -c%s "$WORK/system_b.img")
[ "$F" -eq "$FINAL_SIZE" ] || { echo "FATAL: final size $F != $FINAL_SIZE" >&2; exit 1; }
mkdir -p "$(dirname "$OUT")"
cp "$WORK/system_b.img" "$OUT"
SUM=$(python3 -c "import zlib,sys;print(hex(zlib.crc32(open(sys.argv[1],'rb').read())&0xffffffff))" "$OUT")
echo "OK -> $OUT ($F bytes) crc32=$SUM"
echo -n "tail-16 (must all ff): "; tail -c 16 "$OUT" | od -An -tx1
