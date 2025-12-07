# Fast File Search Tool

High-performance file search using IR techniques. Built with Python standard library and PyQt6.

## Features
- Recursively index folders; build in-memory inverted index
- Metadata: path, name, extension, size, modified-time
- Real-time, case-insensitive substring search
- Ranking: exact > starts-with > contains
- File type filters (extensions)
- Limits results to 100 for responsiveness
- Progress and timing metrics

## Requirements
- Python 3.8+
- PyQt6

## Run

```bash
pip install PyQt6
```

```bash
python3 main.py
```

## Usage
- Click "Select Folder" and choose a directory to index.
- Start typing in the search box; results update live.
- Toggle extension filters to refine results.
- Double-click a result to open it in the default app.

## Notes
- Handles permission errors by skipping unreadable files.
- Index built in memory for speed; no persistence (optional future work).


## Performance Tips
- Indexing shows progress every 1000 files.
- Search limited to 100 results to avoid GUI stalls.
- Lowercased keys computed once during indexing.

## Future Enhancements
- Persist index to disk
- Incremental updates (FS watchers)
- Wildcards and boolean queries
- Icons, more filters, open containing folder
