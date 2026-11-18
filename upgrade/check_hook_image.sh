#!/bin/bash
Q=/mnt/d/projects/cmcc-s4602-rv1109/out/sd_upgrade_work/hook_v2_img/system_b.img/img-1734217813_vol-rootfs.ubifs
echo "--- file type:"; file "$Q"
echo "--- total files:"; unsquashfs -ll "$Q" 2>/dev/null | grep -c '^-'
echo "--- key files:"; unsquashfs -ll "$Q" 2>/dev/null | grep -E 'S99local|bin/swapp$|librkaiq|resetPasswd' 
echo "--- S99local perms:"; unsquashfs -ll "$Q" 2>/dev/null | grep 'S99local'
