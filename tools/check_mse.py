#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
check_mse.py - go2rtc MSE (fMP4 over WebSocket) 协议级检查。

go2rtc 的 MSE 前端通过 ws://host:1984/api/ws?src=<name> 拉取 fMP4 分片。
本脚本手写 RFC6455 客户端（避免依赖 websockets 库），连上后读取二进制帧，
解析 ISO-BMFF box 树，打印：
  - ftyp brand
  - init segment 里的 sample entry fourcc (avc1 / hvc1 / ...)
  - 实际收到的媒体分片数与字节数

用法:
  python tools/check_mse.py [--host 192.168.6.214] [--port 1984] [--src camera1]
                            [--path /api/ws?src=camera1] [--seconds 8]
"""
import argparse
import base64
import json
import os
import socket
import struct
import sys
import time

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_connect(host, port, path, timeout=15):
    s = socket.create_connection((host, port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        "GET %s HTTP/1.1\r\n"
        "Host: %s:%d\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: %s\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n" % (path, host, port, key)
    )
    s.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            raise RuntimeError("handshake closed")
        buf += chunk
    head, rest = buf.split(b"\r\n\r\n", 1)
    if b"101" not in head.split(b"\r\n")[0]:
        raise RuntimeError("handshake failed: %s" % head.split(b"\r\n")[0].decode(errors="replace"))
    return s, rest


def ws_read_frame(s, pending):
    """读一个 websocket 帧 -> (opcode, payload, pending)"""
    def need(n):
        nonlocal pending
        while len(pending) < n:
            chunk = s.recv(65536)
            if not chunk:
                raise RuntimeError("closed")
            pending += chunk
    need(2)
    b0, b1 = pending[0], pending[1]
    opcode = b0 & 0x0F
    masked = b1 & 0x80
    ln = b1 & 0x7F
    off = 2
    if ln == 126:
        need(4)
        ln = struct.unpack(">H", pending[2:4])[0]
        off = 4
    elif ln == 127:
        need(10)
        ln = struct.unpack(">Q", pending[2:10])[0]
        off = 10
    if masked:
        need(off + 4)
        mask = pending[off:off + 4]
        off += 4
    need(off + ln)
    payload = pending[off:off + ln]
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    pending = pending[off + ln:]
    return opcode, payload, pending


def ws_send_text(s, text):
    """发送一个 websocket 文本帧（客户端->服务器，必须加掩码）"""
    payload = text.encode()
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    hdr = bytearray()
    hdr.append(0x81)                      # FIN + opcode=text
    n = len(payload)
    if n < 126:
        hdr.append(0x80 | n)
    elif n < 65536:
        hdr.append(0x80 | 126)
        hdr += struct.pack(">H", n)
    else:
        hdr.append(0x80 | 127)
        hdr += struct.pack(">Q", n)
    s.sendall(bytes(hdr) + mask + masked)


def iter_boxes(buf, start=0, end=None, depth=0, out=None):
    """遍历 ISO-BMFF box，返回 (type, offset, size) 列表"""
    if out is None:
        out = []
    if end is None:
        end = len(buf)
    i = start
    while i + 8 <= end:
        size = struct.unpack(">I", buf[i:i + 4])[0]
        btype = buf[i + 4:i + 8].decode("latin1")
        hdr = 8
        if size == 1:
            if i + 16 > end:
                break
            size = struct.unpack(">Q", buf[i + 8:i + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - i
        if size < hdr or i + size > end:
            break
        out.append((btype, i, size, depth))
        if btype in ("moov", "trak", "mdia", "minf", "stbl", "moof", "traf", "mvex"):
            iter_boxes(buf, i + hdr, i + size, depth + 1, out)
        elif btype in ("stsd",):
            # stsd: version/flags(4) + entry_count(4)
            iter_boxes(buf, i + hdr + 8, i + size, depth + 1, out)
        i += size
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.6.214")
    ap.add_argument("--port", type=int, default=1984)
    ap.add_argument("--src", default="camera1")
    ap.add_argument("--path", default=None)
    ap.add_argument("--seconds", type=float, default=8.0)
    ap.add_argument("--codecs", default="video/mp4; codecs=\"avc1.42001F\"",
                    help="声明支持的 codec（模拟浏览器 MediaSource.isTypeSupported 的结果）")
    args = ap.parse_args()
    path = args.path or ("/api/ws?src=" + args.src)

    print("=" * 60)
    print("[MSE] connect ws://%s:%d%s" % (args.host, args.port, path))
    try:
        s, pending = ws_connect(args.host, args.port, path)
    except Exception as e:
        print("  CONNECT FAILED: %s" % e)
        return 2
    print("  handshake OK (101 Switching Protocols)")

    # go2rtc MSE 流程：客户端必须先发 {type:'mse', value:<codecs>} 协商消息，
    # 服务器才会开始推送 fMP4 二进制帧（见 video-rtc.js onmse()）。
    codecs = args.codecs
    msg = json.dumps({"type": "mse", "value": codecs})
    ws_send_text(s, msg)
    print("  sent negotiate: %s" % msg)

    deadline = time.time() + args.seconds
    frames = 0
    total = 0
    first = None
    text_msgs = []
    try:
        while time.time() < deadline:
            s.settimeout(max(0.2, deadline - time.time()))
            try:
                opcode, payload, pending = ws_read_frame(s, pending)
            except socket.timeout:
                break
            if opcode == 8:
                print("  server CLOSE frame: code=%d reason=%r" % (
                    struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else -1,
                    payload[2:200].decode(errors="replace")))
                break
            if opcode == 1:
                text_msgs.append(payload.decode(errors="replace"))
                continue
            if opcode != 2:
                continue
            frames += 1
            total += len(payload)
            if first is None:
                first = payload
    except Exception as e:
        print("  read error: %s" % e)
    finally:
        try:
            s.close()
        except Exception:
            pass

    print("  binary frames: %d   total bytes: %d" % (frames, total))
    for t in text_msgs[:5]:
        print("  text msg: %s" % t[:200])

    if not first:
        print("  NO DATA received")
        return 1

    print("\n  --- first frame box tree ---")
    boxes = iter_boxes(first)
    for btype, off, size, depth in boxes[:30]:
        print("   %s%-6s size=%d" % ("  " * depth, btype, size))

    ftyp = next((b for b in boxes if b[0] == "ftyp"), None)
    if ftyp:
        brand = first[ftyp[1] + 8:ftyp[1] + 12].decode("latin1")
        print("\n  ftyp brand : %s" % brand)

    entry = next((b for b in boxes if b[0] in ("avc1", "hvc1", "hev1", "avc3", "vp09", "av01")), None)
    if entry:
        print("  sample entry: %s   (avc1=H264 / hvc1|hev1=H265)" % entry[0])
    else:
        print("  sample entry: NOT FOUND in init segment")
    return 0


if __name__ == "__main__":
    sys.exit(main())
