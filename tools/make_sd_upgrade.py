#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S4602 SD 升级固件打包器。

输入  upgrade/s4602_upgrade/   （base/ optional/ flash/system_b.img VERSION）
输出  out/s4602_sd_upgrade/    （SD 卡 FAT32 根目录的直接内容）
        ├── system_b.img           <- U-Boot fatload 用（必须在 FAT 根）
        └── s4602_upgrade/
            ├── VERSION
            ├── MANIFEST.sizes     <- 设备端 bootfix 阶段0 尺寸门控（busybox 无 md5sum）
            ├── MANIFEST.md5       <- 主机侧参考
            ├── README.txt
            ├── base/   ...        <- 基础增强: UDP 推流 + WEB 后台 + 自愈
            └── optional/...       <- 可选模块: go2rtc + easytier(TUN)

用法: python tools/make_sd_upgrade.py           # 打包
      python tools/make_sd_upgrade.py --verify  # 打包后自校验
"""
import hashlib
import shutil
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "upgrade" / "s4602_upgrade"
OUT = ROOT / "out" / "s4602_sd_upgrade"

BASE = ["bootfix.sh", "webd", "index.html", "h264out_v4", "stream.conf",
        "rtlib.tar.gz", "swapp_patched.xz", "np_resetpasswd.sh", "shadow.md5",
        "modules.conf"]
OPTIONAL = ["go2rtc.xz", "go2rtc.yaml", "rtp2pipe", "tun.ko",
            "easytier-mini", "easytier_ltecar.toml"]


def md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def main() -> int:
    errors = []
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "s4602_upgrade").mkdir(parents=True)
    shutil.copy2(SRC / "VERSION", OUT / "s4602_upgrade" / "VERSION")

    sizes, md5s = [], []
    for sub, names in (("base", BASE), ("optional", OPTIONAL)):
        (OUT / "s4602_upgrade" / sub).mkdir()
        for n in names:
            s = SRC / sub / n
            if not s.is_file():
                errors.append(f"missing source: {sub}/{n}")
                continue
            d = OUT / "s4602_upgrade" / sub / n
            shutil.copy2(s, d)
            sizes.append(f"{sub}/{n} {d.stat().st_size}")
            md5s.append(f"{md5(d)}  {sub}/{n}")

    # flash image -> 包内 flash/ + SD 根（U-Boot fatload 必须在 FAT 根）
    img = SRC / "flash" / "system_b.img"
    if not img.is_file():
        errors.append("missing hook image: flash/system_b.img")
    else:
        (OUT / "s4602_upgrade" / "flash").mkdir()
        shutil.copy2(img, OUT / "s4602_upgrade" / "flash" / "system_b.img")
        shutil.copy2(img, OUT / "system_b.img")
        data = img.read_bytes()
        md5s.append(f"{md5(img)}  flash/system_b.img")
        sizes.append(f"flash/system_b.img {len(data)}")
        if len(data) != 46137344:
            errors.append(f"image size {len(data)} != 46137344")
        if zlib.crc32(data) & 0xFFFFFFFF != 0x45BCC5E9:
            errors.append("image crc32 mismatch (expected 0x45bcc5e9)")
        if not all(b == 0xFF for b in data[-4096:]):
            errors.append("image tail is not 0xFF")

    (OUT / "s4602_upgrade" / "MANIFEST.sizes").write_text(
        "\n".join(sizes) + "\n", encoding="utf-8", newline="\n")
    (OUT / "s4602_upgrade" / "MANIFEST.md5").write_text(
        "\n".join(md5s) + "\n", encoding="utf-8", newline="\n")

    readme = ROOT / "upgrade" / "SD_README.txt"
    if readme.is_file():
        shutil.copy2(readme, OUT / "s4602_upgrade" / "README.txt")

    total = sum(int(x.split()[-1]) for x in sizes)
    print(f"packed -> {OUT}")
    print(f"files  : {len(sizes)}  total {total/1e6:.1f} MB")
    if errors:
        print("ERRORS:")
        for e in errors:
            print("  -", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
