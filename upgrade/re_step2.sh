#!/bin/bash
cd /mnt/d/projects/cmcc-s4602-rv1109/out/re_analysis
echo "=== libswkey.so exported FUNCs ==="
arm-linux-gnueabihf-readelf --dyn-syms -W libswkey.so | grep FUNC | grep -v 'UND' | awk '$2 != "00000000" {print $2, $3, $8}'
echo
echo "=== libswkey.so UND imports ==="
arm-linux-gnueabihf-readelf --dyn-syms -W libswkey.so | grep 'UND' | awk '{print $8}' | sort -u | head -50
