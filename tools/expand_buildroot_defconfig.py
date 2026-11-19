#!/usr/bin/env python3
"""Expand Firefly buildroot defconfig `#include` directives into a flat
buildroot defconfig.

Firefly's buildroot defconfigs use a custom `#include "xxx.config"` mechanism
resolved by their SDK build tooling (device/rockchip build/*.sh). Stock
buildroot `make <defconfig>` does NOT understand these, so for CI we expand
them ourselves before `make olddefconfig`.

Resolution order for each include name (relative):
  1. configs/rockchip/<name>   (fragment library, e.g. base.config)
  2. configs/<name>            (full defconfigs, e.g. firefly_rv1126_rv1109_defconfig)
  3. directory of the including file

Kconfig semantics: last assignment wins, so overrides placed AFTER an
#include (in the wrapper defconfig) take precedence.

Some fragments cannot be neutralised by a trailing "# X is not set" comment:
Firefly's defconfig_hook.py re-emits every included fragment into
output/.rockchipconfig, where a "=y" from the fragment wins over our comment
(kconfig "override: reassigning to symbol"). For those, use --exclude to drop
the whole #include at expansion time. E.g. kernel.config selects the buildroot
kernel package (kernel source lives outside this rootfs-only CI job).

Usage:
  python3 expand_buildroot_defconfig.py <buildroot_dir> <in_name> <out_name> \
      [--exclude <fragment.basename> ...]
"""

import os
import sys

SEARCH_SUBDIRS = ("rockchip", "")


def find_fragment(br_dir, name, extra_dirs):
    cands = []
    for sub in SEARCH_SUBDIRS:
        cands.append(os.path.join(br_dir, "configs", sub, name))
    for d in extra_dirs:
        cands.append(os.path.join(d, name))
    for c in cands:
        if os.path.isfile(c):
            return c
    raise FileNotFoundError(
        "cannot resolve #include %r (searched: %s)" % (name, ", ".join(cands))
    )


def expand(br_dir, path, visited, out_lines, depth=0, exclude=()):
    path = os.path.abspath(path)
    if path in visited:
        raise RuntimeError(
            "include cycle at %s" % path
        )
    visited.add(path)
    base = os.path.dirname(path)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\n")
            s = line.strip()
            if s.startswith("#include"):
                inc = s[len("#include"):].strip().strip('"').strip("'")
                if not inc:
                    raise RuntimeError("empty #include in %s" % path)
                # Drop excluded fragments entirely (basename match). This is
                # stronger than a trailing "is not set" comment because the
                # SDK's defconfig_hook.py re-emits included "=y" lines into
                # .rockchipconfig, overriding kconfig comments downstream.
                if os.path.basename(inc) in exclude:
                    print("  " * depth + "[%s] -> SKIP excluded %s" % (
                        os.path.basename(path), inc))
                    continue
                frag = find_fragment(br_dir, inc, [base])
                print("  " * depth + "[%s] -> include %s (%s)" % (
                    os.path.basename(path), inc, os.path.relpath(frag, br_dir)))
                expand(br_dir, frag, visited, out_lines, depth + 1, exclude)
            else:
                out_lines.append(line)
    visited.discard(path)


def main():
    args = sys.argv[1:]
    exclude = set()
    while "--exclude" in args:
        i = args.index("--exclude")
        if i + 1 >= len(args):
            print("ERROR: --exclude needs a fragment basename")
            sys.exit(2)
        exclude.add(args[i + 1])
        del args[i:i + 2]
    if len(args) != 3:
        print(__doc__)
        sys.exit(2)
    br_dir, in_name, out_name = args
    br_dir = os.path.abspath(br_dir)
    # accept either a name resolvable inside buildroot/configs, or a direct
    # path to a wrapper defconfig living outside the buildroot tree (e.g.
    # a board dir of another repo); nested #include resolution is unchanged
    if os.path.isfile(in_name):
        in_path = os.path.abspath(in_name)
    else:
        in_path = find_fragment(br_dir, in_name, [])
    out_lines = []
    expand(br_dir, in_path, set(), out_lines, exclude=exclude)
    if exclude:
        print("excluded fragments: %s" % ", ".join(sorted(exclude)))

    out_path = os.path.join(br_dir, "configs", out_name)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines) + "\n")

    n = len([l for l in out_lines if l.strip() and not l.startswith("#")])
    print("expanded %s -> %s (%d lines, %d non-comment)" % (
        os.path.relpath(in_path, br_dir), out_name, len(out_lines), n))


if __name__ == "__main__":
    main()
