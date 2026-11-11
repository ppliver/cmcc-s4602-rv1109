#!/usr/bin/env python3
"""从官方基线 DTS 派生 S4602 板级 DTS。

基线来源（已下载到 docs/evidence/）：
  OpenIPC/linux 分支 rockchip-rv11xx
  arch/arm/boot/dts/rv1109-38x38-v10-spi-nand-imx307.dts
  （这是 dtc 反编译出来的单文件，官方 defconfig 就构建它）

本脚本只做「可追溯的机械替换」：改 board 标识、按现场确认的 sensor 改名。
它**不会**替你写 sensor 驱动节点、IQ 参数、GPIO 复用或 PHY 配置 —— 那些必须
依据 S4602 实测填写。sensor 型号未确认时，产物头部会带显著警告且保持 IMX307
配置不变，不得拿去做出图验收。

用法：
    python tools/make_dts.py
    python tools/make_dts.py --sensor gc2053 --board-yaml board/s4602/board.yaml
"""
import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "docs" / "evidence" / "rv1109-38x38-v10-spi-nand-imx307.dts"
OUT = ROOT / "board" / "s4602" / "generated" / "rv1109-cmcc-s4602-spi-nand.dts"
YAML = ROOT / "board" / "s4602" / "board.yaml"


def yaml_scalar(path: Path, section: str, key: str):
    """极简取值：只找 section 下第一次出现的 key，够用且不依赖 PyYAML。"""
    cur = None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*$", line)
        if m:
            cur = m.group(1)
            continue
        if cur == section:
            m = re.match(rf"^\s+{re.escape(key)}:\s*(.*?)\s*(?:#.*)?$", line)
            if m:
                v = m.group(1).strip().strip('"').strip("'")
                return None if v in ("", "null", "~", "None") else v
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sensor", help="现场确认的 sensor 型号（小写，例如 gc2053 / sc223a / imx335）")
    ap.add_argument("--board-yaml", type=Path, default=YAML)
    ap.add_argument("--base", type=Path, default=BASE)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    if not args.base.is_file():
        print(f"基线 DTS 不存在: {args.base}", file=sys.stderr)
        return 2
    sensor = args.sensor or yaml_scalar(args.board_yaml, "sensor", "model")
    src = args.base.read_text(encoding="utf-8", errors="replace")

    warnings = [
        "由 tools/make_dts.py 从官方 rv1109-38x38-v10-spi-nand-imx307.dts 派生",
        f"生成时间: {datetime.now().isoformat(timespec='seconds')}",
        "状态: 未编译验证、未在 S4602 上验证",
    ]
    if not sensor:
        warnings += [
            "!! sensor 型号未确认 —— 本文件仍是官方 Longse 板的 IMX307 配置 !!",
            "!! 禁止用它构建固件并宣称出图；先填 board.yaml 的 sensor.model !!",
        ]
    else:
        warnings += [
            f"sensor 型号已按现场记录改为 {sensor}，但仅做了名称替换：",
            "驱动 compatible、寄存器表、lane 数、MCLK、reset/pwdn GPIO、IQ 文件必须逐项核对，",
            "这些不改不会出图，改错可能黑屏或烧坏模组。",
        ]
    warnings += [
        "另需按现场核对: DDR 型号/容量、SPI-NAND 型号与分区、GMAC PHY 与 RMII/RGMII、",
        "IRCUT/IR LED/按键/LED GPIO、串口与 bootargs。",
        "原厂 SPL/DDR bin 保留不动；本 DTS 只影响 Linux 内核侧。",
    ]
    header = "/*\n" + "\n".join(" * " + w for w in warnings) + "\n */\n"

    out = src
    out = re.sub(r'^//Longse RV1109\+IMX307\s*$', '// CMCC S4602 (RV1109, SPI-NAND)', out, flags=re.M)
    out = re.sub(r'model = "Rockchip RV1109 38x38 V10 SPI NAND IMX307 DDR3 Board";',
                 'model = "CMCC S4602 RV1109 SPI-NAND Board";', out)
    out = re.sub(r'compatible = "rockchip,rv1109-38x38-v10-spi-nand-imx307", "rockchip,rv1109";',
                 'compatible = "rockchip,rv1109-cmcc-s4602-spi-nand", "rockchip,rv1109";', out)
    if sensor and sensor.lower() != "imx307":
        out = re.sub(r'\bimx307\b', sensor.lower(), out, flags=re.I)
        # 上面会把 imx307 全部小写替换；匹配大小写不敏感时统一成小写
    out = header + out

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(out, encoding="utf-8")
    print(f"已生成: {args.out} ({len(out.splitlines())} 行)")
    print(f"sensor: {sensor or '未确认（保留官方 IMX307）'}")
    print("验收前置: 用内核树 dtc -I dts -O dtb 编译通过 + 板子实测出图")
    return 0


if __name__ == "__main__":
    sys.exit(main())
