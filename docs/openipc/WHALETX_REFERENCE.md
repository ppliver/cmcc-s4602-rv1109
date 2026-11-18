# WhaleTX（RV1106/RV1103 Rust 发射端）参考评估 → S4602 / RV1109

日期：2026-11-13
参考：https://github.com/HumpbackLab/WhaleTX （master, tree `2fd14fc`）
源码已整份归档到 `out/reference/whaletx/`（15 个文件，含 `csrc/bridge.c`、`src/sink/udp_rtp.rs`、
`src/ffi/mod.rs`、`src/main.rs`、`whaletx.ini`、`build.rs`）。

用户给出的链路描述准确：`V4L2 rkisp path → MPI MMZ dma-buf → VENC → UDP RTP`。
本文评估**哪些能抄、哪些必须改写**。

---

## 1. WhaleTX 到底是什么

README 原文：`whaletx is a Rust implementation of the RV1106 video transmitter pipeline`。
（用户说 RV1103；RV1103/RV1106 共用同一 BSP，`build.rs` 的宏是 `RV1106_RV1103`。）

- 目标：`armv7-unknown-linux-**uclibc**eabihf`，工具链 `arm-rockchip830-linux-uclibcgnueabihf`，
  SDK 根由 `WHALETX_SDK` 提供（Luckfox SDK 布局）。
- 链接库（`build.rs:158-165`）：`librockit.so`、`librockchip_mpp.so`、`librga.so`、
  `librkaiq.so`、`libstdc++`。
- 已验证运行配置：1280×720 @ 90 fps、H.264、`vi_channel=1`（rkisp_selfpath）、
  `payload_type=96`、`mtu=1200`、目标 `192.168.3.2:5600`。
- 架构：`src/` 安全 Rust 逻辑，`src/ffi/` 裸 C ABI，`csrc/bridge.c` 是 V4L2/AIQ/MPI 的
  C 桥（28 KB，含 OSD 共享内存 dirty-rect 优化，与本项目无关）。

### 完整链路（按 `main.rs` 执行顺序）

```
main.rs:80   force_sensor_mode()   → 调 media-ctl 强制 sensor S_FMT（SGRBG10_1X10/WxH）
main.rs:81   IspHandle::start()    → rk_aiq_uapi2_sysctl_* 启动 ISP/3A + IQ 注入
main.rs:82   V4l2Device::open()    → S_FMT NV12 / MPLANE，REQBUFS(memory=DMABUF)
main.rs:89   MpiGuard::init()      → RK_MPI_SYS_Init
main.rs:90   VencGuard::start()    → RK_MPI_VENC_CreateChn(H264 CBR) + StartRecvFrame + RequestIDR
main.rs:103  MmzBuffer::alloc() ×N → RK_MPI_MMZ_Alloc(UNCACHEABLE)
                                   → RK_MPI_MB_SetBufferStride
                                   → RK_MPI_MB_Handle2Fd  ← dma-buf fd
main.rs:117  VIDIOC_QBUF(index, planes[0].m.fd = dma_fd, V4L2_MEMORY_DMABUF)
main.rs:121  VIDIOC_STREAMON
  ┌ capture 线程 ──────────────────────────────────────────────┐
  │ poll() → DQBUF → (可选 --drain 丢旧帧只留最新)               │
  │ → RK_MPI_SYS_MmzFlushCache                                  │
  │ → RK_MPI_VENC_SendFrame(MB_BLK，手工填 VIDEO_FRAME_INFO_S) │
  │ → 重新 QBUF 同一个 dma_fd                                   │
  └─────────────────────────────────────────────────────────────┘
  ┌ stream 线程 ───────────────────────────────────────────────┐
  │ RK_MPI_VENC_GetStream → Annex-B NALU 包                    │
  │ → UdpRtpSink::write()                                       │
  │ → RK_MPI_VENC_ReleaseStream                                │
  └─────────────────────────────────────────────────────────────┘
  ┌ UDP 发送线程（src/sink/udp_rtp.rs）────────────────────────┐
  │ 单帧 pending 队列（新帧替换未发完的旧帧，不堆积延迟）      │
  │ Annex-B 扫 00 00 01 / 00 00 00 01 → 单 NAL 或 FU-A 分片   │
  │ RTP: V=2, PT=96, seq++, ts=pts*9/100 (90kHz), M 标帧末    │
  │ 非关键帧可被抢占丢弃（preempt），保证低延迟                │
  └─────────────────────────────────────────────────────────────┘
```

**核心技巧**（`ffi/mod.rs:257-276` + `bridge.c:346-366`）：用 MPI 的 MMZ 内存池分配 buffer，
`RK_MPI_MB_Handle2Fd()` 导出 dma-buf fd 直接交给 V4L2 `QBUF`；`DQBUF` 后**同一个 MB_BLK**
直接送 VENC。全程零拷贝，用户态完全不碰像素。这正是我们此前方案里缺失的一环。

---

## 2. 逐项对照：能不能抄

| 环节 | WhaleTX (RV1106 / rockit / UAPI2 / uclibc) | S4602 (RV1109 / easymedia / UAPI1 / glibc) | 结论 |
|---|---|---|---|
| 固件/ABI | `armv7-**uclibc**`，Luckfox SDK | glibc **2.38**，`arm-linux-gnueabihf` | ❌ 二进制/工具链都不通用 |
| MPI 库 | `librockit.so` | `libeasymedia.so.1`（575 × `RK_MPI_*`） | ❌ 库不同，结构体 ABI 要重定 |
| `RK_MPI_SYS_Init` | 有 | **有** | ✅ 同名 |
| 退出 | `RK_MPI_SYS_Exit` | 无（可不调） | ⚠️ 缺，非阻塞 |
| MMZ 分配 | `RK_MPI_MMZ_Alloc(UNCACHEABLE)` | `RK_MPI_MB_CreateBuffer` / `MB_POOL_Create` / `MB_CreateImageBuffer` | 🔁 改名，功能等价 |
| buffer stride | `RK_MPI_MB_SetBufferStride` | 无；应在 `MB_CreateImageBuffer` 参数/`MB_SetSize` 里给 | 🔁 需确认签名 |
| **导出 dma-buf fd** | `RK_MPI_MB_Handle2Fd` | **`RK_MPI_MB_GetFD`** ✅ | 🔁 改名，**能力存在** |
| 虚地址 | `RK_MPI_MB_Handle2VirAddr` | **`RK_MPI_MB_GetPtr`** ✅ | 🔁 改名 |
| cache 同步 | `RK_MPI_SYS_MmzFlushCache` | `RK_MPI_MB_BeginCPUAccess` / `EndCPUAccess` | 🔁 改名；UNCACHEABLE 可省 |
| **V4L2 DMABUF** | `REQBUFS(DMABUF)` + `planes[0].m.fd` | **实测 video0 / video5 / video6 全部 `REQBUFS DMABUF OK`** | ✅ **完全可用（本次实证）** |
| 送 VENC | `RK_MPI_VENC_SendFrame` | **无**。有 `RK_MPI_SYS_SendMediaBuffer` / `RK_MPI_SYS_Bind` | 🔁 架构不同：easymedia 走 SYS 通道数据流 |
| 取码流 | `VENC_GetStream` / `ReleaseStream` | **无**。有 `SYS_GetMediaBuffer` / `StartGetMediaBuffer` / `StopGetMediaBuffer` | 🔁 改名 |
| VENC 其余 | CreateChn/StartRecvFrame/SetChnParam/RequestIDR/GetFd | 同名全部存在（33 个 `VENC_*`），另有 **`CreateJpegLightChn`** | ✅ 大部分同名 |
| IQ / AIQ | `rk_aiq_uapi2_sysctl_*`（UAPI2：enumStaticMetasByPhyId / preInit_devBufCnt / init / prepare / start） | **`rk_aiq_uapi_sysctl_*`（UAPI1，37 个 sysctl 符号）**：preInit / init / prepare / start / **updateIq** / stop / deinit | ❌ **版本不兼容，初始化序列必须重写** |
| SDK 头文件 | `WHALETX_SDK` 提供 `rk_mpi_venc.h` / `rk_aiq_user_api2_*.h` | **原厂 rootfs 里 `.h` 数量为 0** | ❌ 不能编译 `bridge.c`，只能 `dlopen/dlsym` + 自定结构体，或另找 RV1109 easymedia SDK 头 |
| ISP path | `vi_channel=0`→mainpath(video11) / `1`→selfpath(video12) | mainpath **video5 = `[off]`**、selfpath **video6 = `[ENABLED]`** | ✅ **对应 `vi_channel=1`，用 `/dev/video6`** |
| sensor 格式 | `media-ctl` 强制 SGRBG10 | 默认格式已协商好；设备无 `media-ctl`、subdev `S_FMT` 实测 ENOTTY | ✅ **可整段删掉** |
| **RTP/UDP 段** | `src/sink/udp_rtp.rs` 纯 Rust 标准库、零 FFI | 与 SoC/SDK 完全无关 | ✅ **几乎可原样复用**（C 重写 ~150 行） |
| 目标/端口 | `192.168.3.2:5600` / PT 96 / mtu 1200 | `192.168.6.109:5600`（已实测 UDP 可达） | 改 IP 即可 |

### 实证记录

**① DMABUF 支持**（`tools/dmabuf_probe.c`，证据 `docs/evidence/dmabuf_probe_out.txt`）：

```
/dev/video0  rkcif           2880x1616 NV12*  REQBUFS MMAP OK / DMABUF OK / USERPTR EINVAL
/dev/video5  rkisp_mainpath  1280x720  NV12   REQBUFS MMAP OK / DMABUF OK / USERPTR OK
/dev/video6  rkisp_selfpath  1280x720  NV12   REQBUFS MMAP OK / DMABUF OK / USERPTR OK
```
> *video0 的 NV12 是之前 `mpcap` 做 `S_FMT NV12` 留下的元数据，实际数据仍是 Bayer RAW10
> （已按 NV12 解读验证为绿色噪声）。分辨率也是当时 S_FMT 设的，不是开机默认值。

**② 原厂 MPI 符号**（`docs/evidence/factory_rkmedia_symbols.txt`）：
`RK_MPI_MB_GetFD`、`RK_MPI_MB_GetPtr`、`RK_MPI_MB_CreateBuffer`、
`RK_MPI_SYS_SendMediaBuffer`、`RK_MPI_SYS_GetMediaBuffer`、`RK_MPI_SYS_Bind`、
`RK_MPI_VENC_CreateJpegLightChn` 全部存在；`MMZ_*` / `VENC_SendFrame` / `VENC_GetStream` 全缺。

**③ rkaiq 版本**（完整 4924 个导出，证据 `docs/evidence/factory_rkaiq_sysctl.txt`）：
只有 `rk_aiq_uapi_sysctl_*`（UAPI1），**没有任何 `rk_aiq_uapi2_*`**。

**④ 头文件**：`out/factory_rootfs/system_list` 里 `.h` / `include/` 命中 **0**。

---

## 3. 结论：抄架构，不抄代码

### ✅ 直接可复用（最高价值）

1. **`udp_rtp.rs` 整段逻辑** —— 与硬件无关，且比我们自己草拟的规格更完整：
   - Annex-B start code 扫描（3B/4B 两种）
   - 单 NAL 直发；大 NAL 用 FU-A：`indicator = (nalu[0]&0xE0)|28`，
     `header = (S<<7)|(E<<6)|(nalu[0]&0x1F)`，`max_payload = mtu - 12`
   - M 位只在**最后一个 NALU 的最后一个分片**置 1
   - PTS(µs) → RTP ts：`delta * 9 / 100`（90 kHz）
   - **独立发送线程 + 单帧 pending 队列**：新帧替换未发完的旧帧，非关键帧可抢占丢弃 → 低延迟不堆积
   - `payload_type=96` / `mtu=1200` 与 OpenIPC majestic 完全一致
2. **MMZ/dma-buf 零拷贝思想** —— 内核侧已实测支持，我们原厂库有 `MB_GetFD`，链路能闭合。
3. **双线程分离**（capture / stream / send 三线程）+ drain-latest 丢帧策略 + 分阶段耗时统计。
4. **VENC 配置模板**（`bridge.c:259-325`）：H264 CBR / HIGH profile / `RK_FMT_YUV420SP` /
   VirWidth/Height 16 对齐 / `u32BufSize = align16(w)*align16(h)*3/2` /
   `StartRecvFrame(s32RecvPicNum=-1)` / `RequestIDR` / `SetSceneMode(ipc)` / `SetChnParam`。

### 🔁 必须改写

- IQ 初始化：UAPI2 → **UAPI1**（`preInit → init → prepare → start → updateIq`，见 `docs/FIRST_LIGHT.md`）。
- 送帧/取流：`VENC_SendFrame/GetStream` → **`SYS_SendMediaBuffer` + `SYS_GetMediaBuffer`**，
  或干脆用 **`RK_MPI_SYS_Bind(VI, VENC)`** 做硬件绑定（easymedia 更原生，连用户态循环都省）。
- 内存分配：`MMZ_Alloc/SetBufferStride/Handle2Fd` → **`MB_CreateBuffer`/`MB_GetFD`**（stride 语义待定）。
- 编译方式：不能 `#include <rk_mpi_venc.h>`，需 `dlopen("libeasymedia.so.1")` + `dlsym`，
  并自行按 RV1109 easymedia ABI 定义 `VENC_CHN_ATTR_S` / `VIDEO_FRAME_INFO_S` / `MB_BLK` 等结构体。
- Rust/uclibc 工具链整体不用；用现有 `arm-linux-gnueabihf-gcc` 静态交叉编译通道写 C。

### ❌ 不适用

- `force_sensor_mode()` 的 `media-ctl`（设备无此工具、subdev S_FMT ENOTTY，且默认格式已协商）。
- `librockit` 的 `RK_MPI_MMZ_*`、`VENC_SendFrame/GetStream`、`rk_aiq_uapi2_*`。
- OSD / msposd 共享内存那一大段（无需 OSD，且 `RGN_*` 结构体 ABI 也不同）。
- WhaleTX 编译产物、Luckfox SDK 布局、`arm-rockchip830-*` 工具链。

---

## 4. 对 `docs/OUTGOING_RTP.md` 路线 B 的修订（现在可以写得非常具体）

原路线 B 只写了「`RK_MPI_VENC` 硬编码 → RFC 3984」。按 WhaleTX 校准后：

```
P2-a  IQ 注入（UAPI1，绕不过）
      LD_LIBRARY_PATH=/mnt/mmcblk1/system/lib
      rk_aiq_uapi_sysctl_preInit / init / prepare / start / updateIq
      注入 /system/resource/scene_param/COMMON_sc5338.xml
      → rkisp selfpath (video6, link 已 ENABLED) 出干净 NV12 1280x720

P2-b  零拷贝采集
      RK_MPI_SYS_Init
      RK_MPI_MB_CreateBuffer / MB_POOL_Create ×2~3 (UNCACHEABLE 语义)
      RK_MPI_MB_GetFD  → dma-buf fd
      VIDIOC_S_FMT(NV12, MPLANE) + REQBUFS(DMABUF) + QBUF(fd) + STREAMON   ← 已实测支持

P2-c  编码
      方案 1（推荐，最贴合 easymedia）：RK_MPI_SYS_Bind(VI → VENC) 全自动
      方案 2（贴合 WhaleTX）：DQBUF → RK_MPI_SYS_SendMediaBuffer(MB_BLK) → VENC
      VENC: H264 CBR / 1280x720 / 90fps 上限按实测调 / GOP / RequestIDR
      取流：RK_MPI_SYS_GetMediaBuffer（或 VENC_GetFd + poll）

P2-d  发送（照抄 udp_rtp.rs）
      Annex-B → 单 NAL / FU-A(1200B) → RTP PT=96 / 90kHz → sendto 192.168.6.109:5600
```

**关键前置不变**：IQ 注入（任务 #33）是唯一硬阻塞。WhaleTX 也把 `IspHandle::start()`
放在 `main.rs:81` 的第一步 —— 说明这不是我们的特例，而是 Rockchip ISP 的固有要求。
好消息是 `video6 selfpath` 的 link 是 ENABLED，正好对应 WhaleTX 验证过的 `vi_channel=1`。

### 一个额外的低成本分支：`RK_MPI_VENC_CreateJpegLightChn`

原厂库里有这个符号 —— **硬件 JPEG 编码**。若 IQ 短期打不通，可用
`video0` + DMABUF + 硬件 JPEG 通道出 MJPEG/RTP(PT 26)，比软件 `libturbojpeg` 更省 CPU，
且仍然绕开 rkisp。这是路线 A 的硬件加速版本，值得作为 P0 的备选实测。

---

## 5. 风险与未知 —— **P0 已逐项解除（2026-11-13）**

详见 **`docs/P0_RUNTIME_ABI.md`**（部署、编译、实测全过程）。

| 原未知项 | P0 结论 |
|---|---|
| easymedia 结构体布局无头文件 | ✅ **已找到同代官方 SDK 头文件**（`Wu-Yumin/demo_h264_rkmedia`，48 个 `.h`，归档 `out/reference/rkmedia/`）。头文件里 VI 段同样没有 `CreateChn/GetChnFrame`，与设备库 dlsym 2/5 精确吻合。`VENC_CHN_ATTR_S`=100 B，库日志 `ParseMediaConfigFromMap: rect.w=1920, rect.h=1080` 证明布局正确 |
| `RK_MPI_MB_GetFD` 语义 / dma-buf 能否对接 V4L2 | ✅ `MB_CreateBuffer(size, bHardWare=1, …)` → `GetFD` 返回有效 fd；**`VIDIOC_QBUF(DMABUF)` 在 video6 返回 0**，零拷贝导入成功 |
| `SYS_SendMediaBuffer` / `GetMediaBuffer` 绑定方式 | ✅ 官方签名已明确：3 参数 `(MOD_ID_E, s32ChnID, MEDIA_BUFFER)` / `(MOD_ID_E, s32ChnID, s32MilliSec)`；`h264_rkmedia_demo.c` 给出 `RegisterOutCb` + `SYS_Bind` 完整范例 |
| `video0` 的 "NV12" 实为 Bayer 贴标签 | ⏳ 仍成立；但 video6 DMABUF 已通，只差 IQ 出帧 |
| UAPI1 `init()` 参数签名 | ✅ `sample_common_isp.c`：`enumStaticMetas(0,&info)` → `init(sensor_name, iq_file_dir, NULL, NULL)` → `prepare(ctx,0,0,WDRMode)` → `start(ctx)` |
| 新增：VENC 通道创建 | ⚠️ 当前 `-33 RK_ERR_VENC_BUSY`（`CreateJpegLightChn` 为 `-21 ILLEGAL_PARAM`），属运行时资源/配置，非 ABI；IQ/VI 通后重测 |
| 新增：旧库进程退出 | ⚠️ `atexit` 触发 `double free (fasttop)`，短命探针须用 `_exit(0)` 绕过 |

---

## 6. 证据与文件索引

```
out/reference/whaletx/                        WhaleTX 源码归档（15 文件）
docs/evidence/dmabuf_probe_out.txt            video0/5/6 DMABUF 支持实测
docs/evidence/factory_rkmedia_symbols.txt     原厂 libeasymedia 575 个导出
docs/evidence/factory_rkaiq_sysctl.txt        原厂 librkaiq 完整 4924 个导出
docs/evidence/factory_elf_deps.txt            GLIBC 版本需求（见 out/factory_rootfs/elf_deps.txt）
tools/dmabuf_probe.c / firmware/dmabuf_probe  本次新增的 DMABUF 探针
tools/rtpprobe.c / firmware/rtpprobe           UDP/RTP 链路探针（见 OUTGOING_RTP.md）
docs/FIRST_LIGHT.md                           抓帧链路 / IQ 文件位置 / 原厂库 ABI
```
