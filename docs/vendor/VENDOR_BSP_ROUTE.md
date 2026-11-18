# 转向 vendor BSP 路线 — 取证与路线分叉评估

> 2026-11-16。用户决定放弃 OpenIPC，转 RK 官方 SDK 适配固件。
> 本文是"先取证后下结论"的产物，所有结论带证据来源，未验证部分明确标注。

## 0. 内核版本澄清（先归位）

| 说法 | 证据 | 判定 |
|---|---|---|
| 原厂 4.19.111 | `qianliyan原厂固件启动信息.log:162`：`Linux version 4.19.111 (lunwenkang@sw) (gcc 6.3.1 20170404 Linaro) #47 SMP PREEMPT Wed Jan 10 07:09:29 UTC 2024`；`board.yaml` 一致 | ✅ 铁证 |
| OpenIPC 4.19.111+ | `live-bootfit-fixed-20261112.stdout.txt:43` | ✅ 同一版本 |
| 5.10 | 全项目仅在 `firmware/waybean-ref/`（Luckfox RV1106 waybean + `mis5001.ko`, `WANT_KERNEL=5.10.160`） | ❌ 另一块板/另一套 SDK，与 S4602 原厂无关 |

**结论：内核版本从来不是画质问题根源。两边同为 4.19.111。**

## 1. ★决定性发现：原厂 rootfs 里整套媒体栈齐全且原生配套

从 `system_a.img`（UBI → rootfs 卷 squashfs，20.5 MiB）解出的完整文件树：
`.stage_extract/vendor_rootfs/squashfs_root/`（取证脚本 `tools/extract_vendor_rootfs.sh` + `unsquash_vendor.sh`）

| 资产 | 路径 | 大小 | 意义 |
|---|---|---|---|
| 主程序 | `system/bin/swapp` | 630 KB | **ELF armhf / interpreter `/lib/ld-linux-armhf.so.3`（glibc，非 uClibc！）** |
| 3A 库 | `system/lib/librkaiq.so` | 2.25 MB | 原厂配套版本，与 IQ 文件匹配 |
| 媒体框架 | `system/lib/libeasymedia.so.1.0.1` | 638 KB | VI/VENC/Bind 全套 |
| 编码库 | `system/lib/librockchip_mpp.so.0` | 1.2 MB | 硬件 VENC |
| IQ 曲线 | `system/resource/scene_param/COMMON_sc5338.xml` | 2.5 MB | 头部 `sensor_name=SC5338`, 注释 `S4602 cmcc物聯網 v0.25-b 支持更低的awb效果 fec关闭` —— **就是这块板的定制 IQ** |
| VQE 参数 | `system/resource/vqefiles/RKAP_3A_Para_{8k,16k}.bin` | — | 3A 算法参数 |
| 启动脚本 | `system/etc/init.d/start_swapp.sh` | — | `export LD_LIBRARY_PATH=/system/lib` → `RKFPS=25` → `sudo swapp &` |

**swapp 的动态依赖 30 个 NEEDED 全部在 `system/lib/` 内**（含 libavformat.so.58 / libavcodec.so.58 / librga / libdrm / librknn / librockx），无缺失。

**这推翻了一条旧结论**：记忆里"厂 app 跑不起来 = `__uClibc_main` undefined"说的是 `divinus`（另一套 Ingenic/UAPI2 参考二进制），**不是 S4602 原厂自己的 swapp**。原厂 swapp 是 glibc armhf，只要用原厂 rootfs 启动就是原生配套环境。

## 2. camera_info 缺口 — 定位到运行期注入，不是缺文件

`strings swapp` 关键命中：
```
rk_aiq_uapi_sysctl_Iqfile is %s
/system/resource/scene_param/
COMMON_sc5338.xml
ID:%d, sensor_name:%s, width:%d, height:%d, iqfiles_dir:%s
m01_f_sc5338 1-0030          <-- 与 OpenIPC 里 media 图名完全一致
want to load iqfile is %s
```

**判定（高置信，待实板验证）**：原厂没有独立的 `sensor_calib.json` 文件；`swapp` 通过 `rk_aiq_uapi_sysctl_init(sensor_name, iq_dir, ...)` + 硬编码的 sensor 静态信息（`ID/width/height/iqfiles_dir`）在运行时向 3A 注入 camera_info。这正是 OpenIPC 路线里 `aiqprobe` 缺的那一步（它跳过注入 → `CamHwIsp20.cpp:732 camerainfo of sc5338 not found` → fake camera）。

**所以 vendor 路线成立的根因**：不是"重编 SDK 就有 camera_info"，而是**原厂 swapp 二进制里就带着 sc5338 的完整 camera_info 注入逻辑，且 IQ 文件同源匹配**。这是任何 OpenIPC/公版 SDK 拼凑都拿不到的资产。

## 3. 路线分叉（需用户决策）

### 路线 A：原厂固件 + 补标准 RTSP 出口（推荐先试，成本最低）
- 用原厂 kernel + rootfs 启动（boot_a 或 boot_b 原槽），让 swapp 原生跑起来 → 3A/ISP 全开。
- swapp 本身走千里眼私有云（18686），**无标准 RTSP**。
- 补法二选一：
  - A1：在原厂 rootfs 里塞一个 `rkmedia_vi_venc_rtsp_test`（用 SDK 编译或现成 demo），与 swapp 共享 isp/3A 输出。风险：两个进程抢 ISP/vb2 队列，需实测。
  - A2：给 swapp 的私有码流做本地转发（逆向 18686 协议，工作量大），不推荐。
- **优点**：零移植、零编译内核、画质原厂级。**缺点**：RTSP 出口是新增件，可能与 swapp 抢资源。

### 路线 B：RK 官方 SDK 全量适配（用户原始要求，成本最高）
- 拉 `rv1126_rv1109_linux_release`（Buildroot 2018.02-rc3 + kernel 4.19 + U-Boot 2017.09；Firefly 公开 manifest 无需 NDA）。
- 换板级 DTB（`docs/evidence/vendor-kernel.dtb`）、移植 SC5338 驱动（`src/kernel-patches/sc5338-tables.h`）、IQ（`COMMON_sc5338.xml`）、camera_info（照 swapp 注入逻辑补）。
- 出图 demo 现成：`rkmedia_vi_venc_rtsp_test -a /etc/iqfiles/` → VI→VENC→RTSP。
- **必须保留原厂 U-Boot**（红线），只换 kernel + rootfs。
- **优点**：完全自主可控、标准 RTSP。**缺点**：数天适配；camera_info 仍需自己补（可从 swapp 逆向注入参数）；quad 画质要重新调 IQ。

### 路线 C（不推荐）：继续 OpenIPC 拼凑
- 已闭环死结，见 MEMORY.md。放弃。

## 4. 我的建议顺序
1. **先做 A 的可行性验证**（1-2 小时）：把原厂 squashfs 重新打包回可启动形态，或用 SD 卡 FAT 加载原厂 uImage+DTB+rootfs，看 swapp 能否原生跑出图。若能，再解决 RTSP 出口。
2. A 若受阻（如 swapp 强依赖云端鉴权），再上 B。
3. 全程不动 U-Boot / loader / _a 槽；所有写入只碰 _b 槽，且写前全片备份已在 `backup/snapshot/full-256M.img`。

## 5. 尚未验证（明确标注）
- swapp 是否强依赖云端鉴权/网络才能出本地图（未实测）。
- swapp 与第三方 RTSP demo 能否共享 ISP 输出（未实测）。
- `COMMON_sc5338.xml` 在公版 SDK rkaiq 版本下能否直接用（版本可能不同，未实测）。
