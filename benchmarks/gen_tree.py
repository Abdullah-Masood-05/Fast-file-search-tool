#!/usr/bin/env python
"""Deterministic synthetic file tree for the benchmarks and the e2e tests.

    python benchmarks/gen_tree.py ROOT --files 300000          # ~320k entries (files + folders)
    python benchmarks/gen_tree.py ROOT --files 300000 --scale 3  # 3x: ~1M entries

The mix imitates a developer's disk: node_modules-like package folders, unicode names,
many extensions, a few big files (truncated to 2 KB of real data so the tree stays small).
Also importable: ``generate(root, files, dirs, seed)`` returns (files, dirs) created.
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import time

LIB = ["node_modules", "src", "lib", "components", "assets", "icons", "utils", "docs", "build", "dist", "test",
       "pkg", "vendor", "images", "Library", "Caches"]
PKGS = ["react", "lodash", "webpack", "babel", "eslint", "axios", "moment", "express", "vue", "jquery", "tslib",
        "rxjs", "core-js", "@types", "chalk", "debug"]
STEMS = ["index", "main", "GiftTopIcon", "GiftBottomIcon", "UserProfile", "README", "package", "config", "utils",
         "helpers", "test", "spec", "component", "button", "Café", "résumé", "日本語ファイル", "über", "naïve",
         "report", "invoice", "photo", "image", "icon", "bundle", "chunk", "vendor", "license", "changelog",
         "types", "server", "client", "handler", "model", "view", "store", "reducer", "action"]
EXTS = [".js", ".ts", ".json", ".md", ".png", ".jpg", ".txt", ".css", ".html", ".py", ".map", ".d.ts", ".svg",
        ".pdf", ".docx", ".xlsx", ".csv", ".log", ".woff2", ".min.js", ""]
WEIGHTS = [30, 12, 8, 6, 6, 3, 2, 4, 3, 3, 5, 5, 3, 2, 1, 1, 1, 2, 1, 2, 1]


def generate(root: str, nfiles: int, ndirs: int, seed: int = 7, quiet: bool = True):
    rnd = random.Random(seed)
    t0 = time.time()
    root = os.path.abspath(root)
    base_depth = root.count(os.sep)
    dirs = [root]
    seen = {root}
    os.makedirs(root, exist_ok=True)
    while len(dirs) < ndirs:
        p = rnd.choice(dirs[-3000:] if rnd.random() < 0.8 else dirs)
        if p.count(os.sep) > 24 + base_depth:
            p = root
        name = rnd.choice(PKGS + LIB) + (str(rnd.randint(0, 99)) if rnd.random() < 0.6 else "")
        d = os.path.join(p, name)
        if d in seen:
            continue
        seen.add(d)
        dirs.append(d)
    for d in dirs:
        os.makedirs(d, exist_ok=True)
    if not quiet:
        print("dirs", len(dirs), round(time.time() - t0, 1), flush=True)
    n = 0
    now = time.time()
    while n < nfiles:
        d = rnd.choice(dirs)
        for _ in range(rnd.randint(1, 30)):
            stem = rnd.choice(STEMS)
            if rnd.random() < 0.5:
                stem += str(rnd.randint(0, 999))
            fp = os.path.join(d, stem + rnd.choices(EXTS, WEIGHTS)[0])
            try:
                with open(fp, "wb") as f:
                    sz = int(rnd.expovariate(1 / 3000)) if rnd.random() < 0.98 else rnd.randint(10 ** 6, 5 * 10 ** 7)
                    f.write(b"x" * min(sz, 2000))
                m = now - rnd.random() * 3 * 365 * 86400
                os.utime(fp, (m, m))
                n += 1
            except OSError:
                pass
            if n >= nfiles:
                break
        if not quiet and n % 20000 < 30:
            print(n, round(time.time() - t0, 1), flush=True)
    return n, len(dirs)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", nargs="?", default=os.path.join(os.environ.get("TEMP", "/tmp"), "ffs_diag_tree"))
    ap.add_argument("--files", type=int, default=300_000)
    ap.add_argument("--dirs", type=int, default=0, help="default: files / 15")
    ap.add_argument("--scale", type=int, default=1, help="multiplies --files and --dirs (3 = about 1M entries)")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args(argv)
    files = a.files * a.scale
    dirs = (a.dirs or max(10, a.files // 15)) * a.scale
    t = time.time()
    n, d = generate(a.root, files, dirs, a.seed, quiet=False)
    print(f"files {n} dirs {d} entries {n + d} in {time.time() - t:.1f}s -> {a.root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
