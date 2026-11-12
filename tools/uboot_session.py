#!/usr/bin/env python3
"""一次打开 COM4，顺序执行多条 U-Boot 命令（逐条等 '=>'），统一落盘日志。

只用于 U-Boot 提示符下的交互；命令由调用方给出，脚本本身不内置任何
写 flash / 启动命令，避免误触。

用法:
  python tools/uboot_session.py "mmc list" "fatls mmc 1"
  python tools/uboot_session.py --timeout 12 "fatload mmc 1 0x1000000 zboot.img.s4602"
  python tools/uboot_session.py --timeout 30 "boot_fit 0x1000000"   # 不返回 => 时等满超时

输出同时写到 docs/evidence/live-uboot-session-<ts>.log。
"""
import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import serial

ROOT = Path(__file__).resolve().parents[1]
EVID = ROOT / "docs" / "evidence"
EVID.mkdir(parents=True, exist_ok=True)

ap = argparse.ArgumentParser()
ap.add_argument("--port", default="COM4")
ap.add_argument("--baud", type=int, default=1500000)
ap.add_argument("--timeout", type=float, default=10.0,
                help="每条命令最长等待秒数；见到 '=>' 可提前结束")
ap.add_argument("--wait-full", action="store_true",
                help="不因 '=>' 提前结束，等满 --timeout（用于 boot_fit 等不返回的命令）")
ap.add_argument("--tag", default="")
ap.add_argument("cmd", nargs="+")
args = ap.parse_args()

TS = datetime.now().strftime("%Y%m%d-%H%M%S")
LOG = EVID / ("live-uboot-session-" + TS + ("-" + args.tag if args.tag else "") + ".log")
out = []


def emit(s):
    print(s, end="", flush=True)
    out.append(s)


ser = serial.Serial(args.port, args.baud, bytesize=8, parity="N", stopbits=1,
                    timeout=0.05, write_timeout=1.0, xonxoff=False,
                    rtscts=False, dsrdtr=False)
ser.reset_input_buffer()
try:
    # 先清空可能残留的命令行
    ser.write(b"\r")
    time.sleep(0.25)
    ser.read(4096)

    for i, cmd in enumerate(args.cmd):
        emit(f"\n===== [{i + 1}/{len(args.cmd)}] {cmd} =====\n")
        ser.write((cmd + "\r").encode("utf-8", errors="replace"))
        buf = bytearray()
        t0 = time.time()
        while time.time() - t0 < args.timeout:
            b = ser.read(4096)
            if b:
                buf.extend(b)
                emit(b.decode("utf-8", errors="replace"))
            else:
                time.sleep(0.02)
            # 检测要放在每轮循环里：数据帧可能在 0.3s 内一次性收完，
            # 之后再无新字节，若只在收到数据时判断就永远不会提前结束。
            # U-Boot 执行完会打印 '=> '；回显的命令文本不含它。
            if (not args.wait_full and b"=>" in bytes(buf[-160:])
                    and time.time() - t0 > 0.3):
                break
        emit(f"\n----- [{i + 1}] done, {len(buf)} B, {time.time() - t0:.2f}s -----\n")
finally:
    ser.close()

LOG.write_text("".join(out), encoding="utf-8", errors="replace")
print(f"\n[saved] {LOG}")
