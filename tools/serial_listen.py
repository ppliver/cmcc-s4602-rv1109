#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S4602 串口监听器（后台常驻）。

- 独占打开 COM4 @1500000 8N1，捕获全部输出 -> docs/evidence/live-serial-<ts>.log
- 轮询 tools/serial_send.txt：一旦存在即把内容逐行发到串口，然后删除该文件
- Ctrl+C / 被杀即退出关闭串口
"""
import sys
import time
from datetime import datetime
from pathlib import Path

import serial

PORT = "COM4"
BAUD = 1500000
ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "evidence"
SEND = ROOT / "tools" / "serial_send.txt"

EVIDENCE.mkdir(parents=True, exist_ok=True)
log = EVIDENCE / ("live-serial-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".log")

ser = serial.Serial(PORT, BAUD, bytesize=8, parity="N", stopbits=1,
                    timeout=0.2, xonxoff=False, rtscts=False, dsrdtr=False)
f = open(log, "a", encoding="utf-8", errors="replace")
print("LISTENER START %s -> %s" % (datetime.now().isoformat(timespec="seconds"), log), flush=True)
f.write("\n===== listener start %s =====\n" % datetime.now().isoformat(timespec="seconds"))

try:
    while True:
        b = ser.read(4096)
        if b:
            f.write(b.decode("utf-8", errors="replace"))
            f.flush()
        # command injection
        try:
            if SEND.exists() and SEND.stat().st_size > 0:
                cmds = SEND.read_text(encoding="utf-8", errors="replace")
                SEND.unlink()
                for line in cmds.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    f.write("\n>>> SEND: %s\n" % line)
                    f.flush()
                    ser.write((line + "\r\n").encode("utf-8", errors="replace"))
                    ser.flush()
                    time.sleep(0.3)
        except Exception as ex:
            f.write("\n[send-err] %s\n" % ex)
            f.flush()
        time.sleep(0.05)
except KeyboardInterrupt:
    pass
finally:
    f.write("\n===== listener stop %s =====\n" % datetime.now().isoformat(timespec="seconds"))
    f.close()
    ser.close()
    print("LISTENER STOP", flush=True)
