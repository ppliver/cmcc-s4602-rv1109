# P1：SC5338 IQ 注入成功 + outgoing 实时出流闭环

> 2026-11-13，S4602 / RV1109 / OpenIPC lite，设备 `192.168.6.5`，主机 `192.168.6.109`。
> 前置：`docs/P0_RUNTIME_ABI.md`（原厂运行时部署 + ABI 实证）。
> 相关：`docs/OUTGOING_RTP.md`（协议规格与路线）、`docs/WHALETX_REFERENCE.md`（架构对照）。

## 0. 一句话结论

**IQ 注入成功了**（rkaiq UAPI1 `init/prepare/start` 全 0，内核 `can not get first iq setting in stream on` 不再出现），
**实时视频也出去了**（MJPEG/RFC 2435/PT 26 over UDP，主机重组出完整 JPEG）。
但**「rkisp 直出干净 NV12 → 硬件 H.264」这一步没通**，原因是原厂 `libeasymedia.so.1` 这个构建自身的两个限制，
不是 IQ、不是内核、不是 ABI。

> **后续更正（2026-11-13，见 `docs/MPP_H264.md`）**：上述两个阻塞只存在于
> **easymedia 封装层**。直接调用其下一层的原厂 MPP（`mpp_create` / `mpp_init` /
> `mpp_enc_cfg_set_s32` / `encode_put_frame`）已实板编出 Annex-B H.264，
> `KEY_FPS` 与 `MPLANE DMABUF` 两个错误都被完全绕过。硬件编码器本身一直是好的。

## 1. 为什么 IQ 能一次成功

`out/factory_rootfs/system/resource/scene_param/COMMON_sc5338.xml` 是标准 rkaiq IQ tuning：

```xml
<?xml version="1.0" encoding="utf-8"?>
<!-- S4602 cmcc物聯網 v1.55-1 ... -->
<matfile>
  <header ...>
    <code_xml_parse_version ...>v1.4.7</code_xml_parse_version>
    <sensor_name ...>SC5338</sensor_name>
```

`librkaiq.so` 的构建路径字符串证实是**同一代 BSP**：
`/home/liyunjian/ipc_rv1109_rv1126/.../camera_engine_rkaiq-1.0/`。

库内 IQ 文件命名格式（strings 实证）：
`/etc/iqfiles/`（默认目录）、`%s/%s.xml`、`%s_%s_%s.xml`、`%s/%s.bin`、
`_normal.xml`、`-hdr3.xml`、`%s-%s-%s.xml`。
原厂只给了 `COMMON_sc5338.xml` 一个名字，所以部署时用 **9 个硬链接**（同一 tmpfs inode，不额外占空间）
覆盖所有可能的命名规则：`tools/deploy_iq.py` → `/tmp/factory/iqfiles/`。
（SD 是 vfat 不支持软链，tmpfs 可以；硬链最省空间。）

## 2. IQ 注入的精确序列（已实板验证）

来自同代 SDK `out/reference/rkmedia/common/sample_common_isp.c` 的
`SAMPLE_COMM_ISP_Init` / `SAMPLE_COMM_ISP_Run`，实现在 `tools/aiqprobe.c`：

```c
setenv("HDR_MODE", "0", 1);                              // 必须在 init 之前
rk_aiq_uapi_sysctl_enumStaticMetas(0, &info);           // 256KB scratch buffer, 不需要头文件
//   → 0；自动识别真实 sensor： "m01_f_sc5338 1-0030"（不是 fake camera）
ctx = rk_aiq_uapi_sysctl_init("m01_f_sc5338 1-0030",
                              "/tmp/factory/iqfiles", NULL, NULL);   // → 有效 ctx
rk_aiq_uapi_setFecEn(ctx, 0);                           // → 0
rk_aiq_uapi_sysctl_prepare(ctx, 0, 0, 0 /*NORMAL*/);   // → 0
rk_aiq_uapi_sysctl_start(ctx);                          // → 0   ★ ISP 3A 引擎开始推参数
/* 进程必须常驻，rkaiq 内部线程持续写 rkisp-input-params */
_exit(0);   // 旧库 atexit double free，探针一律绕过析构
```

实测结果（`docs/evidence/aiqprobe_run1.txt`）：

| 调用 | 返回 |
|---|---|
| `enumStaticMetas(0)` | 0，sensor_name = `m01_f_sc5338 1-0030` |
| `sysctl_init` | `0x4ad148`（有效 ctx） |
| `setFecEn(FALSE)` | 0 |
| `sysctl_prepare` | 0 |
| `sysctl_start` | 0 |

打开的设备句柄（`dump_fds`）：`/dev/v4l-subdev3`(sensor)、`subdev4`(isp)、
`/dev/v4l-subdev0`(ispp)、`video4/11/12/13/14/15`（rawrd/statistics/**input-params**）、
`video0/1/2`(rkcif)、`video6`(selfpath)、`video23/24`(ispp params/stats)、`/dev/rga`。

### IQ 生效的证据

注入前 dmesg 每次 STREAMON 都新增：
```
rkisp rkisp-vir0: can not get first iq setting in stream on
```
注入后（`docs/evidence/aiqprobe_run2.txt`）**该错误不再新增**，最后三条仍是
旧的 `[4828]`、`[5301]`、`[8121]`，本次运行在 `[9039]` 附近只有
`rkcif ... not active buffer` / `dphy stream ON/OFF`。
另外 `rk_aiq_uapi_sysctl_start` 后 sensor 持续出流（dmesg 连续帧日志）。

> IQ XML 解析时有大量 `RkAiqCalibParser: unknown tag ...` / `calib_check_tag_attrs` 警告
> （`mfnr dynamic`、`AecSpeed.DyDampEn` 等），是 XML v1.55 与 rkaiq 1.0 tag 表的
> 小版本差异，**不影响 init/prepare/start**，3A 引擎照常运行。

## 3. easymedia VI→VENC 全链路尝试与两个硬阻塞

按官方 `out/reference/rkmedia/h264_rkmedia_demo.c` 写 `tools/rkstream.c`
（真 SDK 头文件的类型 + `dlsym` 的函数，避免链接 ARM 库）：

```
RK_MPI_SYS_Init() → IQ init/prepare/start
  → RK_MPI_VI_SetChnAttr(0,1,"rkispp_scale0",1280x720,NV12)   → 0
  → RK_MPI_VI_EnableChn(0,1)                                   → -11
  → RK_MPI_VENC_CreateChn(0,H264 CBR)                          → -33
  → 回调/绑定均未生效，0 帧 0 包
```

### 阻塞 1：VENC `-33` 的真因是 `KEY_FPS`，不是 BUSY

```
[RKMEDIA][SYS][Info]:RK_MPI_VENC_CreateChn: Enable VENC[0], Type:6 Start...
[RKMEDIA][SYS][Info]:ParseMediaConfigFromMap: rect.w = 1280, rect.h = 720
[RKMEDIA][SYS][Error]:MediaCfg: fps: KEY_FPS is null!
```

- `-33 = RK_ERR_VENC_BUSY`（`rkmedia_common.h` 枚举），但真实原因是上面这行 Error。
- **P0 的 `vencprobe`（完全没有 VI）也报同样错误**（`docs/evidence/vencprobe_full.txt:62,68`）
  → 与 VI、与 IQ、与绑定都无关，是这个 easymedia 构建内部
  `get_video_encoder_config_string → ParseMediaConfigFromMap` 没往 map 里放 `framerate`。
- 结构体布局确认无误：`VENC_CHN_ATTR_S` = 100 B，库日志读出的 `rect.w/h` 与传入一致，
  `RK_CODEC_TYPE_H264 = 6` 与日志 `Type:6` 一致。
- 库导出 `RK_MPI_SYS_SetFrameRate(MOD_ID_E, s32ChnID, MPP_FPS_ATTR_S*)`
  （RKMedia V1.3.0 新增，`rkmedia_api.h` 无声明），**这是最可能的入口，尚未验证**。
- `RK_MPI_VENC_SetFps` 需通道已存在，CreateChn 失败时用不上。

### 阻塞 2：VI `-11` 是库不支持 MPLANE + DMABUF

```
[RKMEDIA][SYS][Info]:#V4L2Ctx: open /dev/video19, fd 106
[RKMEDIA][SYS][Error]:V4L2: MPLANE not support DMA Buffer yet.
[RKMEDIA][VI][Info]:Create stream v4l2_capture_stream failed
[RKMEDIA][SYS][Warn]:VI[1]:"rkispp_scale0" buffer may be occupied ...
```

- `-11 = RK_ERR_VI_BUSY`，但真因是这行 Error。
- easymedia 的 V4L2 flow 默认拼 `v4l2_mem_type=MEMORY_DMABUF`，遇到 MPLANE 节点直接拒绝。
  **内核侧没问题**：P0 的 `dmabuf_link.c` 已实证 `VIDIOC_REQBUFS(DMABUF)` + `QBUF(fd)` 成功。
- `VI_CHN_ATTR_S` 只有 `pcVideoNode/w/h/pixfmt/bufcnt/workmode`，**不暴露 mem type**。
  库内有 `KEY_V4L2_MEM_TYPE "v4l2_mem_type"`、`MEMORY_MMAP` 字符串与
  `RKMEDIA_LOG_LEVEL` 环境变量，但 `/tmp/loglevel` 的 `all=4` 未打开 Debug
  （日志仍显示 `log_level is 2`），尚未找到注入点。
- 原厂 rootfs 有 `libv4l2.so.0.0.0` + `libv4lconvert.so`，`use_libv4l2=` 配置键存在 →
  部署这两个库让 easymedia 走 libv4l2 兼容路径是另一条可试的路。

### rkaiq 与 V4L2 抓帧器的冲突

rkaiq start 后自己接管了 `video6`：`G_FMT` 变成 **`720x202 FBCG`（2 planes）**，
此时外部 `mpcap /dev/video6` 会 `REQBUFS: Device or resource busy`（或 `QUERYBUF: EINVAL`，
因为 mpcap 只处理 1 plane）。`video5` mainpath 的 link 仍是 `[off]`，`STREAMON` 仍 `EPERM`。
`video19 rkispp_scale0` 的 `G_FMT` 是 `0x0`（rkispp 未配置）。
**结论：IQ 通了也不等于能直接用 V4L2 从 rkisp 拿 NV12；必须由 easymedia VI 协同接管。**

## 4. 交付的实时视频：路线 A（MJPEG / RFC 2435 / PT 26）

既然 easymedia 链路被库自身限制堵住，回到唯一稳定帧源 `/dev/video0`（rkcif RAW10，
实测每次都能 `DQBUF` 6,205,440 B），写了 `tools/mjpegout.c`：

- 静态 ARM 二进制 `firmware/mjpegout`（394,496 B），零运行时依赖，只在 `/tmp` 跑。
- V4L2 MPLANE mmap 连续抓帧 → MIPI RAW10 解包 → 4×4 box demosaic →
  黑电平 140 / 灰度世界白平衡（**必须用减去 black 后的均值算增益**，否则满屏品红）/
  gamma 0.65 → Y/CrCb 色度域 box 降噪（RAD=3、系数 0.35）→ stb JPEG（q70）→
  RFC 2435 分片（1200 B、Q=255 + 从 DQT 提取的 128 B 量化表、M 位标帧末）→ UDP。
- **实测**：20 帧 / **1400 个 RTP 包 100% 到达主机**，重组出 20 张完整 JPEG
  （`ffd8`…`ffd9`）；单帧 69~101 KB；**≈3 fps**；画面百叶窗/灯点/围栏/桌面可辨、
  色调中性，残留轻微摩尔纹。
- 播放器：`out/captures/s4602_mjpeg.sdp`（`m=video 5600 RTP/AVP 26` +
  `a=rtpmap:26 JPEG/90000`），VLC/ffplay/GStreamer 命令见 `docs/OUTGOING_RTP.md` 第 4 节。
- 无播放器也能看：`python3 tools/rtp_mjpeg_receiver.py 5600 out/captures/live.jpg`。

> 本机没有 ffplay / ffmpeg / VLC，**GUI 播放未做目测**；传输层与码流已由主机 Python
> 按 RFC 2435 重组并渲染验证（`out/captures/mjpeg_final.jpg`）。

## 5. 后续可做的（按优先级）

1. **P2-a**：验证 `RK_MPI_SYS_SetFrameRate(RK_ID_VENC, 0, &MPP_FPS_ATTR_S)` 能否补上
   `KEY_FPS` → 让 `VENC_CreateChn` 返回 0。需从 Rockchip RKMedia V1.3.0 文档拿
   `MPP_FPS_ATTR_S` 结构（`{fps_num, fps_den, ...}`）。
2. **P2-b**：解决 VI 的 `v4l2_mem_type` —— 试部署原厂 `libv4l2.so` +
   `use_libv4l2=`，或用 `RK_MPI_LOG_SetLevelConf` 真正打开 Debug 看 VI flow param
   字符串，找 mem-type 注入点。
3. **P2-c**：两者都通后按 `h264_rkmedia_demo.c` 接 `SYS_Bind(VI→VENC)`，
   `video_packet_cb` 拿 H.264 Annex-B，用 `rkstream.c` 里已写好的 FU-A/PT96/UDP 逻辑
   （目前该分支因 VENC 失败未被触发）→ 与 majestic outgoing 等价、可接 wfb-ng。
4. **旁路**：`RK_MPI_VENC_CreateJpegLightChn` 硬件 JPEG（同样受 `KEY_FPS` 影响），
   或用 DMABUF 零拷贝把 RAW10 直接喂给 rkvenc 的 JPEG 通道，省掉软件 demosaic。

## 6. 文件索引

| 文件 | 作用 |
|---|---|
| `tools/deploy_iq.py` | 部署 IQ XML + 9 个命名硬链到 `/tmp/factory/iqfiles` |
| `tools/aiqprobe.c` / `firmware/aiqprobe` | IQ 注入探针（enumStaticMetas / init / prepare / start / hold） |
| `tools/rkstream.c` / `firmware/rkstream` | 全链路探针（IQ + VI + VENC + FU-A/PT96/UDP，含 H.264 打包逻辑） |
| `tools/mjpegout.c` / `firmware/mjpegout` | **可交付的实时 outgoing 发送器** |
| `tools/rtp_mjpeg_receiver.py` | 纯 Python RFC 2435 接收/重组 |
| `out/captures/s4602_mjpeg.sdp` | 播放器 SDP |
| `out/captures/mjpeg_final.jpg` | RTP 重组出的真实帧 |
| `out/reference/stb/stb_image_write.h` | 静态 JPEG 编码器 |
| `docs/evidence/rkaiq_iq_strings.txt` | librkaiq IQ 命名规则 strings |
| `docs/evidence/aiqprobe_run{1,2}.txt` | IQ 注入完整日志 |
| `docs/evidence/iq_video6_capture.txt` | IQ 运行期间 rkisp 抓帧尝试 |
| `docs/evidence/rkstream_run1.txt` / `rkstream_debug2.txt` | VI/VENC 阻塞完整日志 |
| `out/reference/rkmedia/` | 同代官方 SDK 头文件 + demo |

## 7. 红线

本轮**只写 `/tmp` 与构建服务器 `/tmp/gb`，未写 flash、未改 rootfs、未部署常驻进程**；
`_a` 槽原厂固件完整可回滚。IQ 资源在 tmpfs，掉电即失（需要持久化时再按
`docs/FULL_BACKUP.md` 的规范评估）。
