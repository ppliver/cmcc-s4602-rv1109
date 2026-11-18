#!/usr/bin/env python3
"""一次性修链脚本：docs/ 下的 md 已分目录(openipc/vendor/shared)，
把文档间相对链接 + 外部相对链接(../tools, ../board, evidence/...)按旧上下文
docs/ 解析出真实目标位置后，按文档新位置重算相对路径。
用法: python tools/fix_docs_links.py            (dry-run, 打印将改写项)
       python tools/fix_docs_links.py --apply    (实际写入)
"""
import os, re, sys, glob

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
link_re = re.compile(r'\]\(([^)]+)\)')

# index of current .md locations by basename
md_index = {}
for p in glob.glob(os.path.join(DOCS, "**", "*.md"), recursive=True):
    md_index[os.path.basename(p)] = p


def resolve_target(link):
    """Given a link as written in old docs/ context, return current abs path or None."""
    core = link.split('#', 1)[0]
    if not core:
        return None
    if core.lower().endswith('.md'):
        base = core.split('/')[-1]
        return md_index.get(base)
    # non-md: old context was docs/  -> docs/<core>
    cand = os.path.normpath(os.path.join(DOCS, core))
    if os.path.exists(cand):
        return cand
    return None


def relpath_from(newdoc, target):
    return os.path.relpath(target, os.path.dirname(newdoc)).replace(os.sep, '/')


def main(apply):
    counter = {'n': 0}
    for p in glob.glob(os.path.join(DOCS, "**", "*.md"), recursive=True):
        with open(p, encoding='utf-8') as f:
            txt = f.read()
        orig = txt

        def repl(m, _p=p, _c=counter):
            whole = m.group(0)
            link = m.group(1)
            if link.startswith(('http://', 'https://', 'mailto:')):
                return whole
            if link.startswith('/'):
                # absolute path inside repo, leave as-is
                return whole
            tgt = resolve_target(link)
            if tgt is None:
                return whole
            new = relpath_from(_p, tgt)
            core = link.split('#', 1)[0]
            if core == new:
                return whole
            _c['n'] += 1
            anchor = link.split('#', 1)[1] if '#' in link else ''
            return '](' + new + (('#' + anchor) if anchor else '') + ')'

        txt2 = link_re.sub(repl, orig)
        if txt2 != orig:
            if apply:
                with open(p, 'w', encoding='utf-8') as f:
                    f.write(txt2)
            print(f"[{'WRITE' if apply else 'DRY  '}] {os.path.relpath(p, ROOT)}")

    print(f"\ntotal link rewrites: {counter['n']} (apply={apply})")
    if not apply:
        for p in glob.glob(os.path.join(DOCS, "**", "*.md"), recursive=True):
            with open(p, encoding='utf-8') as f:
                txt = f.read()
            for m in link_re.finditer(txt):
                link = m.group(1)
                if link.startswith(('http://', 'https://', 'mailto:', '/')):
                    continue
                if resolve_target(link) is None:
                    print(f"  UNRESOLVED in {os.path.relpath(p, ROOT)}: {link}")


if __name__ == '__main__':
    main(apply = '--apply' in sys.argv)
