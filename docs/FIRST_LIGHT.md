# S4602 首次出图（First Light）— 2026-11-13

## 结论先行

**设备已经出图。** OpenIPC 内核（4.19.111+，自编 `sc5338.c`）驱动
SC5338 sensor → MIPI DPHY(720 Mbps) → CSI2 → rkcif 全链路工作，
通过标准 V4L2 抓到了真实画面帧并渲染成图像。

同时**推翻了此前的一个关键误判**：「vendor 内核禁用标准 media/V4L2 用户态 API、
camera 节点只认 Rockchip 私有 RKMedia 接口」是错的。真实原因是
**所有 rkcif/rkisp/rkispp 的 video 节点都是 `V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE`（多平面）**，
此前的抓帧器用了普通 `VIDEO_CAPTURE` 类型，于是 `G_FMT`/`S_FMT`/`ENUM_FMT`
一律返回 `EINVAL`（而非 `ENOTTY`），看起来像驱动不支持。

> 教训：`EINVAL` ≠ 驱动未实现。先用 `QUERYCAP` 看 `device_caps` / `V4L2_CAP_VIDEO_CAPTURE_MPLANE`
> 再决定 buffer type；`MEDIA_IOC_SETUP_LINK` 返回 `ENOTTY` 只说明该内核未开
> subdev/media 动态配置，并不影响 capture 节点本身出帧。

本文件是出图链路的权威记录；此前文档中「仍缺 rkaiq IQ/3A XML」「标准 V4L2 走不通」
的表述以此为准修订。

---

## 1. 设备侧实测（`tools/v4l2probe.c`）

编译：`tools/build_arm.py v4l2probe.c v4l2probe`（构建服务器
192.168.219.177 静态交叉编译），部署 `/tmp/v4l2probe`，
完整输出见 `docs/evidence/v4l2probe_out.txt`。

| 节点 | driver / card | caps | G_FMT（MPLANE） |
|---|---|---|---|
| video0..3 | `rkcif` | CAPTURE_MPLANE + STREAMING | **2880×1616 BG10**（Bayer RAW10） |
| video4 | rkcif | META_CAPTURE | — |
| **video5** | `rkisp_v4` / `rkisp_mainpath` | CAPTURE_MPLANE + STREAMING | **2880×1616 NV12** |
| **video6** | `rkisp_v4` / `rkisp_selfpath` | CAPTURE_MPLANE + STREAMING | **1920×1080 NV12** |
| video7..10 | rkisp_rawwr0..3 | CAPTURE_MPLANE | 2880×1616 BG10 |
| video11..13 | rkisp_rawrd* | OUTPUT_MPLANE | — |
| video14/16 | rkisp-statistics / mipi-luma | META_CAPTURE | — |
| video15 | rkisp-input-params | STREAMING | — |
| video17 | rkispp_input_image | OUTPUT_MPLANE | — |
| video18..22 | rkispp_m_bypass / scale0-2 / iqtool | CAPTURE_MPLANE | 0×0（未配置） |
| video23/24 | rkispp_input_params / stats | — | — |

`video0` 的 `ENUM_FMT` 里包含 `NV16 NV61 NV12 NV21 YUYV YVYU UYVY VYUY RGB3 RGBP
BGRH RGGB GRBG GBRG BA81 RG10`；`video5/6` 包含完整 YUV/RGB 列表。
**格式在 probe 时已由内核/Sensor 默认协商好**，无需 subdev S_FMT。

节点映射（media 拓扑，见前次 `mediaprobe2`）：
`v4l-subdev3 = m01_f_sc5338 1-0030`、`2 = mipi-dphy-rx`、`1 = mipi-csi2`、
`6 = rkcif_mipi_lvds`、`4 = rkisp-isp-subdev`、`5 = rkisp-csi-subdev`、`0 = rkispp-subdev`。

设备环境：glibc 2.38（Buildroot）、rootfs 22.1 MiB 空闲、SD `/mnt/mmcblk1` 7.3 GiB 空闲、
`/dev/mpp_service`、`/dev/rga`、`/dev/media0-2` 齐全、`lsmod` 为空（驱动全编入内核）。

## 2. 抓帧 SOP

工具 `tools/mpcap.c`（静态 armhf，产物 `firmware/mpcap`）：

```
mpcap <dev> <out.raw> [w h] [timeout_ms]
```

流程：`QUERYCAP` → `G_FMT(MPLANE)` →（可选）`S_FMT(NV12, MPLANE)` →
`REQBUFS(count=4, MMAP)` → `QUERYBUF`/`mmap`（MPLANE 必须用
`buf.m.planes[0].length` 与 `mem_offset`，且 `buf.length=1`）→ `QBUF` →
`STREAMON(MPLANE)` → `select` → `DQBUF` → 写盘 → `STREAMOFF`。

**成功命令**（关键证据）：

```
/tmp/mpcap /dev/video0 /tmp/v0.raw 0 0 4000
[i] G_FMT 2880x1616 fourcc=BG10 planes=1 sizeimage=6205440 bpl=3840
[i] STREAMON ok
[i] DQBUF index=0 bytesused=6205440 seq=0 flags=0x2001 ts=4835.240625
[+] wrote 6205440 bytes -> /tmp/v0.raw
```

对应 dmesg（sensor 确实在流）：

```
rkcif_mipi_lvds: stream[0] start streaming
rockchip-mipi-csi2 ffb10000.mipi-csi2: stream ON
rockchip-mipi-dphy-rx ff4b0000.csi-dphy: stream on:1
rockchip-mipi-dphy-rx: data_rate_mbps 720
rkcif_mipi_lvds: stream[0] stopping finished
```

### 数据格式

`BG10` 帧 = **MIPI RAW10 packed**（4 像素 / 5 字节：`[hi0 hi1 hi2 hi3 lo]`），
`stride = 3840 = 3072 px × 1.25 B`（3072 = 2880 按 256 对齐），
有效宽 2880，`6205440 = 3840 × 1616`。字节周期性统计佐证：
`lag=5` 平均绝对差 45.6 最低（对比 lag=1 的 68.1），`lag=7680`(2 行) 47.7 低于
`lag=3840`(1 行) 60.8（Bayer/HDR 行交替特征）。

## 3. 图像还原（纯 Python，零依赖）

`tools/raw2png.py`：解包 RAW10 → 2×2 Bayer 合成（binning demosaic）→
自动黑电平 / 灰世界白平衡 / gamma → 手写 PNG 编码（zlib + CRC32）。

```
python3 tools/raw2png.py out/captures/s4602_frame_bg10.raw \
        out/captures/s4602_first_light_BGGR.png --bayer BGGR
```

产物（`out/captures/`）：

| 文件 | 说明 |
|---|---|
| `s4602_frame_bg10.raw` | 设备原始帧（6,205,440 B） |
| `S4602_FIRST_LIGHT.png` | 色度降噪后的最终彩色图（720×404） |
| `rkcif_nv12_gray.png` | 把 Bayer 高位当亮度平面的灰度图，**结构最清晰** |
| `bayer_compare.png` | 4 种 Bayer 排列对比 |
| `first_light_rowcorrected.png` | 行级 HDR 曝光归一化结果 |

**画面内容**：带百叶窗的窗户/灯箱（密集水平亮条）、透过窗户的若干灯光亮点、
左侧网格围栏、右下斜面（桌面/地面）。已确认是真实场景，不是噪声或测试图。

**画质局限**：仍有假彩色与细密彩色摩尔纹。原因不是解包错误，而是
① 无 ISP 3A / 去马赛克 / 降噪，软件 binning demosaic 粗糙；
② SC5338 为 `HDR_X2`（`hdr:5`），长短曝光行交错（实测奇偶行归一化增益
1.308 vs 1.416），直接 2×2 合成会混两个曝光；
③ 10-bit RAW 暗部噪声被 gamma 0.55 放大。
色度域 9×9 box 降噪 + 饱和度 0.55 已显著压制，但彻底解决必须走 ISP。

## 4. ISP 侧仍未打通（下一步）

| 节点 | 现象 | 原因 |
|---|---|---|
| video6 selfpath | `STREAMON` 成功但 `select` 超时，无帧 | dmesg `rkisp rkisp-vir0: **can not get first iq setting in stream on**`；`rkisp0: rkisp_stream_stop id:1 timeout` / `check video link or isp input` |
| video5 mainpath | `STREAMON: Operation not permitted` (EPERM) | media1 拓扑里 `rkisp-isp-subdev:pad2 → rkisp_mainpath:pad0` 为 `[off]`，内核未实现 `SETUP_LINK` 无法打开 |
| video0 S_FMT NV12 | 接受并出帧，但 `bpl` 仍 2880、`sizeimage` 仍 6981120 | rkcif 只是把 Bayer 数据贴 YUV 标签，**没有硬件去马赛克**；按 NV12 解读得到绿色噪声 |

**根因**：rkisp 驱动要求 `/dev/video15 (rkisp-input-params)` 先收到第一份 IQ 参数，
原厂由 `librkaiq.so` + SC5338 IQ tuning 完成。

## 5. 原厂运行时盘点（已找到，ABI 兼容）

解包位置 `out/factory_rootfs/system/`，清单 `out/factory_rootfs/system_list`，
依赖分析 `out/factory_rootfs/elf_deps.txt`，导出符号 `docs/evidence/factory_rkmedia_symbols.txt`。

- **ABI 兼容**：原厂全部 `.so` 最高只要求 `GLIBC_2.28`，设备是 **glibc 2.38**
  → 可直接 `LD_LIBRARY_PATH` 加载，**不需要 chroot / 不需要重编译**。
- **RKMedia 核心**：`libeasymedia.so.1.0.1`（SONAME `libeasymedia.so.1`）
  导出 **575 个符号**，含完整 `RK_MPI_SYS_*` / `RK_MPI_VI_*` / `RK_MPI_VENC_*` /
  `RK_MPI_MB_*` / `RK_MPI_RGA_*`。即 Rockchip MPI 的 easymedia 变体。
- **SONAME 陷阱**：`librockchip_mpp.so.0` 的 **SONAME 是 `librockchip_mpp.so.1`**
  （`swapp`/`libeasymedia` 都 NEEDED `.1`）→ 部署时补软链即可，不是缺文件。
  同理 `librga.so.2.1.0`→`librga.so.2`、`libv4l2.so.0.0.0`→`libv4l2.so.0`、
  `libeasymedia.so.1.0.1`→`libeasymedia.so.1`。
- **rkaiq**：`librkaiq.so`（SONAME 同名，导出 4924 符号，含 `rk_aiq_uapi_*`）、
  `libRKAP_3A.so` / `_ANR.so` / `_Common.so`。
- **IQ / 3A tuning（此前记为"缺失"，现已找到）**：
  - `/system/resource/scene_param/COMMON_sc5338.xml` ← **SC5338 IQ tuning**
  - `/system/resource/image_config/sc5338_capture.conf`
  - `/system/resource/vqefiles/RKAP_3A_Para_16k.bin`、`RKAP_3A_Para_8k.bin`
- **没有** `rkipc` / `majestic` / `librockit` / `librkmedia`；流媒体入口是厂商
  `swapp`（`/system/bin/`），它直接调 `RK_MPI_*` + `rk_aiq_uapi_sysctl_*` + ffmpeg
  (`avcodec_send_frame` / `avformat_*`) 完成编码推流。`libthird_media.so` 只导出 1 个符号。
- `swapp` 依赖 50+ 个库（`sw*`、`rknn`、`OpenVX`、`osip/eXosip`、`av*` 等），
  全部在 `/system/lib/` 内，可整套搬到 SD 卡。

## 6. 推荐的后续路径（按风险/收益排序）

1. **P0 最小 IQ 注入**：部署 `/system/lib` + `resource/` 到 `/mnt/mmcblk1/system`，
   补 SONAME 软链；写一个最小 C 程序 `dlopen("libeasymedia.so.1")` 调
   `RK_MPI_SYS_Init` → `RK_MPI_VI_SetChnAttr/EnableChn/StartStream` →
   `RK_MPI_SYS_GetMediaBuffer` + `RK_MPI_MB_GetPtr/GetSize` 取 NV12 帧；
   rkaiq 侧用 `rk_aiq_uapi_sysctl_preInit/init/updateIq` 注入
   `COMMON_sc5338.xml`。成功后即得到硬件 ISP 的干净 NV12 帧。
2. **P1 直接跑 `swapp`**：`LD_LIBRARY_PATH=/mnt/mmcblk1/system/lib` 启动，
   观察是否拉起本地 RTSP（黑盒，依赖云平台配置，风险较高但成本最低）。
3. **P2 编码/出流**：走 `RK_MPI_VENC_CreateChn`（H.264，内核 `/dev/mpp_service`
   与 rkvenc 已 probe 成功）或 ffmpeg，接 RTSP / majestic。
4. **P3 若 mainpath 必需**：`rkisp-isp-subdev:pad2 → rkisp_mainpath` 为 `[off]`
   且内核无 `SETUP_LINK`，要么改用已 ENABLED 的 selfpath(video6)，
   要么在内核里把该 link 设为 immutable-enabled 后重编。

## 7. 工程红线（不变）

- 不碰 `0x0–0x300000` loader / uboot / env 区；只刷一个槽，`_a` 原厂保留回滚。
- `mtd write <name> <addr> <off> <size>` 四参数齐全 + 回读 CRC 比对。
- 本轮全部操作只在 RAM(`/tmp`) 与 SD 卡进行，**未改动 rootfs、未写 flash**。

## 8. 证据与工具索引

```
tools/v4l2probe.c           QUERYCAP/ENUM_FMT/G_FMT 双类型探针
tools/mpcap.c               MPLANE mmap 单帧抓帧器
tools/raw2png.py            RAW10 → PNG（零依赖）
tools/build_arm.py          通用静态 armhf 交叉编译
firmware/v4l2probe, firmware/mpcap
docs/evidence/v4l2probe_out.txt       全节点探针输出
docs/evidence/factory_rkmedia_symbols.txt  原厂库导出符号
out/factory_rootfs/elf_deps.txt       ELF NEEDED + GLIBC 版本需求
out/captures/*.raw, *.png             原始帧与还原图像
```
