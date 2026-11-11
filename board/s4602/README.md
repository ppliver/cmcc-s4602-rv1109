# S4602 板级适配目录

| 文件 | 状态 | 说明 |
|---|---|---|
| `board.yaml` | **已按实测填写** | 唯一参数账本：硬件、14 分区 GPT、U-Boot 能力、当前砖机状态、刷写目标 |
| `s4602_defconfig` | 未构建验证 | 基于官方 `rv1109_lite_defconfig`，只改内核 DTB 目标名 |
| `ubinize-s4602.cfg` | 已核对官方布局 + 本机实测 | 官方 2 卷 UBIFS；UBI 几何与 S4602 实测一致 |
| `parameter.txt.template` | **已填实测值** | 14 分区 mtdparts 原文（GPT / printenv / dmesg 三方一致） |
| `generated/*.dts` | 由 `tools/make_dts.py` 生成 | 从官方基线 DTS 机械派生，需按 `vendor-kernel.dtb` 逐项校正 |

## 板级事实来源已升级

`docs/evidence/vendor-kernel.dtb` 是从原厂 `boot_a.img` 的 FIT（数据区 `0x800`）提取的
**S4602 真实内核 DTB（85,937 B）**，含 SC5338 / GMAC 外置 PHY / ES7202 / PCF8563 /
adc-keys / chosen bootargs。做 OpenIPC 版 DTS 时：

- **板级事实**以 `vendor-kernel.dtb` 为准；
- **功能基线**用 OpenIPC `rockchip-rv11xx` 的 `rv1109-38-v10-spi-nand.dts`；
- 官方 Longse 评估板 DTS（`rv1109-38x38-v10-spi-nand-imx307.dts`）只作为差异参考，
  **不能再当作 S4602 的基线**。

必改项：sensor（IMX307 → SC5338，i2c1@0x30，2-lane）、`ubi.mtd=3` → `ubi.mtd=8`
（system_a = MTD 8）、mtdparts、PHY/GPIO、chosen bootargs。

## 顺序（当前设备是半砖，顺序不能跳）

1. `docs/RECOVERY.md` 阶段 0–2：救砖 → 恢复原厂 → 补备份 `0x0–0x300000` loader 区。
2. 确认原厂能稳定启动、能进 `=>`，把 `board.yaml` 的 `device_state.*` 与
   `flash_plan.backup_verified` 改为 true。
3. `python tools/make_flash_plan.py --check` 通过后才允许生成刷写命令。
4. 构建带 S4602 DTB 的 `zboot.img` → RAM 里 `iminfo`/`bootm` 验证 → 只刷 `_a` 槽 → 验收。

## 已知限制

- `ethaddr` / SN 来自 `devinfo_a`，恢复时必须原样写回。
- 原厂 U-Boot 无 `saveenv`、无 `ubi`、无可用 `nand` 设备；只有 `mtd` 系列命令。
- bootargs 无法靠 `setenv` / `saveenv` / DTB `chosen` 设置（U-Boot 会强制注入
  `rootfstype=squashfs`）；必须开内核 `CONFIG_CMDLINE_FORCE`，见 `docs/BOOT_FEASIBILITY.md`。
