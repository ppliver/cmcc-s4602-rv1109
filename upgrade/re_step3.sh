#!/bin/bash
cd /mnt/d/projects/cmcc-s4602-rv1109
W=out/re_analysis
S=.stage_extract/vendor_rootfs/squashfs_root/system/bin/swapp
arm-linux-gnueabihf-readelf --dyn-syms -W "$S" > "$W/swapp_dynsyms.txt" 2>&1
echo "=== swapp: signature/upgrade related defined funcs ==="
grep -E 'sw_upgrad|swsignature|sw_update|RSA|SHA|sw_01|verify' "$W/swapp_dynsyms.txt" | grep -v UND
echo "=== objdump ==="
arm-linux-gnueabihf-objdump -d "$S" > "$W/swapp_disasm.txt" 2>&1
wc -l "$W/swapp_disasm.txt"
