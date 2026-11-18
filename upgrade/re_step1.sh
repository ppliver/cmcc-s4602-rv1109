#!/bin/bash
# RE analysis of recovery binary — dynamic symbols, NEEDED libs, disassembly
R=/mnt/d/projects/cmcc-s4602-rv1109/.stage_extract/vendor_rootfs/squashfs_root/usr/sbin/recovery
OUT=/mnt/d/projects/cmcc-s4602-rv1109/out/re_analysis
mkdir -p "$OUT"
echo "=== NEEDED / dynamic ==="
arm-linux-gnueabihf-readelf -d "$R" | head -20
echo "=== dynsyms ==="
arm-linux-gnueabihf-readelf --dyn-syms -W "$R" > "$OUT/dynsyms.txt"
grep -E 'UND|FUNC' "$OUT/dynsyms.txt" | head -80
echo "=== verify_file disasm ==="
arm-linux-gnueabihf-objdump -d "$R" > "$OUT/disasm.txt" 2>&1
wc -l "$OUT/disasm.txt"
