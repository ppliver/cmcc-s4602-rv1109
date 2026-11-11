# CMCC S4602 (RV1109 / SPI-NAND) → OpenIPC 移植工程

**当前状态：设备是半砖（2026-06-29 刷写失败），离线工程 + 实证取证已完成，设备侧未执行。**
SPL/DDR 仍正常，但进不了 U-Boot 控制台。完整复盘见 [`docs/INCIDENT.md`](docs/INCIDENT.md)，
救砖与正确移植路径见 [`docs/RECOVERY.md`](docs/RECOVERY.md)，
**如何做一份真正完整的备份见 [`docs/FULL_BACKUP.md`](docs/FULL_BACKUP.md)**。

**构建方向（2026-11-11 调整）：假设设备正常，先交付一份能引导、能挂载官方 nightly rootfs、进 SSH 的 OpenIPC。**
✅ **CI 已绿**：run `42984058196` 成功，产出 artifact `openipc-s4602-rv1109`（zboot.img 3.38 MiB + rootfs.ubi.rv1109 7.5 MiB）。
构建已由 **GitHub Actions** 接管（仓库 `ppliver/cmcc-s4602-rv1109`，见 `.github/workflows/build.yml`）：
push `main` 或手动 `workflow_dispatch` 即从官方工具链 + `OpenIPC/linux@rockchip-rv11xx` 编译内核，
复用原厂 `vendor-kernel.dtb` 打 FIT，并下载官方 `rv1109-nand-lite` rootfs，
产物 `zboot.img` + `rootfs.ubi.rv1109` 以 artifact 分发。**SC5338 出图驱动已编入内核**
（CI 经 `tools/apply_kernel_patch.py` 注入 `CONFIG_VIDEO_SC5338=y`，见 [`docs/SC5338.md`](docs/SC5338.md) 第 6 节）。

现有 14 个分区备份只覆盖 256 MiB 中的 252.5 MiB（98.63 %），
缺 loader 区与 GPT 备表；且 ⑨ 从 offset 0 误写过整片，补读的内容未必干净。

## 一句话

OpenIPC 官方支持 RV1109 并有 NAND 固件，但**不提供该 SoC 的 bootloader**，
且固件绑定 Longse 评估板 + IMX307 的 DTB。S4602 是中国移动定制板，
需要的是**板级 BSP 适配**。上一次尝试只刷了 rootfs、没换内核，
最后一条参数被截断的 `mtd write` 从 flash 偏移 0 写满整片，把引导链前段冲掉了。

现在**先救砖、恢复原厂、补备份，再谈移植**。

按官方安装方式，**U-Boot 不编译、不覆盖，复用原厂 loader**（已由 `## Verified-boot: 0`
与「SPL 至今仍能运行」实证支持，见 [`docs/UBOOT.md`](docs/UBOOT.md)）。
必须自己构建的是内核 + S4602 DTB（`zboot.img`）与 rootfs UBI 镜像。

**「U-Boot 能不能引导 OpenIPC」的判定（[`docs/BOOT_FEASIBILITY.md`](docs/BOOT_FEASIBILITY.md)）**：
能，也不用改 U-Boot —— `boot_fit` 无签名校验，原厂 `boot_a.img` 正好可以当 FIT 模板。
「刷进去能用」还差三项，但现在**三项都有明确解法/资料**：① 官方 `zboot.img` 默认不是 FIT
→ 用原厂 FIT 模板重打包；② U-Boot 强制注入 `rootfstype=squashfs` → 内核
`CONFIG_CMDLINE_FORCE`（前两条已写进 `tools/build_s4602.sh`）；③ OpenIPC 内核树确实没有
SC5338 驱动，但**资料包已备齐**（[`docs/SC5338.md`](docs/SC5338.md)）：公开 SC5338 驱动 +
从原厂内核二进制提取的寄存器表 + OpenIPC 内核树里的同族 V4L2 骨架，
无需再找原厂内核源码，剩下是编码与实板验证。

## 目录

```
firmware/          官方 nightly 固件 openipc.rv1109-nand-lite.tgz + 解包镜像
src/firmware-master/  OpenIPC firmware 源码（核对打包/构建逻辑用）
src/sensor-reference/  SC5338 参考驱动/寄存器表/内核配置（9 份下载源码/配置 + 1 份生成的 .h）
src/kernel-patches/   SC5338 内核驱动成品：`sc5338.c`（V4L2 subdev 驱动）+ `sc5338-tables.h`（寄存器表，
                      由 `tools/gen_sc5338_tables.py` 从参考头提取）+ 参考寄存器头 `sc5338-regs-rockchip.h`
移动千里眼S4602原厂固件备份/  14 个原厂分区镜像，252.5 MiB（md5 见 INCIDENT.md）
docs/PORTING.md    移植报告：证据链、差距矩阵、实施路径、红线、验收标准
docs/BOOT_FEASIBILITY.md  能不能刷 / U-Boot 能否引导：三层判定、FIT 模板解法、bootargs 冲突、L3 缺口
docs/SC5338.md     SC5338 资料包：规格、DTB 链路、公开驱动、原厂寄存器表交叉验证、移植清单
docs/INCIDENT.md   2026-06-29 变砖复盘：时间线、根因、备份清单与缺口、实测硬事实
docs/RECOVERY.md   救砖 → 恢复原厂 → 补备份 → 移植 OpenIPC 的分阶段操作
docs/FULL_BACKUP.md 完整备份 SOP：范围/内容两层、4 条通道、32 MiB 分块、坏块与 OOB、双来源归档
docs/UBOOT.md      U-Boot 要不要编译：引导链决策、闭源 blob 约束、重建清单
docs/evidence/     官方 DTS 基线、原厂 DTB、内核/U-Boot/rkbin tree 快照、取证记录
board/s4602/       已填实测参数：board.yaml / defconfig / UBI 布局 / 分区表
tools/             备份计划生成、全片校验、采证、镜像校验、DTS 派生、刷写计划、构建脚本
uboot/             00 只读探测 → 01 分区备份(历史) / **01-full-backup.txt(全片)** → 02 刷写 → 03 bootargs
```

## 构建 OpenIPC（GitHub Actions，假设设备正常）

仓库 `ppliver/cmcc-s4602-rv1109` 的 `main` 分支每次 push（或手动 `workflow_dispatch`）都会跑
[`.github/workflows/build.yml`](.github/workflows/build.yml)，产出两个 artifact：

| artifact | 内容 | 用途 |
|----------|------|------|
| `openipc-s4602-rv1109` | `firmware/zboot.img` + `firmware/rootfs.ubi.rv1109` | 分别写入原厂 `boot_a`(0x600000) 与 `system_a`(0xE80000) 分区 |

构建步骤（与本地 `tools/build_s4602_kernel.sh` 完全一致）：
1. 装依赖 + 下载官方 `toolchain.rockchip-rv11xx.tgz`（arm-openipc-linux-gnueabihf）。
2. 克隆 `OpenIPC/linux@rockchip-rv11xx`，以 `rv1126.generic.config` 为基线，
   用 `scripts/kconfig/merge_config.sh` 叠加 `board/s4602/s4602_kernel_fragment.config`
   （CMDLINE_FORCE 写死 mtdparts/bootargs、存储栈、ISP、板载外设、**CONFIG_VIDEO_SC5338=y**）。
   叠加前由 `tools/apply_kernel_patch.py` 把 `src/kernel-patches/sc5338.c` + `sc5338-tables.h`
   拷入 `linux/drivers/media/i2c/` 并向 `Makefile`/`Kconfig` 注入 `VIDEO_SC5338`。
3. 编译 `zImage`，配合原厂 `docs/evidence/vendor-kernel.dtb` 经 `tools/make_fit.sh` 打 FIT（`zboot.img`）。
4. 下载官方 `openipc.rv1109-nand-lite.tgz` 取 `rootfs.ubi.rv1109`。

**关键事实**：OpenIPC 内核按分支 HEAD 取源码，nightly rootfs 同源 → 模块 ABI 一致；
存储/网络/ISP 均已编进内核（`=y`），故即使个别模块偏移，引导 + SSH 仍可工作。
SC5338 出图驱动已编入内核（CI 注入），IQ/3A tuning XML 仍待原厂 rootfs 解包，见 [`docs/SC5338.md`](docs/SC5338.md)。

本地复现（需先放好 OpenIPC 工具链到 PATH）：`bash tools/build_s4602_kernel.sh`

## 先读这三份（按顺序）

1. `qianliyan原厂固件启动信息.log` — 原厂正常启动，含完整 `help` / `printenv` / `mtdparts`
2. `s4602变砖过程_20260629_133651.txt` — 9177 行现场记录，变砖根因在第 8840–8842 行
3. `docs/INCIDENT.md` — 上面两份的解读 + 备份 md5 + 被推翻的旧结论

## 本地可做（不碰设备）

```bash
# 解析 14 个备份分区：GPT 表 / FIT 结构 / UBI 卷 / md5 / 覆盖缺口
python tools/inspect_vendor_backup.py
python tools/inspect_vendor_backup.py --json

# 校验官方 OpenIPC UBI 镜像
python tools/verify_image.py firmware/rootfs.ubi.rv1109 \
    --md5 5c1849f7683f7f33bf800d2cf966249a

# 刷写计划前置检查（当前应硬拒绝：设备还是砖）
python tools/make_flash_plan.py --check

# 生成全片备份的只读命令清单（不碰设备）
python tools/make_backup_plan.py --mode uboot --out uboot/01-full-backup.txt
python tools/make_backup_plan.py --mode rockusb --out docs/evidence/snapshot-plan.txt
python tools/make_backup_plan.py --list

# 用现有 14 分区合成全片做工具自检（不是现场 dump，不可归档）
python tools/verify_full_dump.py --source golden

# 现场 dump 回来后：拼接 + 与 golden 逐分区 md5 比对 + loader/GPT 备表检查
python tools/verify_full_dump.py --source chunks --dir snapshot/ --out snapshot/full-256M.img

# SC5338 资料包：DTB 反编译 / dmesg 取证 / 原厂内核解压 / 寄存器表交叉验证
python tools/collect_sc5338.py --check && python tools/collect_sc5338.py
python tools/verify_sc5338_regs.py --check && python tools/verify_sc5338_regs.py
```

## 设备侧顺序（不能跳步）

1. 接 UART（3.3 V / **1500000** 8N1），确认当前是「SPL 失败」还是能进 `=>`；
   同时看 USB OTG 是否枚举 `VID 0x2207`（MASKROM 通道）。
2. 按 `docs/RECOVERY.md` 阶段 1 建立写 flash 通道（需要匹配 LPDDR4 924 MHz 的原厂 loader）。
3. 阶段 2：**只先恢复 gpt + uboot_a/b + boot_a/b**，重启验证能进 U-Boot；再恢复其余分区。
4. 能进 U-Boot 后按 [`docs/FULL_BACKUP.md`](docs/FULL_BACKUP.md) 做 **8 × 32 MiB 全片 dump**
   （现有备份缺 loader 区 2944 KiB **和末尾 GPT 备表**），用 `verify_full_dump.py` 交叉校验；
   补读的 loader 区可能已被 ⑨ 污染，须双来源比对后才算干净。
5. 确认原厂可稳定启动 → 更新 `board.yaml` 的 `device_state.*` 与 `backup_verified`。
6. Ubuntu 构建机构建带 S4602 DTB 的 `zboot.img`，RAM 里 `iminfo`/`bootm` 验证格式。
7. `make_flash_plan.py --check` 通过 → 只刷一个槽 → 验收 SSH 与 RTSP 出图
   （SC5338 驱动编码与逐级验收清单见 [`docs/SC5338.md`](docs/SC5338.md) 第 6 节）。

## 红线

- **设备未救砖、原厂未恢复、loader 区未补备份前，不生成也不执行任何写命令。**
- `mtd write` 四参数齐全：`<name> <addr> <off> <size>`，size = **文件实际字节数**，
  写完 `mtd read` + `cmp.b` 回读比对。禁止缺省 off/size（上次就是这样从 0 写满整片）。
- 只刷 A/B 的一个槽，另一槽保留原厂回滚；不同时刷两套 system。
- 必须同时替换 boot 分区内核与 system 分区 rootfs；只刷 rootfs 起不来。
- 不覆盖 `0x0–0x300000`（loader/SPL/ddr bin）；现有备份不含它。
- 官方 nightly 包**不含 zboot.img**，且 DTB 不是给 S4602 的 —— 不要直接刷。
- `make_flash_plan.py` 没有 `--force`；它拒绝时就说明证据不够，不要绕过手写命令。

## 需要你提供（P0）

1. 当前上电的完整串口输出（判断 SPL 失败还是能进 `=>`）。
2. USB OTG 是否枚举 `2207`；有没有原厂 `MiniLoaderAll.bin` / `parameter.txt` 或同型机可 dump。
   **若能枚举，先用 `rkdeveloptool rl` 做一份只读全片快照，把 ⑨ 之后的 flash 现状固定下来。**
3. 备份目录里那张 SD 卡是否还在（恢复分区时要用）。
4. NAND / DDR / sensor / PHY 的芯片丝印（NAND 目前只有 `e5 f2 e5` 的 ID，料号未定）。
