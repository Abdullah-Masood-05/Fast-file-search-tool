#!/usr/bin/env python3
"""Cross-platform Nuitka build driver for Fast File Search Pro (stdlib only).

Run it with the interpreter that has the project's dependencies (PyQt6, watchdog,
nuitka) installed, e.g. ``uv run python build.py``.

Outputs (under --output-dir, default ``dist``):

  Windows  standalone  dist/FastFileSearchPro/FastFileSearchPro.exe
           onefile     dist/FastFileSearchPro.exe
  Linux    standalone  dist/FastFileSearchPro/fast-file-search-pro
           onefile     dist/fast-file-search-pro
  macOS    standalone  "dist/Fast File Search Pro.app" (ad-hoc signed app bundle)

``files.ico`` is always shipped next to the executable (Contents/MacOS on macOS)
because ``fast_file_search.ui.widgets.find_resource`` looks there, and all non-code
files inside the ``fast_file_search`` package (stylesheets, icons, ...) are included.
"""

from __future__ import annotations

import argparse
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENTRY = ROOT / "main.py"
SRC = ROOT / "src"
PACKAGE = "fast_file_search"
ICO = ROOT / "files.ico"
ICONS = ROOT / "packaging" / "icons"
ICNS = ICONS / "fast-file-search-pro.icns"
PNG = ICONS / "fast-file-search-pro-256.png"

APP_NAME = "Fast File Search Pro"
COMPANY = "Abdullah Masood"
BUNDLE_ID = "com.abdullahmasood.fastfilesearchpro"
DIST_DIR_NAME = "FastFileSearchPro"  # standalone folder name (all platforms)

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def read_version() -> str:
    """Return [project].version from pyproject.toml."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    try:
        import tomllib  # Python 3.11+

        return str(tomllib.loads(text)["project"]["version"])
    except ModuleNotFoundError:
        section = re.search(r"^\[project\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S)
        match = section and re.search(r'^version\s*=\s*"([^"]+)"', section.group(1), re.M)
        if not match:
            raise SystemExit("could not read [project].version from pyproject.toml")
        return match.group(1)


def numeric_version(version: str) -> str:
    """'1.2.0rc1' -> '1.2.0.0' (Windows/macOS version fields accept digits only)."""
    parts = [re.match(r"\d+", p) for p in version.split(".")[:4]]
    nums = [m.group(0) if m else "0" for m in parts]
    return ".".join((nums + ["0"] * 4)[:4])


def exe_name(mode: str) -> str:
    if IS_WINDOWS:
        return f"{DIST_DIR_NAME}.exe"
    if IS_LINUX:
        return "fast-file-search-pro"
    return DIST_DIR_NAME  # binary inside the macOS bundle / plain onefile binary


def find_msvc() -> bool:
    """True if Visual Studio with the x64 C++ toolset is installed (via vswhere)."""
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    vswhere = Path(pf86) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vswhere.is_file():
        return False
    try:
        out = subprocess.run(
            [str(vswhere), "-latest", "-products", "*", "-requires",
             "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
    except OSError:
        return False
    return bool(out)


def compiler_flags(compiler: str) -> list[str]:
    if compiler == "msvc":
        return ["--msvc=latest"]
    if compiler == "mingw":
        return ["--mingw64"]
    if compiler == "clang":
        return ["--clang"]
    if compiler == "auto" and IS_WINDOWS and find_msvc():
        return ["--msvc=latest"]
    return []  # let Nuitka pick (gcc/clang on Linux/macOS)


def nuitka_args(args: argparse.Namespace, version: str) -> list[str]:
    num = numeric_version(version)
    out = Path(args.output_dir).resolve()
    a = [
        sys.executable, "-m", "nuitka",
        "--standalone" if args.mode == "standalone" else "--onefile",
        "--enable-plugin=pyqt6",
        f"--include-package={PACKAGE}",
        f"--include-package-data={PACKAGE}",
        f"--include-data-files={ICO}=files.ico",
        f"--output-dir={out}",
        f"--output-filename={exe_name(args.mode)}",
        f"--company-name={COMPANY}",
        f"--product-name={APP_NAME}",
        f"--file-version={num}",
        f"--product-version={num}",
        f"--file-description={APP_NAME}",
        f"--copyright=Copyright (c) {time.gmtime().tm_year} {COMPANY}. MIT License.",
        "--assume-yes-for-downloads",
        f"--jobs={args.jobs}",
    ]
    if IS_WINDOWS:
        a += ["--windows-console-mode=disable", f"--windows-icon-from-ico={ICO}"]
    elif IS_MACOS:
        a += [
            "--macos-create-app-bundle",
            f"--macos-app-icon={ICNS}",
            f"--macos-app-name={APP_NAME}",
            f"--macos-app-version={version}",
            f"--macos-signed-app-name={BUNDLE_ID}",
            "--macos-app-mode=gui",
        ]
    elif IS_LINUX:
        a += [f"--linux-icon={PNG}"]
    a += compiler_flags(args.compiler)
    if not args.keep_build_dir:
        a.append("--remove-output")
    a += args.nuitka_extra
    a.append(str(ENTRY))
    return a


def _rm(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def fix_macos_bundle(app: Path, version: str) -> None:
    """Set bundle metadata explicitly and re-sign ad hoc (any plist edit breaks a signature)."""
    plist_path = app / "Contents" / "Info.plist"
    with plist_path.open("rb") as fh:
        info = plistlib.load(fh)
    info.update({
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.utilities",
        "NSHumanReadableCopyright": f"Copyright (c) {COMPANY}. MIT License.",
    })
    with plist_path.open("wb") as fh:
        plistlib.dump(info, fh)
    # Keep a copy of the icon in Resources too (find_resource also checks there).
    resources = app / "Contents" / "Resources"
    resources.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ICO, resources / "files.ico")
    if shutil.which("codesign"):
        subprocess.run(["codesign", "--force", "--deep", "-s", "-", str(app)], check=True)
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    else:
        print("warning: codesign not found, bundle left unsigned", file=sys.stderr)


def publish(args: argparse.Namespace, version: str) -> Path:
    """Move Nuitka's output to its stable location and return it."""
    out = Path(args.output_dir).resolve()
    stem = ENTRY.stem
    if IS_MACOS:
        final = out / f"{APP_NAME}.app"
        candidates = [out / f"{stem}.app", out / f"{exe_name(args.mode)}.app"]
        built = next((c for c in candidates if c.is_dir()), None)
        if built is None:
            raise SystemExit(f"expected app bundle not found: {candidates}")
        _rm(final)
        built.rename(final)
        fix_macos_bundle(final, version)
        return final
    if args.mode == "onefile":
        final = out / exe_name(args.mode)
        if not final.is_file():
            raise SystemExit(f"expected onefile binary not found: {final}")
        return final
    built = out / f"{stem}.dist"
    final = out / DIST_DIR_NAME
    if not (built / exe_name(args.mode)).is_file():
        raise SystemExit(f"expected {exe_name(args.mode)} not found in {built}")
    _rm(final)
    built.rename(final)
    # --include-data-files already places files.ico here; make sure regardless.
    if not (final / "files.ico").is_file():
        shutil.copy2(ICO, final / "files.ico")
    return final


def clean_previous(args: argparse.Namespace) -> None:
    out = Path(args.output_dir).resolve()
    stem = ENTRY.stem
    for p in (out / f"{stem}.dist", out / f"{stem}.app", out / f"{exe_name(args.mode)}.app",
              out / f"{stem}.build", out / f"{stem}.onefile-build"):
        _rm(p)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build Fast File Search Pro with Nuitka for the current OS.",
        epilog="Anything after '--' is passed to Nuitka verbatim.",
    )
    p.add_argument("--mode", choices=("standalone", "onefile"), default="standalone",
                   help="standalone folder (default; an .app bundle on macOS) or single file")
    p.add_argument("--compiler", choices=("auto", "msvc", "mingw", "clang"), default="auto",
                   help="C compiler; 'auto' uses MSVC (latest) on Windows when installed, "
                        "otherwise Nuitka's default")
    p.add_argument("--output-dir", default=str(ROOT / "dist"), help="output directory (dist)")
    p.add_argument("--jobs", type=int, default=os.cpu_count() or 2, help="parallel C jobs")
    p.add_argument("--keep-build-dir", action="store_true", help="keep Nuitka's *.build dir")
    p.add_argument("--dry-run", action="store_true", help="print the Nuitka command and exit")
    p.add_argument("--print-version", action="store_true",
                   help="print the version from pyproject.toml and exit")
    argv = list(sys.argv[1:] if argv is None else argv)
    extra: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    args = p.parse_args(argv)
    args.nuitka_extra = extra
    if IS_MACOS and args.mode == "onefile":
        p.error("onefile is not supported on macOS; use the default standalone .app bundle")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    version = read_version()
    if args.print_version:
        print(version)
        return 0

    cmd = nuitka_args(args, version)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SRC), env.get("PYTHONPATH")]))
    print(f"Fast File Search Pro {version} | {platform.system()} {platform.machine()} | "
          f"Python {platform.python_version()} | mode={args.mode}")
    print(" ".join(f'"{c}"' if " " in c else c for c in cmd), flush=True)
    if args.dry_run:
        return 0

    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    clean_previous(args)
    started = time.monotonic()
    code = subprocess.call(cmd, env=env, cwd=ROOT)
    if code != 0:
        print(f"Nuitka failed with exit code {code}", file=sys.stderr)
        return code
    final = publish(args, version)
    print(f"OK ({time.monotonic() - started:.0f}s): {final}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
