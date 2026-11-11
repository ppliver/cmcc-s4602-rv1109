# 能不能刷 OpenIPC？U-Boot 能不能引导？—— 三层判定

> 2026-11-11 按现场串口日志、14 个分区备份、OpenIPC firmware/linux 源码重新核算。
> 前置阅读：[`INCIDENT.md`](INCIDENT.md)（砖态）、[`RECOVERY.md`](RECOVERY.md)（救砖）、
> [`FULL_BACKUP.md`](FULL_BACKUP.md)（完整备份）、[`UBOOT.md`](UBOOT.md)（引导链）。

## 0. 一句话结论

**U-Boot 能引导 OpenIPC 内核，不用编译、不用替换、不用改 env。**
但「刷第三方固件并能用」现在**做不到**，卡点不在 U-Boot，而在后面三层：

| 层 | 判定 | 卡点 |
|---|---|---|
| **L1 U-Boot 能否加载并启动 OpenIPC 内核** | ✅ **能**（有可执行路径） | 官方 `zboot.img` 默认不是 FIT，必须用原厂 `boot_a.img` 作模板重打包 |
| **L2 内核能否挂载 OpenIPC rootfs** | ⚠️ **有硬冲突，需改内核配置** | U-Boot 强制注入 `rootfstype=squashfs`，而 OpenIPC rootfs 是 UBIFS |
| **L3 能否出图 / 变成可用摄像头** | ❌ **目前不能** | OpenIPC 内核树里没有 SC5338 驱动 |
| **L0 现实前置** | ❌ **设备还是半砖** | SPL 活着但进不了 `=>`，上面全部是救砖之后的事 |

**所以准确说法是：U-Boot 不是障碍；障碍是「镜像格式 + bootargs 注入 + sensor 驱动」，外加设备还没救回来。**

---

## 1. L1 — U-Boot 能否引导：能，证据充分

### 1.1 现场实证（原厂 U-Boot 2017.09）

| 证据 | 原文 / 位置 | 含义 |
|---|---|---|
| 启动方式 | `bootcmd=boot_fit;boot_android ${devtype} ${devnum};`（变砖记录 1255、1605 行） | 内核从 **boot_a/b 分区的 FIT** 加载 |
| 命令说明 | `boot_fit - Boot FIT Image from memory or boot/recovery partition`（1166、1489 行） | 既能读分区也能读 RAM |
| 无强签名 | `## Verified-boot: 0`、`FIT: no signed, no conf required`、`HASH(c): OK`（日志 70、变砖记录 57） | 换成自己打的 FIT 不会被公钥拦 |
| 命令集 | 有 `mtd read/write/erase`、`tftp`、`fatload/fatwrite`、`rockusb`、`download`、`bootm`、`boot_fit`、`iminfo` | 不用补 U-Boot 功能 |
| 缺 `ubi` / `saveenv` | `help` 全文 | **不影响**：U-Boot 不需要懂 UBI，内核用 `ubi.mtd=N` 挂 |
| UBI 几何 | 原厂 `system_b`：PEB 131072 / LEB 126976 / min I/O 2048 / VID 2048 | 与官方 `rootfs.ubi.rv1109` **完全一致**，可直接写分区 |
| 原厂 `boot_a.img` | `file` → `Device Tree Blob version 17, size=1536`；magic `d00dfeed`，`-p 0x800` 外部数据 | 是标准 FIT，含 `images/{fdt,kernel,resource}` + sha256 hash + `configurations/conf` |

### 1.2 关键发现：官方 `zboot.img` **默认不是 FIT**

`OpenIPC/linux` 分支 `rockchip-rv11xx` 的 `scripts/mkimg`（存档于
`docs/evidence/openipc-mkimg.sh`）末尾是三分支：

```bash
if [ -f "${BOOT_IMG}" ]; then
    if file -p -b ${BOOT_IMG} | grep -q 'Device Tree Blob' ; then
        repack_itb;          # → mkimage -E -p 0x800 -f boot.its → 输出 FIT
    elif [ -x scripts/repack-bootimg ]; then
        repack_boot_img;     # → Android boot.img 格式
    fi
elif [ -x scripts/mkbootimg ]; then
    make_boot_img;           # → Android boot.img 格式（魔数 ANDROID!）
fi
```

- `firmware` 源码树里搜 `BOOT_IMG` / `.itb` / `.its` **0 个结果**；内核树 68,270 个路径里
  `.itb`/`.its` **也是 0 个** → **默认走 `make_boot_img`，产出 Android boot.img**。
- 而原厂 `boot_fit` 只认 FIT。把官方 `zboot.img` 直接写进 `boot_a/b`，
  大概率复现变砖记录 9005 行的 `Not fit magic`。
- 这解释了 `docs/RECOVERY.md` 3.3 之前标注的「兼容性未证」—— 现在可以明确：**默认不兼容**。

### 1.3 解法：把原厂 `boot_a.img` 当 ITB 模板（本轮最有价值的路径）

`repack_itb` 分支正好需要「一个被 `file` 判为 Device Tree Blob 的 FIT 模板」，
而**备份里的 `boot_a.img` 就是这个东西**：

```
unpack_itb  → 从模板解出 /images/{fdt,kernel,resource}
gen_its     → 反编译成 ITS，删掉 data-size / data-position / value / timestamp /
              hashed-* / signer-*（让 mkimage 重算 hash）
gen_itb     → 用 OpenIPC zImage 替换 kernel、用 S4602 DTB 替换 fdt
            → mkimage -E -p 0x800 -f boot.its zboot.img
```

`-p 0x800` 与原厂 FIT 的外部数据偏移**完全相同**（`boot_a.img` 里 fdt 数据就在 `0x800`）。
产物与原厂同构、hash 由 mkimage 重算，`boot_fit` 能识别、能通过 `HASH(c): OK` 校验。

`tools/build_s4602.sh` 已按此改写：把 `boot_a.img` 复制成内核树里的
`boot-template.img`，用 `BOOT_IMG=boot-template.img make BOARD=s4602_lite` 触发该分支。

**构建期风险点**：`gen_its` 没有删除 `configurations/conf/signature` 节点的
`algo` / `key-name-hint` / `sign-images` 残留，`mkimage` 可能要求 `-k` 密钥目录。
本机 `FIT: no signed` 本就不校验签名，若报错，手工从生成的 `out/boot.its` 里
删掉整个 signature 节点再重打即可。

### 1.4 内核存储栈齐全（不是障碍）

`br-ext-chip-rockchip/board/rv11xx/rv1126.generic.config`
（关键项存档 `docs/evidence/openipc-kernel-config-keys.txt`）：

```
CONFIG_RK_SFC_NAND=y / CONFIG_RK_SFC_NAND_MTD=y   ← 与原厂 dmesg 的 sfc_nand 同驱动
CONFIG_MTD_UBI=y / CONFIG_MTD_UBI_BLOCK=y
CONFIG_UBIFS_FS=y / CONFIG_SQUASHFS=y
CONFIG_MTD_CMDLINE_PARTS=y                       ← 依赖 cmdline 的 mtdparts=
```

MTD 分区、UBI attach、UBIFS 挂载都支持；`mtdparts` 用原厂 14 分区串即可。

---

## 2. L2 — bootargs 硬冲突：U-Boot 会强制覆盖启动参数

### 2.1 现场实验已经证伪「setenv bootargs 可控」

变砖记录 3643 行做过一次干净的对照实验：

```
=> setenv bootargs 'console=ttyFIQ0,1500000 root=ubi0:rootfs rootfstype=ubifs'
=> run bootcmd
```

3706 行内核实际收到的却是：

```
Kernel command line: user_debug=31 console=ttyFIQ0 root=/dev/ubiblock0_0
rootfstype=squashfs android_slotsufix=_b coherent_pool=64K earlycon=...
ubi.mtd=9 ubi.block=0,rootfs snd_aloop.index=7 mtdparts=spi-nand0:...
```

`root=ubi0:rootfs` **完全消失**，`rootfstype=ubifs` 被换回 `squashfs`。
日志里 7 次启动（149 / 2807 / 3706 / 5100 / 5990 / 7035 / 8119 行）
的 cmdline 全部含 `root=/dev/ubiblock0_0 rootfstype=squashfs ubi.mtd=9`。

参数来源已定位 —— `docs/evidence/vendor-kernel.dtb` 的 `chosen`：

```
bootargs = coherent_pool=64K earlycon=uart8250,mmio32,0xff570000 console=ttyFIQ0
           ubi.mtd=3 ubi.block=0,rootfs root=/dev/ubiblock0_0
           rootfstype=squashfs snd_aloop.index=7
```

U-Boot 把它与 env `bootargs` 合并、把 `ubi.mtd=3` 按 slot 改写成 `9`、
追加 `android_slotsufix=_b` / `mtdparts` / `user_debug`。**即 env 改不动 root 参数**，
而且这台机器本来就没有 `saveenv`（见 3.5）。

### 2.2 为什么这会让 OpenIPC rootfs 挂不上

| | 原厂 `system_*` | OpenIPC `rootfs.ubi.rv1109` |
|---|---|---|
| UBI 卷名 | `rootfs` | `rootfs` + `rootfs_data` |
| 卷内文件系统 | **squashfs**（`hsqs` @ `0x41000`） | **UBIFS**（`31 18 10 06`） |
| 启动参数 | `root=/dev/ubiblock0_0 rootfstype=squashfs ubi.block=0,rootfs` | 需要 `root=ubi0:rootfs rootfstype=ubifs` |

`ubi.block=0,rootfs` 会把 UBI 卷伪装成只读块设备再挂 squashfs；UBIFS 卷不是
squashfs，`rootfstype=squashfs` 必然 mount 失败 → kernel panic。

### 2.3 三条解法（按可靠性排序）

**A（推荐）· 内核 `CONFIG_CMDLINE_FORCE=y`，彻底忽略 bootloader 传参**

当前 `rv1126.generic.config`：
```
CONFIG_CMDLINE="user_debug=31"
# CONFIG_CMDLINE_FROM_BOOTLOADER is not set
CONFIG_CMDLINE_EXTEND=y
# CONFIG_CMDLINE_FORCE is not set
```
改成 `FORCE=y` 并把完整 cmdline 写进 `CONFIG_CMDLINE`：

```
earlycon=uart8250,mmio32,0xff570000 console=ttyFIQ0,1500000
ubi.mtd=9 root=ubi0:rootfs rootfstype=ubifs
mtdparts=spi-nand0:<原厂 14 分区串>
```

⚠️ **`CONFIG_MTD_CMDLINE_PARTS=y` 依赖 `mtdparts=`**：FORCE 之后 U-Boot 注入的
`mtdparts` 也没了，必须把 14 分区串一并写死，否则 MTD 分区出不来、UBI attach 不了。
`ubi.mtd` 取 9 还是 8 取决于最终刷哪个槽（见 4.1）。
改完**必须在构建机实测**打印出来的 `Kernel command line` 确认 FORCE 生效。

**B · 只改 FIT 内 DTB 的 `chosen` bootargs**

把 `root=` / `rootfstype=` 改成 UBIFS 并删掉 `ubi.block=0,rootfs`。
风险：U-Boot 仍会按 slot 注入 `ubi.mtd=9` 且可能重新拼装 `root=`，
现场观察到的行为说明它并非简单透传。**只能作为验证手段，不能作为最终方案。**

**C · 把 OpenIPC rootfs 做成 squashfs-in-UBI**

源码里 `general/scripts/ubifs/ubinize_rockchip.cfg` 已有兼容布局
（`rootfs` 卷 = `rootfs.squashfs`，另有 `kernel` / `rootfs_data` / `other` 卷），
原厂 cmdline 可直接挂。但要改 OpenIPC 的 overlay/启动脚本与 `ubi.block` 行为，
改动面最大，仅在 A 验证失败时考虑。

---

## 3. L3 — 出图：材料缺口已关闭，剩编码与实板验证

`OpenIPC/linux` 分支 `rockchip-rv11xx` 完整树 68,270 项
（快照 `docs/evidence/linux-tree-rv11xx.json`）：

- 路径含 `sc5338` / `5338` / `smartsens` / `sc53*`：**0 个**（确实没有 `sc5338.c`）
- 但同族 SmartSens 驱动**并不少**：`sc500ai.c`（2880×1620 + HDR_X2）、`sc401ai.c`、
  `sc5239.c`（2-lane）等 → 可直接当 V4L2 骨架
- `rv1126.generic.config` 已开 `MEDIA_SUPPORT / VIDEO_V4L2 / V4L2_SUBDEV_API /
  VIDEO_ROCKCHIP_CIF / ISP / ISPP`；所有 `CONFIG_VIDEO_SC*` 均未开 → 只差一个 sensor 驱动
- 官方 DTB 是 `rv1109-38x38-v10-spi-nand-imx307`，原厂 DTB 是
  `sc5338@30  compatible = "smartsens,sc5338"  i2c1  2-lane MIPI`

**2026-11-11 更新：不再需要「原厂 4.19.111 内核源码」。**
[`docs/SC5338.md`](SC5338.md) 已把材料备齐：

1. OpenIPC 官方仓库 `OpenIPC/openingenic` 里的 **SC5338 驱动**（T41 平台，
   chip id `0xce50`、2880×1620、2-lane、linear/HDR 两套寄存器表）；
2. 从 `boot_a.img` 的 zImage 解压出的**原厂 vmlinux**，其 `sc5338_set_ctrl` 符号之后
   有 3 张原厂寄存器表；与公开 HDR2 表序列相似度 **0.9551**（差异仅 2 处插入），
   证明同源、可直接复用（报告 `docs/evidence/sc5338-regs-crosscheck.txt`）；
3. OpenIPC 内核树里的 `sc500ai.c` / `sc5239.c` 骨架 + `rk-camera-module.h`
   （`hdr:5 == HDR_X2`）+ Kconfig/Makefile 写法。

**结论仍是：即使 L1/L2 全通，现在也只会得到一个能 SSH、没有视频流的 OpenIPC** ——
因为 `sc5338.c` 还没写、没编译、没实板验证。但瓶颈已从「缺资料」变成「待编码」。
移植改动清单与逐级验收见 `docs/SC5338.md` 第 6 节；唯一仍缺的资料是
**rkaiq 的 IQ/3A tuning XML**（原厂 `system_a.img` 是 squashfs-in-UBI，尚未解包）。

同理，`uboot/` 里的验收标准不能只写「内核起来、ubi attach」，必须写「majestic 能出流」。

---

## 4. 另外三个实施坑

### 4.1 A/B 槽方向与现有约定相反（需现场定夺）

当前活动槽是 `_b`：日志 `android_slotsufix=_b`、`ubi.mtd=9`（`system_b`）、
`boot_fit` 从 `boot_b` 读。而 `board.yaml` 的 `flash_plan` 现在是
`kernel_partition = boot_a` / `rootfs_partition = system_a`，
注释写「只刷 _a、留 _b 回滚」—— **U-Boot 默认根本不读 _a**。

两条路，二选一，**未实测前不要写死**：

| 方案 | 做法 | 代价 |
|---|---|---|
| 刷 `_b`（更省事） | 直接写 `boot_b` + `system_b`，`ubi.mtd=9`；`_a` 自动成为原厂回滚槽 | 需把 `flash_plan` 偏移改成 `0x00A00000` / `0x03A80000` |
| 刷 `_a`（保留现状） | 先搞清 misc 分区 / env 的选槽机制，把活动槽切到 `_a`，`ubi.mtd=8` | 多一个未知项；`misc.img` 当前实测全 `0xFF` |

无论哪条，**只刷一个槽**这条红线不变（上次两个 system 都刷掉，直接失去回滚）。

### 4.2 boot 分区容量

`boot_a/b` 各 4 MiB。原厂 FIT：kernel `0x318cc8`（3.10 MiB）+ fdt `0x14fb1`（86 KB）
+ resource + 头 ≈ 3.3 MiB。OpenIPC lite 内核应能装下，但**构建后必须核对**
`zboot.img` 实际大小 ≤ `0x400000`，并用 `iminfo` 在 RAM 里确认格式后再写 flash。

### 4.3 FIT 内 kernel `load`/`entry`

原厂 FIT 的 `images/kernel` 里 `load` / `entry` = `0xffffff01`（Rockchip 特殊标记），
不要照抄到自建 ITS；模板复用时保持原值即可。
现场已证明把整个 FIT 放到 `0x02008000` 会撞
`KERNEL (0x02008000-0x02320e00) overlap FIT_USER`，用 `0x1000000` 这类高地址。

---

## 5. 分级验收标准

| 级别 | 判据 | 说明 |
|---|---|---|
| ✅ L1-a | `iminfo 0x1000000` 认出 FIT，列出 kernel/fdt/hash OK | 格式路径打通 |
| ✅ L1-b | `boot_fit` / `bootm` 后打印 `Starting kernel ...` + OpenIPC 内核版本 | U-Boot 引导成功 |
| ⚠️ L2-a | `Kernel command line` 打印的是自定义 UBIFS 参数，不是 `rootfstype=squashfs` | CMDLINE_FORCE 生效 |
| ⚠️ L2-b | `ubi0: attached mtd<N>`、UBIFS mounted、`Cmiot login:` 变成 OpenIPC shell / SSH | rootfs 挂载成功 |
| ❌ L3 | `majestic` 起来、`/video` 有流、sensor probe 成功 | **资料齐备但 `sc5338.c` 未编码/未实板验证，达不到**；见 `docs/SC5338.md` |

---

## 6. 当前状态与下一步

设备 2026-06-29 起是半砖（SPL 正常、找不到 U-Boot FIT），
**以上全部是「救砖 → 恢复原厂 → 完整备份」之后的事**。

1. **P0** 按 [`RECOVERY.md`](RECOVERY.md) 救砖并补完整备份（[`FULL_BACKUP.md`](FULL_BACKUP.md)）。
2. **P1** 按 [`docs/SC5338.md`](SC5338.md) 第 6 节编写 `sc5338.c`（骨架 `sc500ai.c`
   + 已导出的原厂寄存器表），加 `CONFIG_VIDEO_SC5338=y` 并构建 ——
   **原厂内核源码已不再是必需材料**；剩余材料缺口是 rkaiq 的 IQ/3A tuning XML。
3. **P1** 在 Ubuntu 构建机跑改好的 `tools/build_s4602.sh`：FIT 模板 + S4602 DTB +
   `CMDLINE_FORCE`，产出真正的 FIT `zboot.img`，核对 ≤ 4 MiB。
4. **P1** 现场定夺 A/B 槽方向（4.1），同步更新 `board.yaml` 的 `flash_plan`。
5. **P2** RAM 里 `iminfo` → `bootm` 逐级验证，再只刷一个槽。

---

## 7. 证据索引

| 文件 | 内容 |
|---|---|
| `qianliyan原厂固件启动信息.log` | 原厂 FIT 启动、chosen cmdline、UBI/squashfs、分区与 MTD 编号 |
| `s4602变砖过程_20260629_133651.txt` | `printenv` / `help` / `setenv bootargs` 对照实验 / 7 次 cmdline / 变砖根因 |
| `移动千里眼S4602原厂固件备份/boot_a.img` | FIT 模板、原厂 kernel + 真实 S4602 DTB |
| `docs/evidence/vendor-kernel.dtb` | 从 FIT @`0x800` 提取的 DTB，含 `chosen bootargs` 与 `sc5338@30` |
| `docs/evidence/openipc-mkimg.sh` | `mkimg` 三分支、`repack_itb`、`-p 0x800` |
| `docs/evidence/openipc-kernel-config-keys.txt` | CMDLINE / SFC_NAND / UBI / UBIFS / SQUASHFS 配置项 |
| `docs/evidence/linux-tree-rv11xx.json` | 68,270 项内核树，SC5338 缺失 / 同族驱动存在的证据 |
| `docs/SC5338.md` | SC5338 资料包：规格、DTB 链路、驱动来源、寄存器表交叉验证、移植清单 |
| `docs/evidence/sc5338-regs-crosscheck.txt` | 9 张表相似度矩阵；原厂 HDR 表与公开表 0.9551 同源 |
| `src/sensor-reference/sc5338-regs-rockchip.h` | 导出的 5 组 `struct regval` 表（原厂 3 + 公开 2） |
| `src/firmware-master/` | defconfig、`ubinize.cfg` / `ubinize_rockchip.cfg`、Makefile 打包逻辑 |
