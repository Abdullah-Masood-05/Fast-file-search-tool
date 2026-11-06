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

Fast File Search Pro is a local search application. It indexes the names of the files and folders you choose, keeps the index current while you work, and shows results as you type. A separate engine process owns the index and the search, so the window stays responsive during large scans.

### Features
* **Instant search**: results update as you type, with exact result counts, from an in-memory index backed by a SQLite file.
* **Query syntax**: `AND`, `OR`, `NOT`, quoted phrases, globs, `ext:py`, `size:>10mb`, `case:` and `re:`.
* **Filters**: type, extension, size, modified date, folder and favorites. Filters apply before ranking, so the counts are exact.
* **Live updates**: file changes in indexed folders are picked up through `watchdog`, with periodic safety rescans.
* **Preview panel**: shows text, code and images, with metadata and on-demand hashes.
* **Light and dark themes**.

---

## Download

Prebuilt, self-contained builds (Python and Qt bundled) for every release are on the
[Releases page](https://github.com/Abdullah-Masood-05/Fast-file-search-tool/releases/latest).
Replace `<version>` below with the release version (for example `2.0.0`).

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

## Privacy

* The app makes no network calls and sends no telemetry or crash reports.
* The index stores file and folder names, sizes and modification times. It does not store or read file contents. The preview panel reads a file only when you select it.
* Everything stays on your computer: the index and your favorites, tags and notes in the per-user cache and data folders, and the logs in the per-user log folder (macOS `~/Library/Logs/Fast File Search Pro`, Windows `%LOCALAPPDATA%\Fast File Search Pro\Logs`, Linux `~/.local/state/fast-file-search-pro/logs`).
* Logs contain file paths only in error messages, or when `FFS_DEBUG=1` is set.
* Deleting those folders removes everything the app has stored.

## Limitations

* File contents are not searched, only names and paths.
* Only the folders you add are indexed. On first run these are the folders that exist among Desktop, Documents, Downloads, Pictures, Music and Videos (Movies on macOS), not your whole home folder. Library, AppData and cache folders inside your home folder are skipped unless you turn them on.
* On macOS, each protected folder (Desktop, Documents, Downloads, removable and network volumes) triggers one system permission prompt. If you deny it, that folder is not indexed until you allow it in System Settings, Privacy & Security. Granting Full Disk Access covers everything with one permission. The app is ad-hoc signed, so macOS may ask again after an update (see `packaging/macos/RELEASE-NOTES-permissions.md`).
* Network drives and removable volumes may be slow to index and may not report live changes. When live updates are limited, the status bar says so and the app rescans on a timer.
* On Linux, watching very large folders can exceed the inotify watch limit. The app then falls back to periodic rescans.
* Very large indexes use memory: about 165 bytes per entry in the engine process, or roughly 50 MB per 300,000 entries.
* Builds are not code-signed or notarized (see Download).

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
git tag v2.0.0 && git push origin v2.0.0
```

---

## License

This project is licensed under the MIT License.
