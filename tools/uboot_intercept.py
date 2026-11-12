#!/usr/bin/env python3
"""等上电 -> 截停 autoboot -> 停在 U-Boot '=>'，不自动执行任何写/启动命令。

用法: python3 tools/uboot_intercept.py [等待秒数]
  python3 tools/uboot_intercept.py 420

截停策略（2026-11-12 实机校准）:
  - bootdelay 仅 1 秒，启动横幅一帧约 2726 B；按「累积文本最后 600 字符」
    检测会漏掉头部 DDR Version，导致一次按键都没发（v1 的 bug）。
  - 改为在**每一帧字节**里找横幅；命中后每 0.06s 发一次空格/CTRL+C，
    1 秒倒计时内有 ~16 次机会。未命中时不发字节，不骚扰已运行的 Linux getty。
  - 看到 '=>' 即停手，发回车清空命令行缓冲后退出，释放 COM4 给后续命令。

日志落盘到 docs/evidence/（用脚本自身相对路径，避免 Windows /tmp 解析歧义）。
"""
import sys
import time
from datetime import datetime
from pathlib import Path

import serial

PORT = "COM4"
BAUD = 1500000
ROOT = Path(__file__).resolve().parents[1]
EVID = ROOT / "docs" / "evidence"
DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 160.0

ser = serial.Serial(PORT, BAUD, bytesize=8, parity="N", stopbits=1,
                    timeout=0.15, xonxoff=False, rtscts=False, dsrdtr=False)
ser.reset_input_buffer()
buf = bytearray()
booting = False
prompt = False
t0 = time.time()
last_send = 0.0
toggle = 0

print(f"[intercept] listening on {PORT} for {DURATION:.0f}s; power-cycle the device now", flush=True)
try:
    while time.time() - t0 < DURATION:
        b = ser.read(4096)
        if b:
            buf.extend(b)
            prev = b.decode("utf-8", errors="replace").replace("\r", "\\r")
            print(f"[rx +{len(b)}] {prev[:200]}", flush=True)
            low = b.decode("utf-8", errors="replace").lower()
            if not booting and any(k in low for k in ("ddr version", "u-boot spl", "spl:", "bootrom", "maskrom")):
                booting = True
                print("[intercept] boot banner seen -> start spamming stop keys", flush=True)
            if booting and "=>" in buf.decode("utf-8", errors="replace")[-600:]:
                prompt = True
                break
            if booting and "booting fit image" in low:
                print("[intercept] WARN: 已开始加载 FIT，本次未截住；继续等待下一次上电", flush=True)
                booting = False
        now = time.time()
        if booting and now - last_send > 0.06:
            ser.write(b"\x03" if toggle % 3 == 0 else b" ")
            ser.flush()
            toggle += 1
            last_send = now
        time.sleep(0.01)

    if prompt:
        time.sleep(0.4)
        ser.write(b"\r\n")
        ser.flush()
        time.sleep(0.6)
        b2 = ser.read(4096)
        if b2:
            buf.extend(b2)
finally:
    ser.close()

EVID.mkdir(parents=True, exist_ok=True)
out = EVID / ("live-uboot-intercept-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".log")
out.write_bytes(bytes(buf))
print(f"[intercept] done. bytes={len(buf)} prompt_seen={prompt}")
print(f"[intercept] saved -> {out}")
