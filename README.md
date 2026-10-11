<div align="center">
  <img src="files.ico" width="128" height="128" alt="Fast File Search Pro Icon">
  
  # Fast File Search Pro

  [![Language](https://img.shields.io/badge/language-Python-blue)](#)
  [![GUI](https://img.shields.io/badge/GUI-PySide6-brightgreen)](#)
  [![License](https://img.shields.io/badge/license-MIT-green)](#)
  [![Builder](https://img.shields.io/badge/build-uv-blueviolet)](#)

  A high-performance desktop file search utility built with Python and PySide6 (Qt 6).
</div>

---

## What it is

Fast File Search Pro is a local search application that indexes your directories into a SQLite database. It uses SQL queries, trigram matching, and directory monitoring to provide search results as you type.

### Features
* **SQL Trigram Search**: Searches are backed by a persistent SQLite trigram index.
* **Complex Queries**: Supports boolean operators (`AND`, `OR`, `NOT`), file extension filters (`ext:py`), and size ranges (`size:>10mb` or `size:1kb..50kb`).
* **Real-time Monitoring**: Integrates `watchdog` to detect file creations, deletions, and modifications in indexed directories.
* **Responsive GUI**: Uses background thread execution for searches and indexing, keeping the user interface active during large scans.
* **Typing Autocomplete**: Provides fast prefix suggestions in the search bar.
* **Preview Panel**: Displays contents of text and image files, with metadata and background MD5/SHA256 hash calculation.
* **Theme Support**: Includes dark and light mode stylesheet options.

---

## Download

Prebuilt, self-contained builds (Python and Qt bundled) for every release are on the
[Releases page](https://github.com/Abdullah-Masood-05/Fast-file-search-tool/releases/latest).
Replace `<version>` below with the release version (for example `1.3.0`).

| Platform | File | Install |
|---|---|---|
| Windows 10/11 (x64) | `Fast-File-Search-Pro-v<version>-Windows-x64-Setup.exe` | Run the installer |
| Windows, portable | `...-Windows-x64-Portable.exe` or `...-Windows-x64.zip` | Run directly / unzip and run `FastFileSearchPro.exe` |
| macOS (Apple Silicon) | `...-macOS-arm64.dmg` | Open, drag the app to Applications |
| macOS (Intel) | `...-macOS-x86_64.dmg` | Open, drag the app to Applications |
| Debian / Ubuntu | `...-Linux-x86_64.deb` | `sudo apt install ./Fast-File-Search-Pro-v<version>-Linux-x86_64.deb` |
| Fedora | `...-Linux-x86_64.rpm` | `sudo dnf install ./Fast-File-Search-Pro-v<version>-Linux-x86_64.rpm` |
| Arch / Manjaro | `...-Linux-x86_64.pkg.tar.zst` | `sudo pacman -U Fast-File-Search-Pro-v<version>-Linux-x86_64.pkg.tar.zst` |
| Other Linux | `...-Linux-x86_64.tar.gz` | Extract, run `./fast-file-search-pro` |

The Linux builds need glibc 2.35 or newer (Ubuntu 22.04+, Debian 12+, Fedora 36+, current Arch).
Packages install to `/opt/fast-file-search-pro` with a `fast-file-search-pro` command and a menu entry.

**Unsigned builds.** On Windows, SmartScreen may say "Windows protected your PC": click
**More info → Run anyway**. On macOS the app is ad-hoc signed but not notarized: the first time,
**right-click the app → Open → Open** (macOS 15+: *System Settings → Privacy & Security → Open Anyway*).

---

## Installation & Setup

### Prerequisites
* Python 3.10 or later
* [uv](https://github.com/astral-sh/uv) (recommended package coordinator)

### Run Locally

1. **Clone the repository**
   ```bash
   git clone https://github.com/Abdullah-Masood-05/Fast-file-search-tool.git
   cd Fast-file-search-tool
   ```

2. **Sync dependencies**
   ```bash
   uv sync
   ```

3. **Start the application**
   ```bash
   uv run python main.py
   ```

---

## Running Tests

Run the test suite with `pytest`:
```bash
uv run pytest tests/ -v
```

---

## Building from Source

`build.py` drives [Nuitka](https://nuitka.net) on all three platforms (all flags, version
info and icons in one place; the version comes from `pyproject.toml`):

```bash
uv sync
uv run python build.py                  # standalone folder: dist/FastFileSearchPro/ (macOS: dist/Fast File Search Pro.app)
uv run python build.py --mode onefile   # single file (Windows/Linux)
uv run python build.py --dry-run        # print the Nuitka command only
```

Requirements: a C compiler (Windows: Visual Studio Build Tools with the C++ x64 workload;
Linux: `gcc` and `patchelf`; macOS: Xcode command line tools).

On Windows, `.\build.ps1` wraps `build.py` with a compiler fallback (MSVC → MinGW64 → Docker)
and `.\build.ps1 -Installer` also builds the Inno Setup installer from `packaging\windows\setup.iss`.

Platform packages (see `packaging/`):

```bash
bash packaging/linux/build-packages.sh   # .deb, .rpm (nfpm) and .tar.gz from dist/FastFileSearchPro
docker run --rm -v "$PWD:/workspace" -w /workspace archlinux:base-devel \
  bash packaging/arch/build-arch.sh     # .pkg.tar.zst (run after build-packages.sh)
bash packaging/macos/make-dmg.sh         # .dmg from the .app (macOS)
```

The [CI / CD workflow](.github/workflows/ci.yml) runs the tests on Windows, macOS and Linux, builds
every package, and publishes a GitHub release when a `v<version>` tag is pushed (the tag must match
the version in `pyproject.toml`):

```bash
git tag v1.3.0 && git push origin v1.3.0
```

---

## License

This project is licensed under the MIT License.
