#!/bin/bash
# 在 WSL armhf gcc 15.2（64-bit time 默认发行版）上验证 -D_TIME_BITS=32 能否恢复 classic ABI
set -u
cd /tmp && rm -rf tbtest && mkdir tbtest && cd tbtest
SRC=/mnt/d/projects/cmcc-s4602-rv1109/tools/h264out.c
INC=/mnt/d/projects/cmcc-s4602-rv1109/third_party/mpp/inc-1.0.9

echo "== baseline (no flag, expect 64-bit time) =="
arm-linux-gnueabihf-gcc -O2 -std=gnu99 -w -s -o h_base "$SRC" -I"$INC" -ldl -lpthread -lm 2>&1 | head -5
strings h_base | grep -E '^__ioctl_time64$|^__clock_gettime64$' && echo "BASELINE: 64-bit time ABI (bad)" || echo "BASELINE: no time64 symbols (?!)"

echo "== with -D_TIME_BITS=32 =="
arm-linux-gnueabihf-gcc -O2 -std=gnu99 -w -s -D_TIME_BITS=32 -D_FILE_OFFSET_BITS=32 \
  -o h_32 "$SRC" -I"$INC" -ldl -lpthread -lm 2>&1 | head -5
if strings h_32 | grep -qE '^__ioctl_time64$|^__clock_gettime64$'; then
  echo "TIME_BITS=32: FAILED - still links time64"
else
  strings h_32 | grep -qE '^ioctl$' && echo "TIME_BITS=32: OK - classic ioctl symbol present" \
    || echo "TIME_BITS=32: no time64 but ioctl symbol missing"
fi
file h_32 | head -1
