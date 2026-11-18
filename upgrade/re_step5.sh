#!/bin/bash
cd /mnt/d/projects/cmcc-s4602-rv1109
W=out/re_analysis
S=.stage_extract/vendor_rootfs/squashfs_root/system/lib/libswmodules.so
arm-linux-gnueabihf-readelf --dyn-syms -W "$S" > "$W/swmodules_dynsyms.txt" 2>&1
echo "=== NEEDED ==="
arm-linux-gnueabihf-readelf -d "$S" | grep NEEDED
echo "=== crypto-related imports ==="
grep -E 'UND' "$W/swmodules_dynsyms.txt" | grep -Ei 'rsa|sha|md5|evp|ssl|bn_|aes|hmac|sign' | head -20
echo "=== all defined funcs (sorted) ==="
grep -E 'FUNC' "$W/swmodules_dynsyms.txt" | grep -v UND | awk '$2 != "00000000"' | sort -k2 | head -60
echo "=== disasm ==="
arm-linux-gnueabihf-objdump -d "$S" > "$W/swmodules_disasm.txt" 2>&1
wc -l "$W/swmodules_disasm.txt"
