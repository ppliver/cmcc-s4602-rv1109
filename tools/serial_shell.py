#!/usr/bin/env python3
"""OpenIPC serial shell runner: optionally log in, run a script file, dump raw log.

Sends `<cmd> ; echo __ZZZ_DONE__` after each command and breaks early when the
marker appears, so runs are fast instead of waiting the full per-command timeout.

Usage:
  python tools/serial_shell.py --script tools/diag_net.txt --log docs/evidence/serial-shell.log
"""
import argparse, serial, time
from pathlib import Path

MARK = "__ZZZ_DONE__"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM4")
    ap.add_argument("--baud", type=int, default=1500000)
    ap.add_argument("--user", default="root")
    ap.add_argument("--password", default="admin888")
    ap.add_argument("--login-timeout", type=float, default=8.0)
    ap.add_argument("--cmd-timeout", type=float, default=15.0)
    ap.add_argument("--script", required=True)
    ap.add_argument("--log", default="docs/evidence/serial-shell.log")
    args = ap.parse_args()

    ser = serial.Serial(args.port, args.baud, timeout=0.15)
    time.sleep(0.4)
    cap = bytearray()

    def read_some():
        b = ser.read(4096)
        if b:
            cap.extend(b)
        return b

    def drain(t):
        t0 = time.time()
        while time.time() - t0 < t:
            if not read_some():
                time.sleep(0.02)

    # probe current state
    ser.write(b"\r")
    drain(1.2)
    txt = cap.decode("utf-8", "replace")
    if "login:" in txt and "Password:" not in txt:
        ser.write((args.user + "\r").encode())
        drain(1.0)
        ser.write((args.password + "\r").encode())
        drain(2.0)
    drain(0.5)

    cmds = [l.strip() for l in open(args.script, encoding="utf-8")
            if l.strip() and not l.lstrip().startswith("#")]
    for cmd in cmds:
        ser.write((cmd + " ; echo " + MARK + "\r").encode())
        t0 = time.time()
        while time.time() - t0 < args.cmd_timeout:
            b = read_some()
            if b and MARK in b.decode("utf-8", "replace"):
                break
            if not b:
                time.sleep(0.02)
        time.sleep(0.1)

    ser.close()
    Path(args.log).write_bytes(cap)
    print("OK wrote", args.log, len(cap), "bytes")


if __name__ == "__main__":
    main()
