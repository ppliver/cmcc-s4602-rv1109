# -*- coding: utf-8 -*-
"""WebRTC H265 协商验证：offer 含 H265 -> go2rtc answer 是否接受 H265。

与 check_webrtc_e2e.py 的差异仅在 offer 的 codec（H265/90000, PT97），
用于验证「浏览器侧声明 H265 时，go2rtc 侧能否正常协商并建流」。
"""
import sys, time, json, urllib.request

sys.path.insert(0, __file__.rsplit("\\", 1)[0].rsplit("/", 1)[0])
from check_webrtc_e2e import parse_answer, ice_udp_test  # noqa: E402

HOST = "192.168.6.214"
API = "http://%s:1984/api/webrtc?src=camera1" % HOST


def build_offer_h265():
    return "\r\n".join([
        "v=0",
        "o=- 4611731400430051336 2 IN IP4 127.0.0.1",
        "s=-",
        "t=0 0",
        "a=group:BUNDLE 0",
        "a=msid-semantic: WMS",
        "m=video 9 UDP/TLS/RTP/SAVPF 97",
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
        "a=rtpmap:97 H265/90000",
        "a=fmtp:97 profile-id=1;level-id=93;tier-flag=0;tx-mode=SRST",
        "",
    ])


def http_post(path, body, timeout=15):
    req = urllib.request.Request(path, data=body.encode(),
                                 headers={"Content-Type": "application/sdp"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode(errors="replace")


def main():
    print("=" * 60)
    print("[STEP 1] SDP offer (H265) -> go2rtc answer")
    try:
        st, ans = http_post(API, build_offer_h265())
        print("status:", st)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print("status: HTTP", e.code, e.reason)
        print("body:", body.strip())
        return 1

    info = parse_answer(ans)
    print("answer codec   :", info["codec"])
    print("answer ufrag   :", info["ufrag"])
    print("candidates     :", len(info["candidates"]))
    for c in info["candidates"]:
        print("   ", c["proto"], c["ip"] + ":" + str(c["port"]), c["typ"])
    print("has H265 in answer:",
          "H265/90000" in ans, "| fmtp:",
          [l for l in ans.splitlines() if l.startswith("a=fmtp:")])

    print()
    print("[STEP 2] ICE UDP 连通性（go2rtc 8555）")
    ok = False
    for c in info["candidates"]:
        if c["proto"].lower() != "udp":
            continue
        good, msg = ice_udp_test(c, info["ufrag"], info["pwd"])
        print("   cand %s:%d -> %s (%s)" % (c["ip"], c["port"],
                                            "OK" if good else "FAIL", msg))
        ok = ok or good
    print()
    print("RESULT:", "PASS" if (info["codec"] and "H265" in info["codec"] and ok)
          else "PARTIAL/FAIL")
    return 0


if __name__ == "__main__":
    sys.exit(main())
