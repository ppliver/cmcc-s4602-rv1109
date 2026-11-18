#!/bin/bash
cd /mnt/d/projects/cmcc-s4602-rv1109/out/re_analysis
echo "=== sw_signature_create wrapper ==="
sed -n '/0000c74c <sw_signature_create@@Base>/,/0000c79/p' swmodules_disasm.txt | grep -E "bl|b\t" | head -5
echo "=== sw_signature_update wrapper ==="
sed -n '/0000c798 <sw_signature_update@@Base>/,/0000c7e/p' swmodules_disasm.txt | grep -E "bl|b\t" | head -5
echo "=== who exports sw_hmac256_* ==="
cd /mnt/d/projects/cmcc-s4602-rv1109/.stage_extract/vendor_rootfs/squashfs_root/system/lib
for L in libswbase.so libswutil.so libswos.so libswparameter.so libswdevinfo.so libswsysclient.so libmd_share.so libod_share.so libswmodules.so; do
  r=$(arm-linux-gnueabihf-readelf --dyn-syms -W "$L" 2>/dev/null | grep "sw_hmac256" | grep -v UND)
  [ -n "$r" ] && echo "-- $L" && echo "$r"
done
echo "=== xrefs to sw_01_13 (0xcebc) in libswmodules ==="
grep -E "bl.*cebc|b\t.*cebc" /mnt/d/projects/cmcc-s4602-rv1109/out/re_analysis/swmodules_disasm.txt | head
