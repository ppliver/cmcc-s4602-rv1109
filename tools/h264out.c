/* h264out.c -- S4602 / RV1109 direct hardware H.264/H.265 RTP sender.
 *
 * Two selectable frame sources (--src raw|nv12):
 *
 * A) --src raw  (DEFAULT, the one that actually produces a picture)
 *   /dev/video0 rkcif : 2880x1616 BG10 MIPI RAW10 packed @ ~26 fps
 *     -> fast Bayer box-binning + grey-world WB + gamma -> NV12 (WxH)
 *     -> MPP normal frame buffer (CPU-written)
 *     -> MPP raw MPI encode_put_frame / encode_get_packet (RKVENC hardware)
 *     -> Annex-B NALs -> RFC 3984 (H.264/PT96) or RFC 7798 (H.265/PT97)
 *     -> UDP straight to the QGroundControl host
 *
 * B) --src nv12 (legacy, retained for reference)
 *   rkisp -> rkispp_scale0 /dev/video19 : NV12 -> VIDIOC_EXPBUF dma-buf
 *     -> mpp_buffer_import_with_tag(MPP_BUFFER_TYPE_EXT_DMA) -> zero copy.
 *   DO NOT USE on this OpenIPC build: the vendor kernel enables the
 *   "rkisp-isp-subdev -> rkisp-bridge-ispp" media link at boot, while
 *   rkispp_hw fails to map its registers ("can't request region for
 *   resource" / "max input:0x0@0fps"). rkispp then emits 25 fps of ALL-ZERO
 *   NV12. Even after disabling the bridge link and enabling mainpath
 *   (linksetup /dev/media1 1 2 13 0 0 ; 1 2 17 0 1), /dev/video5 delivers
 *   frames whose Y plane is still entirely 0. This is a kernel/DT defect,
 *   not fixable from userspace. See docs/MPP_H264.md.
 *
 * Why the raw MPP MPI and not easymedia: this board's libeasymedia build
 * always fails VENC_CreateChn (-33, "KEY_FPS is null") and VI_EnableChn
 * (-11, "MPLANE not support DMA Buffer yet"). MPP is its next layer down and
 * works fine.
 *
 * IMPORTANT: the rkaiq IQ daemon must NOT run in --src raw mode. It grabs
 * the rkcif pipeline and makes VIDIOC_REQBUFS on /dev/video0 fail.
 *
 * Requirements on the device:
 *   /tmp/factory/lib/librockchip_mpp.so.1 (+ LD_LIBRARY_PATH)
 *
 * Build (cross, dynamic, dlopen - no link against ARM libs):
 *   python tools/armbuild.py h264out.c h264out --dynamic \
 *     --inc out/reference/mpp/inc-1.0.9 --libs=-ldl
 *
 * Run:
 *   LD_LIBRARY_PATH=/tmp/factory/lib /tmp/h264out \
 *     --src raw --dev /dev/video0 --ip 192.168.6.88 --port 5600 \
 *     --codec h264 --w 640 --h 360 --fps 15 --kbps 900 --frames 0
 *
 * QGC: Video Source = "UDP h.264"  udp://0.0.0.0:5600
 *                or  "UDP h.265"  udp265://0.0.0.0:5600
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <pthread.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <linux/videodev2.h>

/* Cortex-A7 NEON: RAW10 unpack + box bin vectorisation.  Compiled in only
 * when the toolchain is invoked with -mfpu=neon[-vfpv4]; a scalar fallback
 * keeps the same source buildable without NEON. */
#if defined(__ARM_NEON__) || defined(__ARM_NEON)
#include <arm_neon.h>
#define H264OUT_NEON 1
#endif

#include "rk_mpi.h"
#include "rk_mpi_cmd.h"
#include "mpp_frame.h"
#include "mpp_buffer.h"
#include "mpp_packet.h"
#include "rk_venc_cfg.h"
#include "rk_venc_rc.h"

#define LIB "/tmp/factory/lib/librockchip_mpp.so.1"
#define BT V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE
#define NBUF 6
#define MAXP 3
#define RTP_HDR 12
#define RTP_MTU 1200                    /* max RTP *payload* bytes */
#define MAX_NAL 256
#define HDR_INJECT_INTERVAL 1           /* inject VPS/SPS/PPS before EVERY frame:
                                           QGC joins a running UDP stream with no
                                           SDP exchange, so in-band SPS/PPS on each
                                           frame is what lets it decode instantly
                                           (and recover from a dropped first packet). */
#define NFRMBUF 4                      /* max rotating CPU-written NV12 frames */
#define BLACK 140                      /* SC5338 10-bit pedestal */

/* ---------------------------------------------------------------- config */
typedef struct {
    char ip[64];
    int  port;
    int  hevc;                 /* 0 = H.264, 1 = H.265 */
    int  w, h;                 /* encoder output size */
    int  capw, caph;           /* capture size when center-cropping (0 = same as w/h) */
    int  fps;
    int  kbps;
    int  gop;                  /* 0 = same as fps */
    int  mbrc;                 /* rc:mb_rc (1 = per-MB RC, default 0 frame-level) */
    int  frames;               /* 0 = infinite */
    char src[16];              /* "raw" (rkcif BG10) | "nv12" (rkispp) */
    int  box;                  /* RAW10 binning box 2..4 */
    int  chroma;               /* chroma attenuation % (0 = clean greyscale) */
    int  cbox;                 /* chroma wide-window multiplier (denoise, 1..6) */
    int  nowb;                 /* 1 = freeze grey-world WB (gains stay 1.0) */
    int  threads;              /* RAW->NV12 worker threads (1..2) */
    int  vbufs;                /* V4L2 MMAP buffer count (raw: 2 is plenty) */
    int  nfrmbuf;              /* MPP frame buffers (1..4) */
    unsigned fbuf_type;        /* MppBufferType for CPU-written NV12 */
    char dev[64];
    char dump[128];            /* optional 640x360 PGM of the first NV12 frame */
    int  dry;                  /* 1 = encode but do not send UDP */
    int  mirror_port;          /* optional second UDP destination */
    char mirror_ip[64];
    int  pace_us;              /* weak-link: spread frame's RTP packets over this
                                  many microseconds (0 = burst). Smooths WiFi
                                  burst loss and receiver jitter-buffer growth */
    int  dup_idr;              /* weak-link: send every IDR packet twice (same
                                  seq, receiver dedups). Cheap keyframe armor */
    int  cir;                  /* weak-link: cyclic intra refresh, MB rows per
                                  frame (0 = off). Spreads refresh over frames —
                                  no giant IDR burst, gradual loss recovery */
} cfg_t;
static cfg_t C = {
    .ip = "192.168.6.88", .port = 5600, .hevc = 0,
    .w = 1280, .h = 720, .fps = 15, .kbps = 3000, .gop = 0, .mbrc = 0, .frames = 0,
    .src = "raw", .box = 2, .chroma = 100, .cbox = 3, .nowb = 0, .threads = 2,
    .dev = "", .dry = 0,
    .vbufs = 0, .nfrmbuf = 1, .fbuf_type = 3,
    .mirror_port = 0, .mirror_ip = "",
    .pace_us = 3000, .dup_idr = 0,
    .cir = 2
};
static int g_hs, g_vs;            /* MPP buffer stride (256-aligned) */
/* center-crop mode (nv12): capture larger than encode, copy-crop per frame */
static int cropping;              /* 1 = capture capw x caph, encode w x h */
static int cap_stride;            /* bpl of the capture node (== capw) */
static size_t cap_fsize;          /* capture buffer bytes (capw*caph*1.5) */
static MppBuffer cropbuf[3];      /* encode-side dst ring (DRM, CPU-written) */
static uint8_t *cropptr[3];
static int cropfd[3];             /* DMA-BUF fds of dst ring (for RGA) */
static int cropidx, ncropbuf = 3;

/* RGA 硬件裁切：dlopen vendor librga.so.2 的 LEGACY API（c_RkRgaBlit）。
 * im2d 的 imcrop_t 在此组合（librga 2.1.0 + kernel rga2 3.2.56726）上返回
 * INVALID_PARAM（rga_get_info 版本表不识别 "3.2.56726"），legacy 路径是 vendor
 * 应用同款、无版本表依赖。结构体布局取自 librga 2.1.0 drmrga.h。 */
typedef struct { int xoffset, yoffset, width, height, wstride, hstride, format, size; } rga_rect_l;
typedef struct rga_info_l {
    int fd;
    void *virAddr;
    void *phyAddr;
    unsigned hnd;
    int format;
    rga_rect_l rect;
    unsigned int blend;
    int bufferSize;
    int rotation;
    int color;
    int testLog;
    int mmuFlag;
    int colorkey_en, colorkey_mode, colorkey_max, colorkey_min;
    int scale_mode, color_space_mode, sync_mode;
    int nn[6];
    int dither[6];
    int rop_code;
    int reserve[127];
} rga_info_l;
static void *rga_h;
static int rga_ok;
static int (*p_blit)(rga_info_l *, rga_info_l *, rga_info_l *);
#define RGAFMT_NV12 0xa00        /* RK_FORMAT_YCbCr_420_SP */
static int is_raw(void);          /* forward: used by calc_strides below */
static MppBufferGroup frmgrp;     /* forward: DRM group for nv12 DMABUF import */
static char nv12dump[128] = "";   /* optional full NV12 dump of 1st frame */
static char rawdump[128] = "";    /* optional raw RAW10 mmap dump of 1st frame */
static int g_dumpdelay = 0;       /* skip N frames before dumping (settling) */
static int g_frames_seen = 0;
/* per-frame latency probes (ms): drv = driver timestamp -> DQBUF (queueing),
 * put = encode_put_frame, get = drain until packet, tot = DQBUF -> sent */
static double sum_drv, sum_put, sum_get, sum_tot;
static double max_drv, max_put, max_get, max_tot;
static long   n_lat;
static double t_frame0;
static void calc_strides(void) {
    g_hs = (C.w + 255) & ~255;    /* VEPU requires 256-aligned hor stride */
    if (is_raw())
        g_vs = (C.h + 255) & ~255;    /* and 256-aligned ver stride for the UV plane */
    else {
        /* nv12 (DMABUF import): the driver writes sizeimage = W*H*1.5 with
         * NO vertical alignment (verified: scale2 1280x720 -> 1382400). The
         * UV plane starts at row H, so MPP ver_stride must match H, not a
         * 256-aligned value, or chroma is read from the wrong offset. */
        g_vs = (C.h + 15) & ~15;
    }
}
static int is_raw(void) { return C.src[0] != 'n'; }
static int  fast_init(void);                 /* optimisation path, defined below */
static void raw_to_nv12_fast(const uint8_t *, uint8_t *);
static int  fast_ready;

/* ------------------------------------------------------- MPP raw MPI API */
typedef RK_S32 (*F_create)   (MppCtx *, MppApi **);
typedef RK_S32 (*F_init)    (MppCtx, MppCtxType, MppCodingType);
typedef RK_S32 (*F_destroy) (MppCtx);
typedef RK_S32 (*F_cfg_init)(MppEncCfg *);
typedef RK_S32 (*F_cfg_deinit)(MppEncCfg);
typedef RK_S32 (*F_cfg_set32)(MppEncCfg, const char *, RK_S32);
typedef RK_S32 (*F_grp_get) (MppBufferGroup *, MppBufferType, MppBufferMode,
                             const char *, const char *);
typedef RK_S32 (*F_grp_put) (MppBufferGroup);
typedef RK_S32 (*F_buf_get) (MppBufferGroup, MppBuffer *, size_t,
                             const char *, const char *);
typedef RK_S32 (*F_buf_put) (MppBuffer, const char *);
typedef void  *(*F_buf_ptr) (MppBuffer, const char *);
typedef RK_S32 (*F_buf_info)(MppBuffer, MppBufferInfo *, const char *);
typedef RK_S32 (*F_buf_import)(MppBufferGroup, MppBufferInfo *, MppBuffer *,
                               const char *, const char *);
typedef RK_S32 (*F_frame_init)(MppFrame *);
typedef RK_S32 (*F_frame_deinit)(MppFrame *);
typedef void   (*F_frame_seti)(MppFrame, RK_U32);
typedef void   (*F_frame_setpts)(MppFrame, RK_S64);
typedef RK_S32 (*F_frame_setbuf)(MppFrame, MppBuffer);
typedef RK_S32 (*F_pkt_initbuf)(MppPacket *, MppBuffer);
typedef RK_S32 (*F_pkt_deinit)(MppPacket *);
typedef RK_S32 (*F_pkt_setlen)(MppPacket, size_t);
typedef void  *(*F_pkt_pos)  (MppPacket);
typedef size_t (*F_pkt_len)  (MppPacket);

static void *H;
static F_create p_create; static F_init p_init; static F_destroy p_destroy;
static F_cfg_init p_cfg_init; static F_cfg_deinit p_cfg_deinit;
static F_cfg_set32 p_cfg_set32;
static F_grp_get p_grp_get; static F_grp_put p_grp_put;
static F_buf_get p_buf_get; static F_buf_put p_buf_put;
static F_buf_ptr p_buf_ptr; static F_buf_info p_buf_info;
static F_buf_import p_buf_import;
static F_frame_init p_frame_init; static F_frame_deinit p_frame_deinit;
static F_frame_seti p_set_w, p_set_h, p_set_hs, p_set_vs, p_set_fmt, p_set_eos;
static F_frame_setpts p_set_pts;
static F_frame_setbuf p_set_buf;
static F_pkt_initbuf p_pkt_initbuf; static F_pkt_deinit p_pkt_deinit;
static F_pkt_setlen p_pkt_setlen; static F_pkt_pos p_pkt_pos;
static F_pkt_len p_pkt_len;

#define SYM(name, var, T) do { var = (T)dlsym(H, name); \
    if (!var) { fprintf(stderr, "[FATAL] dlsym %s: %s\n", name, dlerror()); _exit(2); } } while (0)

/* ------------------------------------------------------------- utilities */
static volatile sig_atomic_t stopping = 0;
static void on_signal(int s) { (void)s; stopping = 1; }

static double now_s(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}
static int xioctl(int fd, unsigned long req, void *arg) {
    int r;
    do { r = ioctl(fd, req, arg); } while (r == -1 && errno == EINTR);
    return r;
}

/* ------------------------------------------------- rkcif RAW10 -> NV12 ---
 * The sensor delivers 2880x1616 BGGR MIPI RAW10 *packed*: every group of
 * 4 pixels occupies 5 bytes (4 x 8 msb + 1 byte holding the 4 x 2 lsb).
 *
 * Two-stage, aliasing-free downscale (the old code SKIPPED source rows and
 * produced heavy moire):
 *   stage1: box x box bin of the FULL raw -> bw x bh RGB (bw = src_w/box)
 *   stage2: area-average bw x bh -> C.w x C.h, then NV12 + chroma low-pass
 * Every source pixel is visited exactly once in stage1, so the cost is
 * (almost) independent of the output resolution: 1280x720 looks ~4x sharper
 * than 640x360 for the same frame rate. Grey-world gains carry over.
 */
static int src_w = 2880, src_h = 1616, src_stride = 3840;
static uint16_t *rows[4];
static uint8_t gamma_lut[1024];
static double wb_r = 1.0, wb_b = 1.0;
static const int chmap[4] = { 0, 1, 1, 2 };   /* BGGR -> B,G,G,R */
static int16_t *ubuf, *vbuf, *ctmp;            /* half-res chroma scratch */
static int cw, ch;
static int bw, bh;                             /* stage1 bin resolution */
static uint16_t *binR, *binG, *binB;           /* stage1 RGB accumulators */

static int raw_init(void) {
    for (int v = 0; v < 1024; v++) {
        double t = (v - BLACK) / (double)(1023 - BLACK);
        if (t < 0) t = 0;
        gamma_lut[v] = (uint8_t)(255.0 * pow(t, 0.65) + 0.5);
    }
    int box = C.box;
    if (box < 2) box = 2; if (box > 8) box = 8;
    bw = src_w / box; bh = src_h / box;
    size_t npix = (size_t)bw * bh;
    binR = (uint16_t *)malloc(npix * sizeof(uint16_t));
    binG = (uint16_t *)malloc(npix * sizeof(uint16_t));
    binB = (uint16_t *)malloc(npix * sizeof(uint16_t));
    if (!binR || !binG || !binB) { fprintf(stderr, "[FATAL] bin malloc\n"); return -1; }
    for (int k = 0; k < 4; k++) {
        rows[k] = (uint16_t *)malloc((size_t)src_w * sizeof(uint16_t));
        if (!rows[k]) { fprintf(stderr, "[FATAL] RAW row malloc\n"); return -1; }
    }
    cw = (C.w + 1) / 2;
    ch = (C.h + 1) / 2;
    ubuf = (int16_t *)malloc((size_t)cw * ch * sizeof(int16_t));
    vbuf = (int16_t *)malloc((size_t)cw * ch * sizeof(int16_t));
    /* ctmp holds TWO chroma planes (U at [0,cw*ch), V at [cw*ch,2*cw*ch))
     * used by fast_blur_h, so it must be 2 x one plane — not 1. */
    ctmp = (int16_t *)malloc((size_t)2 * cw * ch * sizeof(int16_t));
    if (!ubuf || !vbuf || !ctmp) { fprintf(stderr, "[FATAL] chroma malloc\n"); return -1; }
    printf("[raw] stage1 bin %dx%d (box=%d) -> stage2 %dx%d, chroma %dx%d (cbox=%d)\n",
           bw, bh, box, C.w, C.h, cw, ch, C.cbox);
    if (box == 2 || box == 4 || box == 8)
        fast_ready = (fast_init() == 0);
    printf("[raw] converter = %s box=%d\n", fast_ready ? "FAST (integer/2-thread)" : "generic", box);
    return 0;
}

/* RAW10 unpack.  NEON path handles 16 pixels (4 packed groups = 20 bytes)
 * per iteration using table lookups; bit-exact with the scalar loop.  The
 * p+24 load reads 4 bytes of row padding (stride 3840 > 3600 useful) — safe. */
#ifdef H264OUT_NEON
static inline void decode_row(const uint8_t *src, uint16_t *dst) {
    const uint8x8_t IDX_MLS = { 0, 1, 2, 3, 5, 6, 7,  8 };
    const uint8x8_t IDX_MSH = { 10,11,12,13,15,16,17,18 };
    const uint8x8_t IDX_LLS = { 4, 4, 4, 4, 9, 9, 9,  9 };
    const uint8x8_t IDX_LLH = { 14,14,14,14,19,19,19,19 };
    const int8x8_t  SH      = { 0,-2,-4,-6, 0,-2,-4,-6 };
    const uint8x8_t MASK3   = vdup_n_u8(3);
    int g = 0;
    for (; g + 16 <= src_w; g += 16) {
        const uint8_t *p = src + (g / 4) * 5;
        uint8x8x4_t t;
        t.val[0] = vld1_u8(p);
        t.val[1] = vld1_u8(p + 8);
        t.val[2] = vld1_u8(p + 16);
        t.val[3] = vld1_u8(p + 24);
        uint8x8_t mlo = vtbl4_u8(t, IDX_MLS);
        uint8x8_t mhi = vtbl4_u8(t, IDX_MSH);
        uint8x8_t llo = vand_u8(vshl_u8(vtbl4_u8(t, IDX_LLS), SH), MASK3);
        uint8x8_t lhi = vand_u8(vshl_u8(vtbl4_u8(t, IDX_LLH), SH), MASK3);
        /* BUGFIX: must widen to u16 BEFORE shifting left by 2.  The old
         * vshl_n_u8(mlo,2) overflowed in the 8-bit domain (e.g. 241<<2 -> 196),
         * discarding the top 2 bits and darkening the whole image ~8x. */
        uint16x8_t base_lo = vshlq_n_u16(vmovl_u8(mlo), 2);
        uint16x8_t base_hi = vshlq_n_u16(vmovl_u8(mhi), 2);
        vst1q_u16(dst + g,     vorrq_u16(base_lo, vmovl_u8(llo)));
        vst1q_u16(dst + g + 8, vorrq_u16(base_hi, vmovl_u8(lhi)));
    }
    for (; g < src_w; g += 4) {
        const uint8_t *p = src + (g / 4) * 5;
        uint8_t lo = p[4];
        dst[g+0] = ((uint16_t)p[0] << 2) | (lo & 3);
        dst[g+1] = ((uint16_t)p[1] << 2) | ((lo >> 2) & 3);
        dst[g+2] = ((uint16_t)p[2] << 2) | ((lo >> 4) & 3);
        dst[g+3] = ((uint16_t)p[3] << 2) | ((lo >> 6) & 3);
    }
}
#else
static inline void decode_row(const uint8_t *src, uint16_t *dst) {
    for (int g = 0; g < src_w / 4; g++) {
        const uint8_t *p = src + g * 5;
        uint8_t lo = p[4];
        dst[g*4+0] = ((uint16_t)p[0] << 2) | (lo & 3);
        dst[g*4+1] = ((uint16_t)p[1] << 2) | ((lo >> 2) & 3);
        dst[g*4+2] = ((uint16_t)p[2] << 2) | ((lo >> 4) & 3);
        dst[g*4+3] = ((uint16_t)p[3] << 2) | ((lo >> 6) & 3);
    }
}
#endif

static inline int clamp255(int v) { return v < 0 ? 0 : (v > 255 ? 255 : v); }

/* Separable 7-tap box blur on one half-resolution chroma plane. */
static void blur_chroma(int16_t *buf) {
    const int RAD = 3, DIV = 2 * RAD + 1;
    for (int y = 0; y < ch; y++)
        for (int x = 0; x < cw; x++) {
            int s = 0;
            for (int k = -RAD; k <= RAD; k++) {
                int xx = x + k;
                if (xx < 0) xx = 0; if (xx >= cw) xx = cw - 1;
                s += buf[y * cw + xx];
            }
            ctmp[y * cw + x] = (int16_t)(s / DIV);
        }
    for (int y = 0; y < ch; y++)
        for (int x = 0; x < cw; x++) {
            int s = 0;
            for (int k = -RAD; k <= RAD; k++) {
                int yy = y + k;
                if (yy < 0) yy = 0; if (yy >= ch) yy = ch - 1;
                s += ctmp[yy * cw + x];
            }
            buf[y * cw + x] = (int16_t)(s / DIV);
        }
}

/* Convert one captured RAW10 frame into an MPP-ready NV12 buffer.
 * nv12 must be hs*vs*3/2 bytes with hs=(w+15)&~15, vs=(h+15)&~15. */
static void raw_to_nv12(const uint8_t *frame, uint8_t *nv12) {
    const int hs = g_hs, vs = g_vs;
    const size_t fsize = (size_t)hs * vs * 3 / 2;
    const int box = C.box;
    const size_t uvoff = (size_t)hs * vs;
    memset(nv12, 128, fsize);           /* padding rows/planes = neutral */

    /* ---- stage1: box-bin the FULL raw into bw x bh RGB (one pass) ---- */
    for (int by = 0; by < bh; by++) {
        int sy0 = by * box;
        for (int k = 0; k < box; k++)
            decode_row(frame + (size_t)(sy0 + k) * src_stride, rows[k]);
        for (int bx = 0; bx < bw; bx++) {
            int sx0 = bx * box;
            uint32_t a[3] = { 0, 0, 0 };
            int cnt[3] = { 0, 0, 0 };
            for (int k = 0; k < box; k++) {
                int sy = sy0 + k;
                for (int d = 0; d < box; d++) {
                    int sx = sx0 + d;
                    int cc = chmap[((sy & 1) << 1) | (sx & 1)];
                    a[cc] += rows[k][sx]; cnt[cc]++;
                }
            }
            size_t o = (size_t)by * bw + bx;
            binR[o] = cnt[2] ? (uint16_t)(a[2] / cnt[2]) : 0;
            binG[o] = cnt[1] ? (uint16_t)(a[1] / cnt[1]) : 0;
            binB[o] = cnt[0] ? (uint16_t)(a[0] / cnt[0]) : 0;
        }
    }

    /* ---- stage2: area-average bin -> output, then NV12 + chroma ---- */
    double sR = 0, sG = 0, sB = 0;
    long nR = 0, nG = 0, nB = 0;
    for (int ty = 0; ty < C.h; ty++) {
        int y0 = ty * bh / C.h, y1 = (ty + 1) * bh / C.h;
        if (y1 <= y0) y1 = y0 + 1;
        uint8_t *yrow = nv12 + (size_t)ty * hs;
        for (int tx = 0; tx < C.w; tx++) {
            int x0 = tx * bw / C.w, x1 = (tx + 1) * bw / C.w;
            if (x1 <= x0) x1 = x0 + 1;
            uint32_t sr = 0, sg = 0, sb = 0; int n = 0;
            for (int yy = y0; yy < y1; yy++)
                for (int xx = x0; xx < x1; xx++) {
                    size_t o = (size_t)yy * bw + xx;
                    sr += binR[o]; sg += binG[o]; sb += binB[o]; n++;
                }
            int R10 = sr / n, G10 = sg / n, B10 = sb / n;
            sR += R10 > BLACK ? R10 - BLACK : 0; nR++;
            sG += G10 > BLACK ? G10 - BLACK : 0; nG++;
            sB += B10 > BLACK ? B10 - BLACK : 0; nB++;
            int R = (int)((R10 - BLACK) * wb_r) + BLACK;
            int B = (int)((B10 - BLACK) * wb_b) + BLACK;
            int G = G10;
            if (R < 0) R = 0; if (R > 1023) R = 1023;
            if (G < 0) G = 0; if (G > 1023) G = 1023;
            if (B < 0) B = 0; if (B > 1023) B = 1023;
            int r8 = gamma_lut[R], g8 = gamma_lut[G], b8 = gamma_lut[B];
            yrow[tx] = (uint8_t)((2 * r8 + 3 * g8 + b8) / 6);
        }
    }

    /* ---- chroma pass: wide-window average of the stage1 bin grid ----
     * The half-res chroma plane is the noisy part: a naive per-output-pixel
     * chroma averages only ~one source R and ~one source B (each bin holds at
     * most a single R/B), so the sensor's per-pixel chroma noise survives.
     * Here every chroma cell averages a centered cbox x cbox SUPER-window of
     * the bin grid, multiplying the effective R/B sample count by ~cbox^2 and
     * suppressing that noise well below the luma.  Chroma is low-pass anyway,
     * so the extra blur is visually invisible.  ubuf/vbuf stay SIGNED and
     * zero-centred (Cb-128 / Cr-128). */
    {
        const double bx = (double)bw / C.w, by = (double)bh / C.h;
        /* cbox is the chroma-pixel half-window; bins-per-chroma-pixel = bx*2,
         * so a cbox=3 covers a ~7-bin (≈15-source-px) square -> ~200 R/B
         * samples per chroma pixel and strong sensor-noise suppression. */
        const int hx = (int)(bx * 2 * C.cbox + 0.5), hy = (int)(by * 2 * C.cbox + 0.5);
        for (int cy = 0; cy < ch; cy++) {
            int py = (cy * 2 + 1) * bh / C.h;        /* chroma cell centre, bin rows */
            int y0 = py - hy; if (y0 < 0) y0 = 0;
            int y1 = py + hy; if (y1 > bh) y1 = bh; if (y1 <= y0) y1 = y0 + 1;
            for (int cx = 0; cx < cw; cx++) {
                int px = (cx * 2 + 1) * bw / C.w;    /* chroma cell centre, bin cols */
                int x0 = px - hx; if (x0 < 0) x0 = 0;
                int x1 = px + hx; if (x1 > bw) x1 = bw; if (x1 <= x0) x1 = x0 + 1;
                uint32_t sr = 0, sg = 0, sb = 0; int n = 0;
                for (int yy = y0; yy < y1; yy++)
                    for (int xx = x0; xx < x1; xx++) {
                        size_t o = (size_t)yy * bw + xx;
                        sr += binR[o]; sg += binG[o]; sb += binB[o]; n++;
                    }
                int R10 = sr / n, G10 = sg / n, B10 = sb / n;
                int R = (int)((R10 - BLACK) * wb_r) + BLACK;
                int B = (int)((B10 - BLACK) * wb_b) + BLACK;
                int G = G10;
                if (R < 0) R = 0; if (R > 1023) R = 1023;
                if (G < 0) G = 0; if (G > 1023) G = 1023;
                if (B < 0) B = 0; if (B > 1023) B = 1023;
                int r8 = gamma_lut[R], g8 = gamma_lut[G], b8 = gamma_lut[B];
                ubuf[cy * cw + cx] = (int16_t)((-43 * r8 - 85 * g8 + 128 * b8) >> 8);
                vbuf[cy * cw + cx] = (int16_t)((128 * r8 - 107 * g8 - 21 * b8) >> 8);
            }
        }
    }
    /* 4:2:0 chroma: low-pass U/V on the half-resolution plane (suppresses the
     * residual Bayer false colour); C.chroma (100 = full colour) scales them.
     * NOTE: ubuf/vbuf are SIGNED, zero-centred chroma (Cb-128 / Cr-128), so we
     * store straight as 128 + ubuf. Subtracting 128 here would re-bias to ~0
     * and collapse every pixel to saturated green. */
    blur_chroma(ubuf);
    blur_chroma(vbuf);
    for (int y = 0; y < ch; y++)
        for (int x = 0; x < cw; x++) {
            uint8_t *p = nv12 + uvoff + (size_t)y * hs + x * 2;
            p[0] = (uint8_t)clamp255(128 + ubuf[y * cw + x] * C.chroma / 100);
            p[1] = (uint8_t)clamp255(128 + vbuf[y * cw + x] * C.chroma / 100);
        }

    /* Grey-world gains for the NEXT frame (luminance-independent). --nowb
     * freezes them at 1.0 (sensor's native colour; no adaptive drift). */
    if (!C.nowb) {
        double mR = sR / (nR ? nR : 1), mG = sG / (nG ? nG : 1), mB = sB / (nB ? nB : 1);
        if (mR < 1) mR = 1; if (mG < 1) mG = 1; if (mB < 1) mB = 1;
        double gR = mG / mR, gB = mG / mB;
        wb_r = gR < 0.6 ? 0.6 : (gR > 2.0 ? 2.0 : gR);
        wb_b = gB < 0.6 ? 0.6 : (gB > 2.0 ? 2.0 : gB);
    }
}

/* =====================================================================
 * FAST RAW10 -> NV12  (box=2 only, integer-only, 2 threads)
 *
 * Measured cost of the generic path above on this A7 @1.0 GHz:
 *   320x180 = 305 ms/frame, 720p = 617 ms, 1080p = 1057 ms.
 * Three pathologies: per-pixel `double` WB maths, a variable integer
 * division in every hot loop (built without hw-divide codegen), and an
 * O(cbox^2) chroma gather (~200 bin reads per chroma cell).
 *
 * This path keeps the same algorithm but:
 *   - stage1 box=2 is specialised: R/G/B counts are exactly 1/2/1, no div
 *   - WB gains are Q16 integers; no FP anywhere in the pixel loops
 *   - down/up-resampling uses PRECOMPUTED integer Q8 tap tables
 *   - chroma uses a separable running-sum box blur, O(1) per pixel
 *   - the two frame halves are processed in parallel by 2 pthreads
 * ===================================================================== */
#define FAST_MAXTAP 8
#define RING_ROWS   8

static uint8_t *fbY;                 /* bw*bh  bin-resolution luma   */
static int8_t  *fbU, *fbV;           /* bw*bh  bin-resolution chroma */
static int     *ht_i, *ht_w, *ht_n;  /* C.w x FAST_MAXTAP taps       */
static int     *vt_i, *vt_w, *vt_n;  /* C.h x FAST_MAXTAP taps       */
static int      wbR_q16 = 65536, wbB_q16 = 65536;
/* MUST be signed: chroma is centred on zero, and a uint32 multiplier would
 * promote negative sums to unsigned before the >>16 (logical shift) and turn
 * every negative Cb/Cr into a large positive value. */
static int inv_n[65];                /* 65536/n for n=1..64          */
static int blur_mul;                 /* 65536/(2R+1)                 */
static const uint8_t *g_rawframe;
static uint8_t *g_outnv12;

/* Build upsample(bilinear) / downsample(area-average) tap tables. */
static int build_taps(int outn, int inn, int *ti, int *tw, int *tn)
{
    for (int o = 0; o < outn; o++) {
        int *I = ti + (size_t)o * FAST_MAXTAP;
        int *W = tw + (size_t)o * FAST_MAXTAP;
        int n;
        if (inn == outn) {
            I[0] = o; W[0] = 256; tn[o] = 1;
        } else if (inn < outn) {                 /* upsample: bilinear */
            long pos = ((long)(2 * o + 1) * inn * 128) / outn - 128; /* Q8 */
            int i0 = (int)(pos >> 8), f = (int)(pos & 255);
            if (i0 < 0) { i0 = 0; f = 0; }
            if (i0 >= inn) { i0 = inn - 1; f = 0; }
            I[0] = i0; W[0] = 256 - f; n = 1;
            if (f) { I[1] = i0 + 1 < inn ? i0 + 1 : i0; W[1] = f; n = 2; }
            tn[o] = n;
        } else {                                 /* downsample: area avg */
            int a = (int)((long)o * inn / outn);
            int b = (int)((long)(o + 1) * inn / outn);
            if (b <= a) b = a + 1;
            if (b > inn) b = inn;
            n = b - a;
            if (n > FAST_MAXTAP) return -1;
            int base = 256 / n, rem = 256 - base * n;
            for (int k = 0; k < n; k++) {
                I[k] = a + k; W[k] = base + (k < rem ? 1 : 0);
            }
            tn[o] = n;
        }
    }
    return 0;
}

static int fast_init(void)
{
    if (C.box != 2 && C.box != 4 && C.box != 8) return -1;  /* FAST: 2/4/8 */
    size_t nb = (size_t)bw * bh;
    fbY = (uint8_t *)malloc(nb);
    fbU = (int8_t *)malloc(nb);
    fbV = (int8_t *)malloc(nb);
    ht_i = (int *)malloc(sizeof(int) * (size_t)C.w * FAST_MAXTAP);
    ht_w = (int *)malloc(sizeof(int) * (size_t)C.w * FAST_MAXTAP);
    ht_n = (int *)malloc(sizeof(int) * C.w);
    vt_i = (int *)malloc(sizeof(int) * (size_t)C.h * FAST_MAXTAP);
    vt_w = (int *)malloc(sizeof(int) * (size_t)C.h * FAST_MAXTAP);
    vt_n = (int *)malloc(sizeof(int) * C.h);
    if (!fbY || !fbU || !fbV || !ht_i || !ht_w || !ht_n || !vt_i || !vt_w || !vt_n)
        return -1;
    if (build_taps(C.w, bw, ht_i, ht_w, ht_n) ||
        build_taps(C.h, bh, vt_i, vt_w, vt_n))
        return -1;
    for (int i = 1; i < (int)(sizeof(inv_n) / sizeof(inv_n[0])); i++)
        inv_n[i] = 65536 / i;
    blur_mul = 65536 / (2 * C.cbox + 1);
    printf("[fast] tap tables %dx%d -> %dx%d, chroma blur r=%d, threads=%d\n",
           bw, bh, C.w, C.h, C.cbox, C.threads);
    return 0;
}

/* ---- 2-thread barrier ---- */
static pthread_mutex_t fast_mtx = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t  fast_cnd = PTHREAD_COND_INITIALIZER;
static int fast_bcnt, fast_bgen, fast_bn = 1;
static void fast_barrier(void)
{
    if (fast_bn <= 1) return;
    pthread_mutex_lock(&fast_mtx);
    int gen = fast_bgen;
    if (++fast_bcnt == fast_bn) {
        fast_bcnt = 0; fast_bgen++;
        pthread_cond_broadcast(&fast_cnd);
    } else {
        while (gen == fast_bgen) pthread_cond_wait(&fast_cnd, &fast_mtx);
    }
    pthread_mutex_unlock(&fast_mtx);
}

typedef struct {
    int by0, by1;                    /* bin rows owned by this worker */
    int ty0, ty1;                    /* output luma rows              */
    int cy0, cy1;                    /* output chroma rows            */
    uint16_t *r[8];                  /* up to eight unpacked RAW rows */
    uint8_t  *ring;                  /* RING_ROWS x C.w horizontal rows */
    int       tag[RING_ROWS];
    uint64_t  aR, aG, aB, aN;
    int32_t   accv[2][FAST_MAXTAP * 512]; /* vertical blur accumulators */
} wk_t;
static wk_t wk[2];

/* stage1: BGGR box bin -> bin-resolution Y/U/V, integer WB + gamma LUT.
 * box=2: 4 samples (B1 G2 R1); box=4: 16 samples (B4 G8 R4).  Both start on
 * an even source row/col so the BGGR phase is identical for every bin. */
static void fast_stage1(wk_t *w)
{
    const int box = C.box;
    const int ivB = inv_n[box == 2 ? 1 : (box == 4 ? 4 : 16)];
    const int ivG = inv_n[box == 2 ? 2 : (box == 4 ? 8 : 32)];
    const int ivR = ivB;
    for (int by = w->by0; by < w->by1; by++) {
        const int sy0 = by * box;
        const uint8_t *p0 = g_rawframe + (size_t)sy0 * src_stride;
        for (int k = 0; k < box; k++)
            decode_row(p0 + (size_t)k * src_stride, w->r[k]);
        uint8_t *yrow = fbY + (size_t)by * bw;
        int8_t  *urow = fbU + (size_t)by * bw;
        int8_t  *vrow = fbV + (size_t)by * bw;
        for (int bx = 0; bx < bw; bx++) {
            int x0 = bx * box;
            int b10, g10, r10;
            if (box == 2) {
                b10 = w->r[0][x0];
                g10 = (w->r[0][x0 + 1] + w->r[1][x0]) >> 1;
                r10 = w->r[1][x0 + 1];
            } else { /* box == 4/8: BGGR, even-even origin */
                uint32_t sb = 0, sg = 0, sr = 0;
                for (int k = 0; k < box; k++) {
                    const uint16_t *row = w->r[k];
                    for (int d = 0; d < box; d++) {
                        /* pos = row_parity + col_parity: 0=B 1=G 1=G 2=R.
                         * (Using bitwise OR here mapped (odd,odd)->1 and
                         * made R vanish => green/cyan image.) */
                        int pos = (k & 1) + (d & 1);
                        if (pos == 0)      sb += row[x0 + d];
                        else if (pos == 2) sr += row[x0 + d];
                        else               sg += row[x0 + d];
                    }
                }
                b10 = (int)((sb * (uint64_t)ivB) >> 16);
                g10 = (int)((sg * (uint64_t)ivG) >> 16);
                r10 = (int)((sr * (uint64_t)ivR) >> 16);
            }
            if ((bx & 3) == 0) {          /* sample 1/4 for grey-world */
                w->aR += r10 > BLACK ? r10 - BLACK : 0;
                w->aG += g10 > BLACK ? g10 - BLACK : 0;
                w->aB += b10 > BLACK ? b10 - BLACK : 0;
                w->aN++;
            }
            int R = BLACK + (int)(((int64_t)(r10 - BLACK) * wbR_q16) >> 16);
            int B = BLACK + (int)(((int64_t)(b10 - BLACK) * wbB_q16) >> 16);
            if (R < 0) R = 0; else if (R > 1023) R = 1023;
            if (g10 < 0) g10 = 0; else if (g10 > 1023) g10 = 1023;
            if (B < 0) B = 0; else if (B > 1023) B = 1023;
            int r8 = gamma_lut[R], g8 = gamma_lut[g10], b8 = gamma_lut[B];
            yrow[bx] = (uint8_t)((2 * r8 + 3 * g8 + b8) / 6);
            urow[bx] = (int8_t)((-43 * r8 - 85 * g8 + 128 * b8) >> 8);
            vrow[bx] = (int8_t)((128 * r8 - 107 * g8 - 21 * b8) >> 8);
        }
    }
}

/* luma: precomputed taps + cached horizontal rows -> output Y plane */
static void fast_luma(wk_t *w)
{
    const int hs = g_hs;
    for (int ty = w->ty0; ty < w->ty1; ty++) {
        const int *VI = vt_i + (size_t)ty * FAST_MAXTAP;
        const int *VW = vt_w + (size_t)ty * FAST_MAXTAP;
        int vn = vt_n[ty];
        for (int k = 0; k < vn; k++) {
            int bi = VI[k], slot = bi & (RING_ROWS - 1);
            if (w->tag[slot] == bi) continue;
            const uint8_t *src = fbY + (size_t)bi * bw;
            uint8_t *h = w->ring + (size_t)slot * C.w;
            for (int tx = 0; tx < C.w; tx++) {
                const int *HI = ht_i + (size_t)tx * FAST_MAXTAP;
                const int *HW = ht_w + (size_t)tx * FAST_MAXTAP;
                int hn = ht_n[tx], s = 0;
                for (int j = 0; j < hn; j++) s += HW[j] * src[HI[j]];
                h[tx] = (uint8_t)((s + 128) >> 8);
            }
            w->tag[slot] = bi;
        }
        uint8_t *dst = g_outnv12 + (size_t)ty * hs;
        if (vn == 1) {
            memcpy(dst, w->ring + (size_t)(VI[0] & (RING_ROWS - 1)) * C.w, C.w);
        } else {
            for (int x = 0; x < C.w; x++) {
                int s = 0;
                for (int k = 0; k < vn; k++)
                    s += VW[k] * w->ring[(size_t)(VI[k] & (RING_ROWS - 1)) * C.w + x];
                dst[x] = (uint8_t)((s + 128) >> 8);
            }
        }
    }
}

/* chroma: bin-resolution U/V -> 4:2:0 cell average (integer reciprocal) */
static void fast_chroma_dec(wk_t *w)
{
    for (int cy = w->cy0; cy < w->cy1; cy++) {
        int y0 = (2 * cy) * bh / C.h, y1 = (2 * cy + 2) * bh / C.h;
        if (y1 <= y0) y1 = y0 + 1;
        if (y1 > bh) y1 = bh;
        for (int cx = 0; cx < cw; cx++) {
            int x0 = (2 * cx) * bw / C.w, x1 = (2 * cx + 2) * bw / C.w;
            if (x1 <= x0) x1 = x0 + 1;
            if (x1 > bw) x1 = bw;
            int su = 0, sv = 0, n = 0;
            for (int yy = y0; yy < y1; yy++) {
                const int8_t *pu = fbU + (size_t)yy * bw;
                const int8_t *pv = fbV + (size_t)yy * bw;
                for (int xx = x0; xx < x1; xx++) { su += pu[xx]; sv += pv[xx]; n++; }
            }
            int iv = inv_n[n < 65 ? n : 64];
            ubuf[cy * cw + cx] = (int16_t)((su * iv) >> 16);
            vbuf[cy * cw + cx] = (int16_t)((sv * iv) >> 16);
        }
    }
}

/* separable running-sum box blur, U and V together (O(1) per pixel) */
static void fast_blur_h(wk_t *w, int r)
{
    for (int y = w->cy0; y < w->cy1; y++) {
        const int16_t *u = ubuf + (size_t)y * cw, *v = vbuf + (size_t)y * cw;
        int16_t *qu = ctmp + (size_t)y * cw;
        int16_t *qv = ctmp + (size_t)cw * ch + (size_t)y * cw;
        int au = 0, av = 0;
        for (int k = -r; k <= r; k++) {
            int xx = k < 0 ? 0 : (k >= cw ? cw - 1 : k);
            au += u[xx]; av += v[xx];
        }
        for (int x = 0; x < cw; x++) {
            qu[x] = (int16_t)((au * blur_mul) >> 16);
            qv[x] = (int16_t)((av * blur_mul) >> 16);
            int xi = x + r + 1; if (xi >= cw) xi = cw - 1;
            int xo = x - r;     if (xo < 0) xo = 0;
            au += u[xi] - u[xo];
            av += v[xi] - v[xo];
        }
    }
}

static void fast_blur_v(wk_t *w, int r)
{
    int32_t *au = w->accv[0], *av = w->accv[1];
    if (cw > FAST_MAXTAP * 512) return;         /* safety, never hit */
    for (int x = 0; x < cw; x++) { au[x] = 0; av[x] = 0; }
    for (int k = -r; k <= r; k++) {
        int yy = w->cy0 + k;
        if (yy < 0) yy = 0; if (yy >= ch) yy = ch - 1;
        const int16_t *u = ctmp + (size_t)yy * cw;
        const int16_t *v = ctmp + (size_t)cw * ch + (size_t)yy * cw;
        for (int x = 0; x < cw; x++) { au[x] += u[x]; av[x] += v[x]; }
    }
    const size_t uvoff = (size_t)g_hs * g_vs;
    for (int y = w->cy0; y < w->cy1; y++) {
        int16_t *du = ubuf + (size_t)y * cw, *dv = vbuf + (size_t)y * cw;
        uint8_t *out = g_outnv12 + uvoff + (size_t)y * g_hs;
        for (int x = 0; x < cw; x++) {
            int u = (au[x] * blur_mul) >> 16;
            int v = (av[x] * blur_mul) >> 16;
            du[x] = (int16_t)u; dv[x] = (int16_t)v;
            out[x * 2]     = (uint8_t)clamp255(128 + u * C.chroma / 100);
            out[x * 2 + 1] = (uint8_t)clamp255(128 + v * C.chroma / 100);
        }
        int yi = y + r + 1; if (yi >= ch) yi = ch - 1;
        int yo = y - r;     if (yo < 0) yo = 0;
        const int16_t *ui = ctmp + (size_t)yi * cw;
        const int16_t *vi = ctmp + (size_t)cw * ch + (size_t)yi * cw;
        const int16_t *uo = ctmp + (size_t)yo * cw;
        const int16_t *vo = ctmp + (size_t)cw * ch + (size_t)yo * cw;
        for (int x = 0; x < cw; x++) {
            au[x] += ui[x] - uo[x];
            av[x] += vi[x] - vo[x];
        }
    }
}

static void *fast_worker(void *arg)
{
    wk_t *w = (wk_t *)arg;
    fast_stage1(w);
    fast_barrier();                  /* all bin rows ready */
    fast_luma(w);
    fast_chroma_dec(w);
    fast_barrier();                  /* all 4:2:0 cells ready */
    fast_blur_h(w, C.cbox);
    fast_barrier();                  /* horizontal blur plane ready */
    fast_blur_v(w, C.cbox);
    return NULL;
}

static void raw_to_nv12_fast(const uint8_t *frame, uint8_t *nv12)
{
    static int wk_allocd = 0;
    g_rawframe = frame; g_outnv12 = nv12;
    if (!wk_allocd) {
        for (int i = 0; i < 2; i++) {
            /* decode_row emits one 10-bit sample per SOURCE column (src_w),
             * and stage1 indexes up to r[x0+1] with x0 = 2*bx < 2*bw = 2*(src_w/2) = src_w-2.
             * So each row buffer must be src_w uint16, NOT bw. */
            for (int k = 0; k < 8; k++) {
                wk[i].r[k] = (uint16_t *)malloc((size_t)src_w * sizeof(uint16_t));
                if (!wk[i].r[k]) { fprintf(stderr, "[FATAL] worker row alloc\n"); exit(2); }
            }
            wk[i].ring  = (uint8_t  *)malloc((size_t)RING_ROWS * C.w);
            if (!wk[i].ring) {
                fprintf(stderr, "[FATAL] worker scratch alloc failed\n");
                exit(2);
            }
        }
        wk_allocd = 1;
    }
    /* NOTE: MPP frame buffers are memset to 128 once at allocation time;
     * this fast path only overwrites the active w x h region, so the
     * stride/padding rows keep their neutral value without a per-frame memset. */
    int nt = C.threads;
    if (nt < 1) nt = 1; if (nt > 2) nt = 2;
    fast_bn = nt; fast_bcnt = 0;
    pthread_t th[2];
    if (nt == 2) {
        int mid_b = bh / 2, mid_t = C.h / 2, mid_c = ch / 2;
        wk[0].by0 = 0; wk[0].by1 = mid_b;
        wk[1].by0 = mid_b; wk[1].by1 = bh;
        wk[0].ty0 = 0; wk[0].ty1 = mid_t;
        wk[1].ty0 = mid_t; wk[1].ty1 = C.h;
        wk[0].cy0 = 0; wk[0].cy1 = mid_c;
        wk[1].cy0 = mid_c; wk[1].cy1 = ch;
        for (int i = 0; i < 2; i++) {
            wk[i].aR = wk[i].aG = wk[i].aB = wk[i].aN = 0;
            memset(wk[i].tag, 0, sizeof wk[i].tag);
        }
        if (pthread_create(&th[0], NULL, fast_worker, &wk[0]) != 0) {
            /* no worker could start: run the whole frame inline */
            fast_bn = 1;
            wk[0].by0 = 0; wk[0].by1 = bh;
            wk[0].ty0 = 0; wk[0].ty1 = C.h;
            wk[0].cy0 = 0; wk[0].cy1 = ch;
            fast_worker(&wk[0]);
            nt = 1;
        } else if (pthread_create(&th[1], NULL, fast_worker, &wk[1]) != 0) {
            /* one worker only: release th[0] from the barriers, do rest inline */
            pthread_mutex_lock(&fast_mtx);
            fast_bn = 1; fast_bgen++;
            pthread_cond_broadcast(&fast_cnd);
            pthread_mutex_unlock(&fast_mtx);
            fast_worker(&wk[1]);
            pthread_join(th[0], NULL);
        } else {
            pthread_join(th[0], NULL); pthread_join(th[1], NULL);
        }
    } else {
        wk[0].by0 = 0; wk[0].by1 = bh;
        wk[0].ty0 = 0; wk[0].ty1 = C.h;
        wk[0].cy0 = 0; wk[0].cy1 = ch;
        wk[0].aR = wk[0].aG = wk[0].aB = wk[0].aN = 0;
        memset(wk[0].tag, 0, sizeof wk[0].tag);
        fast_worker(&wk[0]);
    }

    if (!C.nowb) {
        uint64_t aR = 0, aG = 0, aB = 0, aN = 0;
        for (int i = 0; i < nt; i++) {
            aR += wk[i].aR; aG += wk[i].aG; aB += wk[i].aB; aN += wk[i].aN;
        }
        if (aN) {
            double mR = (double)aR / aN, mG = (double)aG / aN, mB = (double)aB / aN;
            if (mR < 1) mR = 1; if (mG < 1) mG = 1; if (mB < 1) mB = 1;
            double gR = mG / mR, gB = mG / mB;
            if (gR < 0.6) gR = 0.6; if (gR > 2.0) gR = 2.0;
            if (gB < 0.6) gB = 0.6; if (gB > 2.0) gB = 2.0;
            wbR_q16 = (int)(gR * 65536.0);
            wbB_q16 = (int)(gB * 65536.0);
        }
    }
#ifdef H264OUT_DEBUG_Y
    {
        static int dbg_n = 0;
        if (((dbg_n++) & 63) == 0) {
            long sb = 0, ob = 0; int mn = 255, mx = 0;
            for (int y = 0; y < bh; y++) {
                const uint8_t *p = fbY + (size_t)y * bw;
                for (int x = 0; x < bw; x++) { sb += p[x]; if (p[x]<mn) mn=p[x]; if (p[x]>mx) mx=p[x]; }
            }
            for (int y = 0; y < C.h; y++) {
                const uint8_t *p = nv12 + (size_t)y * g_hs;
                for (int x = 0; x < C.w; x++) ob += p[x];
            }
            printf("[dbgY] fbY mean=%.1f [%d..%d]  finalY mean=%.1f  wbR=%.3f wbB=%.3f (n=%d)\n",
                   (double)sb/(bw*bh), mn, mx, (double)ob/(C.w*C.h),
                   wbR_q16/65536.0, wbB_q16/65536.0, dbg_n);
        }
    }
#endif
}

/* ------------------------------------------------------------- RTP state */
static int udp = -1;
static struct sockaddr_in dst, mirror;
static uint16_t rtp_seq = 12345;
static uint32_t rtp_ssrc = 0x53463032;   /* "SF02" */
static double t0;
static long stat_rtp = 0, stat_bytes = 0, stat_frames = 0;
static unsigned stat_send_err = 0;

/* fragment list: one element == one RTP datagram payload.
 * The builder fills txb[build_slot]; finished frames are queued for the
 * pacer worker. Slots make builder/sender fully independent — a slow send
 * can never corrupt the frame being built. */
typedef struct { uint8_t b[RTP_MTU]; int n; } frag_t;
/* ---- pacer ring state (shared: builder thread + sender thread) ---- */
typedef struct { int slot; int n; uint32_t ts; int idr; } txent_t;
#define TXN 4                                   /* slots: build + 2 queued + sending */
#define TXS_FREE 0
#define TXS_QUEUED 1
#define TXS_SENDING 2
static frag_t *txb[TXN];
static int txcap[TXN];
static int nfrag = 0;                           /* frags in the build slot */
static int build_slot = 0;                      /* slot the builder fills */
static txent_t pend[TXN];
static int phead, ptail, pcount;
static int slot_state[TXN] = { TXS_FREE, TXS_FREE, TXS_FREE, TXS_FREE };
static pthread_mutex_t pace_mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t  pace_cv = PTHREAD_COND_INITIALIZER;
static volatile int tx_quit = 0;

static void push_frag(const uint8_t *payload, int len) {
    if (nfrag >= txcap[build_slot]) {
        txcap[build_slot] = txcap[build_slot] ? txcap[build_slot] * 2 : 256;
        frag_t *p = realloc(txb[build_slot], (size_t)txcap[build_slot] * sizeof(frag_t));
        if (!p) { fprintf(stderr, "[FATAL] OOM fragments\n"); _exit(3); }
        txb[build_slot] = p;
    }
    if (len > RTP_MTU) len = RTP_MTU;
    memcpy(txb[build_slot][nfrag].b, payload, len);
    txb[build_slot][nfrag].n = len;
    nfrag++;
}

static int g_idr = 0;             /* current access unit carries an IDR NAL */

/* build one RTP datagram from a fragment and send it (ndup copies when the
 * access unit is an IDR: same sequence number, receiver dedups — a single
 * lost packet in a keyframe otherwise corrupts every frame until the next
 * IDR, the classic weak-link failure). */
static void send_pkt(const frag_t *f, int last, uint32_t ts, int idr) {
    uint8_t pkt[RTP_HDR + RTP_MTU];
    int n = f->n;
    pkt[0] = 0x80;                              /* V=2, P=0, X=0, CC=0 */
    pkt[1] = (uint8_t)((C.hevc ? 97 : 96) | (last ? 0x80 : 0));
    pkt[2] = (uint8_t)(rtp_seq >> 8); pkt[3] = (uint8_t)rtp_seq;
    pkt[4] = (uint8_t)(ts >> 24); pkt[5] = (uint8_t)(ts >> 16);
    pkt[6] = (uint8_t)(ts >> 8);  pkt[7] = (uint8_t)ts;
    pkt[8] = (uint8_t)(rtp_ssrc >> 24); pkt[9] = (uint8_t)(rtp_ssrc >> 16);
    pkt[10] = (uint8_t)(rtp_ssrc >> 8); pkt[11] = (uint8_t)rtp_ssrc;
    memcpy(pkt + RTP_HDR, f->b, n);
    int ncopies = (C.dup_idr && idr) ? 2 : 1;
    for (int c = 0; c < ncopies; c++) {
        if (!C.dry) {
            if (sendto(udp, pkt, RTP_HDR + n, 0,
                       (struct sockaddr *)&dst, sizeof(dst)) < 0) stat_send_err++;
            if (C.mirror_port > 0)
                sendto(udp, pkt, RTP_HDR + n, 0,
                       (struct sockaddr *)&mirror, sizeof(mirror));
        }
        stat_rtp++;
        stat_bytes += RTP_HDR + n;
    }
    rtp_seq++;
}

/* ---------- weak-link pacer (worker thread + 4-slot ring) ----------
 * Sending a whole frame as one back-to-back burst (30+ datagrams) overflows
 * WiFi tx queues and inflates the receiver's jitter buffer. The pacer thread
 * drains finished frames spread over C.pace_us while the capture/encode path
 * keeps running — zero added pipeline latency. The ring decouples both
 * threads completely: even if a send runs longer than a frame interval
 * (usleep overshoot on a loaded 2-core SoC), the builder always writes to a
 * slot the sender cannot touch, so packets can never be corrupted. */
static void *pacer_thread(void *arg) {
    (void)arg;
    while (1) {
        pthread_mutex_lock(&pace_mu);
        while (pcount == 0 && !tx_quit) pthread_cond_wait(&pace_cv, &pace_mu);
        if (tx_quit && pcount == 0) { pthread_mutex_unlock(&pace_mu); break; }
        txent_t e = pend[ptail]; ptail = (ptail + 1) % TXN; pcount--;
        slot_state[e.slot] = TXS_SENDING;
        pthread_cond_signal(&pace_cv);
        pthread_mutex_unlock(&pace_mu);
        /* size-proportional pacing: small P-frames (~15 frags) barely need
         * smoothing — scale the window by frame size so latency cost lands
         * only on the big bursts (IDR / high-bitrate frames) that can
         * actually overflow WiFi queues. */
        int win = C.pace_us * e.n / 24;
        if (win > C.pace_us) win = C.pace_us;
        if (win > 0 && C.fps > 0) {             /* never pace past ~60% of a frame slot */
            int cap = 600000 / C.fps;
            if (win > cap) win = cap;
        }
        int gap = (win > 0 && e.n > 1) ? win / e.n : 0;
        for (int i = 0; i < e.n; i++) {
            send_pkt(&txb[e.slot][i], i == e.n - 1, e.ts, e.idr);
            if (gap > 0 && i < e.n - 1) usleep(gap);
        }
        pthread_mutex_lock(&pace_mu);
        slot_state[e.slot] = TXS_FREE;
        pthread_cond_signal(&pace_cv);
        pthread_mutex_unlock(&pace_mu);
    }
    return NULL;
}

/* split one Annex-B-stripped NAL into RTP payload fragments */
static void packetise_nal(const uint8_t *nal, int len) {
    uint8_t tmp[RTP_MTU];
    if (len <= 2) return;

    if (!C.hevc) {                                    /* RFC 3984, H.264 */
        uint8_t type = nal[0] & 0x1f;
        if (len <= RTP_MTU) { push_frag(nal, len); return; }
        uint8_t ind = (uint8_t)((nal[0] & 0xe0) | 28);   /* FU-A */
        const uint8_t *p = nal + 1;
        int left = len - 1;
        int first = 1;
        while (left > 0) {
            int chunk = left;
            if (chunk > RTP_MTU - 2) chunk = RTP_MTU - 2;
            left -= chunk;
            tmp[0] = ind;
            tmp[1] = (uint8_t)((first ? 0x80 : 0) | (left ? 0 : 0x40) | type);
            memcpy(tmp + 2, p, chunk);
            push_frag(tmp, chunk + 2);
            p += chunk; first = 0;
        }
    } else {                                          /* RFC 7798, H.265 */
        uint8_t type = (uint8_t)((nal[0] >> 1) & 0x3f);
        if (len <= RTP_MTU) { push_frag(nal, len); return; }
        uint8_t ph0 = (uint8_t)((nal[0] & 0x81) | (49 << 1)); /* FU */
        uint8_t ph1 = nal[1];
        const uint8_t *p = nal + 2;
        int left = len - 2;
        int first = 1;
        while (left > 0) {
            int chunk = left;
            if (chunk > RTP_MTU - 3) chunk = RTP_MTU - 3;
            left -= chunk;
            tmp[0] = ph0; tmp[1] = ph1;
            tmp[2] = (uint8_t)((first ? 0x80 : 0) | (left ? 0 : 0x40) | type);
            memcpy(tmp + 3, p, chunk);
            push_frag(tmp, chunk + 3);
            p += chunk; first = 0;
        }
    }
}

/* find Annex-B start code at/after pos; return offset and its length */
static int next_start(const uint8_t *d, int len, int from, int *sclen) {
    for (int i = from; i + 3 < len; i++) {
        if (d[i] == 0 && d[i + 1] == 0) {
            if (d[i + 2] == 1) { *sclen = 3; return i; }
            if (d[i + 2] == 0 && d[i + 3] == 1) { *sclen = 4; return i; }
        }
    }
    return -1;
}

typedef struct { const uint8_t *p; int n; } nal_t;

static int split_annexb(const uint8_t *d, int len, nal_t *out, int max) {
    int pos = 0, n = 0, sc;
    int s = next_start(d, len, 0, &sc);
    if (s < 0) return 0;
    while (s >= 0 && n < max) {
        int start = s + sc;
        int e = next_start(d, len, start, &sc);
        int end = (e < 0) ? len : e;
        while (end > start && d[end - 1] == 0) end--;   /* trailing_zero_8bits */
        if (end > start) { out[n].p = d + start; out[n].n = end - start; n++; }
        if (e < 0) break;
        s = e;
    }
    return n;
}

/* VPS/SPS/PPS captured once from MPP_ENC_GET_HDR_SYNC */
static uint8_t *hdr_nals[MAX_NAL];
static int hdr_len[MAX_NAL], hdr_cnt = 0;

static void save_headers(const uint8_t *d, int len) {
    nal_t nals[MAX_NAL];
    int n = split_annexb(d, len, nals, MAX_NAL);
    for (int i = 0; i < n && hdr_cnt < MAX_NAL; i++) {
        hdr_len[hdr_cnt] = nals[i].n;
        hdr_nals[hdr_cnt] = malloc(nals[i].n);
        memcpy(hdr_nals[hdr_cnt], nals[i].p, nals[i].n);
        hdr_cnt++;
    }
    printf("[hdr] %d parameter-set NALs (VPS/SPS/PPS), %d B total\n", hdr_cnt, len);
}

static void inject_headers(void) {
    for (int i = 0; i < hdr_cnt; i++) packetise_nal(hdr_nals[i], hdr_len[i]);
}

/* hand the finished frame to the pacer thread: enqueue its slot, then claim
 * a fresh free slot for the next frame. Waits only if the sender has fallen
 * more than a frame or two behind (never corrupts, just adds latency). */
static void flush_frags(void) {
    uint32_t ts = (uint32_t)((now_s() - t0) * 90000.0);
    pthread_mutex_lock(&pace_mu);
    /* need one slot left over (besides build + queued + sending) for the
     * next frame; also require the build slot itself to become free —
     * free_now counts it, so wait while free < 2 */
    for (;;) {
        int free_slots = 0;
        for (int i = 0; i < TXN; i++) if (slot_state[i] == TXS_FREE) free_slots++;
        if (free_slots >= 2) break;
        pthread_cond_wait(&pace_cv, &pace_mu);
    }
    pend[phead] = (txent_t){ build_slot, nfrag, ts, g_idr };
    phead = (phead + 1) % TXN; pcount++;
    slot_state[build_slot] = TXS_QUEUED;
    for (int i = 0; i < TXN; i++)
        if (slot_state[i] == TXS_FREE) { build_slot = i; break; }
    nfrag = 0; g_idr = 0;
    pthread_cond_signal(&pace_cv);
    pthread_mutex_unlock(&pace_mu);
    stat_frames++;
}

/* ------------------------------------------------------------- V4L2 capture */
typedef struct { void *map; size_t len; unsigned mem_off; int dmafd; MppBuffer mb; } vbuf_t;
static vbuf_t vb[NBUF];
static int vfd = -1, vcount = 0;

static int v4l2_init(void) {
    /* NONBLOCK is essential: if the rkaiq IQ engine dies, a blocking
     * DQBUF sleeps forever and makes the process unkillable, leaving the
     * video19 queue busy for every subsequent sender. */
    vfd = open(C.dev, O_RDWR | O_NONBLOCK);
    if (vfd < 0) { fprintf(stderr, "[FATAL] open %s: %s\n", C.dev, strerror(errno)); return -1; }

    struct v4l2_format fmt; memset(&fmt, 0, sizeof fmt);
    fmt.type = BT;
    if (xioctl(vfd, VIDIOC_G_FMT, &fmt)) { perror("G_FMT"); return -1; }
    printf("[v4l2] %s G_FMT %ux%u %c%c%c%c\n", C.dev,
           fmt.fmt.pix_mp.width, fmt.fmt.pix_mp.height,
           (int)(fmt.fmt.pix_mp.pixelformat & 0xff),
           (int)((fmt.fmt.pix_mp.pixelformat >> 8) & 0xff),
           (int)((fmt.fmt.pix_mp.pixelformat >> 16) & 0xff),
           (int)((fmt.fmt.pix_mp.pixelformat >> 24) & 0xff));
    /* nv12 source: ALWAYS S_FMT. Skipping it when G_FMT already matches is a
     * trap: a previously aborted S_FMT (e.g. rejected 1920x1080 on rkispp
     * scale2) leaves the node with a stale bytesperline (1920) while G_FMT
     * still reports the old WxH - every QBUF then fails EINVAL forever.
     * Re-issuing S_FMT forces the driver to recompute bpl from width. RAW
     * source keeps the strict check (rkcif always has a negotiated format). */
    if (!is_raw()) {
        /* crop 模式：S_FMT 用采集尺寸（必须正好等于 ISPP 输入 2560x1440 才能走
         * 驱动 bypass 放行），编码侧再中心裁切到 w x h。 */
        int cw = (C.capw > 0) ? C.capw : C.w;
        int ch = (C.caph > 0) ? C.caph : C.h;
        fmt.fmt.pix_mp.width = cw;
        fmt.fmt.pix_mp.height = ch;
        fmt.fmt.pix_mp.pixelformat = V4L2_PIX_FMT_NV12;
        /* 关键修复（720p 切换失效根因）：清零 bytesperline/sizeimage 再 S_FMT。
         * 驱动 rkispp_set_fmt 对 plane0 取 max(用户 bpl, 计算值)，而上面 G_FMT
         * 带回的是上一次会话的旧 bpl（如 1440p 旁路的 2560）→ 驱动"合法"保留
         * 2560 → REQBUFS/QBUF 尺寸不匹配 → QBUF EINVAL 永挂。清零后驱动按当前
         * 宽度重算（1280x720 → bpl 1280），任意分辨率来回切换都干净。 */
        {
            unsigned int pi;
            for (pi = 0; pi < VIDEO_MAX_PLANES; pi++) {
                fmt.fmt.pix_mp.plane_fmt[pi].bytesperline = 0;
                fmt.fmt.pix_mp.plane_fmt[pi].sizeimage = 0;
            }
        }
        if (xioctl(vfd, VIDIOC_S_FMT, &fmt)) {
            fprintf(stderr, "[FATAL] S_FMT %dx%d NV12: %s\n", cw, ch, strerror(errno));
            return -1;
        }
        /* re-read negotiated geometry */
        memset(&fmt, 0, sizeof fmt); fmt.type = BT;
        if (xioctl(vfd, VIDIOC_G_FMT, &fmt)) { perror("G_FMT2"); return -1; }
        printf("[v4l2] S_FMT -> %ux%u NV12 sizeimage=%u bpl=%u\n",
               fmt.fmt.pix_mp.width, fmt.fmt.pix_mp.height,
               fmt.fmt.pix_mp.plane_fmt[0].sizeimage,
               fmt.fmt.pix_mp.plane_fmt[0].bytesperline);
    }

    src_w = fmt.fmt.pix_mp.width;
    src_h = fmt.fmt.pix_mp.height;
    src_stride = fmt.fmt.pix_mp.plane_fmt[0].bytesperline;
    /* capture/encode split for center-crop */
    cropping = 0;
    if (!is_raw() && C.capw > 0 && C.caph > 0 && (C.capw != C.w || C.caph != C.h)) {
        if (src_w != (unsigned)C.capw || src_h != (unsigned)C.caph) {
            fprintf(stderr, "[FATAL] crop: S_FMT got %ux%u, want capture %dx%d\n",
                    src_w, src_h, C.capw, C.caph);
            return -1;
        }
        if (C.w > C.capw || C.h > C.caph) {
            fprintf(stderr, "[FATAL] crop: encode %dx%d larger than capture %dx%d\n",
                    C.w, C.h, C.capw, C.caph);
            return -1;
        }
        cropping = 1;
        cap_stride = src_stride;
        cap_fsize = fmt.fmt.pix_mp.plane_fmt[0].sizeimage;
        printf("[crop] capture %dx%d (bpl %d) -> encode %dx%d center-crop\n",
               C.capw, C.caph, cap_stride, C.w, C.h);
    }
    /* RAW10 packed needs bpl >= w*1.25; NV12 is fine with bpl == w. */
    if (is_raw()) {
        if (src_w <= 0 || src_h <= 0 || src_stride < src_w * 5 / 4) {
            fprintf(stderr, "[FATAL] implausible RAW geometry %dx%d bpl=%d\n",
                    src_w, src_h, src_stride);
            return -1;
        }
    } else {
        if (src_w <= 0 || src_h <= 0 || src_stride < src_w) {
            fprintf(stderr, "[FATAL] implausible NV12 geometry %dx%d bpl=%d\n",
                    src_w, src_h, src_stride);
            return -1;
        }
    }
    if (is_raw()) {
        /* rkcif already negotiated BG10 with the sensor. NEVER call S_FMT
         * here: the ISP/rkcif link is shared and S_FMT can wedge the whole
         * pipeline. Just keep the format and read the packed RAW10 bytes. */
        if (fmt.fmt.pix_mp.pixelformat != v4l2_fourcc('B','G','1','0')) {
            printf("[v4l2][warn] expected BG10, got %c%c%c%c - decode still attempted\n",
                   (int)(fmt.fmt.pix_mp.pixelformat & 0xff),
                   (int)((fmt.fmt.pix_mp.pixelformat >> 8) & 0xff),
                   (int)((fmt.fmt.pix_mp.pixelformat >> 16) & 0xff),
                   (int)((fmt.fmt.pix_mp.pixelformat >> 24) & 0xff));
        }
        printf("[v4l2] RAW source %dx%d bpl=%d sizeimage=%u (keep G_FMT, no S_FMT)\n",
               src_w, src_h, src_stride, fmt.fmt.pix_mp.plane_fmt[0].sizeimage);
    } else {
        /* A previous sender may have left the queue streaming; calling
         * S_FMT then fails with EBUSY. The format is already correct. */
        printf("[v4l2] format %dx%d NV12 (stride %d)\n", src_w, src_h, src_stride);
    }

    struct v4l2_requestbuffers rb; memset(&rb, 0, sizeof rb);
    rb.count = C.vbufs; rb.type = BT;
    rb.memory = is_raw() ? V4L2_MEMORY_MMAP : V4L2_MEMORY_DMABUF;
    if (xioctl(vfd, VIDIOC_REQBUFS, &rb)) { perror("REQBUFS"); return -1; }
    vcount = rb.count;
    printf("[v4l2] REQBUFS %s got %d\n", is_raw() ? "MMAP" : "DMABUF", vcount);

    if (is_raw()) {
        /* RAW path: kernel-allocated MMAP buffers, CPU reads the packed
         * RAW10 bytes directly. QUERYBUF/EXPBUF are not needed. */
        for (int i = 0; i < vcount; i++) {
            struct v4l2_buffer b; memset(&b, 0, sizeof b);
            struct v4l2_plane planes[MAXP]; memset(planes, 0, sizeof planes);
            b.type = BT; b.memory = V4L2_MEMORY_MMAP; b.index = i;
            b.length = 1; b.m.planes = planes;
            if (xioctl(vfd, VIDIOC_QUERYBUF, &b)) { perror("QUERYBUF"); return -1; }
            vb[i].len = planes[0].length;
            vb[i].mem_off = planes[0].m.mem_offset;
            vb[i].dmafd = -1; vb[i].mb = NULL; vb[i].map = MAP_FAILED;
            vb[i].map = mmap(NULL, vb[i].len, PROT_READ | PROT_WRITE, MAP_SHARED,
                             vfd, planes[0].m.mem_offset);
            if (vb[i].map == MAP_FAILED) { perror("mmap"); return -1; }
            printf("[raw] buf%d mmap %zu bytes (CPU-written NV12 goes to MPP heap)\n",
                   i, vb[i].len);
        }
    } else {
        /* Vendor rkisp/rkispp capture nodes do NOT implement QUERYBUF
         * (verified: both rkisp_v4 mainpath and rkispp_v0 scale2 return
         * ENOTTY). The UAPI2 rockit world expects DMABUF import: we
         * allocate our own DRM buffers via MPP and hand the fds to the
         * driver with QBUF(memory=DMABUF). Zero copy into the encoder
         * unless cropping - then the buffer only feeds the crop copy. */
        const size_t fsize2 = cropping ? cap_fsize : (size_t)g_hs * g_vs * 3 / 2;
        RK_S32 r2 = p_grp_get(&frmgrp, MPP_BUFFER_TYPE_DRM,
                              MPP_BUFFER_INTERNAL, "h264out", "nv12");
        if (r2 || !frmgrp) { printf("[FATAL] nv12 DRM group ret=%d\n", r2); return -1; }
        for (int i = 0; i < vcount; i++) {
            MppBuffer mb2 = NULL;
            r2 = p_buf_get(frmgrp, &mb2, fsize2, "h264out", "nv12");
            if (r2 || !mb2) { printf("[FATAL] nv12 DRM buf %d ret=%d\n", i, r2); return -1; }
            vb[i].len = fsize2;
            vb[i].mem_off = 0;
            MppBufferInfo binfo; memset(&binfo, 0, sizeof binfo);
            p_buf_info(mb2, &binfo, "nv12");
            vb[i].dmafd = binfo.fd;
            vb[i].mb = mb2;
            vb[i].map = (uint8_t *)p_buf_ptr(mb2, "nv12");
            printf("[nv12] buf%d DRM fd=%d size=%zu ptr=%p\n",
                   i, vb[i].dmafd, fsize2, (void *)vb[i].map);
            if (vb[i].dmafd < 0) { printf("[FATAL] nv12 buf%d: no dma fd\n", i); return -1; }
            if (vb[i].map == NULL) printf("[nv12][warn] buf%d: no CPU ptr (dump disabled)\n", i);
        }
        /* crop 模式：另分配 3 块编码侧 DRM 缓冲（RGA/行拷贝目标，hs=对齐 g_hs） */
        if (cropping) {
            const size_t dfsize = (size_t)g_hs * g_vs * 3 / 2;
            for (int i = 0; i < ncropbuf; i++) {
                RK_S32 r3 = p_buf_get(frmgrp, &cropbuf[i], dfsize, "h264out", "crop");
                if (r3 || !cropbuf[i]) { printf("[FATAL] crop dst buf %d ret=%d\n", i, r3); return -1; }
                cropptr[i] = (uint8_t *)p_buf_ptr(cropbuf[i], "crop");
                if (!cropptr[i]) { printf("[FATAL] crop dst buf %d: no CPU ptr\n", i); return -1; }
                {
                    MppBufferInfo ci2; memset(&ci2, 0, sizeof ci2);
                    p_buf_info(cropbuf[i], &ci2, "crop");
                    cropfd[i] = ci2.fd;
                }
                memset(cropptr[i], 128, dfsize);
            }
            printf("[crop] %d dst bufs %zu bytes (hs=%d vs=%d)\n", ncropbuf, dfsize, g_hs, g_vs);
            /* RGA 探测（legacy c_RkRgaBlit） */
            rga_h = dlopen("librga.so.2", RTLD_NOW | RTLD_LOCAL);
            if (!rga_h) rga_h = dlopen("librga.so", RTLD_NOW | RTLD_LOCAL);
            if (rga_h) {
                p_blit = (int (*)(rga_info_l *, rga_info_l *, rga_info_l *))dlsym(rga_h, "c_RkRgaBlit");
                rga_ok = p_blit != NULL;
                if (rga_ok) {
                    int (*init_fn)(void) = (int (*)(void))dlsym(rga_h, "c_RkRgaInit");
                    if (init_fn) { int ir = init_fn();
                        printf("[crop] c_RkRgaInit ret=%d\n", ir); }
                }
            }
            printf("[crop] RGA %s (%s)\n", rga_ok ? "READY" : "unavailable",
                   rga_ok ? "c_RkRgaBlit legacy" : "no librga/c_RkRgaBlit");
        }
    }
    return 0;
}

static int v4l2_streamon(void) {
    for (int i = 0; i < vcount; i++) {
        struct v4l2_buffer b; memset(&b, 0, sizeof b);
        struct v4l2_plane planes[MAXP]; memset(planes, 0, sizeof planes);
        b.type = BT;
        b.memory = is_raw() ? V4L2_MEMORY_MMAP : V4L2_MEMORY_DMABUF;
        b.index = i;
        b.length = 1; b.m.planes = planes;
        if (is_raw()) {
            b.m.planes[0].m.mem_offset = vb[i].mem_off;
            b.m.planes[0].length = vb[i].len;
        } else {
            b.m.planes[0].m.fd = vb[i].dmafd;
            b.m.planes[0].length = vb[i].len;
        }
        if (xioctl(vfd, VIDIOC_QBUF, &b)) { perror("QBUF"); return -1; }
    }
    enum v4l2_buf_type t = BT;
    if (xioctl(vfd, VIDIOC_STREAMON, &t)) {
        fprintf(stderr, "[FATAL] STREAMON: %s (is the rkaiq IQ engine running?)\n",
                strerror(errno));
        return -1;
    }
    printf("[v4l2] STREAMON OK\n");
    return 0;
}

/* ------------------------------------------------------------- MPP encoder */
static MppCtx ctx = NULL;
static MppApi *mpi = NULL;
static MppEncCfg cfg = NULL;
static MppBufferGroup grp = NULL;
static MppBuffer hdrbuf = NULL;
/* Rotating CPU-written NV12 buffers for --src raw. MPP keeps a reference to
 * the frame until the packet is produced, so never overwrite the same buffer
 * on consecutive frames. */
static MppBufferGroup frmgrp = NULL;
static MppBuffer frmbuf[NFRMBUF];
static uint8_t *frmptr[NFRMBUF];
static int frmidx = 0;

static int mpp_init_all(void) {
    H = dlopen(LIB, RTLD_NOW | RTLD_GLOBAL);
    if (!H) { fprintf(stderr, "[FATAL] dlopen %s: %s\n", LIB, dlerror()); return -1; }
    SYM("mpp_create",                    p_create,      F_create);
    SYM("mpp_init",                       p_init,        F_init);
    SYM("mpp_destroy",                    p_destroy,     F_destroy);
    SYM("mpp_enc_cfg_init",                p_cfg_init,    F_cfg_init);
    SYM("mpp_enc_cfg_deinit",            p_cfg_deinit,  F_cfg_deinit);
    SYM("mpp_enc_cfg_set_s32",            p_cfg_set32,   F_cfg_set32);
    SYM("mpp_buffer_group_get",            p_grp_get,     F_grp_get);
    SYM("mpp_buffer_group_put",            p_grp_put,     F_grp_put);
    SYM("mpp_buffer_get_with_tag",         p_buf_get,     F_buf_get);
    SYM("mpp_buffer_put_with_caller",      p_buf_put,     F_buf_put);
    SYM("mpp_buffer_get_ptr_with_caller",  p_buf_ptr,     F_buf_ptr);
    /* old MPP (b2ee155) has no mpp_buffer_get_fd; info query carries the fd */
    SYM("mpp_buffer_info_get_with_caller", p_buf_info,    F_buf_info);
    SYM("mpp_buffer_import_with_tag",      p_buf_import,  F_buf_import);
    SYM("mpp_frame_init",                 p_frame_init,  F_frame_init);
    SYM("mpp_frame_deinit",               p_frame_deinit,F_frame_deinit);
    SYM("mpp_frame_set_width",             p_set_w,       F_frame_seti);
    SYM("mpp_frame_set_height",           p_set_h,       F_frame_seti);
    SYM("mpp_frame_set_hor_stride",        p_set_hs,      F_frame_seti);
    SYM("mpp_frame_set_ver_stride",        p_set_vs,      F_frame_seti);
    SYM("mpp_frame_set_fmt",               p_set_fmt,     F_frame_seti);
    SYM("mpp_frame_set_eos",               p_set_eos,     F_frame_seti);
    SYM("mpp_frame_set_pts",               p_set_pts,     F_frame_setpts);
    SYM("mpp_frame_set_buffer",            p_set_buf,     F_frame_setbuf);
    SYM("mpp_packet_init_with_buffer",     p_pkt_initbuf, F_pkt_initbuf);
    SYM("mpp_packet_deinit",              p_pkt_deinit,  F_pkt_deinit);
    SYM("mpp_packet_set_length",          p_pkt_setlen,  F_pkt_setlen);
    SYM("mpp_packet_get_pos",             p_pkt_pos,     F_pkt_pos);
    SYM("mpp_packet_get_length",          p_pkt_len,     F_pkt_len);

    RK_S32 r = p_create(&ctx, &mpi);
    if (r || !mpi) { fprintf(stderr, "[FATAL] mpp_create ret=%d\n", r); return -1; }
    if (mpi->size != sizeof(MppApi)) {
        fprintf(stderr, "[FATAL] MppApi ABI mismatch: runtime=%u compile=%u\n",
                mpi->size, (unsigned)sizeof(MppApi));
        return -1;
    }
    MppCodingType coding = C.hevc ? MPP_VIDEO_CodingHEVC : MPP_VIDEO_CodingAVC;
    r = p_init(ctx, MPP_CTX_ENC, coding);
    printf("[mpp] init coding=0x%x ret=%d\n", (unsigned)coding, r);
    if (r) return -1;

    /* The dma-buf/rkispp path needed a 100 ms input timeout (1 ms made every
     * encode_put_frame fail). The CPU-written heap path must keep MPP's
     * DEFAULT timeouts -- mppprobe proves those work, and overriding them
     * makes the VEPU541 HAL fail MPP_IOC_CFG_V1 with ENOMEM. */
    if (!is_raw()) {
        RK_S64 in_to = 100, out_to = 1;
        mpi->control(ctx, MPP_SET_INPUT_TIMEOUT,  (MppParam)&in_to);
        mpi->control(ctx, MPP_SET_OUTPUT_TIMEOUT, (MppParam)&out_to);
    }

    p_cfg_init(&cfg);
    mpi->control(ctx, MPP_ENC_GET_CFG, cfg);
#define SET(k, v) do { RK_S32 _r = p_cfg_set32(cfg, k, v); \
        if (_r) printf("[mpp][warn] %-22s = %d ret=%d\n", k, v, _r); } while (0)
    int hs = g_hs, vs = g_vs;
    SET("prep:width", C.w);
    SET("prep:height", C.h);
    SET("prep:hor_stride", hs);
    SET("prep:ver_stride", vs);
    SET("prep:format", MPP_FMT_YUV420SP);
    SET("rc:mode", MPP_ENC_RC_MODE_CBR);
    SET("rc:fps_in_num", C.fps);
    SET("rc:fps_out_num", C.fps);
    SET("rc:gop", C.gop > 0 ? C.gop : C.fps);
    SET("rc:bps_target", C.kbps * 1000);
    /* Tight CBR window: steadier per-frame size, smaller VBR bursts that
     * pile up in the UDP path and show as latency in QGC. */
    SET("rc:bps_max", C.kbps * 1000 * 11 / 10);
    SET("rc:bps_min", C.kbps * 1000 * 9 / 10);
    SET("rc:mb_rc", C.mbrc);
    SET("codec:type", (int)coding);
    r = mpi->control(ctx, MPP_ENC_SET_CFG, cfg);
    printf("[mpp] SET_CFG ret=%d (%dx%d stride %dx%d, %d kbps, gop %d, mbrc %d)\n",
           r, C.w, C.h, hs, vs, C.kbps, C.gop > 0 ? C.gop : C.fps, C.mbrc);
    if (r) return -1;

    /* weak-link: cyclic intra refresh (MPP_ENC_SET_RC_CFG deprecated-but-
     * working path; vendor lib prints intra_refresh_mode/arg from it).
     * Every frame refreshes C.cir MB rows: loss artifacts repair gradually
     * instead of waiting for a whole IDR, and IDR bursts stay small. */
    if (C.cir > 0) {
        MppEncRcCfg rccfg;
        memset(&rccfg, 0, sizeof rccfg);
        rccfg.change = MPP_ENC_RC_CFG_CHANGE_REFRESH;
        rccfg.refresh_en = 1;
        rccfg.refresh_mode = MPP_ENC_RC_INTRA_REFRESH_ROW;
        rccfg.refresh_num = C.cir;
        int rr = mpi->control(ctx, MPP_ENC_SET_RC_CFG, &rccfg);
        printf("[mpp] SET_RC_CFG CIR %d MB-rows/frame ret=%d (full cycle %d frames)\n",
               C.cir, rr, (C.h + 15) / 16 / C.cir);
    }

    /* Buffers + header. The raw path mirrors mppprobe's known-good setup
     * EXACTLY: one NORMAL group, frame buffers first, then packet and
     * motion-info buffers, and GET_HDR_SYNC on the packet buffer. */
    const size_t fsize = (size_t)hs * vs * 3 / 2;
    if (is_raw()) {
        RK_S32 r2 = p_grp_get(&frmgrp, (MppBufferType)C.fbuf_type,
                              MPP_BUFFER_INTERNAL, "h264out", "raw");
        printf("[raw] MppBufferType=0x%x\n", C.fbuf_type);
        if (r2 || !frmgrp) { printf("[FATAL] frame group ret=%d\n", r2); return -1; }
        for (int i = 0; i < C.nfrmbuf; i++) {
            r2 = p_buf_get(frmgrp, &frmbuf[i], fsize, "h264out", "raw");
            if (r2 || !frmbuf[i]) { printf("[FATAL] frame buf %d ret=%d\n", i, r2); return -1; }
            frmptr[i] = (uint8_t *)p_buf_ptr(frmbuf[i], "raw");
            if (!frmptr[i]) { printf("[FATAL] frame buf %d: no CPU ptr\n", i); return -1; }
            memset(frmptr[i], 128, fsize);
        }
        MppBuffer pktbuf = NULL, mdbuf = NULL;
        r2 = p_buf_get(frmgrp, &pktbuf, (size_t)hs * vs, "h264out", "pkt");
        r2 |= p_buf_get(frmgrp, &mdbuf, (size_t)hs * vs / 8, "h264out", "md");
        printf("[raw] %d x %zu-byte NV12 @ %p (hs=%d vs=%d) pkt=%p md=%p ret=%d\n",
               C.nfrmbuf, fsize, (void *)frmptr[0], hs, vs,
               (void *)pktbuf, (void *)mdbuf, r2);
        grp = frmgrp;
        hdrbuf = pktbuf;
        if (raw_init()) return -1;
    } else {
        p_grp_get(&grp, MPP_BUFFER_TYPE_NORMAL, MPP_BUFFER_INTERNAL, "h264out", "hdr");
        p_buf_get(grp, &hdrbuf, (size_t)65536, "h264out", "hdr");
    }

    MppPacket hp = NULL;
    if (hdrbuf && !p_pkt_initbuf(&hp, hdrbuf)) {
        p_pkt_setlen(hp, 0);
        r = mpi->control(ctx, MPP_ENC_GET_HDR_SYNC, hp);
        int hl = (int)p_pkt_len(hp);
        printf("[mpp] GET_HDR_SYNC ret=%d len=%d\n", r, hl);
        if (!r && hl > 0) save_headers((uint8_t *)p_pkt_pos(hp), hl);
        p_pkt_deinit(&hp);
    }
    return hdr_cnt ? 0 : -1;
}

/* parse one encoded Annex-B packet into RTP fragments and send it */
static void emit_packet(MppPacket pkt) {
    size_t len = p_pkt_len(pkt);
    uint8_t *p = (uint8_t *)p_pkt_pos(pkt);
    if (len <= 0) return;
    nal_t nals[MAX_NAL];
    int n = split_annexb(p, (int)len, nals, MAX_NAL);
    if (!n) return;
    /* QGC joins an in-progress UDP stream with no SDP exchange: repeat the
     * VPS/SPS/PPS (or SPS/PPS) once per second so it can start decoding. */
    if (stat_frames % HDR_INJECT_INTERVAL == 0) inject_headers();
    /* keyframe detection for send-time duplication (H.264 type 5,
     * H.265 IDR_W_RADL 19 / IDR_N_LP 20) */
    for (int i = 0; i < n && !g_idr; i++) {
        if (!C.hevc) { if ((nals[i].p[0] & 0x1f) == 5) g_idr = 1; }
        else         { int t = (nals[i].p[0] >> 1) & 0x3f; if (t == 19 || t == 20) g_idr = 1; }
    }
    for (int i = 0; i < n; i++) packetise_nal(nals[i].p, nals[i].n);
    if (nfrag) flush_frags();
}

/* feed one captured V4L2 buffer; drain any encoded packets MPP has ready.
 * Returns the number of access units emitted. */
static int encode_one(MppBuffer mb) {
    MppFrame frame = NULL;
    if (p_frame_init(&frame)) return 0;
    p_set_w(frame, C.w); p_set_h(frame, C.h);
    p_set_hs(frame, g_hs); p_set_vs(frame, g_vs);
    p_set_fmt(frame, MPP_FMT_YUV420SP);
    p_set_buf(frame, mb);
    double t0 = now_s();
    RK_S32 r = mpi->encode_put_frame(ctx, frame);
    double t1 = now_s();
    double put_ms = (t1 - t0) * 1000.0;
    p_frame_deinit(&frame);
    if (r) { printf("[mpp][warn] put_frame ret=%d\n", r); return 0; }
    if (put_ms < 1000) { sum_put += put_ms; if (put_ms > max_put) max_put = put_ms; }

    int got = 0;
    double tg0 = now_s();
    for (int attempt = 0; attempt < 6; attempt++) {
        MppPacket pkt = NULL;
        r = mpi->encode_get_packet(ctx, &pkt);
        if (r || !pkt) { usleep(200); continue; }
        emit_packet(pkt);
        p_pkt_deinit(&pkt);
        got++;
    }
    double get_ms = (now_s() - tg0) * 1000.0;
    if (get_ms < 1000) { sum_get += get_ms; if (get_ms > max_get) max_get = get_ms; }
    return got;
}

/* Diagnostic: downsample the captured Y plane to a small PGM so we can tell
 * whether the ISP itself produced a black frame or whether the dma-buf import
 * handed MPP the wrong memory. */
static void dump_first_frame(const uint8_t *y, int stride) {
    if (!C.dump[0]) return;
    char path[128]; snprintf(path, sizeof path, "%s", C.dump);
    C.dump[0] = 0;                          /* one shot */
    const int dw = 640, dh = 360;
    FILE *f = fopen(path, "wb");
    if (!f) { printf("[dump] fopen %s failed: %s\n", path, strerror(errno)); return; }
    fprintf(f, "P5\n%d %d\n255\n", dw, dh);
    for (int j = 0; j < dh; j++)
        for (int i = 0; i < dw; i++) {
            int sx = i * C.w / dw, sy = j * C.h / dh;
            fputc(y[sy * stride + sx], f);
        }
    fclose(f);
    printf("[dump] wrote 640x360 greyscale PGM -> %s\n", path);
}

/* ------------------------------------------------------------------ main */
static void usage(void) {
    printf("h264out: rkcif RAW10 (software NV12) or rkispp NV12 -> MPP HW H.264/H.265 -> RTP/UDP\n"
           "  --src raw|nv12     (default raw = /dev/video0 BG10; nv12 = /dev/video19 legacy)\n"
           "  --ip IP           (default 192.168.6.88)\n"
           "  --port N          (default 5600)\n"
           "  --codec h264|h265 (default h264; PT 96 / 97)\n"
           "  --w --h           (default 640 360 encoder output)\n"
           "  --box N           (raw only, Bayer binning 2..4, default 2)\n"
           "  --chroma N        (chroma attenuation %, default 100; 0 = greyscale)\n"
           "  --cbox N          (chroma denoise radius 1..10, default 3)\n"
           "  --threads N       (RAW->NV12 workers 1..2, default 2)\n"
           "  --vbufs N         (V4L2 MMAP buffers, raw default 2, max 6)\n"
           "  --nfrmbuf N       (MPP frame buffers 1..4, default 1)\n"
           "  --fbuf-type N     (MppBufferType, default 0x3 DRM; 0x1 NORMAL)\n"
           "  --fps N           (default 15)\n"
           "  --kbps N          (default 3000; use 1500 for lower latency)\n"
           "  --gop N           (GOP frames, default = fps; larger = fewer IDR bursts)\n"
           "  --mbrc 0|1        (rc:mb_rc per-MB RC, default 0 = frame-level)\n"
           "  --frames N        (0 = infinite)\n"
           "  --dev PATH        (default /dev/video0 for raw, /dev/video19 for nv12)\n"
           "  --dump FILE       (first frame Y plane as 640x360 PGM)\n"
           "  --mirror IP:PORT  (optional second destination)\n"
           "  --pace US         (weak-link: spread each frame's RTP packets over\n"
           "                     US microseconds, default 8000; 0 = burst)\n"
           "  --dup-idr         (send every IDR packet twice with the same seq;\n"
           "                     OFF by default — PixelPilot's depacketizer\n"
           "                     cannot handle duplicate seqs mid-FU-A)\n"
           "  --cir N           (weak-link: cyclic intra refresh, N MB rows per\n"
           "                     frame, default 2; 0 = off. Loss artifacts heal\n"
           "                     gradually, no giant IDR burst)\n"
           "  --dry             (encode, do not send UDP)\n");
}

int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IOLBF, 0);
    int dev_set = 0;
    for (int i = 1; i < argc; i++) {
        char *a = argv[i];
        if      (!strcmp(a, "--help"))  { usage(); return 0; }
        else if (!strcmp(a, "--dry"))  C.dry = 1;
        else if (!strcmp(a, "--ip") && i+1 < argc)     snprintf(C.ip, sizeof C.ip, "%s", argv[++i]);
        else if (!strcmp(a, "--dev") && i+1 < argc)  { snprintf(C.dev, sizeof C.dev, "%s", argv[++i]); dev_set = 1; }
        else if (!strcmp(a, "--src") && i+1 < argc)     snprintf(C.src, sizeof C.src, "%s", argv[++i]);
        else if (!strcmp(a, "--box") && i+1 < argc)     C.box = atoi(argv[++i]);
        else if (!strcmp(a, "--chroma") && i+1 < argc)  C.chroma = atoi(argv[++i]);
        else if (!strcmp(a, "--cbox") && i+1 < argc)    C.cbox = atoi(argv[++i]);
        else if (!strcmp(a, "--nowb"))                  C.nowb = 1;
        else if (!strcmp(a, "--threads") && i+1 < argc) C.threads = atoi(argv[++i]);
        else if (!strcmp(a, "--vbufs") && i+1 < argc)   C.vbufs = atoi(argv[++i]);
        else if (!strcmp(a, "--nfrmbuf") && i+1 < argc) C.nfrmbuf = atoi(argv[++i]);
        else if (!strcmp(a, "--fbuf-type") && i+1 < argc) C.fbuf_type = (unsigned)strtoul(argv[++i], NULL, 0);
        else if (!strcmp(a, "--dump") && i+1 < argc)  snprintf(C.dump, sizeof C.dump, "%s", argv[++i]);
        else if (!strcmp(a, "--nv12dump") && i+1 < argc) snprintf(nv12dump, sizeof nv12dump, "%s", argv[++i]);
        else if (!strcmp(a, "--rawdump") && i+1 < argc) snprintf(rawdump, sizeof rawdump, "%s", argv[++i]);
        else if (!strcmp(a, "--dumpdelay") && i+1 < argc) g_dumpdelay = atoi(argv[++i]);
        else if (!strcmp(a, "--port") && i+1 < argc)   C.port = atoi(argv[++i]);
        else if (!strcmp(a, "--w") && i+1 < argc)      C.w = atoi(argv[++i]);
        else if (!strcmp(a, "--h") && i+1 < argc)      C.h = atoi(argv[++i]);
        else if (!strcmp(a, "--capw") && i+1 < argc)   C.capw = atoi(argv[++i]);
        else if (!strcmp(a, "--caph") && i+1 < argc)   C.caph = atoi(argv[++i]);
        else if (!strcmp(a, "--fps") && i+1 < argc)     C.fps = atoi(argv[++i]);
        else if (!strcmp(a, "--kbps") && i+1 < argc)    C.kbps = atoi(argv[++i]);
        else if (!strcmp(a, "--gop") && i+1 < argc)     C.gop = atoi(argv[++i]);
        else if (!strcmp(a, "--mbrc") && i+1 < argc)    C.mbrc = atoi(argv[++i]);
        else if (!strcmp(a, "--frames") && i+1 < argc) C.frames = atoi(argv[++i]);
        else if (!strcmp(a, "--pace") && i+1 < argc)    C.pace_us = atoi(argv[++i]);
        else if (!strcmp(a, "--cir") && i+1 < argc)     C.cir = atoi(argv[++i]);
        else if (!strcmp(a, "--dup-idr"))               C.dup_idr = 1;
        else if (!strcmp(a, "--no-dup-idr"))            C.dup_idr = 0;
        else if (!strcmp(a, "--codec") && i+1 < argc) {
            const char *v = argv[++i];
            C.hevc = (!strncmp(v, "h265", 4) || !strncmp(v, "hevc", 4)) ? 1 : 0;
        }
        else if (!strcmp(a, "--mirror") && i+1 < argc) {
            char *m = argv[++i], *colon = strchr(m, ':');
            if (colon) { *colon = 0; snprintf(C.mirror_ip, sizeof C.mirror_ip, "%s", m);
                         C.mirror_port = atoi(colon + 1); }
        }
    }
    if (!dev_set || !C.dev[0])
        snprintf(C.dev, sizeof C.dev, "%s",
                 is_raw() ? "/dev/video0" : "/dev/video19");
    if (C.box < 2) C.box = 2;
    if (C.box > 4) C.box = 4;
    if (C.chroma < 0) C.chroma = 0;
    if (C.chroma > 100) C.chroma = 100;
    if (C.threads < 1) C.threads = 1;
    if (C.threads > 2) C.threads = 2;
    if (C.cbox < 1) C.cbox = 1;
    if (C.cbox > 10) C.cbox = 10;
    if (C.nfrmbuf < 1) C.nfrmbuf = 1;
    if (C.nfrmbuf > NFRMBUF) C.nfrmbuf = NFRMBUF;
    if (C.vbufs < 2) C.vbufs = is_raw() ? 2 : NBUF;
    if (C.vbufs > NBUF) C.vbufs = NBUF;
    if (C.w < 16 || C.h < 16 || C.fps < 1 || C.kbps < 1) {
        fprintf(stderr, "[FATAL] bad --w/--h/--fps/--kbps\n"); return 1;
    }
    printf("[h264out] src=%s dev=%s %s %dx%d @%d fps %dkbps box=%d chroma=%d%% cbox=%d wb=%s th=%d -> %s:%d %s%s\n",
           C.src, C.dev, C.hevc ? "H.265/PT97" : "H.264/PT96",
           C.w, C.h, C.fps, C.kbps, C.box, C.chroma, C.cbox,
           C.nowb ? "fixed" : "auto", C.threads,
           C.ip, C.port, C.dry ? "(DRY RUN)" : "",
           C.mirror_port ? " +mirror" : "");
    signal(SIGINT, on_signal); signal(SIGTERM, on_signal);

    memset(&dst, 0, sizeof dst);
    dst.sin_family = AF_INET; dst.sin_port = htons(C.port);
    dst.sin_addr.s_addr = inet_addr(C.ip);
    memset(&mirror, 0, sizeof mirror);
    mirror.sin_family = AF_INET; mirror.sin_port = htons(C.mirror_port);
    mirror.sin_addr.s_addr = inet_addr(C.mirror_ip);

    udp = socket(AF_INET, SOCK_DGRAM, 0);
    if (udp < 0) { perror("socket"); return 1; }
    int bufsz = 1 << 20;
    setsockopt(udp, SOL_SOCKET, SO_SNDBUF, &bufsz, sizeof bufsz);

    calc_strides();
    if (mpp_init_all()) { fprintf(stderr, "[FATAL] MPP init failed\n"); return 2; }
    if (v4l2_init())    { fprintf(stderr, "[FATAL] V4L2 init failed\n"); return 3; }
    if (v4l2_streamon()) { fprintf(stderr, "[FATAL] STREAMON failed\n"); return 4; }

    t0 = now_s();
    double t_start = t0, t_last = t0;
    int captured = 0, encoded = 0, timeouts = 0;
    pthread_t pacer_tid;
    if (pthread_create(&pacer_tid, NULL, pacer_thread, NULL) != 0) {
        fprintf(stderr, "[FATAL] pacer thread\n"); return 5;
    }
    printf("[pacer] weak-link mode: pace=%dus dup_idr=%s\n", C.pace_us, C.dup_idr ? "on" : "off");
    printf("[run] capture loop started (%s)\n",
           is_raw() ? "rkcif RAW10; IQ daemon must NOT be running"
                    : "IQ engine must stay alive");

    while (!stopping && (C.frames == 0 || captured < C.frames)) {
        fd_set rfds; FD_ZERO(&rfds); FD_SET(vfd, &rfds);
        struct timeval tv = { 2, 0 };
        int sr = select(vfd + 1, &rfds, NULL, NULL, &tv);
        if (sr < 0) { if (errno == EINTR) continue; perror("select"); break; }
        if (sr == 0) {
            timeouts++;
            printf("[warn] no frame for 2 s (%d/3) - is the rkaiq IQ engine alive?\n", timeouts);
            if (timeouts >= 3) { printf("[FATAL] capture stalled, giving up\n"); break; }
            continue;
        }
        timeouts = 0;

        struct v4l2_buffer db; memset(&db, 0, sizeof db);
        struct v4l2_plane planes[MAXP]; memset(planes, 0, sizeof planes);
        db.type = BT;
        db.memory = is_raw() ? V4L2_MEMORY_MMAP : V4L2_MEMORY_DMABUF;
        db.length = 1; db.m.planes = planes;
        if (xioctl(vfd, VIDIOC_DQBUF, &db)) {
            if (errno == EINTR || errno == EAGAIN) continue;
            printf("[FATAL] DQBUF: %s\n", strerror(errno)); break;
        }
        captured++;
        t_frame0 = now_s();
        double drv_ms = (t_frame0 - (db.timestamp.tv_sec + db.timestamp.tv_usec / 1e6)) * 1000.0;
        if (drv_ms > 0 && drv_ms < 1000) {
            sum_drv += drv_ms;
            if (drv_ms > max_drv) max_drv = drv_ms;
        }
        int can_dump = (g_frames_seen >= g_dumpdelay);
        g_frames_seen++;
        MppBuffer mb;
        if (is_raw()) {
            if (rawdump[0] && can_dump) {
                FILE *rf = fopen(rawdump, "wb");
                if (rf) { fwrite(vb[db.index].map, 1, vb[db.index].len, rf); fclose(rf);
                          printf("[rawdump] wrote %zu bytes -> %s\n", vb[db.index].len, rawdump); }
                rawdump[0] = 0;
            }
            if (fast_ready) raw_to_nv12_fast((const uint8_t *)vb[db.index].map,
                                             frmptr[frmidx]);
            else            raw_to_nv12((const uint8_t *)vb[db.index].map,
                                        frmptr[frmidx]);
            mb = frmbuf[frmidx];
            dump_first_frame(frmptr[frmidx], g_hs);
            if (nv12dump[0] && can_dump) {
                size_t fsize = (size_t)g_hs * g_vs * 3 / 2;
                FILE *nf = fopen(nv12dump, "wb");
                if (nf) { fwrite(frmptr[frmidx], 1, fsize, nf); fclose(nf);
                          printf("[nv12dump] wrote %zu bytes -> %s\n", fsize, nv12dump); }
                nv12dump[0] = 0;          /* one shot */
            }
        } else {
            if (cropping) {
                int rga_done = 0;
                if (rga_ok) {
                    /* RGA 硬件裁切：中心窗口，阻塞提交 */
                    rga_info_l s, d;
                    memset(&s, 0, sizeof s); memset(&d, 0, sizeof d);
                    s.fd = vb[db.index].dmafd; s.mmuFlag = 1; s.format = RGAFMT_NV12;
                    s.rect.xoffset = (C.capw - C.w) / 2;
                    s.rect.yoffset = (C.caph - C.h) / 2;
                    s.rect.width   = C.w;
                    s.rect.height  = C.h;
                    s.rect.wstride = C.capw;
                    s.rect.hstride = C.caph;
                    s.rect.format  = RGAFMT_NV12;
                    d.fd = cropfd[cropidx]; d.mmuFlag = 1; d.format = RGAFMT_NV12;
                    d.rect.width   = C.w;
                    d.rect.height  = C.h;
                    d.rect.wstride = g_hs;
                    d.rect.hstride = g_vs;
                    d.rect.format  = RGAFMT_NV12;
                    int st = p_blit(&s, &d, NULL);
                    if (st == 0) {
                        rga_done = 1;
                        mb = cropbuf[cropidx];
                        cropidx = (cropidx + 1) % ncropbuf;
                        dump_first_frame(cropptr[(cropidx + ncropbuf - 1) % ncropbuf], g_hs);
                    } else {
                        printf("[crop][warn] c_RkRgaBlit ret=%d, falling back to CPU copy\n", st);
                        rga_ok = 0;
                    }
                }
                if (!rga_done) {
                    /* CPU 中心裁切：Y/UV 按行 memcpy（非缓存内存，慢，仅兜底） */
                    const uint8_t *src = (const uint8_t *)vb[db.index].map;
                    uint8_t *dst = cropptr[cropidx];
                    const int xoff = (C.capw - C.w) / 2, yoff = (C.caph - C.h) / 2;
                    const uint8_t *sY = src + (size_t)yoff * cap_stride + xoff;
                    for (int r = 0; r < C.h; r++)
                        memcpy(dst + (size_t)r * g_hs, sY + (size_t)r * cap_stride, C.w);
                    uint8_t *dU = dst + (size_t)g_hs * g_vs;
                    const uint8_t *sU = src + (size_t)C.capw * C.caph
                                      + (size_t)(yoff / 2) * cap_stride + xoff;
                    for (int r = 0; r < C.h / 2; r++)
                        memcpy(dU + (size_t)r * g_hs, sU + (size_t)r * cap_stride, C.w);
                    mb = cropbuf[cropidx];
                    cropidx = (cropidx + 1) % ncropbuf;
                    dump_first_frame(dst, g_hs);
                }
            } else {
                mb = vb[db.index].mb;
                dump_first_frame((const uint8_t *)vb[db.index].map, C.w);
            }
        }
        if (encode_one(mb)) encoded++;
        double tot_ms = (now_s() - t_frame0) * 1000.0;
        n_lat++;
        if (tot_ms > 0 && tot_ms < 1000) { sum_tot += tot_ms; if (tot_ms > max_tot) max_tot = tot_ms; }
        if (is_raw()) frmidx = (frmidx + 1) % C.nfrmbuf;
        /* MPP has already produced the packet for this buffer, so it is safe
         * to hand it back to the driver (or overwrite it 4 frames later). */
        struct v4l2_plane qp[MAXP]; memset(qp, 0, sizeof qp);
        if (is_raw()) {
            qp[0].m.mem_offset = vb[db.index].mem_off;
            qp[0].length = vb[db.index].len;
        } else {
            qp[0].m.fd = vb[db.index].dmafd;
            qp[0].length = vb[db.index].len;
        }
        db.m.planes = qp;
        if (xioctl(vfd, VIDIOC_QBUF, &db)) { printf("[FATAL] re-QBUF: %s\n", strerror(errno)); break; }

        double t = now_s();
        if (t - t_last >= 5.0) {
            double cap_fps = captured / (t - t_start);
            printf("[stat] captured=%d encoded=%d rtp=%ld bytes=%ld cap_fps=%.2f "
                   "out_fps=%.2f send_err=%u\n",
                   captured, encoded, stat_rtp, stat_bytes, cap_fps,
                   stat_frames / (t - t_start), stat_send_err);
            if (n_lat > 0) {
                double k = (double)n_lat;
                printf("[lat] avg/max ms: drv=%.1f/%.1f put=%.1f/%.1f get=%.1f/%.1f "
                       "tot=%.1f/%.1f (n=%ld)\n",
                       sum_drv / k, max_drv, sum_put / k, max_put,
                       sum_get / k, max_get, sum_tot / k, max_tot, n_lat);
            }
            t_last = t;
        }
    }

    /* Always release the queue: a killed sender that leaves video19
     * streaming makes every later start fail with REQBUFS EBUSY. */
    enum v4l2_buf_type t = BT;
    xioctl(vfd, VIDIOC_STREAMOFF, &t);
    struct v4l2_requestbuffers rb0; memset(&rb0, 0, sizeof rb0);
    rb0.count = 0; rb0.type = BT; rb0.memory = V4L2_MEMORY_MMAP;
    xioctl(vfd, VIDIOC_REQBUFS, &rb0);
    for (int i = 0; i < vcount; i++) {
        if (vb[i].map && vb[i].map != MAP_FAILED) munmap(vb[i].map, vb[i].len);
        if (vb[i].dmafd >= 0) close(vb[i].dmafd);
    }
    close(vfd);
    printf("[done] captured=%d encoded=%d rtp=%ld bytes=%ld elapsed=%.2fs\n",
           captured, encoded, stat_rtp, stat_bytes, now_s() - t_start);
    /* old MPP/easymedia builds crash in atexit destructors -> bypass them */
    _exit(0);
}
