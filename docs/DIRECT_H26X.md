# 设备端直发 H.264 / H.265 RTP（2026-11-13 实板闭环）

> 结论：**不再需要 Windows 中继**。S4602 现在自己 `/dev/video0 RAW10 → 软件 NV12 →
> MPP 硬件 VENC → RTP/UDP → 192.168.6.88:5600`，H.264 与 H.265 都已解出真实画面。
> QGC 直接选 `UDP h.264` / `UDP h.265` 即可，端到端延迟不再有旧链路的 ~6 s 积压。

---

## 1. 为什么放弃了「rkisp/rkispp 直出 NV12」零拷贝方案

之前 `--src nv12`（`/dev/video19` = `rkispp_scale0`）能以 25 fps 出 NV12 帧、
`VIDIOC_EXPBUF` → `mpp_buffer_import_with_tag` 零拷贝导入成功、MPP 编码也成功，
**但 Y 平面全 0**，解码出来是黑屏。本轮把根因查到底了：

内核启动日志：

```
rkisp rkisp-vir0: Entity type for entity rkisp-bridge-ispp was not initialized!
rkispp_hw ffb60000.rkispp: max input:0x0@0fps
rkispp_hw ffb60000.rkispp: can't request region for resource [mem 0xffb60000-0xffb7ffff]
rkispp_hw ffb60000.rkispp: failed to get cru reset
```

`/dev/media1` 的实际拓扑（`/tmp/mediaent /dev/media1`）：

```
src=1:p2 (rkisp-isp-subdev) -> sink=13:p0 (rkisp-bridge-ispp)   ENABLED  <-- 独占 ISP 输出
src=1:p2                    -> sink=17:p0 (rkisp_mainpath)      off
src=1:p2                    -> sink=23:p0 (rkisp_selfpath)      ENABLED
src=89:p0 (rkcif_mipi_lvds) -> sink=1:p0                       ENABLED
```

- ISP 输出被 **ENABLED 的 bridge-ispp 链路**拿走，而 bridge 后面接的 rkispp 硬件
  因寄存器区间申请失败根本没初始化 → `video19` 只是按节拍吐全 0 buffer。
- 直接 `linksetup /dev/media1 1 2 17 0 1` 打开 mainpath 会失败：
  streaming 时 `EBUSY(16)`；停流后 `EINVAL(22)`，dmesg 明确给出理由：

  ```
  rkisp-isp-subdev: link error rkisp-isp-subdev -> rkisp_mainpath
      csi dvp lvds dmaread can't work together
      rawrd can't work with dvp lvds dmaread
      bridge can't work with mainpath/selfpath
  ```

- 先关 bridge 再开 mainpath **可以成功**（实测两个 `SETUP_LINK` 都返回 0）：

  ```sh
  /tmp/linksetup /dev/media1 1 2 13 0 0    # bridge-ispp OFF
  /tmp/linksetup /dev/media1 1 2 17 0 1    # mainpath ON
  ```

  干净重启 → 改链路 → 启动 IQ daemon → `mpcap /dev/video5` 也确实能
  `DQBUF bytesused=6981120`，但取回分析 **Y 仍全 0**（`min=max=mean=0`）。

  ⇒ 这是 OpenIPC 内核 / 设备树层的 ISP→ISPP 通路缺陷，用户态无法修复；
  修它需要改 DT / 驱动并重刷内核，本轮不做（不写 flash、保留 `_a` 原厂槽）。

另有一条重要实测：**rkaiq IQ daemon 会占住 rkcif 管线**，它运行时
`VIDIOC_REQBUFS` 在 `/dev/video0` 直接失败；关掉 IQ 后 video0 立刻能独立出帧。
所以新方案的 raw 模式必须保证 IQ 不运行。

---

## 2. 新链路

```
/dev/video0  rkcif  2880x1616 BG10 MIPI RAW10 packed  @ 26.67 fps
   │  V4L2 CAPTURE_MPLANE / MMAP (3 buffers)
   ▼
软件转换（单遍、整数运算）
   ├─ 5B→4px RAW10 解包
   ├─ box 2x2 / 3x3 / 4x4 Bayer 分箱（BGGR → R,G,B）
   ├─ 黑电平 140、灰世界白平衡（增益取上一帧，省一遍全图扫描）、gamma 0.65
   ├─ Y=(2R+3G+B)/6，BT.601 Cb/Cr
   └─ 半分辨率 UV 可分离 7-tap 盒式低通 + 35% 衰减（压 HDR_X2 假色）
   ▼
NV12 (WxH, stride 16 对齐) 写入 **MPP_BUFFER_TYPE_DRM** 帧缓冲
   ▼
MPP raw MPI（/dev/mpp_service RKVENC / VEPU2）
   encode_put_frame / encode_get_packet
   ▼
Annex-B NAL → RFC 3984 FU-A (H.264, PT 96) 或 RFC 7798 FU (H.265, PT 97)
   ▼
UDP 直发 192.168.6.88:5600（可再镜像一路给验证主机）
```

保留 `--src nv12` 旧路径代码仅作对照，实际不可用。

---

## 3. 关键坑（都实板踩过）

| 现象 | 根因 | 修法 |
|---|---|---|
| `MPP_IOC_CFG_V1 failed errno 12 Cannot allocate memory`，每帧只有 ~100 B 空洞包 | raw 路径沿用了 dma-buf 路径的 `MPP_SET_INPUT_TIMEOUT=100 / OUTPUT_TIMEOUT=1`，且另开独立 header buffer group | raw 路径**不要改 MPP 超时**，并且必须复刻 `mppprobe` 已知成功配置：单个 `MPP_BUFFER_TYPE_DRM` group，先帧缓冲、再 packet + motion-info，`GET_HDR_SYNC` 用 packet buffer |
| `nfrmbuf>=2` 又触发 ENOMEM | 这版 VEPU541 HAL 只接受 1 个帧缓冲 | `--nfrmbuf 1`；MPP 在本用法下同步性足够，单缓冲复用无撕裂 |
| 解码出来**纯绿**、Y≈0 / UV 全 0 | `MPP_BUFFER_TYPE_NORMAL(0x1)` 是普通堆内存，CPU 写的 NV12 硬件 DMA 读不到（该库无 `mpp_buffer_sync_*_f` 导出，无法刷 cache） | 用 `MPP_BUFFER_TYPE_DRM(0x3)`；此前 `mppprobe` 的 memset 测试数据其实也没被硬件读到，只是没解帧所以没发现 |
| 画面**整体偏绿/纯绿**（U/V≈0） | chroma 公式 `(-43r-85g+128b)>>8` 已是**有符号零居中**（Cb-128/Cr-128），旧代码又误减了一次 128 → 全部像素压到饱和绿 | 写入用 `128+ubuf`，**绝不再减 128**；修复后 U/V 均值居中（≈132/122） |
| 画面假色 / 彩色噪点极重（rainbow 斑点） | Bayer 分箱后每个 chroma 像素只取到约 1 个源 R、1 个源 B，传感器逐像素噪声几乎全保留 | 新增 `--cbox`（chroma 宽窗半宽，默认 3）：每个 chroma 像素在已分箱的 bin 网格上取 `(2·cbox+1)²` 宽窗求平均，R/B 有效样本数 ×~200，同时压掉边缘假色。实测局部 chroma 噪声 **3.84→0.95（4.0×↓）**、HF 残差 **8.24→4.96（1.66×↓）**；`--chroma 0` 仍可得干净灰度 |
| IQ daemon 运行时 `REQBUFS` 失败 | rkaiq 独占 rkcif | `start_h264out.sh` 在 `SRC=raw` 时主动杀掉 IQ |

---

## 4. 实测数据（2026-11-13）

`--box 2 --chroma 35 --vbufs 3 --nfrmbuf 1`，dry-run 与 UDP 直发结果一致。

| 输出分辨率 | 实测帧率 | 备注 |
|---|---|---|
| 320×180 | 13.7 ~ 23.8 fps | 最低延迟，推荐 QGC 使用 |
| 480×270 | ~12.8 fps | |
| 640×360 | 6.6 ~ 8.7 fps | 加色度降噪后略降 |

端到端 RTP 校验（`tools/verify_direct_stream.py`，主机收包 + 重组 NAL + ffmpeg 解帧）：

| codec | 帧数 | RTP 包 | 丢包 | NAL 分布 | 解码 |
|---|---|---|---|---|---|
| H.264 / 320×180 | 60 | 240 | **0** | `{6:6, 7:3, 8:3, 28:60}` | ✅ 真实画面 |
| H.264 / 640×360 | 40 | 274 | **0** | `{6:4, 7:2, 8:2, 28:40}` | ✅ 真实画面 |
| H.265 / 640×360 | 30 | 235 | **0** | `{1:28, 19:2, 32:2, 33:2, 34:2, 39:4}`（VPS/SPS/PPS/SEI/IDR 齐全） | ✅ 真实画面 |

证据文件：`out/captures/direct_h264_320.jpg`、`direct_h264_clean.jpg`、
`direct_h265_640.jpg`，以及对应的 `.h264` / `.h265` Annex-B 码流和
`direct_*_stats.json`。亮度单帧 `out/captures/raw_nv12_luma.png` 是干净可辨认场景。

### 4.1 画质修复（2026-11-13 第二轮：绿屏 + 彩色噪点）

1. **整体偏绿/纯绿**：`raw_to_nv12` 的 chroma 公式已是零居中（Cb-128/Cr-128），
   旧代码又误减 128 → 全饱和绿。删除后 U/V 均值恢复居中（`U-128` 由 ≈120 变 ≈0.3）。
2. **rainbow 彩色噪点**：每 chroma 像素原来只取 ~1 个源 R/B，传感器噪声全留。
   新增 `--cbox N`（chrona 宽窗半宽，默认 3）：在已分箱的 bin 网格上取
   `(2N+1)²` 宽窗平均 R/G/B，再做 7-tap 低通。**稳定帧实测**（`--dumpdelay 4`
   跳过首帧传感器/AE 收敛）：U/V 局部噪声 **1.47 → 0.72（2.05×↓）**，
   HF 残差 **3.78 → 3.08（1.23×↓）**，色度保留 36.9%、均值居中。

> 说明：首帧（IDR）亮度偏高（Y≈148）是 rkcif 上电瞬时，第 2 帧起稳定在
> Y≈89；自平衡 WB 稳定后 G/R≈1.18（比固定 `--nowb` 的 1.37 更中性，故默认开启）。

### 4.2 FAST 软件 demosaic 内核（1080p 帧率瓶颈，2026-11-16）

1080p 帧率差（初测 0.7fps）根因 = **软件 RAW10→NV12 demosaic**，非 MPP 编码（MPP 数百 fps 级）。重写 `raw_to_nv12_fast()`：

- **NEON + 双线程**：`-mcpu=cortex-a7 -mfpu=neon-vfpv4 -mfloat-abi=hard`，`pthread` 按行切 2 worker 做 stage1（RAW10 解码 + box bin），`--threads N`（默认 2）。
- **整数化 WB**：`cinv/100` 表 + 定点乘，去浮点链。
- **running-sum 可分离 box blur**：`--blur` 替代逐像素重算，O(1)/像素。
- **`inv_n[65536]` 乘法表**：`inv_n[n]=65536/n`，面积平均用整数移位。

**关键 bug（640×360 堆越界崩溃，已修）**：`fast_init` 的 `ctmp` 只分配 1 个 chroma 平面（`cw*ch`），但 `fast_blur_h` 按 U/V 两平面写（V 平面偏移 `CW*CH`）→ 640 下越界写 → `malloc` 崩溃（coredump 0x2a880≈640×360×1）。修：`malloc(2*cw*ch)`。另修：`wk_t.r0/r1/ring` 未分配就传 worker→NULL segfault（入口按 `src_w` malloc）；chroma 二次模糊乘数 `uint32_t` 使负值右移变正→改 `int32_t`。

**帧率实测（FAST + 2线程，含 MPP 编码 + RTP 发送）：**

| 分辨率 | 帧率 | 相对优化前 | 距 30fps |
|---|---|---|---|
| 640×360 | **8.8 fps** | +23%（7.1→8.8） | 3.4× |
| 720p | **6.6 fps** | 持平（6.43→6.6） | 4.5× |
| 1080p | **4.4 fps** | +18%（3.73→4.4） | **6.8×** |

**结论**：软件 demosaic 是架构瓶颈（RV1109 双核 A7 算力有限，NEON + 2线程 + 整数化后 1080p 仅 4.4fps）。**1080p30 在软件 RAW 通路不可达**（需 6.8× 提速）。要 1080p 高帧率只能走 **ISP 硬件通路**（rkcif→rkaiq→isp0 出 YUV，VPU 硬编），但需把 rkaiq daemon 跑起来 + 接 ISP 驱动；设备当前 rkaiq 占 rkcif，`--with-iq` 会死锁（§1 三坑③）。**1080p30 属未达成项，需定方向：ISP 硬件通路 / 接受低帧率 720p / 换硬件。**

调参：`--cbox`↑ 更干净但色更糊；`--chroma`<100 去饱和；`--nowb` 冻结白平衡；
`--dumpdelay N` 让 dump 落在稳定帧。对比图：`out/captures/compare_cbox.png`。

> 帧率上限来自双核 A7 上的软件 RAW10 转换，**不是 sensor 能力**
> （sensor 26.67 fps，MPP 硬件编码 640×360 单独测有数百 fps）。
> 若日后修好内核 ISP 通路，可切回 `--src nv12` 拿到零拷贝 25 fps。

---

## 5. 使用

设备端（`/tmp` 与 SD 卡 `/mnt/mmcblk1/s4602/` 均已部署，重启后由
`tools/reboot_restore.py` 自动从 SD 恢复）：

```sh
sh /tmp/start_h264out.sh start|stop|restart|status
```

改 `tools/h264out.conf` 切换 codec / 分辨率 / 码率：

```sh
CODEC=h264        # 或 h265
SRC=raw
WIDTH=640
HEIGHT=360
BOX=2
CHROMA=100        # 0 = 灰度
CBOX=3            # chroma 宽窗降噪半宽（越大越干净越糊）
DST_IP=192.168.6.88
DST_PORT=5600
```

QGC：Video Source = `UDP h.264`，`udp://0.0.0.0:5600`；
H.265 用 `UDP h.265` / `udp265://0.0.0.0:5600`，并打开 Low Latency Mode。

构建与验证：

```bash
python tools/armbuild.py h264out.c h264out --dynamic \
    --inc out/reference/mpp/inc-1.0.9 --libs="-ldl -lm"
python tools/verify_direct_stream.py --codec h264 --w 640 --h 360
python tools/verify_direct_stream.py --codec h265 --w 640 --h 360
```

SDP（给 ffplay / VLC 排障用）：`out/captures/s4602_h264_direct.sdp`、
`out/captures/s4602_h265_direct.sdp`。

---

## 6. 与旧链路的关系

- `tools/qgc_h264_relay.py` + `start_qgc_relay.bat`（Windows MJPEG→H.264 中继）：
  **不再需要**，仅作为历史方案保留。用户已取消的后台任务 `q2MPX2` 不要重启。
- `tools/mjpegout.c` / `start_outgoing.sh`（MJPEG/PT26 常驻）：仍是回滚链路，
  但其 ~3.5 fps + 主机转码 + ~6 s 延迟已被本方案取代。
- 全程未写 flash、未改 rootfs，`_a` 原厂槽保持可回滚。
