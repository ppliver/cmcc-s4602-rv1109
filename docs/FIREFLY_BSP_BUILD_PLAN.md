# Firefly / RK RV1109 BSP 重建固件 — 计划

> 2026-11-17。路线 P2：放弃 OpenIPC，用 **Firefly 公开 manifest 的 `rv1126_rv1109_linux_release`**
> （RK 官方 BSP，Buildroot + kernel 4.19 + U-Boot 2017.09）重建一份**适配 S4602 的固件**，
> 保留原厂 U-Boot/loader，只换 kernel + rootfs。长耗时构建走 **GitHub Actions**。
> 本文是"先取证后下结论"产物；结论带出处，未验证项明确标注。

## 0. 为什么走这条（相对 OpenIPC 的增量价值）

OpenIPC 死结 = 预编译 `librkaiq` + 缺 sc5338 `camera_info` → 硬件 ISP/3A 出不来图。
Firefly 官方 BSP **自带整条官方媒体栈**（`external/rkmedia` + `camera_engine_rkaiq` +
`isp2-ipc` + `external/mpp` + `app/firefly_rkmedia_demo`），其出图 demo
`rkmedia_vi_venc_rtsp_test -a /etc/iqfiles/` 就是"普通网络摄像头"链路
（VI→VENC→RTSP，`vlc rtsp://ip/live/main_stream`）。内核与原厂**同为 4.19.111**，
所以驱动/ABI 不跨代。**目标：把官方 3A/IQ 通路原样带进来，出图不再靠软件 demosaic 猜。**

## 1. 供应渠道（已实测）

| 项 | 事实 | 出处 |
|---|---|---|
| manifest 仓 | `gitlab.com/firefly-linux/manifests.git`，公开、**无需 NDA/账号**，`git ls-remote` 实测可达 | 本地实测 2026-11-17 |
| 入口 | `-m rv1126_rv1109_linux_release.xml`（顶层是 stub，真内容在 `rv1126_rv1109/` 子目录） | 浅克隆 HEAD `6377120`（2025-01-14） |
| 当前 pin | `rv1126_rv1109_linux_release_20241011_v2.2.5g.xml` → tag `rv1126_rv1109/linux_release_v2.2.5g` | manifest 内容 |
| 远端 | `fetch="."` = `https://gitlab.com/firefly-linux`，**全部仓公开可直拉**（`kernel`/`device/rockchip`/`u-boot`/`tools`/`rkbin` 等 10 个核心仓 `git ls-remote` 实测 OK）→ **GitHub Actions 能直拉，不需要 RK 技术窗口** | 本地实测 |
| RK 技术窗口 | `git@www.rockchip.com.cn`（SSH 公钥授权）—— 仅当要拿**更完整/私有素材**（如某 sensor IQ、闭源库源码）时备选；路径/可见性 [以窗口为准]，本地未验证 | 用户提供 |
| 出图全家桶 | release manifest 含 `app/firefly_rkmedia_demo`、`external/{rkmedia,camera_engine_rkaiq,isp2-ipc,mpp}`、`app/{mediaserver,netserver,ipc-daemon}` | manifest 内容 |

## 2. 与 S4602 的适配面（逐点判定）

| 面 | 现状 | 判定 |
|---|---|---|
| 内核 | Firefly `rv1126_defconfig`（370 行），4.19.111，与原厂同代 | ✅ 直接可用 |
| SC5338 sensor | Firefly i2c 目录只有 `gc2053/imx307/imx415/ov5640/sc2335`，**无 SC5338** | ⚠️ 需注入 `src/kernel-patches/sc5338.c` + `-tables.h`（复用 OpenIPC 的 `tools/apply_kernel_patch.py`，i2c Makefile/Kconfig 锚点与 OpenIPC 4.19 树一致；驱动能否在这棵 4.19 编过 **未验证**） |
| SPI-NAND id | Firefly `drivers/rkflash/sfc_nand.c` 表无 `e5 f2 e5`；`struct nand_info` 字段顺序（id0..id2/sec_per_page/page_per_blk/plane_per_die/blk_per_plane/feature/density/max_ecc/has_qe/meta[4]/ecc_status）与 `apply_kernel_patch.py` 注入项**逐字段一致**，marker `static struct nand_info spi_nand_tbl[] = {` 也一致 | ✅ 注入可直接生效 |
| YT8512B PHY | Firefly `drivers/net/phy/` 无 motorcomm，只有 micrel/realtek | ⚠️ 注入 `motorcomm.c`（复用，PHY 目录锚点 `MICREL_PHY` 存在）；未验证 |
| 板级 DTS | Firefly 有 `rv1109-38-v10-spi-nand.dts`（参考板）；S4602 金标 DTS = `docs/evidence/vendor-kernel.dtb`（反编译 `board/s4602/generated/*.dts`）。参考板 dts 与 S4602 硬件（DDR3 vs LPDDR4、PHY、audio）不同 | ⚠️ 先编参考板 dts 验证内核能过；DTS 换 vendor 为下一里程碑 |
| GPT/parameter | Firefly `parameter-fit-nand-256M.txt` 是它自己的 7 分区 GPT，**与 vendor 14 分区完全不同** → 必须沿用 vendor 布局（`board/s4602/board.yaml` 的 mtdparts） | ✅ 已有权威表 |
| cmdline | Firefly `CONFIG_CMDLINE="user_debug=31"`（不 FORCE）；原厂 U-Boot 7 次实证强制注入 `root=/dev/ubiblock0_0 rootfstype=squashfs ubi.mtd=9` | ⚠️ 刷 ubi/ubifs rootfs 时沿用已验证的 `CMDLINE_FORCE` + 完整 mtdparts 串 |
| 工具链 | `build.sh` 探测 `prebuilts/gcc/.../gcc-arm-10.3-2021.07`（第 397/516 行），但 manifest pin 的是 `gcc-arm-8.3` / `gcc-linaro-6.3.1` → **三者对不上，build.sh 的交叉编译探测会落空** | ⚠️ 已定解法：workflow 直接用 manifest 自带的 `gcc-linaro-6.3.1`（`arm-linux-gnueabihf-`）作 CROSS_COMPILE，绕过 build.sh 探测（原厂也是 6.3.1 Linaro 编的，最贴近） |

## 3. 分期

**Stage 1（本计划交付，GitHub Actions 可跑）**：拉 BSP 核心仓 → 注入 S4602
（SC5338 + SPI-NAND id + motorcomm + fragment）→ 编 **kernel `zImage` + DTS + 打 FIT**
（复用 `tools/make_fit.sh` + vendor `boot_a.img` 模板）→ 产出可刷 `boot_b` 的内核 FIT。
rootfs 暂沿用已有 `firmware/rootfs.ubi.rv1109`（OpenIPC）或 vendor rootfs，不阻塞内核引导验证。

**Stage 2（Stage 1 绿后）**：编 Buildroot rootfs（`RK_CFG_BUILDROOT=rockchip_rv1126_rv1109_spi_nand`）
+ 官方出图 demo `rkmedia_vi_venc_rtsp_test` + `COMMON_sc5338.xml`/iqfiles → 打完整 `updateimg`/`otapackage`。

**红线（沿用，不放宽）**：不碰原厂 U-Boot/loader；只写一个槽（_a 留原厂回滚）；
任何 `mtd write` 四参数 + 回读 CRC；写前 `tools/make_flash_plan.py --check`；
全片备份在 `backup/snapshot/full-256M.img`。

## 4. 交付物（本次落盘）

- `docs/FIREFLY_BSP_BUILD_PLAN.md`（本文）
- `.github/workflows/build-firefly.yml`（Stage 1：clone BSP + 注入 + 编 kernel FIT）
- `board/s4602/firefly/s4602_fragment.firefly.config`（基于 `rv1126_defconfig` 的增量 fragment）
- `board/s4602/firefly/BoardConfig-s4602-spi-nand.mk`（板级 .mk，DTS/fragment/parameter/buildroot 指 S4602）

## 5. 验证进展（2026-11-17，构建机 192.168.219.177 实测）

**已消项：**
- ✅ **SC5338 / motorcomm 驱动能在 Firefly 这棵 4.19.111 树 + gcc-9.5 下编译**：
  `make ARCH=arm drivers/media/i2c/sc5338.o` → 194,920 B OK；
  `drivers/net/phy/motorcomm.o` → 225,156 B OK。
- ✅ **SPI-NAND 免注入**：Firefly 树 `sfc_nand.c:112` **已内置** `/* DS35Q2GB-IB */`
  `{0xE5,0xF2,...density=19...ecc_status6}` 表项（= S4602 原厂 flash 同一颗旺宏 256MiB），
  `apply_kernel_patch.py` 幂等自动跳过。`sfc_nand.o` → 54,612 B OK。
- ✅ 符号解析（rv1126_defconfig + fragment + olddefconfig）：SC5338/MOTORCOMM/STMMAC/
  DWMAC/CMDLINE_FORCE/MTD_CMDLINE_PARTS/RK_FLASH/RK_SFC_NAND/MTD_UBI/UBIFS/SQUASHFS/PCF8563 全通过。

**两个环境坑（GA ubuntu-latest 一样会踩，已修进 `.github/workflows/build-firefly.yml`）：**
1. 4.19 内核 kconfig 走 `#!/usr/bin/env python`（Python2 写法），无 `python` 命令时 defconfig 挂 →
   `sudo ln -sf $(which python3) /usr/local/bin/python`（4.19 kconfig 兼容 py3）。
2. host `scripts/dtc/dtc` 在新 binutils（默认 `--no-common`）下 `yylloc` 多重定义 →
   make 全程带 `HOSTLDFLAGS="-Wl,--allow-multiple-definition"`。

**仍未验证（下一步）：**
1. 参考板 DTS `rv1109-38-v10-spi-nand` 编出的内核能否被**原厂 U-Boot** `boot_fit` 起（DTS 与 S4602 硬件匹配度）—— 需上机。
2. 出图（Stage 2：buildroot + rkmedia RTSP demo + vendor DTS + 14 分区 parameter）在 RV1109 的实际效果 —— 全路线收益点也是最大不确定项。

**★★2026-11-17 容量缺口已解除（Stage 1 收尾达成）：**
- 目标：把 rv1126 defconfig 编出的 zboot.img 从 5.90 MiB 压回 boot_b 4 MiB 以内（对照 OpenIPC 3.38 MiB 基线）。
- **达成：zboot.img 最终 2,954,252 B = 2.82 MiB，富余 1,240,052 B（1.18 MiB）可写 boot_b，魔数 `d00dfeed` 有效。**
  进度曲线（每轮 177 kconfig 校验 + GA 权威字节数）：
  `5.90(r0 原 defconfig) → 5.10(r1 裁 DRM/SND/USB) → 4.33(r2 裁无线/SCSI/MMC/input, 关 select 源 WLAN/WL_ROCKCHIP)
   → 4.24(r3 裁 MPP 解码侧+crypto 孤儿) → 2.63(r4 内核压缩 LZ4→XZ) → 2.82 MiB ✅(r5 恢复 USB host + 加 4G RNDIS 网卡驱动, 回涨 198 KB)`。
- **r5 是需求变更（2026-11-17 用户）**：板上有 USB 口、后续插 4G 无线网卡 RNDIS 模式，**USB host 子系统不能裁**
  （r1 误裁，已撤）。恢复 defconfig 的 USB host（XHCI/EHCI/OHCI/STORAGE），并补上 4G 卡数据驱动
  （defconfig 默认全关，光有 host 也识别不了）：`USB_USBNET` + `USB_NET_RNDIS_HOST`（auto-select CDCETHER）
  + `CDC_NCM`/`CDCETHER` + `USB_SERIAL_WWAN/OPTION/QUALCOMM`，全 `=y` 内建。仍关 USB 设备角色（GADGET/CONFIGFS）。
  代价 198 KB，仍富余 1.18 MiB，未破红线。
- **r4 是关键一刀**：前三轮"裁功能模块"收益递减（顶层 CRYPTO 被 44 处 select 关不掉）；
  改走"不裁功能、换压缩"——`KERNEL_XZ`（树内 `XZ_DEC_ARM`/`BCJ` 已齐，BCJ 对 ARM 代码再压几 %），
  4.24→2.63 MiB，保留 MPP 解码 + 全部 crypto 给 Stage 2，代价仅启动解压 +1~2s。
- **裁剪边界（`board/s4602/firefly/s4602_fragment.firefly.config`，kconfig 校验 `tools/fl_fix_kconfig.py` 全绿）**：
  关 = DRM/SND/无线(WLAN select WIRELESS、WL_ROCKCHIP+AP6XXX select MAC80211/CFG80211)/SCSI/MMC/键盘触摸HID/背光/PSTORE/IIO/DEVFREQ/thermal/USB-gadget；
  **保留 = 出图链路 `MEDIA_SUPPORT`/`VIDEO_ROCKCHIP_{CIF,ISP,ISPP}`/`VIDEO_SC5338`/`STMMAC`/`MOTORCOMM_PHY`/`UBI`/`UBIFS`/`SQUASHFS`/`I2C`/`SPI`/`CMDLINE_FORCE`
  + MPP 编码侧 `RKVENC/VEPU2/MPP_SERVICE` + crypto 核心 + USB host 子系统 + 4G 网卡数据驱动（USB 见 §"硬件约束"）**。
  **关键经验：`select` 优先级高于 `# ... is not set`，关叶子会被 select 源拉回，必须关 select 源头（WLAN/WL_ROCKCHIP）。**
- 权威产物已归位 `firmware/firefly-s4602-rv1109-usb4g-r5-run44020818093.zip`（GA run `44020818093`，
  commit `c6cad32`：zboot 2,954,252 B + `rv1109-38-v10-spi-nand.dtb` 87,116 B + `vendor-kernel.dtb` 85,937 B）。
- **下一步 = 上机**：台架 SD 三参 `bootm` 或刷 `boot_b`，确认原厂 U-Boot 能起这个内核 + `dmesg` 看 sc5338/sfc_nand 命中。


**★★2026-11-17 GA 首跑成功 + 容量缺口（决定性发现）：**
- ✅ GA run `43998544748` **success**（commit `d7ba710`），artifact `firefly-s4602-rv1109` 已产出并本地核对：
  `zboot.img` 6,191,772 B（**5.90 MiB**，魔数前 4 字节 `d00dfeed` 有效 FIT）+
  `rv1109-38-v10-spi-nand.dtb` 87,116 B + `vendor-kernel.dtb` 85,937 B。
  全量 4.19 zImage + 参考 DTS + 打 FIT 这条 177 装不下的链路，**GA 一次跑通**。
- ⚠️ **容量缺口 = Stage 1 上机前的硬阻塞**：`zboot.img` 5.90 MiB **> boot_b 分区 4 MiB（0x400000=4,194,304 B）**，
  超 **1,997,468 B（≈1.9 MiB）**，直接 `mtd write boot_b` 写不下。
  根因：`rv1126_defconfig` 比 OpenIPC 精简配置肥（OpenIPC 同代 4.19 的 zboot.img 仅 **3.38 MiB**，见 `board.yaml` CI run 43177394588）。
  本 kernel 本体 zImage=6,102,608 B（5.82 MiB）→ 单 kernel 就顶格，FIT 头 + dtb 再叠加。**解法 = 换精简 defconfig**
  （裁掉 camera 链路用不到的 fb/graphics/多余 mpp/视频 codec），目标把 zboot.img 压回 <4 MiB；
  这是「刷 boot_b」这一步的前置，**未做前不得动设备**。
- 校验 bug 已修（commit `d7ba710`）：step9 旧写法 `assert d.hex().endswith('d00dfeed')` 对整个 8 字节 hex 串判断，
  而 FIT header 第 4–8 字节是 `image_header.timestamp`（本例 728），并非 magic，导致有效 FIT 误报 `not a valid FIT`
  （run `43997690368` 因此挂）；改为只判前 4 字节 `magic.hex()=='d00dfeed'`，容量缺口降为 **WARNING**（artifact 照常产出，不卡 CI）。

## 6. 行动项（优先级）

1. ~~触发 GA 收 Stage 1 kernel FIT artifact~~ ✅ run `43998544748` 已 success，artifact 已核对（魔数有效）。
2. ~~压小内核使 zboot < 4 MiB~~ ✅ **已达成**：裁剪 + 压缩切换 + 恢复 USB host 后 zboot = 2.82 MiB（富余 1.18 MiB），见 §5「容量缺口已解除」。
3. **【当前主行动项】台架引导**：SD 三参 `bootm` 或 `mtd write boot_b` 刷 **round5 产物**（`firefly-s4602-rv1109-usb4g-r5-run44020818093.zip`，含 USB host + 4G RNDIS 驱动）→ 确认原厂 U-Boot 起内核 + `dmesg`（sc5338 Detected、sfc_nand id e5f2e5 命中、插 4G 卡后出现 `usb0`/`wlan` 网卡）。
4. 通过后开 Stage 2（buildroot + rkmedia RTSP demo + vendor DTS + 14 分区 parameter）。
5. 全路线验收 = `vlc rtsp://<ip>/live/main_stream` 出**硬件 ISP 画质**（对比现有 8.8fps 软件 demosaic）。

> **硬件约束（2026-11-17 用户）**：板上有 USB 口、要插 4G 无线网卡 RNDIS 模式 → **USB host 子系统绝不能裁**，
> 内核须含 USB host(XHCI/EHCI/OHCI) + `USB_USBNET`/`USB_NET_RNDIS_HOST`/`CDC_NCM`/`CDCETHER`/`USB_SERIAL_WWAN/OPTION/QUALCOMM`。
> 仍关 USB 设备角色（GADGET/CONFIGFS）。
