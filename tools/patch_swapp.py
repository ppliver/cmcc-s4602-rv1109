#!/usr/bin/env python3
"""Regenerate swapp_patched from the runtime vendor swapp (byte-deterministic).

Base binary: upgrade/factory/swapp_runtime.bin
  = device /system/bin/swapp (3,646,428 B, md5 2b7dbb1570853cd8dcb3f80765f83f88)
  NOTE: this is the *runtime* swapp pulled from the device, NOT the 630 KB
  /system/bin/swapp inside the factory squashfs (that one is a different,
  smaller binary).

3 ARM word patches (file offset = vaddr - 0x10000), from 2026-11-17 session.
Hex values are the raw little-endian bytes as stored in the file:
  0xA804C: 0a00a0e1 -> 0000a0e3   (e1a0000a mov r0,sl -> e3a00000 mov r0,#0, force linear)
  0xA7FD8: 0120a011 -> 0000a0e1   (11a02001 movne r2,r1 -> e1a00000 nop, fps cap always 25)
  0x882AC: 03c0a011 -> 0000a0e1   (11a0c003 movne ip,r3 -> e1a00000 nop, SetFrameRate ignores HDR limit)

Reference output (device-verified swapp_patched): md5 488b51c2f6d0cc92d86f2122acde65ca
The patch chain is byte-deterministic, so the rebuild MUST match that md5.

Output: upgrade/s4602_upgrade/base/swapp_patched.xz (xz; container bytes may
differ from the device-side xz, decompressed content is asserted identical).
"""
import argparse
import hashlib
import lzma
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "upgrade" / "factory" / "swapp_runtime.bin"
OUT = ROOT / "upgrade" / "s4602_upgrade" / "base" / "swapp_patched.xz"
REF_XZ = ROOT / ".stage_http" / "swapp_patched.xz"  # optional local reference

BASE_MD5 = "2b7dbb1570853cd8dcb3f80765f83f88"
PATCHES = [
    (0xA804C, "0a00a0e1", "0000a0e3"),
    (0xA7FD8, "0120a011", "0000a0e1"),
    (0x882AC, "03c0a011", "0000a0e1"),
]
EXPECT_MD5 = "488b51c2f6d0cc92d86f2122acde65ca"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true",
                    help="write regenerated xz over upgrade/.../swapp_patched.xz "
                         "(default: verify only)")
    args = ap.parse_args()
    d = bytearray(BASE.read_bytes())
    md5 = hashlib.md5(bytes(d)).hexdigest()
    print(f"base: {BASE} {len(d)}B md5={md5}")
    if md5 != BASE_MD5:
        print("FATAL: base binary md5 mismatch - wrong source", file=sys.stderr)
        return 1
    for off, old, new in PATCHES:
        cur = bytes(d[off:off + 4]).hex()
        if cur != old:
            print(f"FATAL: {off:#x}: found {cur}, expected {old}", file=sys.stderr)
            return 1
        d[off:off + 4] = bytes.fromhex(new)
        print(f"patch {off:#x}: {old} -> {new}")
    md5 = hashlib.md5(bytes(d)).hexdigest()
    if md5 != EXPECT_MD5:
        print(f"FATAL: patched md5 {md5} != reference {EXPECT_MD5}", file=sys.stderr)
        return 1
    xz = lzma.compress(bytes(d), format=lzma.FORMAT_XZ,
                       preset=9 | lzma.PRESET_EXTREME)
    # decompress-verify: content must equal the patched bytes
    if hashlib.md5(lzma.decompress(xz)).hexdigest() != EXPECT_MD5:
        print("FATAL: xz round-trip mismatch", file=sys.stderr)
        return 1
    OUT.write_bytes(xz) if args.write else None
    print(f"{'WROTE' if args.write else 'verified'} -> {OUT} "
          f"({len(xz)}B, content md5={md5})")
    if REF_XZ.is_file():
        ref = hashlib.md5(lzma.decompress(REF_XZ.read_bytes())).hexdigest()
        print(f"device reference xz content md5={ref} -> "
              + ("IDENTICAL" if ref == md5 else "MISMATCH"))
        if ref != md5:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
