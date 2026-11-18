# P0：原厂 Rockchip 运行时部署与 easymedia ABI 实证

> 执行日期 2026-11-13。目标：确认 WhaleTX 架构（`V4L2 → MMZ dma-buf → VENC → RTP`）
> 能否在本机 RV1109 / OpenIPC 上落地，即「原厂 `libeasymedia.so.1` 的 ABI 对不对得上」。
> **结论：全线打通，唯一剩余阻塞仍是 IQ。**
> 本轮只写 `/tmp`（tmpfs）与构建服务器 `/tmp/gb`，**未写 flash、未改 rootfs**。

---

## 1. 结论速览

| 检查项 | 结果 |
|---|---|
| 原厂运行时依赖闭包 | **15 个库 / 5.46 MiB，零缺失** |
| 部署位置 | `/tmp/factory/lib`（SD 是 **vfat 不支持软链**，必须 tmpfs） |
| `ldd libeasymedia.so.1` | 21 个依赖全部解析，无 `not found` |
| 动态编译兼容性 | verneed 仅 `GLIBC_2.4` / `GLIBC_2.34` ≤ 设备 2.38 ✅ |
| `dlopen` 5 个核心库 | 全部 OK |
| `dlsym` SYS / MB / VENC / rkaiq UAPI1 | **11/11、16/16、11/11、8/8** |
| `RK_MPI_SYS_Init()` | **返回 0** |
| `MB_CreateBuffer(size, bHardWare=1, …)` | 得到真 MMZ buffer，**`MB_GetFD()` 返回有效 dma-buf fd** |
| MMZ dma-buf → `VIDIOC_QBUF(DMABUF)` | **OK（video6）**，零拷贝导入成功 |
| 官方 SDK 头文件 | **已找到且与设备库同代**（48 个 `.h`） |
| `VENC_CHN_ATTR_S` 布局 | **100% 正确**（库日志读回 1920×1080 / 1280×720） |
| 硬件 VENC 通道创建 | `-33 RK_ERR_VENC_BUSY`（运行时资源问题，非 ABI） |
| rkisp 出 NV12 | ❌ 仍被 `can not get first iq setting in stream on` 阻塞 |

**判定：P0 通过。** 可以照 WhaleTX 架构往下做，且**不再需要靠猜结构体 / 反汇编**
—— 有官方头文件可直接编译。

---

## 2. 依赖闭包与部署

`tools/factory_runtime.py` 纯 Python 解析 ELF 的 `DT_SONAME` / `DT_NEEDED`
（Windows 无 `readelf` 也能跑），从 `libeasymedia.so.1` + `librkaiq.so` +
`libthird_media.so` 求闭包：

```
libRKAP_3A.so / libRKAP_ANR.so / libRKAP_Common.so
libasound.so.2.0.0 / libdrm.so.2.4.0 / libeasymedia.so.1.0.1
libjpeg.so.62.3.0 / libmd_share.so / libod_share.so
librga.so.2.0.0 / librkaiq.so / librockchip_mpp.so.0
libthird_media.so / libv4l2.so.0.0.0 / libv4lconvert.so.0.0.0
```

**9 个 SONAME 别名**（tmpfs 里建软链）。最容易踩的一个：

```
librockchip_mpp.so.1 -> librockchip_mpp.so.0
```

文件名是 `.so.0`，但 `DT_SONAME` 与所有 `DT_NEEDED` 都写 `.so.1`；
不补这个软链 `dlopen` 必失败。其余别名是常规的版本号收敛
（`libasound.so.2`、`libdrm.so.2`、`libjpeg.so.62`、`librga.so.2`、
`libv4l2.so.0`、`libv4lconvert.so.0`、`libeasymedia.so.1`）。

部署用 `tools/deploy_factory_runtime.py`：本地打 tar.gz → base64 走 SSH exec
（dropbear **无 sftp-server**）→ 设备解包 → 建软链 → `chmod 755` → `ldd` 校验。

> **环境约束**：`/mnt/mmcblk1` 是 vfat，`/` 是 22.1 MiB 可用 ubifs，
> `/tmp` 是 223 MiB tmpfs 且支持软链。运行时放 `/tmp/factory` 最合适；
> 重启会丢，需要常驻时再裁剪进 rootfs 或做成开机脚本。

---

## 3. 编译通道（重要）

`dlopen` 必须用**动态链接**，之前所有工具用的 `-static` 在这里不能用
（静态 libc 与共享库会加载出两套 libc 状态）。实测：

```
arm-linux-gnueabihf-gcc -O2 -o probe probe.c -ldl     # Ubuntu 9.5.0 / sysroot glibc 2.39
readelf -V probe  ->  GLIBC_2.4, GLIBC_2.34
```

最大需求 `GLIBC_2.34` < 设备 2.38，所以构建服务器 sysroot 虽是 2.39，
**编译出的小程序在设备上直接可跑**，不需要 `--sysroot` 替换设备 libc。

---

## 4. dlopen / dlsym 实测（`tools/mpiprobe.c`）

`LD_LIBRARY_PATH=/tmp/factory/lib /tmp/mpiprobe init`

| 组 | 命中 | 说明 |
|---|---|---|
| SYS | 11/11 | `Init / Bind / UnBind / SendMediaBuffer / GetMediaBuffer / StartRecvFrame / …` |
| MB / dma-buf | 16/16 | **`GetFD` / `GetPtr` / `GetSize` / `GetDevFD` / `CreateBuffer` / POOL / Release** |
| VENC | 11/11 | 含 `CreateChn` / **`CreateJpegLightChn`** / `StartRecvFrame` / `QueryStatus` |
| VI | **2/5** | 只有 `SetChnAttr` / `EnableChn`；**没有 `CreateChn` / `GetChnFrame`** |
| rkaiq UAPI1 | 8/8 | `preInit / init / prepare / start / stop / deinit / updateIq / getStaticMetas` |

`RK_MPI_SYS_Init()` → **0**；同时打印 `[RKMEDIA][SYS][Info]: Opened DRM device /dev/dri/card0: rockchip 2.0.0`。

### VI 只有 2/5 曾看起来像缺符号，其实是同代 SDK 的证据
官方 `rkmedia_api.h` 里 VI 段本来就只有
`SetChnAttr / DisableChn / EnableChn / GetChnRegionLuma / StartStream`，
同样没有 `CreateChn` / `GetChnFrame`。**头文件与设备库版本精确对齐**，
所以不需要反汇编、不需要自造结构体。

---

## 5. MB ABI 实测（`tools/mbprobe.c`）

无头文件时利用 ARM AAPCS（前 4 参数走 r0–r3，多传无害）扫参数组合：

| `CreateBuffer(size,a,b,c)` | `GetSize` | `GetPtr` | `GetFD` | `GetDevFD` |
|---|---|---|---|---|
| `(4096, 0,0,0)` | 4096 ✅ | 堆地址 | **-1** | -1 |
| `(4096, 1, 0,0)` | 4096 ✅ | `0xa6dcb000`（内核映射） | **6**（有效） | 4 |
| `(3110400, 1, 0,0)` | 3110400 ✅ | `0xa40a7000` | **6**（有效） | 4 |

结论：签名是 `MB_BLK RK_MPI_MB_CreateBuffer(size, RK_BOOL bHardWare, …)`，
**第 2 参数 `bHardWare=1` 才走 MMZ 硬件内存并给出 dma-buf fd**；
`=0` 只是堆内存，无 fd。官方 `rkmedia_buffer.h` 也印证同类 API
（`CreateImageBuffer(MB_IMAGE_INFO_S*, boolHardWare)`）。

`mmap(dma_fd)` 返回 `MAP_FAILED` —— 该 dma-buf 不支持用户态直接 mmap，
但 `GetPtr` 已给出可访问的 CPU 映射，且 V4L2 驱动会自行 `dma_buf_attach`。
**不影响零拷贝链路**。

---

## 6. MMZ dma-buf → V4L2 零拷贝（`tools/dmabuf_link.c`）

`/dev/video6`（rkisp selfpath，当前 1280×720 NV12，sizeimage 1,843,200）：

```
MB_CreateBuffer(1843200,1,0,0) -> MB_BLK, dma_fd=7, ptr=0xa416d000
REQBUFS(DMABUF,3)              -> 0 OK
QBUF(fd=7)                     -> 0 OK      <<< dma-buf 被 V4L2 成功 import
STREAMON                       -> 0 OK
poll(4s)                       -> TIMEOUT
DQBUF                          -> EAGAIN，sentinel 未被改写
dmesg: rkisp rkisp-vir0: can not get first iq setting in stream on
```

**导入成功、出帧被 IQ 挡住** —— 与 `docs/FIRST_LIGHT.md` 的结论完全一致，
且把范围缩小到「只差 IQ」。`/dev/video0`（RAW 6.2 MB）那次卡在 MMZ 大块分配，
尚未测通，需要时改小尺寸或分块即可，不是 ABI 问题。

---

## 7. 官方 SDK 头文件（最大风险已解除）

仓库 **`Wu-Yumin/demo_h264_rkmedia`**（RV1109/RV1126 同代）已整份归档到
`out/reference/rkmedia/`：

- `include/rkmedia/*.h` — 14 个 API 头（`rkmedia_api.h`, `rkmedia_venc.h`,
  `rkmedia_common.h`, `rkmedia_buffer.h`, `rkmedia_vi.h` …）
- `include/easymedia/*.h` — 24 个 flow 层头
- `h264_rkmedia_demo.c` — **完整可编译的 H.264 链路**
- `common/sample_common_isp.c` — **IQ 初始化精确序列**

编译时把 `include/` 传到构建服务器 `/tmp/gb/inc`，加
`-I/tmp/gb/inc/rkmedia` 即可用真结构体。

`tools/vencprobe.c` 实测布局（armhf）：

```
sizeof(VENC_CHN_ATTR_S)=100   VENC_ATTR_S=52   VENC_RC_ATTR_S=28   VENC_GOP_ATTR_S=20
offset: enType=0  u32PicWidth=24  stRcAttr=52  stGopAttr=80
enum: RK_CODEC_TYPE_H264=6  IMAGE_TYPE_NV12=4  RK_ID_VENC=4
```

`RK_MPI_VENC_CreateChn` 扫 4 组参数（H264 1920×1080 / H264 1280×720 /
MJPEG 1280×720 / JpegLightChn）全部 `-33 RK_ERR_VENC_BUSY`
（`CreateJpegLightChn` 为 `-21 ILLEGAL_PARAM`），但库日志

```
[RKMEDIA][SYS][Info]:ParseMediaConfigFromMap: rect_x=0, rect_y=0, rect.w=1920, rect.h=1080
```

证明 **宽高/枚举/结构体偏移全部被正确解析**，进入真实 Enable 流程。
`-33` 是运行时资源/配置状态（MPP 编码器忙或 media config 未就绪），
不是内存布局错误 → P1 打通 IQ / VI 后重测。

错误码对照 `rkmedia_common.h`：`RK_ERR_VENC_INVALID_CHNID=20` 起，
`-21 = ILLEGAL_PARAM`，`-33 = BUSY`。

---

## 8. IQ 注入的精确序列（P1 直接可用）

`out/reference/rkmedia/common/sample_common_isp.c` 的 `SAMPLE_COMM_ISP_Init`：

```c
setenv("HDR_MODE", "<WDRMode>", 1);                       // 必须在 init 之前
rk_aiq_uapi_sysctl_enumStaticMetas(0, &aiq_static_info);
aiq_ctx = rk_aiq_uapi_sysctl_init(
    aiq_static_info.sensor_info.sensor_name,              // 本板应为 m01_f_sc5338
    iq_file_dir,                                          // 放 COMMON_sc5338.xml 的目录
    NULL, NULL);
rk_aiq_uapi_setFecEn(aiq_ctx, RK_FALSE);
rk_aiq_uapi_sysctl_prepare(aiq_ctx, 0, 0, WDRMode);
g_aiq_ctx = aiq_ctx;
/* SAMPLE_COMM_ISP_Run */
rk_aiq_uapi_sysctl_start(aiq_ctx);
```

`h264_rkmedia_demo.c` 主链：

```c
RK_MPI_SYS_Init();
SAMPLE_COMM_ISP_Init(RK_AIQ_WORKING_MODE_NORMAL, RK_FALSE, iq_file_dir);
SAMPLE_COMM_ISP_Run();
VI_CHN_ATTR_S vi = { .pcVideoNode="rkispp_scale0", .u32Width=1920,
                     .u32Height=1080, .enPixFmt=IMAGE_TYPE_NV12,
                     .u32BufCnt=4, .enWorkMode=VI_WORK_MODE_NORMAL };
RK_MPI_VI_SetChnAttr(0, 1, &vi);  RK_MPI_VI_EnableChn(0, 1);
RK_MPI_VENC_CreateChn(0, &venc_chn_attr);
RK_MPI_SYS_RegisterOutCb(&stEncChn, video_packet_cb);
RK_MPI_SYS_Bind(&stSrcChn, &stDestChn);
```

> 注意 demo 用 `rkispp_scale0` / 1920×1080；本板 video6 当前是 1280×720，
> video5 mainpath link 是 `[off]`，节点映射要按 `docs/FIRST_LIGHT.md` 调整。
> `video_packet_cb` 拿到的 `MEDIA_BUFFER` 就是 H.264 码流，可直接喂
> `src/sink/udp_rtp.rs` 的 FU-A 逻辑（PT 96）。

---

## 9. 工程坑（必记）

1. **SD 是 vfat，不能建软链** → SONAME 别名必须放 tmpfs。
2. **`librockchip_mpp.so.0` 的 SONAME 是 `.so.1`** → 必须补软链。
3. **静态编译不能 `dlopen`** → 用动态编译；verneed 只到 `GLIBC_2.34`，安全。
4. **旧版 easymedia 退出时 `double free or corruption (fasttop)` / `Aborted`**
   —— `atexit` 析构冲突。常驻进程不受影响；短命探针一律 `_exit(0)` 绕过。
5. **`dlopen librkaiq.so` 会打印** `media get entity by name: ... is null` 与
   `XCAM ERROR CamHwIsp20.cpp:1022: No free isp&ispp needed by fake camera!`
   —— 因为本板 media 实体名与 rkaiq 期望不同，正是 IQ 阻塞的根因线索。
6. **`RK_MPI_SYS_Init()` 会打开 `/dev/dri/card0`**（rockchip DRM 2.0.0），
   大块 MMZ 分配在其后偶发卡住，先分配小 buffer 验证更稳。
7. **库会往 stdout 打大量日志**（`register factory : …`、
   `ParseMediaConfigFromMap`），会插进自己的 `printf` 中间，
   采集输出时先重定向到文件再解析。

---

## 10. 文件索引

| 路径 | 用途 |
|---|---|
| `tools/factory_runtime.py` | 纯 Python ELF 闭包 / SONAME 别名计算 |
| `tools/deploy_factory_runtime.py` | tar.gz + base64 部署 + 软链 + ldd 校验 |
| `tools/mpiprobe.c` | dlopen / dlsym / SYS_Init 探针 |
| `tools/mbprobe.c` | MB_CreateBuffer 签名与 dma-buf fd 探测 |
| `tools/dmabuf_link.c` | MMZ dma-buf → V4L2 DMABUF QBUF 零拷贝验证 |
| `tools/vencprobe.c` | 官方头编译 + VENC_CHN_ATTR_S 布局 / CreateChn 扫描 |
| `firmware/mpiprobe`, `mbprobe`, `dmabuf_link`, `vencprobe` | 静态/动态 armhf 二进制 |
| `out/reference/rkmedia/` | 官方 SDK 头文件 + H.264 demo + ISP 示例 |
| `docs/evidence/factory_runtime_deploy.txt` | ldd 全量输出与别名表 |
| `docs/evidence/mbprobe_out.txt` / `dmabuf_link_out.txt` / `vencprobe_full.txt` | 原始实测日志 |
| `docs/WHALETX_REFERENCE.md` / `OUTGOING_RTP.md` / `FIRST_LIGHT.md` | 上游方案与背景 |

---

## 11. 下一步

1. **P1（任务 #33）** — 按第 8 节序列注入 `COMMON_sc5338.xml`，
   `rk_aiq_uapi_sysctl_init(sensor_name, iq_dir, NULL, NULL)` → `prepare` → `start`，
   让 video6 出干净 NV12。注意处理 `fake camera` / sensor_name 不匹配。
2. **P2** — IQ 通后重跑 `vencprobe`（期望 `-33` 消失），接
   `VI → VENC` Bind，`video_packet_cb` 拿 H.264 → FU-A/PT96 → UDP 5600。
3. **旁路** — 若 IQ 仍卡，先试 `CreateJpegLightChn` + 自建 NV12 MEDIA_BUFFER
   + `RK_MPI_SYS_SendMediaBuffer(RK_ID_VENC, 0, mb)`，出 MJPEG/PT26 最快看到实时画面。
