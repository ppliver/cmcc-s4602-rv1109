/*
 * rtp2pipe.c — RTP -> Annex-B bitstream converter for go2rtc exec source.
 *
 * Listens on UDP :<port> for the h264out mirror stream (PT96 H.264/RFC3984
 * or PT97 H.265/RFC7798, both FU-fragmented), depacketizes and writes a raw
 * Annex-B H.264/H.265 bitstream to stdout. go2rtc exec source (pipe mode)
 * parses this directly:  streams: camera1: exec:/data/www/rtp2pipe 5602
 *
 * Handles: single NAL, FU-A (H264), FU (H265), STAP-A (H264 aggregation).
 * Dedups the duplicated-IDR packets (h264out --dup_idr sends same seq twice).
 * Skips fragments joined mid-frame until the next Start-bit fragment.
 *
 * Build: python tools/armbuild.py rtp2pipe.c rtp2pipe --no-deploy  (static)
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <signal.h>
#include <unistd.h>
#include <errno.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>

#define MAXNAL (2 * 1024 * 1024)
static volatile sig_atomic_t g_stop = 0;
static void on_sig(int s) { (void)s; g_stop = 1; }

static uint8_t nal[MAXNAL];
static size_t  nal_len = 0;
static int     nal_open = 0;          /* fragment assembly in progress */
/* go2rtc magic/bitstream probe requires the FIRST NAL of the stream to be
 * VPS (h265, type 32) or SPS (h264, type 7). We join the RTP stream at an
 * arbitrary point, so hold all output until the first parameter set passes
 * by (worst case one GOP, ~1s at gop=25). */
static int     g_ready = 0;

static int     g_gap = 0;              /* RTP seq discontinuity seen */
static int     nal_broken = 0;         /* current reassembled NAL lost fragments */
static unsigned long stats_drops = 0;  /* reassembled NALs dropped (loss) */

static int seq_bad(uint16_t seq) {     /* returns 1 if duplicate (skip) */
    static uint16_t last = 0; static int have = 0;
    if (have) {
        if (seq == last) return 1;
        if ((uint16_t)(last + 1) != seq) g_gap = 1;
    }
    last = seq; have = 1;
    return 0;
}

static void emit_single(const uint8_t *p, size_t n) {
    static const uint8_t sc[4] = {0,0,0,1};
    if (n == 0 || n + 4 > MAXNAL) return;
    if (!g_ready) {
        unsigned t265 = (p[0] >> 1) & 0x3f;
        unsigned t264 = p[0] & 0x1f;
        if (t265 == 32 || t264 == 7) g_ready = 1;
        else return;
    }
    fwrite(sc, 1, 4, stdout);
    fwrite(p, 1, n, stdout);
}

static void fu_begin(const uint8_t *p, size_t n, int is_h265) {
    if (nal_len) nal_len = 0;           /* resync: drop unfinished frame */
    static const uint8_t sc[4] = {0,0,0,1};
    memcpy(nal, sc, 4); nal_len = 4;
    nal[nal_len++] = p[0];
    if (is_h265) nal[nal_len++] = p[1]; /* H265 keeps 2-byte NAL header */
    nal_open = 1;
    nal_broken = g_gap; g_gap = 0;      /* gap before S -> missing start */
}

static void fu_cont(const uint8_t *p, size_t n, int is_h265) {
    size_t off = is_h265 ? 3 : 2;       /* PayloadHdr(+1B FU hdr for H265) */
    if (!nal_open) return;
    if (nal_len + (n - off) > MAXNAL) { nal_len = 0; nal_open = 0; return; }
    memcpy(nal + nal_len, p + off, n - off);
    nal_len += n - off;
}

static void fu_end(const uint8_t *p, size_t n, int is_h265) {
    fu_cont(p, n, is_h265);
    nal_open = 0;
    if (g_gap) nal_broken = 1;          /* gap among fragments */
    g_gap = 0;
    if (g_ready && nal_len > 4 && !nal_broken) fwrite(nal, 1, nal_len, stdout);
    else if (nal_broken) stats_drops++;
    nal_len = 0;
}

static void handle_h264(const uint8_t *p, size_t n) {
    if (n < 1) return;
    unsigned t = p[0] & 0x1f;
    if (t >= 1 && t <= 23) { emit_single(p, n); return; }
    if (t == 24) {                       /* STAP-A */
        size_t i = 1;
        while (i + 2 <= n) {
            uint16_t sz = (p[i] << 8) | p[i+1]; i += 2;
            if (sz == 0 || i + sz > n) break;
            emit_single(p + i, sz); i += sz;
        }
        return;
    }
    if (t == 28) {                       /* FU-A */
        uint8_t fh = p[1];
        if (fh & 0x80) {                              /* S */
            fu_begin(p, n, 0);
            nal[nal_len-1] = (p[0] & 0xe0) | (fh & 0x1f);
            fu_cont(p, n, 0);
        }
        else if (fh & 0x40) fu_end(p, n, 0);
        else fu_cont(p, n, 0);
    }
    /* other types (7,8,6 arrive as single NALs anyway) */
}

static void handle_h265(const uint8_t *p, size_t n) {
    if (n < 2) return;
    unsigned t = (p[0] >> 1) & 0x3f;
    if (t < 32) { emit_single(p, n); return; }        /* slice NAL unit */
    if (t >= 32 && t <= 40) { emit_single(p, n); return; } /* VPS/SPS/PPS/SEI/AUD */
    if (t == 48) {                                    /* STAP: hdr + [size + NAL] */
        size_t i = 2;
        while (i + 2 <= n) {
            uint16_t sz = (p[i] << 8) | p[i+1]; i += 2;
            if (sz == 0 || i + sz > n) break;
            emit_single(p + i, sz); i += sz;
        }
        return;
    }
    if (t == 49) {                                    /* FU */
        uint8_t fh = p[2];
        if (fh & 0x80) {                              /* S */
            fu_begin(p, n, 1);
            nal[nal_len-2] = (p[0] & 0x81) | ((fh & 0x1f) << 1);
            nal[nal_len-1] = p[1];
            fu_cont(p, n, 1);
        } else if (fh & 0x40) fu_end(p, n, 1);
        else fu_cont(p, n, 1);
    }
}

int main(int argc, char **argv) {
    int port = (argc > 1) ? atoi(argv[1]) : 5602;
    signal(SIGTERM, on_sig); signal(SIGINT, on_sig); signal(SIGPIPE, SIG_IGN);
    setvbuf(stdout, NULL, _IONBF, 0);

    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) { perror("socket"); return 1; }
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    struct sockaddr_in a; memset(&a, 0, sizeof a);
    a.sin_family = AF_INET;
    a.sin_addr.s_addr = htonl(INADDR_ANY);
    a.sin_port = htons((uint16_t)port);
    if (bind(fd, (struct sockaddr *)&a, sizeof a) < 0) {
        fprintf(stderr, "bind %d: %s\n", port, strerror(errno));
        return 1;
    }
    fprintf(stderr, "rtp2pipe: listening udp:%d\n", port);
    {
        int rcvb = 512 * 1024;
        setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &rcvb, sizeof rcvb);
    }

    uint8_t buf[2048];
    unsigned long stats_pkts = 0, stats_bytes = 0;
    while (!g_stop) {
        ssize_t r = recvfrom(fd, buf, sizeof buf, 0, NULL, NULL);
        if (r < 12) continue;
        uint8_t pt = buf[1] & 0x7f;
        uint16_t seq = (uint16_t)((buf[2] << 8) | buf[3]);
        if (seq_bad(seq)) continue;                    /* dup_idr copies */
        const uint8_t *pl = buf + 12;
        size_t pn = (size_t)r - 12;
        stats_pkts++; stats_bytes += (unsigned long)r;
        if (pt == 96)      handle_h264(pl, pn);
        else if (pt == 97) handle_h265(pl, pn);
        if ((stats_pkts & 0x3fff) == 0)
            fprintf(stderr, "rtp2pipe: %lu pkts %.1f MB drops:%lu\n",
                    stats_pkts, stats_bytes / 1048576.0, stats_drops);
    }
    close(fd);
    fprintf(stderr, "rtp2pipe: exit (%lu pkts)\n", stats_pkts);
    return 0;
}
