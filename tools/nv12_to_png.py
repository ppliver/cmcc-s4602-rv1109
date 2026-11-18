#!/usr/bin/env python3
"""Convert a dumped h264out NV12 frame to PNG (MPP-aligned stride aware).

Usage: python tools/nv12_to_png.py out/captures/foo.nv12 [prefix]

The frame is g_hs x g_vs = 768 x 512 with 640x360 active pixels; Y stride is
768 bytes and the UV plane is (VS/2) x (HS/2) interleaved U,V pairs.
"""
import os, sys
import numpy as np
from PIL import Image

path = sys.argv[1]
prefix = sys.argv[2] if len(sys.argv) > 2 else os.path.splitext(path)[0]
W, H, HS, VS = 640, 360, 768, 512
raw = np.fromfile(path, dtype=np.uint8)[: HS * VS * 3 // 2]
y = raw[: HS * VS].reshape(VS, HS)[:H, :W]
uv = raw[HS * VS:].reshape(VS // 2, HS // 2, 2)[:H // 2, :W // 2, :]
U = np.repeat(np.repeat(uv[..., 0], 2, 0), 2, 1).astype(np.int16) - 128
V = np.repeat(np.repeat(uv[..., 1], 2, 0), 2, 1).astype(np.int16) - 128
Y = y.astype(np.int16)
r = np.clip(Y + 1.402 * V, 0, 255).astype(np.uint8)
g = np.clip(Y - 0.344 * U - 0.714 * V, 0, 255).astype(np.uint8)
b = np.clip(Y + 1.772 * U, 0, 255).astype(np.uint8)
Image.fromarray(np.dstack([r, g, b])).save(prefix + "_rgb.png")
Image.fromarray(y).save(prefix + "_y.png")
print("Y=%.1f U=%.1f V=%.1f -> %s_rgb.png / %s_y.png"
      % (y.mean(), uv[..., 0].mean(), uv[..., 1].mean(), prefix, prefix))
