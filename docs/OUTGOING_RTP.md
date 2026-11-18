# S4602 / OpenIPC：实现 `outgoing`（RTP over UDP）方案评估

日期：2026-11-13
目标：设备主动把视频以 RTP/UDP 推到局域网地址，用外部播放器（VLC / ffplay /
GStreamer / wfb-ng）直接出画面，等价于 OpenIPC majestic 的 `outgoing` 功能。

> 本文是**评估 + 实施规格**，不是已完成的实现。当前只做了链路验证，
> 未改 rootfs、未写 flash、未部署任何常驻进程。

---

## 1. OpenIPC `outgoing` 到底是什么

 majestic 的 `outgoing` 不是 RTSP，也不是 MPEG-TS，而是**裸 RTP/AVP over UDP 单播/多播**，
由摄像头主动 `sendto`，接收端不需要回连、不需要握手。

配置（` majestic.yaml`，官方 wiki）：

```yaml
outgoing:
  enabled: true
  server: udp://192.168.1.10:5600        # 单个目标
  naluSize: 1200                          # 单个 RTP 包的最大 NAL 分片字节数
  # 也支持数组形式，可同时推多个目标：
  # - udp://192.168.1.10:5600
  # - udp://192.168.1.11:5600
  # - unix:/tmp/rtpstream.sock            # 给本机 wfb-ng
  # - rtmp://... / rtmps://...            # RTMP 是另一条分支，不是 RTP
rtsp:
  enabled: false                          # outgoing 与 RTSP 可并存，也可关掉 RTSP
```

CLI 等价写法：

```sh
cli -s .outgoing.enabled true
cli -s .outgoing.server udp://192.168.6.109:5600
killall -HUP majestic
```

### 协议规格（已核实）

| 项 | 值 |
|---|---|
| 传输 | UDP，每个 RTP 包一个 datagram，目标端口常用 **5600**（wfb-ng 惯例 **5602**） |
| RTP | RFC 3550，V=2，12 字节头，无扩展、无 CSRC |
| clock-rate | **90000**（视频恒定） |
| H.264 payload type | **96**（动态，`openipc-core::rtp::RTP_PAYLOAD_TYPE_H264 = 96`） |
| H.265 payload type | **97**（`RTP_PAYLOAD_TYPE_H265 = 97`） |
| H.264 打包 | RFC 3984：单 NAL 包 / **FU-A**（type 28）分片，`naluSize` 通常 1200 |
| H.265 打包 | RFC 7798：FU type 49 |
| SPS/PPS | 写进 SDP `sprop-parameter-sets`，或每个 IDR 前以 Annex-B 插入 |
| M 位 | 一个访问单元（一帧）的最后一个 RTP 包置 1 |
| 分片上限 | 1200 B 左右，保证 ≤ 以太网 MTU，避免 IP 分片（wfb-ng 无线链路尤其重要） |

接收端**必须**知道 codec 和 PT（要么 SDP，要么 gstreamer caps），因为 UDP 没有协商。

---

## 2. 为什么这台设备不能直接照抄 `majestic.yaml`

| 事实 | 依据 |
|---|---|
| OpenIPC 对 RV1109 / RV1126 **只发布 `Lite NAND` 固件** | openipc.org SoC 页：「publishes firmware ... but not a bootloader」，固件名 `OpenIPC Linux (Lite NAND) for Rockchip RV1109`；SoC 状态 `Experts only` |
| Lite = **不含 majestic / 任何 streamer** | 设备实测：无 `majestic`、`rkipc`、`ffmpeg`、`gst-launch-1.0`、`v4l2-ctl`、`python3` |
| majestic 是**按 SoC 闭源二进制**发布 | Prosperity Public License 3.0.0，二进制形式分发；RV1109 无对应构建 |
| 因此从别的 SoC 拷 `majestic` 二进制不可用 | ISP/VENC 私有 ABI 与 sensor 模块绑定，RV1109 无匹配构建 |

**结论**：`outgoing` 的配置语法可以直接借用，但载体要自己写。好消息是 RTP/UDP 这一层
极其简单，且**链路已经实测打通**（见第 3 节）。

### 设备现状（2026-11-13 实测）

```
CPU      ARMv7 双核 (v7l, NEON/vfpv4)，MemTotal 504 MB
网络      192.168.6.5/24，网关 192.168.6.1，可出外网
工具      nc(busybox 1.36.1，无 -u)、curl、wget；无 ffmpeg/gst/python3
节点      /dev/video0..24、/dev/media0..2、/dev/v4l-subdev0..6、
          /dev/mpp_service (244,0)、/dev/rga (10,59)；rkvenc 内核已 probe
抓帧      /dev/video0 已稳定 STREAMON+DQBUF → 2880×1616 BG10 (MIPI RAW10)，
          dmesg 实证 MIPI DPHY 720 Mbps stream ON
ISP       rkisp 输出 NV12 仍报 "can not get first iq setting in stream on"（任务 #33）
原厂库    libturbojpeg.so.0.2.0、libjpeg.so.62.3.0、libavcodec.so.58、
          libavformat.so.58、librockchip_mpp.so.0、libeasymedia.so.1（575×RK_MPI_*）、
          librkaiq.so；GLIBC 需求 ≤ 2.28 < 设备 2.38 → 可直接加载，无需 chroot
```

---

## 3. 已验证：设备 → 播放器主机的 RTP/UDP 通道

工具 `tools/rtpprobe.c`（静态 armhf，`firmware/rtpprobe`）。设备侧发 5 个 RTP 形状包到
`192.168.6.109:5600`，Windows 主机用 UDP socket 接收并解析 RTP 头：

```
5/5 包到达；from 192.168.6.5  V=2 PT=96 seq=1000..1004
ts=12345678 +3000/包  ssrc=0x53343630  M 位仅最后一包为 1
```

即：**单播 UDP 可达、Windows 防火墙未拦截、RTP 头格式能被标准解析器识别**。
`outgoing` 的传输层没有任何障碍，剩下的只是「往 payload 里放什么」。

> 坑：设备上的 BusyBox `nc` 是 1.36.1，**不支持 `-u`**（`nc: invalid option -- 'u'`），
> 默认只走 TCP；shell 层也没有 `/dev/udp`。所以设备侧发 UDP 必须用 C/socket 或
> 静态编译程序，不能靠 busybox 一行命令。

---

## 4. 三条可落地路线

### 路线 A —— MJPEG over RTP (RFC 2435)，**✅ 2026-11-13 已实板闭环**

完全绕开 ISP IQ 阻塞、绕开 rkvenc/MPP、绕开整套 50+ 个原厂库。发送器
**`tools/mjpegout.c`** 是单个静态 ARM 二进制（`firmware/mjpegout`，394 KB），
零运行时依赖，只在 `/tmp` 运行。

```
/dev/video0 (MPLANE, 2880×1616 BG10, stride 3840)
  → MIPI RAW10 解包 (4px/5B)
  → 4×4 box demosaic + 黑电平/灰度世界白平衡/gamma(0.65) → 640×360 RGB
  → Y/CrCb 色度域 box 降噪 (RAD=3, 0.35) —— 压住 HDR_X2 假彩
  → JPEG 编码 (stb_image_write v1.18, quality 70, 内存回调)
  → RFC 2435 分片 (MTU 1200, Q=255 + 128B DQT 表头, M 位标帧末)
  → RTP PT=26 / clock 90000 / ts += 90000/fps
  → UDP 单播 192.168.6.109:5600
```

RFC 2435 每个 RTP 包的 8 字节 JPEG payload header：

```
byte0  Type-specific = 0
1..3   Fragment Offset (24-bit BE)，第一片为 0
byte4  Type = 0（baseline，无 restart marker）
byte5  Q = 255（→ 首片附 4B 表头 + 128B 量化表，从 JPEG 的 DQT 段提取）
byte6  Width/8, byte7 Height/8
```

PT **26 是静态 payload type**，SDP 不需要任何 `sprop`，播放器原生识别 —— 这是它最大的优势。

#### 实板实测（2026-11-13）

| 检查项 | 结果 |
|---|---|
| 设备侧 | `REQBUFS(4) / STREAMON / DQBUF` 连续出帧，dmesg 实证 `mipi-dphy 720 Mbps stream ON` |
| RTP 包 | 一次 20 帧发出 **1400 个 datagram，主机 1400/1400 全收**；V=2 / PT=26 / Q=255 / seq 连续 / M 位正确 |
| 重组 | 按 RFC 2435 去掉 8B 主头（首片再去掉 4B+128B 表头）拼回 **20 张完整 JPEG**（`ffd8`…`ffd9`） |
| 单帧 | 69~101 KB（quality 70，640×360），约 8~12 个 RTP 包/帧 |
| 实测帧率 | **≈2.9~3.1 fps**（A7 软件 demosaic + 色度降噪 + JPEG 是瓶颈；限制帧率参数 8 未达上限）。**这是软件路径上限，不是设备能力**：硬件 H.264 见 `docs/MPP_H264.md` |
| 画质 | 百叶窗/灯点/围栏/桌面清晰可辨，色调中性；仍有轻微 Bayer 摩尔纹（无 ISP 3A） |

运行方式：

```bash
# 设备（frames=0 表示常驻）
/tmp/mjpegout /dev/video0 192.168.6.109 5600 0 8 640 360 70 /tmp/frame.jpg
# 用法: mjpegout <dev> <dst_ip> <port> [frames] [fps] [W] [H] [quality] [save.jpg] [box]

# 播放器主机
vlc   out/captures/s4602_mjpeg.sdp
ffplay -protocol_whitelist file,udp,rtp -i out/captures/s4602_mjpeg.sdp
gst-launch-1.0 udpsrc port=5600 caps='application/x-rtp,media=video,encoding-name=JPEG,payload=26,clock-rate=90000' \
  ! rtpjpegdepay ! jpegdec ! autovideosink sync=false

# 或者不需要播放器：纯 Python 重组并把最新帧写成图片
python3 tools/rtp_mjpeg_receiver.py 5600 out/captures/live.jpg
```

产物：`out/captures/mjpeg_final.jpg`（RTP 重组后的真实帧）、`S4602_FIRST_LIGHT.png`（离线最佳画质）、
`out/captures/s4602_mjpeg.sdp`、`tools/rtp_mjpeg_receiver.py`。

| 维度 | 评价 |
|---|---|
| 依赖 | 1 个静态 C 程序 + stb 单头文件（已归档 `out/reference/stb/`），零运行时依赖 |
| 算力 | 双核 A7 软件路径实测 ~3 fps @ 640×360；降到 320×180 / 提高编译优化可再提升。硬件 VENC 不占 CPU（见 `docs/MPP_H264.md`） |
| 带宽 | 每帧 69~101 KB @ 3 fps ≈ **1.7~2.4 Mbps**，局域网无压力 |
| 画质 | 可辨认、色调中性、有残留摩尔纹；根治要靠 ISP 3A（路线 B2） |
| 兼容性 | VLC / ffplay / GStreamer `rtpjpegdepay` 全部原生支持 |

> **硬件加速备选**：原厂库导出 `RK_MPI_VENC_CreateJpegLightChn` —— 可用 rkvenc 的
> 硬件 JPEG 通道替代软件 stb，配合已实测的 DMABUF 走
> `video0 → 硬件 JPEG → RTP PT=26`。但 P1 实测该库版本 `VENC_CreateChn` 报
> `MediaCfg: fps: KEY_FPS is null`（见下），暂不可用。

### 路线 B —— H.264 over RTP (RFC 3984)，**最终目标 / 与 majestic 完全兼容**

需要一个 H.264 码流源，两条子路：

- **B1（绕开 IQ，但画质受限）**：`RK_MPI_VENC_CreateChn` + `RK_MPI_SYS_SendMediaBuffer`，
  把自建 NV12 MEDIA_BUFFER 直接喂给 VENC，不做 VI→VENC 绑定。
  依赖 `libeasymedia.so.1` + `librockchip_mpp.so.1`（注意 SONAME 是 `.so.1`，
  原厂文件名却是 `.so.0`，要补软链）+ `/dev/mpp_service` + rkvenc。
  帧源是路线 A 的 demosaic 结果；注意 `video0` 的 "NV12" 只是 Bayer 贴标签，
  零拷贝直通 VENC 会得到绿色噪点，**仍需软件 demosaic，牺牲零拷贝**。
- **B2（正统，推荐）**：先完成任务 #33 的 IQ 注入，让 rkisp selfpath(video6, link 已 ENABLED)
  输出干净 NV12，再走 `RK_MPI_SYS_Bind(VI→VENC)` 或 dma-buf 手动送帧。
  WhaleTX 也把 `IspHandle::start()` 放在启动第一步 → **IQ 是 Rockchip ISP 的固有前置，绕不过**。

> **2026-11-13 补充**：已按 `HumpbackLab/WhaleTX`（RV1106 Rust 发射端）校准，
> 完整 API 映射、DMABUF 实测结果和可执行序列见 **`docs/WHALETX_REFERENCE.md`**。
> 要点：`V4L2_MEMORY_DMABUF` 在 video0/5/6 **实测全部支持**；原厂库有
> `RK_MPI_MB_GetFD`（≈ WhaleTX 的 `MB_Handle2Fd`）可做零拷贝；但送帧/取流要换成
> `SYS_SendMediaBuffer`/`SYS_GetMediaBuffer`，IQ 从 UAPI2 换成 UAPI1。
> RTP/FU-A 那段（`src/sink/udp_rtp.rs`）与硬件无关，可几乎原样复用。

拿到 Annex-B NALU 后的打包：

```
NAL ≤ 1200 B            → 单 NAL RTP 包（payload 就是含 1 字节 NAL header 的 NALU）
NAL > 1200 B            → FU-A：
   payload[0] = (nal_hdr & 0xE0) | 28
   payload[1] = (S<<7)|(E<<6)|(nal_hdr & 0x1F)
   payload[2:] = NAL 数据分片（去掉原 NAL header）
SPS/PPS                 → SDP sprop-parameter-sets（base64），或每个 IDR 前插 Annex-B
PT=96, clock=90000, M 位标帧末
```

| 维度 | 评价 |
|---|---|
| 兼容性 | **与 majestic outgoing 逐字节同级**：wfb-ng、VLC、ffplay、任何 RTSP/GB28181 网关都能收 |
| 带宽/延迟 | 0.5~2 Mbps，硬件编码延迟最低（适合以后接 WFB-ng FPV） |
| 风险 | 要部署整套原厂库到 SD（GLIBC 已确认兼容）、调 VENC 参数；可能还要重编内核把 mainpath link 打开 |
| 工作量 | 最大；建议路线 A 验证完播放器侧之后再上 |

### 路线 C —— 主机代编码（拆分式 outgoing，用于快速验证）

设备只做「抓帧 + UDP 发降采样灰度/RAW 帧」，局域网 x86（`192.168.6.109` 或构建服务器
`192.168.219.177`）用 ffmpeg / GStreamer 做 demosaic → H.264 → RTP 再转发给播放器。

- 优点：今天就能把「UDP → SDP → VLC 出画」整条链路校准好，不必先在 A7 上写编解码。
- 缺点：多一跳，不是设备自主 outgoing，需要主机常驻进程。
- 定位：路线 A/B 的**播放器侧联调脚手架**，不是最终形态。

### 路线 D —— 刷 majestic / 换固件：不可行

OpenIPC 没有 RV1109 的 majestic 构建；换 SoC 二进制 ABI 不匹配。
原厂固件 `swapp` 出的是 **RTSP 拉流**（客户端发起），不是主动 UDP outgoing，
且当前跑的是 OpenIPC 内核，原厂用户态栈需要整套搬到 SD 才能起。

---

## 5. 播放器侧配置（三条路线通用，改 `m=` 一行即可）

把下面存成 `s4602.sdp`，用 `vlc s4602.sdp` 或 `ffplay -protocol_whitelist file,udp,rtp -i s4602.sdp` 打开。

**H.264（路线 B，PT 96）**

```
v=0
o=- 0 0 IN IP4 127.0.0.1
s=S4602 H264 outgoing
c=IN IP4 127.0.0.1
t=0 0
m=video 5600 RTP/AVP 96
a=rtpmap:96 H264/90000
a=fmtp:96 packetization-mode=1
```

> 若编码器每个 IDR 前都插了 SPS/PPS，`a=fmtp` 可省略 `sprop-parameter-sets`；
> 否则必须填 `sprop-parameter-sets=<b64 SPS>,<b64 PPS>`，不然首帧解不出。

**MJPEG（路线 A，PT 26）**

```
v=0
o=- 0 0 IN IP4 127.0.0.1
s=S4602 MJPEG outgoing
c=IN IP4 127.0.0.1
t=0 0
m=video 5600 RTP/AVP 26
a=rtpmap:26 JPEG/90000
```

**GStreamer（不写 SDP，直接指定 caps）**

```sh
# H.264
gst-launch-1.0 -v udpsrc port=5600 \
  caps='application/x-rtp,media=(string)video,clock-rate=(int)90000,encoding-name=(string)H264,payload=(int)96' \
  ! rtph264depay ! avdec_h264 ! autovideosink sync=false

# MJPEG
gst-launch-1.0 -v udpsrc port=5600 \
  caps='application/x-rtp,encoding-name=(string)JPEG,payload=(int)26' \
  ! rtpjpegdepay ! jpegdec ! autovideosink sync=false
```

**ffplay 最快验证**

```sh
ffplay -protocol_whitelist file,udp,rtp -i s4602.sdp -fflags nobuffer -flags low_delay
```

**多目标 / 多播**：majestic 支持 `udp://` 列表；自研 sender 只要对目标数组循环 `sendto`
即可。组播需把目标换成 `239.x.x.x` 并把 SDP 的 `c=` 也改成组播地址。

---

## 6. 执行状态（2026-11-13 收尾）

| 优先级 | 动作 | 状态 |
|---|---|---|
| **P0** | 路线 A 发送器 `tools/mjpegout.c` + SDP + 接收脚本 | ✅ 完成并实板验证：20 帧 / 1400 RTP 包全收、重组出完整 JPEG |
| **P1** | 播放器侧 SDP / 端口 / 防火墙校准 | ✅ 主机 UDP 5600 实测可达；SDP 与 GStreamer/ffplay/VLC 命令已给出（本机未装播放器，未做 GUI 目测） |
| **P1′** | 任务 #33：注入 `COMMON_sc5338.xml` | ✅ **IQ 注入成功**（`init/prepare/start` 全 0，rkisp 不再报 `can not get first iq setting`）；但 rkisp NV12 直出与 easymedia VI/VENC 仍被库自身限制阻塞，详见 `docs/P1_IQ_INJECTION.md` |
| **P2** | 路线 B：`RK_MPI_VENC` 硬编码 H.264 → RFC 3984 → PT 96 | ⏸ 待解 `KEY_FPS` + `MPLANE not support DMA Buffer yet`；与 majestic 等价的最终形态 |

### P1′ 的两个 easymedia 阻塞（实证，非猜测）

1. **VENC**：`RK_MPI_VENC_CreateChn` 恒返 `-33 (RK_ERR_VENC_BUSY)`，紧邻日志
   `[RKMEDIA][SYS][Error]:MediaCfg: fps: KEY_FPS is null!`。P0 的 `vencprobe`
   （完全没有 VI）也报同样错误 → **与 VI/IQ 无关**，是这个 easymedia 构建内部
   `get_video_encoder_config_string` → `ParseMediaConfigFromMap` 的配置路径问题。
   库导出 `RK_MPI_SYS_SetFrameRate(MOD_ID_E, s32ChnID, MPP_FPS_ATTR_S*)`（V1.3.0 新增，
   `rkmedia_api.h` 无声明）可能是入口，尚未验证。
2. **VI**：`RK_MPI_VI_EnableChn` 返 `-11 (RK_ERR_VI_BUSY)`，日志
   `[RKMEDIA][SYS][Error]:V4L2: MPLANE not support DMA Buffer yet.` —— 该库版本
   的 V4L2 flow 默认 `v4l2_mem_type=MEMORY_DMABUF`，遇到 MPLANE 节点直接拒绝
   （尽管内核侧 `VIDIOC_QBUF(DMABUF)` 已由 P0 实证可用）。库内有
   `MEMORY_MMAP` / `v4l2_mem_type=` 配置键与 `RKMEDIA_LOG_LEVEL` 环境变量，
   但 VI 的 `VI_CHN_ATTR_S` 没有暴露 mem-type 字段。

**因此当前可交付的实时视频是路线 A（MJPEG/PT26），不是 H.264/PT96。**

工程红线不变：不碰 `0x0–0x300000` loader/uboot/env；只刷 `_a`/`_b` 单槽、`_a` 原厂保留回滚；
`mtd write` 四参数齐全 + 回读 CRC；本方案所有程序只在 `/tmp` 与 SD 卡运行，**未写 flash、未改 rootfs**。

### 6.1 实板常驻配置（当前生效：`192.168.6.88:5600`）

路线 A 已从"一次性测试"升级为**开机后可随时恢复的常驻 outgoing**。设备侧文件：

| 路径 | 作用 |
|---|---|
| `/mnt/mmcblk1/s4602/mjpegout` | 静态 ARM 二进制（SD 持久副本，394 KB） |
| `/mnt/mmcblk1/s4602/outgoing.conf` | 目的地址等唯一配置源 |
| `/mnt/mmcblk1/s4602/start_outgoing.sh` | `start / stop / restart / status` 控制脚本 |
| `/mnt/mmcblk1/s4602/s4602.sdp` | 给播放器用的 SDP |
| `/tmp/mjpegout`、`/tmp/outgoing.conf`、`/tmp/start_outgoing.sh`、`/tmp/mjpegout.log` | 运行态（tmpfs，重启丢失） |

`/tmp` 三个文件由脚本自动从 SD 复制补齐，所以**设备重启后直接**：

```sh
sh /mnt/mmcblk1/s4602/start_outgoing.sh start
```

改目的地址：编辑 `/tmp/outgoing.conf`（或 SD 上那份）的 `DST_IP` / `DST_PORT`，然后
`sh /tmp/start_outgoing.sh restart`。`FRAMES=0` 表示无限推流；`MIRROR_IP` 是可选的第二个
接收主机（验证用，置空即关闭），主目的始终是 `DST_IP`。

当前配置：`DST_IP=192.168.6.88`、`DST_PORT=5600`、`640×360`、`quality=70`、`FPS=8`
（仅为 pacing 上限，双核 A7 软件 demosaic 实际约 **3.4 fps**）。

**两个已修的兼容性坑（重要）**：
1. RTP 时间戳必须跟随真实 90 kHz 时钟（`CLOCK_MONOTONIC`），不能按配置的 fps 累加。
   否则声明 8 fps、实际 3 fps 会让 VLC/ffplay 判定持续丢帧并卡在 jitter buffer。
2. 每帧处理完再固定 `usleep(1/fps)` 会把帧率再砍一半（2.4 fps）；改为**绝对速率 pacing**
   （目标时刻减去已耗时，只在还有余量时睡）。

**验收数据（实板）**：常驻进程 `pid=1550`，跨两个独立 SSH 会话确认存活；8 秒稳定态
收到 **1567 个 RTP 包 / 27 帧 / 27 张完整 JPEG**（`ffd8`…`ffd9` 全齐），帧间时间戳
平均 0.291 s，与墙钟平均 0.291 s 的 `max|Δ| = 0.0102 s`，无跳变。设备到
`192.168.6.88` 的 ping 与 ARP 均正常。首包 `V=2 PT=26 ssrc=0x53343630`。

**播放器侧（在 192.168.6.88 上操作）**：

```sh
# SDP（最稳，PT 26 是静态类型，无需 sprop）
vlc s4602.sdp
ffplay -protocol_whitelist file,udp,rtp -i s4602.sdp

# 不依赖 SDP
gst-launch-1.0 udpsrc port=5600 \
  caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=JPEG,payload=26" \
  ! rtpjpegdepay ! jpegdec ! autovideosink sync=false
```

SDP 的 `c=IN IP4 192.168.6.88` 是本机地址；若播放器报 bind 失败可改 `0.0.0.0`。
码流用 RFC 2435 的 `Q=255` + 128 字节量化表头，VLC/ffmpeg/GStreamer 均支持。
若黑屏依次检查：① 防火墙放行入站 UDP/5600；② `sh /tmp/start_outgoing.sh status`
确认 `RUNNING` 且日志帧数递增；③ 在本机 `192.168.6.109` 跑
`python3 tools/rtp_mjpeg_receiver.py` 看镜像流是否在发。

> 本机（192.168.6.109）没有 ffplay/ffmpeg/VLC/GStreamer，**GUI 播放未目测**；
> 传输层、RTP 头、分片重组、时间戳与画面内容均由 Python 接收器逐帧验证。

### 6.2 QGroundControl 收不到视频 —— 根因与 H.264 转换链路（2026-11-13）

**现象**：把 `DST_IP` 设成 `192.168.6.88:5600` 后，VLC/ffplay 用 MJPEG SDP 能出图，
但 **QGC 地面站始终黑屏**。

**根因：不是网络问题，是编码格式不匹配。**

QGC 的 `udp://` 视频源在 `src/GstSourceFactory.cc:402-420` 把 `udpsrc` 的 caps 写死成

```
application/x-rtp, media=video, clock-rate=90000, encoding-name=H264   (udp://)
application/x-rtp, media=video, clock-rate=90000, encoding-name=H265   (udp265://)
```

下游固定 `rtph264depay → parsebin`（H.265 是 `rtph265depay → h265parse`）。
scheme 白名单只有 `rtsp / udp / udp265 / mpegts / tcp`，**没有任何 JPEG/MJPEG 分支**。
本设备 outgoing 是 RFC 2435 **MJPEG、PT=26**，进 `rtph264depay` 后全部被丢弃，
所以即使包 100% 送达，QGC 也永远等不到可解码的第一帧。

现场证据（QGC 黑屏时）：设备 `pid=1550`、`frames=1130`、`fps=3.44` 持续增长；
本机 `192.168.6.109` 镜像收到 **27 帧 / 1567 包 / lost=0**；`.88` ping 正常。
→ 传输层、RTP 头、时间戳全部正常，唯一不匹配的是 payload。

**解法：主机侧转码中继**（不写 flash、不改 rootfs，等价于第 4 节路线 C 的实用化）：

```
摄像头 192.168.6.5                 中转主机 192.168.6.109              QGC 192.168.6.88
/dev/video0 → MJPEG(q70)  --UDP/5600-->  qgc_h264_relay.py  --UDP/5600-->  udp://0.0.0.0:5600
               RTP PT=26                 (ffmpeg + libx264)        RTP PT=96 / FU-A
                                                                 SPS/PPS 随 IDR 带内下发
```

| 文件 / 配置 | 内容 |
|---|---|
| `tools/outgoing.conf` | `DST_IP=192.168.6.109`、`MIRROR_IP=`（空）；已同步到设备 `/tmp` 与 SD 持久副本 |
| `tools/qgc_h264_relay.py` | MJPEG/RTP(PT26) → H.264/RTP(PT96)。**v2**：Python 自重组 RFC 2435 帧 → `image2pipe` 逐帧直送 ffmpeg，零探测缓冲；见 6.3 |
| `tools/start_qgc_relay.bat` | Windows 一键启动，可带参数改 QGC 主机：`start_qgc_relay.bat 192.168.6.88`（默认带 `--burn-clock`） |
| `out/captures/s4602_h264.sdp` | 每次启动由 ffmpeg 生成，给 `.88` 上的 VLC/ffplay 做旁证（QGC 本身不用 SDP） |
| `imageio-ffmpeg` | 本机原来没有 ffmpeg，已 `pip install imageio-ffmpeg`（内置 libx264 的 ffmpeg 9.0.2） |

运行：

```bat
:: 1) 设备（已部署，仅需确认）
sh /tmp/start_outgoing.sh status        :: RUNNING -> 192.168.6.109:5600

:: 2) 中转主机 192.168.6.109（先起中继，再重启设备推流）
tools\start_qgc_relay.bat 192.168.6.88
:: 或： python -u tools\qgc_h264_relay.py --dst-ip 192.168.6.88 --out-port 5600 --burn-clock

:: 3) 设备（中继监听 5600 后再执行，否则设备 UDP socket 会静默停发）
sh /tmp/start_outgoing.sh restart

:: 4) QGC（在 .88 上）：Video Source = "UDP h.264 Video Stream"，URL udp://0.0.0.0:5600
::    并勾选 Low Latency Mode；Windows 防火墙需放行入站 UDP 5600
```

**验收（回环 127.0.0.1:5602 实测，避免干扰 QGC）**：9 秒收到 **933 个 PT=96 包**、
70 个 M 位（≈7.8 fps）、893 个 FU-A 分片、seq 连续；NAL 类型统计含
`24=STAP-A`（SPS/PPS 聚合）、`1=非 IDR`、`28=FU-A`；ffmpeg 解码出
`out/captures/qgc_h264_decoded.jpg` —— 百叶窗/桌面/灯点可辨，画面正常。
QGC 是中途加入的无握手流，需等到**第一个 IDR**（≤1 s）才出图，属正常。

> 若 QGC 仍黑屏，按顺序查：① `.88` 是否真的是 QGC 所在主机（若 QGC 在别的机器，
> 直接改 `--dst-ip`）；② `.88` 防火墙入站 UDP/5600；③ QGC 的 Video Source 选的是
> **UDP h.264** 而非 RTSP/UDP h.265；④ 端口是否被 VLC/Skype 等占用；
> ⑤ 中转窗口日志是否有 `frame=` 递增。

**遗留**：这是"主机代编码"，仍需一台 x86 常驻。设备原生 H.264（路线 B / 任务 #35）
仍未完成 —— `RK_MPI_VENC_CreateChn` 的 `KEY_FPS is null` 与
`VI_EnableChn` 的 `MPLANE not support DMA Buffer yet` 两处 easymedia 阻塞尚未解决；
另一个候选是交叉编译 x264 直接在 RV1109 上软编，避免中转。

### 6.3 QGC「能出图但延迟很大」—— 根因与 v2 逐帧直送（2026-11-13）

用户反馈：QGC 画面正常，但**滞后数秒**。这不是网络问题（丢包 0、RTT < 1 ms）。

#### 根因 1（主因）：ffmpeg 的 RTP/SDP 输入探测积压 ≈ 6 秒

v1 让 ffmpeg 直接读 `s4602_mjpeg_in.sdp`。`avformat_find_stream_info()` 默认
`analyzeduration=5 s` —— 它要先攒够约 5~6 秒的帧才确认 MJPEG 尺寸/格式，
之后把这批帧一次性冲给编码器。ffmpeg 日志可量化：

```
frame=   39 ... time=00:00:17.14 ... speed=1.51x elapsed=0:00:11.33
                 ^^^^^^^^^^^^^^                    ^^^^^^^^^^^^^^^^^
                 媒体时间 17.14 s                  墙钟只有 11.33 s
                 → time - elapsed ≈ +5.9 s（恒定，不收敛）
```

即：**输出媒体时间比真实时间多跑 5.9 秒**，且之后按 1:1 前进，差值不消失。
QGC 的视频 sink 默认 `sync=true`（`src/GstVideoReceiver.cc:1168`），按 PTS
排程播放 → 这批"提前"的帧被摊到未来 5.9 秒播放，画面**永久滞后约 6 秒**。

v1 还有第二个放大器：`-vf fps=8` 把真实 3.5 fps 复制成 8 fps，媒体时间以
`speed≈1.04x` 略快于墙钟，QGC 的 jitterbuffer 会继续累积播放延迟。

**试过但无效/有害的参数**（实板记录）：
- `-use_wallclock_as_timestamps 1` 用在 **RTP 输入**上会破坏 RFC 2435 分片重组
  （同一帧 10 个分片靠相同 RTP 时间戳聚合，wallclock PTS 让每片时间戳都不同），
  ffmpeg 报 `unspecified size` / `Output file does not contain any stream`。
- `-analyzeduration 0 / 200000 / 1000000` + 小 `probesize`、`-max_delay 0`
  同样让探测拿不到完整 JPEG 帧 → 同样失败。
- `-fflags +bitexact` 只改 RTP 随机初始时间戳，与积压无关（已实测否定）。

#### 根因 2：QGC 自身的播放缓冲

本 fork 的默认设置（`src/VideoReceiver.h:119-120`）：

| 变量 | 默认 | 影响 |
|---|---|---|
| `_lowLatency` | `false` | `GstVideoReceiver.cc:119` `_buffer = lowLatency() ? -1 : 0` |
| `_rtpJitterLatencyMs` | `80` | `configureJitterBuffer()` 设 `rtpjitterbuffer latency=80`, `do-retransmission=TRUE` |
| sink `sync` | `true`（`_buffer >= 0`） | 按 PTS 排程渲染，帧率不稳时越积越久 |

勾选 QGC 的 **Low Latency Mode** 后：`jitterBuffer = None`、sink `sync=false`
→ 帧到达即渲染，彻底去掉这段播放延迟。

#### 根因 3（下限）：**当前软件链路** 3.5 fps（不是设备能力）

`mjpegout` 在双核 A7 上软件 demosaic + 色度降噪 + stbi JPEG，实测
**3.4~3.5 fps**，单帧固有耗时 ≈ 0.29 s，`top` 显示单进程占 45.4 % CPU
（一个 A7 核跑满）。这只是这一条软件路径的硬下限，与网络/转码无关。

> **重要更正（2026-11-13）**：sensor 实际按 **26.67 fps** 出帧
> （dmesg `rkcif... skip current frame` 中位间隔 37.502 ms），约 86.8 % 的帧
> 被软件链路丢弃；原厂 MPP 的**硬件 H.264 编码器已实测可用**
> （640×360 573 fps / 1080p 106 fps）。详见 **`docs/MPP_H264.md`**。
> 改用 MPP 后本节这个「下限」会被彻底消除。

#### v2 解法：去掉 ffmpeg 的 RTP demuxer，改为逐帧管道

```
摄像头 --UDP/5600 MJPEG RTP-->  Python（常驻持端口）
                                 ├─ RFC 2435 重组（实测 6 ms/帧）
                                 └─ 每收齐一帧立刻 write 到 ffmpeg stdin
                                        ffmpeg: -f image2pipe -vcodec mjpeg
                                                -analyzeduration 0 -probesize 32
                                                -use_wallclock_as_timestamps 1
                                        → libx264 zerolatency → RTP PT96
```

要点：
- **没有 5 秒探测积压**：image2pipe 只需解析第一张 JPEG 头，帧到即编码即发。
- **时间戳跟真实时钟**：`-use_wallclock_as_timestamps 1` 用在管道输入上是安全的
  （RTP 输入上不行），实测 RTP 时间戳增量 / 墙钟增量 = **1.0010**，无漂移。
- **不再复制帧**：`-fps_mode passthrough`，输出帧率 = 当前软件链路的 3.5 fps。
- **顺带修掉设备侧怪象**：Python 常驻持有 UDP 5600。之前杀掉中继后 5600 无监听，
  设备 `mjpegout` 的未连接 UDP socket 会静默停止发包（`frames` 仍在涨但主机 0 包），
  必须 `sh /tmp/start_outgoing.sh restart` 才恢复。现在中继重启不会释放端口。
- `--burn-clock` 用 `drawtext` 把主机墙钟烧进每帧，QGC 上直接读出端到端滞后；
  `out/captures/qgc_v2_frame.jpg` 是实测样例（左上角 `2026-11-13 13:09:41`）。
  字体 `tools/arial.ttf`（从 `C:\Windows\Fonts` 拷贝，规避 Windows 盘符冒号转义）。

**验收（回环 127.0.0.1:5602，10 秒）**：

| 指标 | v1（RTP/SDP 直读） | v2（逐帧直送） |
|---|---|---|
| `time - elapsed` | **+5.9 s 恒定** | 无此概念（无探测阶段） |
| 输出帧率 | 8 fps（复制帧） | **3.6 fps（= 当前软件链路帧率，非硬件上限）** |
| RTP 时间戳 / 墙钟 | 有漂移风险 | **1.0010** |
| 重组耗时 | — | **0.006 s/帧** |
| PT=96 包 / 10 s | — | 182 包 / 36 帧 / 0 丢包 |
| SPS/PPS | 每 8 帧 | 每 4 帧（≈1.1 s 一个 IDR，QGC 中途加入更快出图） |

#### 现场操作顺序（重要）

1. **先**在 `.109` 起中继（它绑定 5600）→ `tools\start_qgc_relay.bat 192.168.6.88`。
2. **再**在设备上 `sh /tmp/start_outgoing.sh restart`。顺序反了会触发上面那个
   UDP socket 静默停发。
3. QGC 勾 **Low Latency Mode**，Video Source = `UDP h.264`，URL `udp://0.0.0.0:5600`。
4. 看画面左上角水印：与 PC 时钟差多少就是端到端延迟。正常应 < 1 s。
5. 若还想再降：把设备 `outgoing.conf` 的 `WIDTH/HEIGHT` 从 `640 360` 降到
   `480 270`（像素少 44%，帧率约翻倍），可把 0.29 s 的固有延迟压到 ~0.15 s；
   中继里加 `scale=640:360` 可保持显示尺寸。此项**尚未实施**。
6. **根本解法（✅ 2026-11-13 已实施）**：设备端 MPP 硬件 H.264/H.265 直出，
   Windows 中转与整个 MJPEG 软编路径均可删除 → 权威记录 **`docs/DIRECT_H26X.md`**、
   `tools/h264out.c`。下面 6.1~6.3 的 MJPEG 中继链路仅作为历史/回滚方案保留。

---

## 7. 参考与证据

- OpenIPC wiki `majestic-streamer.md` — `outgoing` / `naluSize` / `udp://` / RTMP 分支
- `wfb-ng` README — `udp://your_ip:5602` 与 `rtph264depay` 解码管线
- `openipc-core::rtp` — `RTP_PAYLOAD_TYPE_H264 = 96`、`RTP_PAYLOAD_TYPE_H265 = 97`
- `themactep/test-video-delay.sh` — `udpsrc port=5600` + `encoding-name=H264/H265` 官方示例
- `docs/FIRST_LIGHT.md` — 抓帧链路、原厂库 ABI、IQ 文件位置
- `docs/evidence/v4l2probe_out.txt` — 全节点 MPLANE 能力
- `tools/rtpprobe.c` / `firmware/rtpprobe` — 本次 UDP/RTP 链路验证工具
- `tools/dmabuf_probe.c` / `firmware/dmabuf_probe` — V4L2 DMABUF 支持探针
- **`docs/WHALETX_REFERENCE.md`** — WhaleTX(RV1106) 逐项对照 + 零拷贝 API 映射 + DMABUF 实证
- `out/reference/whaletx/` — WhaleTX 源码归档；`docs/evidence/factory_rkaiq_sysctl.txt` — librkaiq 全导出
- `tools/mpcap.c` / `tools/raw2png.py` — 现成的 MPLANE 抓帧与 RAW10 解码
- **`docs/P0_RUNTIME_ABI.md`** — P0 实证：原厂运行时部署 / dlopen+dlsym / MB dma-buf /
  `VIDIOC_QBUF(DMABUF)` 导入 / 官方 SDK 头文件 / VENC 结构体布局 / IQ 注入序列
- `out/reference/rkmedia/` — 同代官方 SDK 48 个 `.h` + H.264 demo + ISP 初始化示例
- `tools/{factory_runtime,deploy_factory_runtime}.py`、`tools/{mpiprobe,mbprobe,dmabuf_link,vencprobe}.c`
  — P0 全套部署与 ABI 探针
- **`docs/P1_IQ_INJECTION.md`** — P1 权威记录：IQ 注入成功、easymedia VI/VENC 阻塞定位、后续入口
- **`tools/mjpegout.c` / `firmware/mjpegout`** — 路线 A 发送器（静态 ARM，零依赖）
- **`tools/rtp_mjpeg_receiver.py`** — 纯 Python RFC 2435 接收/重组器
- **`tools/deploy_iq.py` / `tools/aiqprobe.c` / `tools/rkstream.c`** — P1 IQ 部署与链路探针
- `out/reference/stb/stb_image_write.h` — 静态 JPEG 编码器（公共领域）
- `out/captures/s4602_mjpeg.sdp` — 播放器 SDP；`out/captures/mjpeg_final.jpg` — RTP 重组帧
- `docs/evidence/{aiqprobe_run1,aiqprobe_run2,iq_video6_capture,rkstream_run1,rkstream_debug2}.txt` — P1 全部原始日志

---

## 8. 最终形态：设备端直发 H.264 / H.265（2026-11-13 闭环）

`tools/h264out.c` 已把「路线 B」真正落地，且**两种编码都实板验收**：

```
/dev/video0 RAW10 → 软件 NV12 → MPP 硬件 VENC → RTP/UDP → 192.168.6.88:5600
```

- H.264 = PT 96 / RFC 3984 FU-A；H.265 = PT 97 / RFC 7798 FU（VPS/SPS/PPS 周期插入）。
- QGC：`UDP h.264` → `udp://0.0.0.0:5600`；`UDP h.265` → `udp265://0.0.0.0:5600`。
- `sh /tmp/start_h264out.sh start|stop|restart|status`；配置在 `tools/h264out.conf`
  （改 `CODEC=h265` 即切 H.265）。
- 端到端校验：`python tools/verify_direct_stream.py --codec h264|h265`
  （收 UDP、重组 NAL、ffmpeg 解帧）。实测两种 codec 均 **0 丢包**，
  H.265 NAL 分布 `{1:28, 19:2, 32:2, 33:2, 34:2, 39:4}`。
- 帧率：320×180 约 14~24 fps、640×360 约 7~9 fps（软件 RAW10 转换是瓶颈，
  sensor 本身 26.67 fps、MPP 硬件编码数百 fps）。
- **不再需要** `qgc_h264_relay.py` / `start_qgc_relay.bat`；被用户取消的后台任务
  `q2MPX2` 不要重启。旧 MJPEG 常驻链路保留作回滚。

为什么要走软件 NV12 而不是 rkisp 零拷贝：`/dev/media1` 的
`rkisp-isp-subdev:p2 → rkisp-bridge-ispp` 链路默认 ENABLED，而 rkispp 硬件
启动即报 `can't request region for resource` / `max input:0x0@0fps`，
`video19` 只会吐全 0 NV12；关掉 bridge 打开 mainpath 后 `video5` 虽能出帧但 Y 仍全 0。
属内核/DT 层缺陷，用户态不可修。完整取证与 MPP 缓冲类型坑见 **`docs/DIRECT_H26X.md`**。
