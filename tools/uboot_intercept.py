#!/usr/bin/env python3
"""S4602 U-Boot 后台截停器 v3（2026-11-12）。

目的：脚本先挂起在 COM4 上，用户什么时候上电都行，不必再卡着 1~2 秒的
bootdelay 手动按键。截停成功后立即释放串口，交给后续 fatload / boot_fit 命令。

用法:
  python tools/uboot_intercept.py --duration 1800
  python tools/uboot_intercept.py --port COM4 --duration 1800
  python tools/uboot_intercept.py --no-require-fresh   # 打开时已在 '=>' 就直接算成功

状态机:
  ARMED     设备可能断电/静默：持续交替发空格 + Ctrl-C（0.035s），覆盖上电瞬间
  HOLD      打开串口时设备已经停在 U-Boot '=>'（旧会话）：保持静默、不污染命令行，
            等新一轮上电的 DDR/SPL 横幅出现再恢复截停
  CAPTURING 已看到本轮启动横幅：0.03s 高频截停，直到出现 '=>'
  SUCCESS   成功：发一个回车清行、读回显、关闭串口，退出码 0
  TIMEOUT   超时：关闭串口，退出码 2

为什么要有 HOLD：用户常常是「文件准备好了 -> 重新上电让 U-Boot 重新枚举 SD」。
若脚本一看到旧的 '=>' 就退出，上电时反而没人截停了。

实时产物（都在 docs/evidence/）:
  live-uboot-intercept-<ts>.log   原始串口字节，逐帧 flush
  intercept.status.json           状态/字节数/结果，原子替换写入，可随时读取

红线：只发按键和回车，绝不自动执行任何写 flash / 启动命令。
"""
import argparse
import json
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
ap.add_argument("--duration", type=float, default=1800.0,
                help="最长等待秒数，默认 1800（30 分钟）")
ap.add_argument("--no-require-fresh", action="store_true",
                help="打开串口时若已在 '=>' 就立即算成功，不等新一轮上电")
args = ap.parse_args()

TS = datetime.now().strftime("%Y%m%d-%H%M%S")
START_ISO = datetime.now().isoformat(timespec="seconds")
LOG = EVID / ("live-uboot-intercept-" + TS + ".log")
STATUS = EVID / "intercept.status.json"

# 实测横幅形态见 docs/evidence/live-bootfit-full-20261112-165743.log：
# "\x00DDR Version V1.08 20210825\r\n" -> "U-Boot SPL board init" -> "U-Boot 2017.09"
BANNER = (
    "ddr version", "u-boot spl", "u-boot 2017", "spl:", "bootrom",
    "maskrom", "trying to boot from", "hit key to stop autoboot",
    "verified-boot", "op-tee version", "i/tc:",
)
LINUX = ("cmiot login:", "login:", "drop_caches", "starting kernel",
         "kernel panic", "ubifs:")

S = {
    "state": "ARMED",
    "bytes": 0,
    "fresh": False,
    "already": False,
    "last_line": "",
    "note": "",
}


def status(state, **kw):
    d = {
        "state": state,
        "port": args.port,
        "baud": args.baud,
        "started_at": START_ISO,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "log": str(LOG),
        "bytes": S["bytes"],
        "fresh_boot_seen": S["fresh"],
        "already_at_prompt": S["already"],
        "last_line": S["last_line"][-160:],
        "note": S["note"],
        "result": "",
    }
    d.update(kw)
    tmp = STATUS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATUS)


try:
    ser = serial.Serial(args.port, args.baud, bytesize=8, parity="N", stopbits=1,
                        timeout=0.02, write_timeout=0.2, xonxoff=False,
                        rtscts=False, dsrdtr=False)
except Exception as e:
    S["state"] = "PORT_ERROR"
    S["note"] = repr(e)
    status("PORT_ERROR", result="cannot open " + args.port + ": " + repr(e))
    print("[intercept] PORT ERROR:", e, flush=True)
    sys.exit(3)

ser.reset_input_buffer()
ser.reset_output_buffer()
logf = LOG.open("wb")
t0 = time.time()
last_send = 0.0
toggle = 0
interval = 0.035
carry = ""          # 上一帧尾部，用于跨帧匹配关键字
banner_t = None

status("ARMED", note="stop keys armed; power-cycle the device whenever ready")
print(f"[intercept] {args.port} armed for {args.duration:.0f}s -> {LOG.name}", flush=True)

try:
    while time.time() - t0 < args.duration:
        b = ser.read(4096)
        now = time.time()
        if b:
            logf.write(b)
            logf.flush()
            S["bytes"] += len(b)
            txt = b.decode("utf-8", errors="replace").replace("\r", "")
            frame = carry + txt
            carry = frame[-64:]
            low = frame.lower()
            hit_banner = any(k in low for k in BANNER)
            hit_prompt = "=>" in frame
            hit_linux = any(k in low for k in LINUX)
            lines = [x.strip() for x in txt.splitlines() if x.strip()]
            if lines:
                S["last_line"] = lines[-1]
                if hit_banner or hit_prompt:
                    print(f"[rx +{len(b)}] {lines[-1][:120]}", flush=True)

            if hit_banner:
                if not S["fresh"]:
                    S["fresh"] = True
                    S["note"] = "fresh boot banner seen; hammering stop keys"
                    print("[intercept] boot banner -> CAPTURING", flush=True)
                banner_t = now
                S["state"] = "CAPTURING"
                interval = 0.03
                carry = ""
                status("CAPTURING")

            elif S["state"] == "ARMED" and hit_prompt and not S["fresh"]:
                # 打开串口时设备已经停在 U-Boot（或我们的 Ctrl-C 打出了提示符）
                if args.no_require_fresh:
                    S["state"] = "SUCCESS"
                    break
                S["already"] = True
                S["state"] = "HOLD"
                S["note"] = "already at '=>'; silent, waiting for a fresh power cycle"
                carry = ""
                status("HOLD")
                print("[intercept] already at '=>' -> HOLD (silent), waiting for power cycle",
                      flush=True)

            if S["state"] == "CAPTURING" and hit_prompt:
                S["state"] = "SUCCESS"
                break

            if (S["state"] == "CAPTURING" and banner_t
                    and now - banner_t > 25 and hit_linux):
                S["state"] = "ARMED"
                S["fresh"] = False
                banner_t = None
                S["note"] = "WARN: this boot reached Linux, prompt missed; waiting next cycle"
                status("ARMED")
                print("[intercept] WARN: prompt missed, re-arming", flush=True)

            if S["state"] == "ARMED" and now - t0 > 60 and interval < 0.25:
                interval = 0.25
                S["note"] = "idle >60s, lowered key rate; still listening"
                status("ARMED")

        if S["state"] in ("ARMED", "CAPTURING") and now - last_send >= interval:
            ser.write(b"\x03" if toggle % 3 == 0 else b" ")
            toggle += 1
            last_send = now
        time.sleep(0.005)
    else:
        S["state"] = "TIMEOUT"
        S["note"] = "no fresh U-Boot interception within the wait window"
finally:
    if S["state"] == "SUCCESS":
        time.sleep(0.25)
        ser.write(b"\r")           # 只清命令行，不执行任何命令
        time.sleep(0.6)
        b2 = ser.read(4096)
        if b2:
            logf.write(b2)
            logf.flush()
            S["bytes"] += len(b2)
    logf.close()
    ser.close()                    # 关键：释放 COM4 给后续脚本

ok = S["state"] == "SUCCESS"
status(S["state"],
       result=("stopped at U-Boot prompt; COM released" if ok
               else "no fresh interception; COM released"),
       finished_at=datetime.now().isoformat(timespec="seconds"))
print(f"[intercept] {S['state']} bytes={S['bytes']} fresh={S['fresh']} "
      f"already={S['already']} log={LOG}", flush=True)
sys.exit(0 if ok else 2)
