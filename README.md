<div align="center">
  <img src="files.ico" width="128" height="128" alt="Fast File Search Pro Icon">
  
  # Fast File Search Pro

  [![Language](https://img.shields.io/badge/language-Python-blue)](#)
  [![GUI](https://img.shields.io/badge/GUI-PyQt6-brightgreen)](#)
  [![License](https://img.shields.io/badge/license-MIT-green)](#)
  [![Builder](https://img.shields.io/badge/build-uv-blueviolet)](#)

  A high-performance desktop file search utility built with Python and PyQt6.
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

## Installation & Setup

### Prerequisites
* Python 3.10 or later
* [uv](https://github.com/astral-sh/uv) (recommended package coordinator)

### Run Locally

1. **Clone the repository**
   ```bash
   git clone https://github.com/yourusername/fast-file-search-pro.git
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

## Packaging as an Executable

To compile the application into a standalone executable using `Nuitka`:

```bash
uv run nuitka main.py \
  --standalone \
  --plugin-enable=pyqt6 \
  --windows-console-mode=disable \
  --windows-icon-from-ico=files.ico
```

---

## License

This project is licensed under the MIT License.
