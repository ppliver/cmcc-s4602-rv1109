#!/usr/bin/env python3
"""极简只读 TFTP 服务器，供 U-Boot 2017.09 的 tftpboot 下载镜像到 RAM。

仅实现 RFC 1350 读请求 (RRQ) + DATA/ACK，外加 RFC 2347 选项协商
(blksize / tsize / timeout 的 OACK)。不实现 WRQ —— 设备侧无法反向上传，
全片备份仍走 SD fatwrite。

用法:
  python3 tools/tftp_server.py --root firmware --host 0.0.0.0 --port 69

设计要点:
  - 主 socket 监听 :69 只收 RRQ；每个传输在独立线程里用「新的临时端口」
    发 DATA（TFTP 规范要求），ACK 也回到该临时端口。
  - 每个 DATA 带 2s 超时重传（最多 6 次），避免 U-Boot 侧偶发丢包卡死。
  - 文件名做 basename 处理，禁止路径穿越。
"""
import argparse
import os
import socket
import struct
import sys
import threading
import time

OP_RRQ = 1
OP_DATA = 3
OP_ACK = 4
OP_ERROR = 5
OP_OACK = 6


def log(msg):
    print("[tftp] " + msg, flush=True)


def err(sock, addr, code, text):
    pkt = struct.pack("!HH", OP_ERROR, code) + text.encode() + b"\x00"
    sock.sendto(pkt, addr)


def parse_rrq(pkt):
    parts = pkt[2:].split(b"\x00")
    if len(parts) < 2:
        return None, None, {}
    filename = parts[0].decode("utf-8", "replace")
    mode = parts[1].decode("utf-8", "replace").lower()
    opts = {}
    i = 2
    while i + 1 < len(parts):
        k = parts[i].decode("utf-8", "replace").lower()
        v = parts[i + 1].decode("utf-8", "replace")
        if k:
            opts[k] = v
        i += 2
    return filename, mode, opts


def wait_ack(sock, expect, timeout=2.0, retries=6):
    """等待指定 block 的 ACK；返回 (ok, addr)。"""
    for _ in range(retries):
        sock.settimeout(timeout)
        try:
            pkt, addr = sock.recvfrom(2048)
        except socket.timeout:
            return False, None
        if len(pkt) < 4:
            continue
        op, blk = struct.unpack("!HH", pkt[:4])
        if op == OP_ACK and blk == expect:
            return True, addr
        if op == OP_ERROR:
            log("  client sent ERROR: %r" % pkt[4:])
            return False, addr
    return False, None


def transfer(root, filename, mode, opts, client):
    safe = os.path.basename(filename).replace("\\", "/").split("/")[-1]
    path = os.path.join(root, safe)
    log("RRQ from %s:%d file=%r mode=%r opts=%r -> %s"
        % (client[0], client[1], filename, mode, opts, path))

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))
    local_port = sock.getsockname()[1]
    try:
        if not os.path.isfile(path):
            log("  NOT FOUND")
            err(sock, client, 1, "File not found")
            return
        data = open(path, "rb").read()
        size = len(data)
        log("  serving %d bytes from local UDP port %d" % (size, local_port))

        blksize = 512
        if "blksize" in opts:
            try:
                blksize = max(8, min(int(opts["blksize"]), 1468))
            except ValueError:
                pass
        timeout_s = 2.0
        if "timeout" in opts:
            try:
                timeout_s = max(0.5, min(float(opts["timeout"]), 10.0))
            except ValueError:
                pass

        peer = client
        if opts:
            oack = struct.pack("!H", OP_OACK)
            if "blksize" in opts:
                oack += b"blksize\x00" + str(blksize).encode() + b"\x00"
            if "tsize" in opts:
                oack += b"tsize\x00" + str(size).encode() + b"\x00"
            if "timeout" in opts:
                oack += b"timeout\x00" + str(int(timeout_s)).encode() + b"\x00"
            sock.sendto(oack, peer)
            ok, peer = wait_ack(sock, 0, timeout=timeout_s)
            if not ok:
                log("  no ACK for OACK, abort")
                return
            log("  OACK acked, blksize=%d timeout=%.1fs" % (blksize, timeout_s))

        total_blocks = (size + blksize - 1) // blksize
        blk = 1
        off = 0
        while off < size or (size % blksize == 0 and blk == total_blocks + 1):
            chunk = data[off:off + blksize]
            off += len(chunk)
            pkt = struct.pack("!HH", OP_DATA, blk % 65536) + chunk
            sent_ok = False
            for attempt in range(6):
                sock.sendto(pkt, peer)
                ok, peer2 = wait_ack(sock, blk % 65536, timeout=timeout_s, retries=1)
                if ok:
                    peer = peer2
                    sent_ok = True
                    break
                log("  retransmit block %d (attempt %d)" % (blk, attempt + 1))
            if not sent_ok:
                log("  giving up at block %d" % blk)
                return
            if blk % 200 == 0 or blk == total_blocks:
                log("  block %d/%d (%d/%d bytes)" % (blk, total_blocks, off, size))
            if len(chunk) < blksize:
                break
            blk += 1
        log("  DONE %s (%d bytes, %d blocks)" % (safe, size, blk))
    finally:
        sock.close()


def main():
    ap = argparse.ArgumentParser(description="Minimal read-only TFTP server for U-Boot")
    ap.add_argument("--root", default="firmware", help="directory served (default: firmware)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=69)
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        raise SystemExit("ERROR: root not found: %s" % root)
    log("root=%s" % root)
    for fn in sorted(os.listdir(root)):
        p = os.path.join(root, fn)
        if os.path.isfile(p):
            log("  %-28s %d bytes" % (fn, os.path.getsize(p)))

    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((args.host, args.port))
    except OSError as e:
        raise SystemExit("ERROR: cannot bind UDP %d: %s" % (args.port, e))
    log("listening on %s:%d (UDP) - waiting for U-Boot RRQ ..." % (args.host, args.port))

    while True:
        try:
            pkt, client = srv.recvfrom(2048)
        except KeyboardInterrupt:
            break
        except OSError as e:
            log("recv error: %s" % e)
            continue
        if len(pkt) < 4:
            continue
        opcode = struct.unpack("!H", pkt[:2])[0]
        if opcode != OP_RRQ:
            log("ignored opcode %d from %s" % (opcode, client[0]))
            continue
        filename, mode, opts = parse_rrq(pkt)
        if filename is None:
            continue
        t = threading.Thread(target=transfer, args=(root, filename, mode, opts, client), daemon=True)
        t.start()


if __name__ == "__main__":
    main()
