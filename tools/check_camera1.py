# -*- coding: utf-8 -*-
"""验证 S4602 上 go2rtc camera1 的实际输出：
  1) RTSP DESCRIBE -> 看 SDP 里是 H264 还是 H265（WebRTC 兼容性的决定因素）
  2) webrtc POST offer -> 看 go2rtc 是否返回 answer
用法: python tools/check_camera1.py
"""
import socket, sys

HOST = "192.168.6.214"


def rtsp_describe():
    s = socket.create_connection((HOST, 8554), timeout=10)
    req = (
        "DESCRIBE rtsp://%s:8554/camera1 RTSP/1.0\r\n"
        "CSeq: 1\r\n"
        "Accept: application/sdp\r\n"
        "User-Agent: probe\r\n\r\n" % HOST
    )
    s.sendall(req.encode())
    buf = b""
    s.settimeout(8)
    try:
        while b"\r\n\r\n" not in buf and len(buf) < 65536:
            d = s.recv(4096)
            if not d:
                break
            buf += d
    except socket.timeout:
        pass
    s.close()
    return buf.decode(errors="replace")


def rtsp_play_and_read():
    """PLAY 一下并抓 SDP + 少量 RTP，确认真有码流"""
    s = socket.create_connection((HOST, 8554), timeout=10)
    s.sendall(("DESCRIBE rtsp://%s:8554/camera1 RTSP/1.0\r\nCSeq: 1\r\n"
                "Accept: application/sdp\r\n\r\n" % HOST).encode())
    s.settimeout(8)
    buf = b""
    try:
        while b"\r\n\r\n" not in buf:
            d = s.recv(4096)
            if not d:
                break
            buf += d
    except socket.timeout:
        pass
    sdp = buf.decode(errors="replace")
    # 交 interleaved TCP channel
    s.sendall(("SETUP rtsp://%s:8554/camera1/trackID=0 RTSP/1.0\r\nCSeq: 2\r\n"
               "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n\r\n" % HOST).encode())
    s.settimeout(5)
    try:
        setup = s.recv(4096).decode(errors="replace")
    except socket.timeout:
        setup = "(setup timeout)"
    s.sendall(("PLAY rtsp://%s:8554/camera1 RTSP/1.0\r\nCSeq: 3\r\n"
               "Range: npt=0.000-\r\n\r\n" % HOST).encode())
    s.settimeout(6)
    data = b""
    try:
        while len(data) < 65536:
            d = s.recv(65536)
            if not d:
                break
            data += d
    except socket.timeout:
        pass
    s.close()
    return sdp, setup, data


def webrtc_offer():
    sdp = "\r\n".join([
        "v=0",
        "o=- 0 0 IN IP4 127.0.0.1",
        "s=-",
        "t=0 0",
        "a=group:BUNDLE 0",
        "a=msid-semantic: WMS",
        "m=video 9 UDP/TLS/RTP/SAVPF 96",
        "c=IN IP4 0.0.0.0",
        "a=rtcp:9 IN IP4 0.0.0.0",
        "a=ice-ufrag:EsAw",
        "a=ice-pwd:P0123456789012345678901",
        "a=fingerprint:sha-256 11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:"
                "11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00",
        "a=setup:actpass",
        "a=mid:0",
        "a=sendrecv",
        "a=rtcp-mux",
        "a=rtpmap:96 H264/90000",
        "a=fmtp:96 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f",
        "",
    ])
    import urllib.request
    # go2rtc 信令 API 在 api 端口 1984（8555 是 WebRTC 媒体端口，只走加密 RTP，不受理 HTTP）
    req = urllib.request.Request(
        "http://%s:1984/api/webrtc?src=camera1" % HOST,
        data=sdp.encode(),
        headers={"Content-Type": "application/sdp"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode(errors="replace")
    except Exception as e:
        body = ""
        if hasattr(e, "read"):
            try:
                body = e.read().decode(errors="replace")
            except Exception:
                pass
        return "ERR: %s" % e, body


if __name__ == "__main__":
    print("=" * 60)
    print("[1] RTSP DESCRIBE camera1")
    print(rtsp_describe())
    print("=" * 60)
    print("[2] RTSP PLAY: 抓 SDP + 码流")
    sdp, setup, data = rtsp_play_and_read()
    print("--- SDP ---")
    print(sdp)
    print("--- SETUP resp ---")
    print(setup)
    print("--- interleaved data bytes: %d ---" % len(data))
    print(repr(data[:120]))
    print("=" * 60)
    print("[3] WebRTC offer (H264)")
    st, body = webrtc_offer()
    print("status:", st)
    print(body[:1500])
