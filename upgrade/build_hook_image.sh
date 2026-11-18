#!/bin/bash
# Rebuild S4602 system_b hook image v2 (SD-upgrade bootstrap) in WSL.
# Replicates the proven 2026-11-17 procedure byte-geometry-wise:
#   mksquashfs -comp xz -b 131072
#   ubinize -p 131072 -m 2048 -Q 1734217813, dynamic vol id=0,
#   vol_size=42156032 -> reserved_pebs=332 (MUST match factory; 352 = boot panic)
#   final size 46137344 (0x2C00000, full mtd9), 0xFF tail padding.
set -euo pipefail

ROOT=/mnt/d/projects/cmcc-s4602-rv1109
SRC=$ROOT/.stage_extract/vendor_rootfs/squashfs_root
NEW_S99=$ROOT/upgrade/hook-src/S99local.sh
OUT=$ROOT/upgrade/s4602_upgrade/flash/system_b.img

WORK=/tmp/s4602_hook_v2
VOL_SIZE=42156032      # 332 * 126976 (LEB)
FINAL_SIZE=46137344    # 0x2C00000

rm -rf "$WORK"
mkdir -p "$WORK"
cp -a "$SRC" "$WORK/rootfs"

# sanity: pristine factory tree must NOT contain S99local (361 files)
if [ -f "$WORK/rootfs/etc/init.d/S99local.sh" ]; then
    echo "FATAL: factory tree already has S99local"; exit 1
fi

install -m 755 "$NEW_S99" "$WORK/rootfs/etc/init.d/S99local.sh"
sh -n "$WORK/rootfs/etc/init.d/S99local.sh"

mksquashfs "$WORK/rootfs" "$WORK/rootfs.sqfs" -comp xz -b 131072 -noappend -all-root \
    -no-progress -quiet 2>&1 | tail -2
SQ=$(stat -c%s "$WORK/rootfs.sqfs")
echo "squashfs size: $SQ (must be <= $VOL_SIZE)"
[ "$SQ" -le "$VOL_SIZE" ] || { echo "FATAL: squashfs exceeds vol_size"; exit 1; }

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

ubinize -o "$WORK/rootfs.ubi" -p 131072 -m 2048 -Q 1734217813 "$WORK/ubinize.cfg" 2>/dev/null \
  || ( cd "$WORK" && ubinize -o rootfs.ubi -p 131072 -m 2048 -Q 1734217813 ubinize.cfg )
UBI=$(stat -c%s "$WORK/rootfs.ubi")
echo "ubi size: $UBI"

# 0xFF tail pad to full mtd9 size
CUR="$UBI"
if [ "$CUR" -gt "$FINAL_SIZE" ]; then echo "FATAL: ubi exceeds mtd9"; exit 1; fi
PAD=$((FINAL_SIZE - CUR))
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
echo "OK -> $OUT ($F bytes)"
# tail bytes must be 0xFF
tail -c 16 "$OUT" | od -An -tx1
