#!/bin/bash
# Rebuild S4602 system_b hook image v2 (SD-upgrade bootstrap).
# Fully self-contained: derives EVERYTHING from the committed factory image
# upgrade/factory/system_b.img (crc32 0x38df701f, pulled from mtd9 backup).
# Runs on: WSL (local) and ubuntu-latest (GitHub Actions). Needs:
#   mksquashfs / unsquashfs (squashfs-tools), ubinize (mtd-utils),
#   ubireader_extract_images (pip install ubi-reader)
#
# Geometry (MUST match factory; reserved_pebs=352 variant = boot panic):
#   mksquashfs -comp xz -b 131072
#   ubinize -p 131072 -m 2048 -Q 1734217813, dynamic vol id=0, vol_size=42156032
#     -> reserved_pebs=332
#   final size 46137344 (0x2C00000, full mtd9), 0xFF tail padding.
# Reference output crc32: 0x45bcc5e9 (2026-11-18, WSL squashfs-tools).
# NOTE: different squashfs-tools versions may produce non-identical bytes;
# correctness is gated on size/tail/UBI-geometry, not byte identity.
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"   # pip --user console scripts (ubireader_*)

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FACTORY_IMG="$ROOT/upgrade/factory/system_b.img"
NEW_S99="$ROOT/upgrade/hook-src/S99local.sh"
OUT="$ROOT/upgrade/s4602_upgrade/flash/system_b.img"

WORK="$(mktemp -d /tmp/s4602_hook_v2.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
VOL_SIZE=42156032      # 332 * 126976 (LEB)
FINAL_SIZE=46137344    # 0x2C00000

[ -f "$FACTORY_IMG" ] || { echo "FATAL: factory image missing: $FACTORY_IMG"; exit 1; }
F_CRC=$(python3 -c "import zlib,sys;print(hex(zlib.crc32(open(sys.argv[1],'rb').read())&0xffffffff))" "$FACTORY_IMG")
echo "factory image: $FACTORY_IMG crc32=$F_CRC (expect 0x38df701f)"
[ "$F_CRC" = "0x38df701f" ] || echo "WARN: factory crc mismatch - source changed"

# ---- 1. extract factory UBI volume (squashfs bytes) ----
ubireader_extract_images -o "$WORK/ubi_out" "$FACTORY_IMG" >/dev/null 2>&1
SQ_IN=$(find "$WORK/ubi_out" -name "*vol-rootfs*" -type f | head -1)
[ -n "$SQ_IN" ] || { echo "FATAL: no rootfs volume in factory image"; find "$WORK/ubi_out" -type f; exit 1; }
echo "factory volume: $SQ_IN ($(stat -c%s "$SQ_IN") bytes)"

# ---- 2. unsquashfs to staging tree ----
unsquashfs -d "$WORK/rootfs" -no-progress "$SQ_IN" >/dev/null
FCOUNT=$(find "$WORK/rootfs" -type f | wc -l)
echo "factory tree: $FCOUNT files"

# ---- 3. inject bootstrap hook ----
[ -f "$WORK/rootfs/etc/init.d/S99local.sh" ] && { echo "FATAL: factory tree already has S99local"; exit 1; }
[ -f "$WORK/rootfs/system/bin/swapp" ] || { echo "FATAL: factory swapp missing"; exit 1; }
install -m 755 "$NEW_S99" "$WORK/rootfs/etc/init.d/S99local.sh"
sh -n "$WORK/rootfs/etc/init.d/S99local.sh"

# ---- 4. repack squashfs ----
mksquashfs "$WORK/rootfs" "$WORK/rootfs.sqfs" -comp xz -b 131072 -noappend -all-root \
    -no-progress -quiet 2>&1 | tail -2
SQ=$(stat -c%s "$WORK/rootfs.sqfs")
echo "squashfs size: $SQ (must be <= $VOL_SIZE)"
[ "$SQ" -le "$VOL_SIZE" ] || { echo "FATAL: squashfs exceeds vol_size"; exit 1; }

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
( cd "$WORK" && ubinize -o rootfs.ubi -p 131072 -m 2048 -Q 1734217813 ubinize.cfg )
UBI=$(stat -c%s "$WORK/rootfs.ubi")
echo "ubi size: $UBI"

# ---- 6. 0xFF tail pad to full mtd9 size ----
if [ "$UBI" -gt "$FINAL_SIZE" ]; then echo "FATAL: ubi exceeds mtd9"; exit 1; fi
PAD=$((FINAL_SIZE - UBI))
if [ "$PAD" -gt 0 ]; then
    head -c "$PAD" /dev/zero | tr '\0' '\377' > "$WORK/pad.bin"
    cat "$WORK/rootfs.ubi" "$WORK/pad.bin" > "$WORK/system_b_v2.img"
else
    cp "$WORK/rootfs.ubi" "$WORK/system_b_v2.img"
fi
F=$(stat -c%s "$WORK/system_b_v2.img")
[ "$F" -eq "$FINAL_SIZE" ] || { echo "FATAL: final size $F != $FINAL_SIZE"; exit 1; }

mkdir -p "$(dirname "$OUT")"
cp "$WORK/system_b_v2.img" "$OUT"
OUT_CRC=$(python3 -c "import zlib,sys;print(hex(zlib.crc32(open(sys.argv[1],'rb').read())&0xffffffff))" "$OUT")
echo "OK -> $OUT ($F bytes) crc32=$OUT_CRC (reference 0x45bcc5e9)"
# tail bytes must be 0xFF
tail -c 16 "$OUT" | od -An -tx1
