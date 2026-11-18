# S4602 / OpenIPC：软件 RAW→NV12→MPP 出图 与 色彩"破案"记录

> 日期 2026-11-16（延续 11-13 首次出图，见 [`FIRST_LIGHT.md`](FIRST_LIGHT.md) /
> [`MPP_H264.md`](MPP_H264.md) / [`DIRECT_H26X.md`](DIRECT_H26X.md)）。
> 主线：在 **OpenIPC 4.19.111 内核**上，用 `tools/h264out.c` 走
> `/dev/video0` rkcif BG10(RAW10) → 软件 demosaic/box-bin → NV12 → MPP 硬件 VENC →
> RFC3984/7798 FU-A RTP/UDP 直发，**稳定出图**（640×360，实测 13.3fps，send_err=0）。
> 画质不算好（见下），但"能不能出画面"已经解决——这是本记录的核心。

---

## 1. 一句话结论

OpenIPC 这套栈**硬件 ISP 走不通**（`librkaiq` 独占 rkcif 后只出 1 帧、从不 QBUF，
3A 空转，已在 11-13 结论里闭环死结）。但**软件 RAW 路径完全可行**：
rkcif 能持续吐 2880×1616 RAW10，我们在 CPU 上 demosaic+box-bin 降到 640×360，
交给 MPP 硬件编码器，再 RTP 推出去。这是当前唯一能稳定出图的路径。

- 帧源：`/dev/video0`（rkcif）BG10，G_FMT `2880x1616 bpl=3840`
- 编码：MPP `RKVENC`（`/proc/mpp_service` 支持 `RKVENC 0x50600311`），H.264 640×360 硬件 ~572fps 上限
- 推流：RFC3984 PT96 H.264 / RFC7798 PT97 H.265，UDP → `192.168.6.88:5600`，QGC 直接 `UDP h.264`
- 实测：`640×360 box=4` 双核 A7 **13.3fps**，`send_err=0`

---

## 2. 本轮"画面不能看"的两层根因（都定位并修复）

用户反馈"推流稳定但画面和实际环境完全对不上、基本不能看"。分两次破案：

### 2.1 青绿/假彩主因 —— box=4 FAST 的 Bayer 相位 bug（`|` vs `+`）

`fast_stage1()` 里判断某像素属于 B/G/R 哪个通道，用的是：

```c
int odd = (k & 1) | (d & 1);   /* 错：按位或 */
if (odd == 0)      sb += ...;   /* B */
else if (odd == 2) sr += ...;   /* R */
```

`(奇行, 奇列)` 时 `1|1 = 1`（不是 2），导致 **R 通道从未被累加，恒为 0**。
gamma 后 R=0 → 整幅画面青绿，U/V 实测 `154/29`（正常应 ≈128）。
灰世界 WB 因此把 `wbR` 顶到上限 2.0 也救不回来（`[dbgY]` 见 `wbR=2.000`）。

修复：改成加法 `int pos = (k & 1) + (d & 1);`（0=B 1=G 1=G 2=R）。
修完 U/V 回到 `125/125`，青绿消失，场景立刻可辨。

### 2.2 残余彩虹摩尔纹 —— 是传感器 CFA 假色本身，不是算法可完全消的

修完 2.1 后仍有彩色摩尔纹（物体边缘彩虹带）。做了几组对照验证：

- **抓帧解析坑**：`h264out` dump 出的 NV12 行宽是 MPP 对齐的 `g_hs=768 / g_vs=512`
  （活动区 640×360），Y stride=768，UV 按 `(VS/2, HS/2, 2)` 交错。**按 640 stride
  读会把正常画面读成"纯条纹噪声"**——曾据此误判。现用 `tools/nv12_to_png.py`
  正确解析。设备无 SFTP，文件回拉用 `tools/nc_fetch.py`（设备端 `cat f | nc`）。
- **排除白平衡**：`--nowb` 冻结增益后 V 仍 ≈32，说明残留在色度通道不是 WB。
- **加大 box 无改善**：把 FAST 扩展到 `box=8`（16R/32G/16B，总处理量不变、帧率不掉），
  假色仍无进一步减少 → 不是色块采样密度问题。
- **穷举 4 种 Bayer 排列**（BGGR/RGGB/GRBG/GBRG）：无论哪种，`|U|>20` 都占 40–50%；
  但**关掉色度（CHROMA=0）后灰度画面干净、场景清晰可辨**。

结论：RAW/CFA 的彩色分量噪声就是这么大，box-bin 无真插值，无法像硬件 ISP 那样
彻底压掉彩虹纹。工程上只能**降色度强度 + 加宽色度平滑**压制。

---

## 3. 最终可用的画质折中（已固化）

`tools/h264out.conf` 默认改为：

| 参数 | 值 | 说明 |
|---|---|---|
| `BOX` | 4 | FAST 整数路径，13.2fps |
| `CHROMA` | **25** | 色度强度降到 25%（≈"带一点彩色的灰度"） |
| `CBOX` | **7** | 色度宽窗半宽 7，强平滑压假色 |
| `NOWB` | 0 | 自动灰世界 WB |

效果：画面从"彩虹带不能用"变成"近似灰度、能看清桌面/物体轮廓"。
若要最干净的监控画面，可设 `CHROMA=0`（纯灰度）。真正要"硬件 ISP 画质"
只能走 vendor BSP 路线（[`VENDOR_BSP_ROUTE.md`](VENDOR_BSP_ROUTE.md)），
当前 OpenIPC 栈上做不出。

---

## 4. 本轮交付物清单

| 文件 | 说明 |
|---|---|
| `tools/h264out.c` | 软件 RAW→NV12→MPP→RTP 发送器；**已修 NEON decode_row 8 位移位溢出**（11-16 早，黑屏主因）+ **Bayer 相位 `\|`→`+`**（本段 2.1）+ box=8 扩展 |
| `tools/h264out.conf` | 固化 `BOX=4 CHROMA=25 CBOX=7 NOWB=0` |
| `firmware/h264out` | 修复版交叉编译产物，md5 `c4fef2854e1a782b985b739b6ab1e606`（已上设备 `/tmp` + SD 卡） |
| `tools/capture_live_frame.py` | 临时重启取流、抓编码前 NV12/RAW 一帧、自动恢复推流 |
| `tools/nc_fetch.py` | 设备无 SFTP，经 `cat f \| nc HOST PORT` 拉回文件 |
| `tools/nv12_to_png.py` | 按 MPP 对齐 stride(768/512) 正确解析 NV12→PNG（避免读错成条纹噪声） |

---

## 5. 与硬件 ISP / vendor 路线的边界（勿重复踩）

- 硬件 ISP（`/dev/video5 rkisp_mainpath`）：`librkaiq` 常驻后独占 rkcif，
  `not active buffer`、只出 1 帧、3A 空转 —— 死结，**不要再调 link/IQ/feeder/互斥**。
- 唯一破法 = 补 sc5338 `camera_info`/`sensor_calib` 喂 `CamHwIsp20`，或整套换回
  Rockchip vendor BSP（内核+`rockit` 库全配套，`docs/P0_RUNTIME_ABI.md` 已证运行时 15 库齐全）。
- 本记录里的一切画质妥协都发生在**软件 box-bin** 路径，不影响"能出图"这个事实。
