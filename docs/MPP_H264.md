# RV1109 硬件 H.264 编码实证（绕开 easymedia）

> 2026-11-13。触发：QGC 链路实测只有 3.5 fps，用户质疑「设备真实帧率怎么可能只有 3.6fps」
> 并给出 `https://github.com/rockchip-linux/mpp`。
> **结论：质疑成立。3.5 fps 是当前软件链路的上限，不是硬件能力。**
> 本轮只写 `/tmp`（tmpfs）与构建服务器 `/tmp/gb`，**未写 flash、未改 rootfs、
> 未中断正在运行的 QGC 出流**。

---

## 1. 结论速览

| 检查项 | 实测 |
|---|---|
| sensor / MIPI 出帧节拍 | **26.67 fps**（dmesg 80 个样本，中位间隔 37.502 ms） |
| 当前 `mjpegout` 吞吐 | 3.52 fps，`top` 显示 **45.4 % CPU = 一个 A7 核跑满** |
| 丢帧率 | ≈ **86.8 %** 的 sensor 帧被软件链路丢掉 |
| 内核编码硬件 | `/proc/mpp_service/support_device`：`DEVICE[16]:RKVENC HW_ID:0x50600311`、`DEVICE[18]:VEPU2` |
| 原厂 MPP 库 | `librockchip_mpp.so.0`，1,206,712 B，git `b2ee155 liyunjian 2021-11-19`，构建路径 `rockchip_rv1126_rv1109_facial_gate` —— **与本板同一套 BSP** |
| `mpp_create` / `MppApi` ABI | `mpi->size=128` == 头文件 `sizeof(MppApi)=128` → **精确匹配** |
| `mpp_init(MPP_CTX_ENC, AVC)` | **0 OK** |
| `MPP_ENC_GET_CFG` / `MPP_ENC_SET_CFG` | **0 / 0**，库日志 `h264e_api_v2: w:h [640:360] stride [640:368]` |
| `MPP_ENC_GET_HDR_SYNC` | **0**，36 B，`00 00 00 01 67 42 c0 1f`（SPS） |
| `encode_put_frame` / `encode_get_packet` | **0 / 0**，输出 Annex-B 码流 898 B（`00 00 00 01 06 ...` SEI + IDR） |
| 硬件编码吞吐（不含采集） | 640×360 **572.65 fps** / 1280×720 **216.37 fps** / 1920×1080 **106.43 fps** |

> 吞吐数字是「重复编码同一张全灰帧」，码流极小（200 帧仅 9,880 B @640×360），
> 因此偏乐观；但它足以证明 **VENC 比软件 JPEG 路径快两个数量级**，
> 1920×1080@30fps 毫无压力。真实场景需按实际码率复测。

**`docs/OUTGOING_RTP.md` / `P1_IQ_INJECTION.md` 里所有「≈3 fps 是 A7 瓶颈」的描述，
指的都只是「RAW10 软件 demosaic + 色度降噪 + stbi JPEG」这一条路径，不是设备帧率。**

---

## 2. 为什么之前认为「硬件编码走不通」

`P1_IQ_INJECTION.md` 记录的两个阻塞都属于 **easymedia 高层封装**，不是硬件：

```
[RKMEDIA][SYS][Error]:MediaCfg: fps: KEY_FPS is null!   → VENC_CreateChn -33
[RKMEDIA][SYS][Error]:V4L2: MPLANE not support DMA Buffer yet. → VI_EnableChn -11
```

`libeasymedia.so.1` 的 `ParseMediaConfigFromMap` 没往 map 里放 `framerate`，
所以 `CreateChn` 永远失败。**MPP 是 easymedia 的下一层**（easymedia 内部就是调 MPP），
直接调 `mpp_create / mpp_init / mpp_enc_cfg_set_s32 / encode_put_frame` 完全绕过
那个坏掉的 media-config 解析，不依赖 IQ、不依赖 VI、不依赖任何 `RK_MPI_VENC_*`。

---

## 3. 最小可用调用链（`tools/mppprobe.c` 已实板验证）

头文件用 MPP **release 1.0.9**（`out/reference/mpp/inc-1.0.9/`，与设备库同代），
`dlopen("/tmp/factory/lib/librockchip_mpp.so.1")` + `dlsym`，无需链接 ARM 库。

```c
mpp_create(&ctx, &mpi);                  /* mpi->size 必须 == sizeof(MppApi) */
mpp_init(ctx, MPP_CTX_ENC, MPP_VIDEO_CodingAVC);        /* 7 */

mpp_enc_cfg_init(&cfg);
mpi->control(ctx, MPP_ENC_GET_CFG, cfg); /* 0x320002 */
mpp_enc_cfg_set_s32(cfg, "prep:width",      640);
mpp_enc_cfg_set_s32(cfg, "prep:height",     360);
mpp_enc_cfg_set_s32(cfg, "prep:hor_stride", 640);   /* 16 对齐 */
mpp_enc_cfg_set_s32(cfg, "prep:ver_stride", 368);   /* 16 对齐 */
mpp_enc_cfg_set_s32(cfg, "prep:format",  MPP_FMT_YUV420SP); /* 0 = NV12 */
mpp_enc_cfg_set_s32(cfg, "rc:mode",       MPP_ENC_RC_MODE_CBR);
mpp_enc_cfg_set_s32(cfg, "rc:fps_in_num", 25);
mpp_enc_cfg_set_s32(cfg, "rc:fps_out_num",25);
mpp_enc_cfg_set_s32(cfg, "rc:gop",        25);
mpp_enc_cfg_set_s32(cfg, "rc:bps_target", 1500000);
mpp_enc_cfg_set_s32(cfg, "codec:type", MPP_VIDEO_CodingAVC);
mpi->control(ctx, MPP_ENC_SET_CFG, cfg); /* 0x320001, ret 0 */

mpp_buffer_group_get(&grp, MPP_BUFFER_TYPE_NORMAL /* 0x1 */,
                     MPP_BUFFER_INTERNAL, "tag", "caller");
mpp_buffer_get_with_tag(grp, &frm_buf, hs*vs*3/2, "tag", "caller");
memset(mpp_buffer_get_ptr_with_caller(frm_buf, "caller"), 128, hs*vs*3/2);

mpp_packet_init_with_buffer(&hdr, pkt_buf);
mpp_packet_set_length(hdr, 0);
mpi->control(ctx, MPP_ENC_GET_HDR_SYNC, hdr);  /* 0x32000d → SPS/PPS 36 B */

mpp_frame_init(&frame);
mpp_frame_set_width/height/hor_stride/ver_stride/fmt(frame, ...);
mpp_frame_set_buffer(frame, frm_buf);
mpp_frame_set_eos(frame, 1);              /* 最后一帧 */
mpi->encode_put_frame(ctx, frame);
mpp_frame_deinit(&frame);
mpi->encode_get_packet(ctx, &pkt);         /* Annex-B NAL, MPP 内部分配 packet */
mpp_packet_get_pos(pkt) / mpp_packet_get_length(pkt);
_exit(0);                                  /* 老库 atexit double free */
```

### ABI 坑清单（踩过的，别再踩）

1. **`mpp_buffer_get` / `mpp_buffer_put` / `mpp_buffer_get_ptr` 都是宏，不是符号。**
   头文件 `mpp_buffer.h:227-296` 把它们展开成
   `mpp_buffer_get_with_tag(...)` / `mpp_buffer_put_with_caller(...)` /
   `mpp_buffer_get_ptr_with_caller(...)`。直接 `grep -a` 库文件会看到假阳性，
   必须解析 `.dynsym`（`out/reference/mpp/librockchip_mpp.so.0`，1,803 个导出符号）。
2. **buffer 类型只能用 `MPP_BUFFER_TYPE_NORMAL (0x1)` 或 `MPP_BUFFER_TYPE_DRM (0x3)`。**
   `DRM|FLAGS_CACHABLE(0x20003)` / `DRM|CONTIG(0x10003)` / `DRM|WC(0x40003)`
   一律在 `mpp_buffer_get_with_tag` 内 **Segmentation fault**；
   `ION(0x2)` 报 `NOT found ion allocator` → `mpp_buffer_create failed`。
   `NORMAL` 与 `DRM plain` 均实测成功且能出码流。
3. **`rc:fps_in_denom` / `rc:fps_out_denom` 在这个 2021 构建里返回 -1**（key 名不同），
   但库日志 `fps fix [25/1] -> fix [25/1]` 证明分母默认已是 1，忽略即可。
4. **命令枚举值（1.0.9 头）**：`MPP_ENC_SET_CFG=0x320001`、`GET_CFG=0x320002`、
   `GET_HDR_SYNC=0x32000d`。与 `MppApi->size=128` 一起构成版本指纹。
5. 旧库退出时 `double free or corruption (fasttop)` → 探针/常驻进程收尾一律 `_exit(0)`。
6. 该库**没有** `mpp_buffer_sync_*_f` 导出，无法显式刷 cache；用 `NORMAL` 堆 buffer
   或后续用 dma-buf 导入时要留意一致性。

---

## 4. 剩余工作：把 NV12 数据源接上（**已于 2026-11-13 收口，见第 6 节**）

> ⚠️ 本节是当时的方案评估，结论已被后续实证推翻：**方案 A（rkisp/rkispp 直出 NV12）
> 在本 OpenIPC 内核上不可用**（bridge-ispp 链路独占 ISP 输出 + rkispp 硬件未初始化，
> 出的是全 0 帧）。最终落地的是方案 B 的变体：RAW10 → 软件 NV12 → MPP 硬件编码。
> 权威记录见 **`docs/DIRECT_H26X.md`**。

MPP 编码器只吃 `MPP_FMT_YUV420SP (NV12)`。目前 `/dev/video0` 是
`2880×1616 BG10` MIPI RAW10，不能直接送进去。三条路：

| 方案 | 链路 | 预期帧率 | 工作量 / 风险 |
|---|---|---|---|
| **A 推荐：rkisp 直出零拷贝** | IQ 常驻 → `/dev/video6` `S_FMT NV12` → `VIDIOC_EXPBUF` 导出 dma-buf fd → `mpp_buffer_import_with_tag(MPP_BUFFER_TYPE_EXT_DMA)` → VENC → FU-A/PT96 → UDP | **25~30 fps**，端到端 < 150 ms | 中。当前 `video6` 的 G_FMT 是 `720x202 FBCG`（rkaiq 遗留 AFBC 配置），需重置格式；IQ 进程要常驻；`mpp_buffer_import_with_tag` 已确认导出 |
| **B 最快落地：软件 demosaic → NV12** | 保留现有 `mjpegout` 的 RAW10 处理，但**删掉色度降噪 + stbi JPEG**，直接写 Y/UV 平面 → MPP | 8~12 fps（仍受 A7 限制） | 低。改动小，但达不到硬件满速 |
| C：RGA 硬件转换 | RAW10 → RGA → NV12 | 未知 | 高。RGA 大概率不支持 Bayer RAW10 输入，不建议先试 |

**方案 A 接上后，QGC 链路可以彻底简化**：设备直接发 H.264/PT96 到 `.88:5600`，
Windows 上 `qgc_h264_relay.py` 的 MJPEG→H.264 转码、逐帧重组、水印全部可以删掉，
延迟从当前 ~1 s（0.29 s 采集 + 转码 + 播放缓冲）降到采集/编码/网络的固有值。

> 本轮**只做到「证明硬件编码可用」为止**，没有改产品链路、没有中断现有 QGC 出流。
> 选 A / B 之后再动 `mjpegout.c` 或新建 `mppout.c`。

---

## 5. 文件索引

| 路径 | 用途 |
|---|---|
| `tools/mppprobe.c` | 本轮探针：dlopen 原厂 MPP → 配 H.264 → 编 N 帧 → 打印吞吐 |
| `tools/build_mpp_probe.py` | 交叉编译 + 部署 + 运行（可带 `width height raw_buf_type frames`） |
| `firmware/mppprobe` | armhf 动态二进制（17 KB） |
| `out/reference/mpp/inc-1.0.9/` | MPP 1.0.9 官方头文件（16 个） |
| `out/reference/mpp/inc/` | MPP develop 头文件（对照用） |
| `out/reference/mpp/test/mpi_enc_test.c` | 官方 1.0.9 编码器示例 |
| `out/reference/mpp/librockchip_mpp.so.0` | 设备原厂 MPP 库原件（用于 `.dynsym` 分析） |
| `docs/evidence/mppprobe_h264_ok.txt` | `NORMAL` / `DRM` 两次成功运行的完整日志 |
| `docs/evidence/mppprobe_run1.txt` | 首次运行（ABI 探索 + 默认类型崩溃）日志 |

复现：

```bash
python tools/build_mpp_probe.py 640 360 0x1 200
# 或直接在设备上：
LD_LIBRARY_PATH=/tmp/factory/lib /tmp/mppprobe 1280 720 0x3 200
```

---

## 6. 后续收口（2026-11-13）：硬件编码已接入真实画面

`tools/h264out.c` 现在双 codec 直发 RTP/UDP，H.264 与 H.265 都实板解出真实画面：

```
/dev/video0 (BG10 RAW10) -> 软件 NV12 -> MPP HW VENC -> RTP (PT96/PT97) -> UDP
```

关键补充结论（详见 `docs/DIRECT_H26X.md`）：

1. **`MPP_BUFFER_TYPE_NORMAL(0x1)` 的堆缓冲不能用于 CPU 写入后交硬件编码**：
   本库无 `mpp_buffer_sync_*_f` 导出，CPU 写的 NV12 硬件 DMA 看不到，
   解码出来是纯绿（Y≈0、UV 全 0）。必须用 **`MPP_BUFFER_TYPE_DRM(0x3)`**。
   第 3 节说「NORMAL 与 DRM plain 均实测成功且能出码流」——能出码流是真的，
   但 `mppprobe` 只测了合成灰帧、没解码验证，NORMAL 的画面内容其实没被硬件读到。
2. **raw 路径不能覆盖 MPP 超时**、**不能另开独立 header buffer group**：
   否则 `MPP_IOC_CFG_V1 failed errno 12`。必须复刻 `mppprobe` 的单 group
   （帧缓冲 → packet → motion-info，`GET_HDR_SYNC` 用 packet buffer）配置。
3. `nfrmbuf` 只能为 1，≥2 触发同样的 ENOMEM（这版 VEPU541 HAL 的限制）。
4. rkaiq IQ daemon 会占住 rkcif，raw 模式必须关掉它。

实测：`640x360` 6.6~8.7 fps、`320x180` 13.7~23.8 fps；两种 codec 均 **0 丢包**，
NAL 含完整 VPS/SPS/PPS。证据 `out/captures/direct_*.{jpg,h264,h265}`。

新增文件：`tools/h264out.c`（双源）、`tools/verify_direct_stream.py`（端到端 RTP 校验）、
`tools/h264out.conf`、`tools/start_h264out.sh`、`docs/DIRECT_H26X.md`。
