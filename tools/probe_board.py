#!/usr/bin/env python3
"""S4602 串口只读探针。

只发只读命令，不写 flash、不改 U-Boot 环境、不重启设备。
支持本地 COM 口与 TCP 串口桥。用法：

    python tools/probe_board.py --port COM3 --baud 1500000
    python tools/probe_board.py --tcp 192.168.6.195:2000 --baud 115200
    python tools/probe_board.py --dry-run          # 只打印将执行的命令

前置：你已经在串口终端里手动打断启动、停在 "=>" 提示符。
脚本不会替你按回车打断启动（那需要精确时序，人工更可靠）。

依赖：本地 COM 口需要 pyserial（pip install pyserial）；TCP 模式无额外依赖。
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"

READONLY_COMMANDS = [
    "version",
    "printenv",
    "help",
    "bdinfo",
    "coninfo",
    "mtd list",
    "mtdparts",
    "nand info",
    "ubi info",
    "sf probe",
]

KEYS = [
    "bootcmd", "bootargs", "mtdparts", "ethaddr", "ipaddr", "serverip",
    "gatewayip", "netmask", "bootdelay", "fdt_addr", "kernel_addr_r",
]


class Transport:
    def __init__(self, args):
        self.buf = b""
        self.sock = None
        self.ser = None
        if args.tcp:
            import socket
            host, port = args.tcp.rsplit(":", 1)
            self.sock = socket.create_connection((host, int(port)), timeout=args.timeout)
            self.sock.settimeout(0.3)
        elif args.port:
            try:
                import serial
            except ImportError:
                print("需要 pyserial: pip install pyserial", file=sys.stderr)
                sys.exit(2)
            self.ser = serial.Serial(args.port, args.baud, timeout=0.3)

    def send(self, line: str):
        data = (line + "\r\n").encode()
        if self.sock:
            self.sock.sendall(data)
        elif self.ser:
            self.ser.write(data)

    def read(self, seconds: float) -> str:
        end = time.time() + seconds
        out = bytearray()
        while time.time() < end:
            try:
                chunk = self.sock.recv(4096) if self.sock else (self.ser.read(4096) if self.ser else b"")
            except Exception:
                chunk = b""
            if chunk:
                out += chunk
            else:
                time.sleep(0.05)
        return out.decode("utf-8", "replace")

    def close(self):
        for x in (self.sock, self.ser):
            if x:
                try:
                    x.close()
                except Exception:
                    pass


def parse_env(text: str) -> dict:
    env = {}
    for key in KEYS:
        m = re.search(rf"^{re.escape(key)}=(.*)$", text, flags=re.M)
        if m:
            env[key] = m.group(1).strip()
    parts = re.findall(r"mtdparts[^\r\n]*", text)
    if parts:
        env["mtdparts_lines"] = parts
    cmds = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line[0].isalnum() and len(line.split()) == 1:
            cmds.append(line)
    if "help" in text:
        for cmd in ("tftp", "tftpboot", "nand", "spinand", "mtd", "ubi",
                    "ubifsload", "ubifsls", "rockusb", "bootm", "booti"):
            if re.search(rf"\b{re.escape(cmd)}\b", text):
                cmds.append(cmd)
    env["commands_seen"] = sorted(set(cmds))
    return env


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--port", help="本地串口，如 COM3")
    g.add_argument("--tcp", help="TCP 串口桥，如 192.168.6.195:2000")
    g.add_argument("--dry-run", action="store_true", help="不连设备，只打印命令清单")
    ap.add_argument("--baud", type=int, default=1500000)
    ap.add_argument("--timeout", type=float, default=3.0, help="每条命令等待秒数")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    if args.dry_run:
        print("只读命令清单（不连接设备）:")
        for c in READONLY_COMMANDS:
            print("  " + c)
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    EVIDENCE.mkdir(exist_ok=True)
    log_path = args.out or (EVIDENCE / f"uboot-{stamp}.log")
    t = Transport(args)
    chunks = []
    try:
        for cmd in READONLY_COMMANDS:
            t.send(cmd)
            out = t.read(args.timeout)
            chunks.append(f"\n===== {cmd} =====\n{out}")
            print(f"[{cmd}] {len(out)} bytes")
    finally:
        t.close()
    text = "".join(chunks)
    log_path.write_text(text, encoding="utf-8")
    env = parse_env(text)
    (EVIDENCE / f"probe-{stamp}.json").write_text(
        json.dumps({"log": str(log_path), "env": env}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"\n日志: {log_path}")
    print("解析出的环境变量/命令:")
    print(json.dumps(env, ensure_ascii=False, indent=2))
    print("\n注意: 若 bootcmd/bootargs/mtdparts 为空，说明命令集不同或没进 U-Boot 控制台。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
