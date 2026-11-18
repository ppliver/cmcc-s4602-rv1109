#!/bin/bash
cd /mnt/d/projects/cmcc-s4602-rv1109/.stage_extract/vendor_rootfs/squashfs_root/system/lib
for L in libswsysclient.so libswmodules.so libswbase.so libswutil.so libswparameter.so libswos.so libswdevinfo.so libmd_share.so libod_share.so; do
  echo "== $L =="
  arm-linux-gnueabihf-readelf --dyn-syms -W "$L" 2>/dev/null | grep -E 'sw_upgrad|swsignature|sw_update' | head -10
done
