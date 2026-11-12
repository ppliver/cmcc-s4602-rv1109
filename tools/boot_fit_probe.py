#!/usr/bin/env python3
"""在 U-Boot `=>` 下做非破坏性 RAM 启动验证，完整落盘内核日志（不截断）。

用法:
    python3 tools/boot_fit_probe.py [镜像名] [捕获秒数]

流程:
    1. fatload mmc 1 0x1000000 <镜像>   (整卡 FAT 卷, 无分区表, 不能写 1:1)
    2. crc32 / fdt header 校验 FIT
    3. boot_fit 0x1000000              (从 RAM 启动, 不写 flash)
    4. 全程捕获到 docs/evidence/, 并打印关键行摘要

注意: 启动内核后 U-Boot 会话结束, 内核 panic 会挂死 CPU, 需断电复位才能回到 `=>`.
"""
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import serial

PORT = "COM4"
BAUD = 1500000
ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "evidence"

image = sys.argv[1] if len(sys.argv) > 1 else "zboot.img.s4602"
duration = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0

cmd = (
    "fatload mmc 1 0x1000000 " + image + " ; "
    "crc32 0x1000000 0x339b14 ; "
    "fdt addr 0x1000000 ; fdt header ; "
    "boot_fit 0x1000000"
)

ser = serial.Serial(PORT, BAUD, bytesize=8, parity="N", stopbits=1,
                    timeout=1, xonxoff=False, rtscts=False, dsrdtr=False)
ser.flushInput()
ser.write((cmd + "\r\n").encode("utf-8", errors="replace"))

buf = bytearray()
t0 = time.time()
while time.time() - t0 < duration:
    b = ser.read(4096)
    if b:
        buf.extend(b)
    else:
        time.sleep(0.05)
ser.close()

text = buf.decode("utf-8", errors="replace")
EVIDENCE.mkdir(parents=True, exist_ok=True)
out = EVIDENCE / ("live-bootfit-ram-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".log")
out.write_text(text, encoding="utf-8", errors="replace")
print("saved full log:", out, "bytes:", len(buf))

KEY = re.compile(
    r"Kernel command line|Linux version|Booting Linux|Starting kernel|"
    r"Fatal exception|Kernel panic|Oops|Unhandled fault|Unable to handle|"
    r"Call trace|PC is at|LR is at|bad mode|Internal error|"
    r"ubi[0-9]?:|UBIFS|VFS:|mtdparts|of_parse|OF:|reserved-memory|"
    r"sc5338|rockchip-pinctrl|cpu cpu0|smp|Secondary CPU|Error|failed|Failed",
    re.IGNORECASE,
)
hits = [ln for ln in text.splitlines() if KEY.search(ln)]
print("=== key lines (%d) ===" % len(hits))
for ln in hits[:120]:
    print(ln[:240])
if len(hits) > 120:
    print("... (%d more lines in %s)" % (len(hits) - 120, out.name))
