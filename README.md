# DuplicateFileFinder

Tools for finding and managing duplicate files on Windows with a simple local web UI.

## What’s included

- **DuplicateFileSizeFinder.py** — Finds *exact duplicates* by grouping on file size and then verifying with SHA-256.  
  Shows a results page sorted by potential space savings. Files can be opened in Explorer or safely moved to the Recycle Bin.

- **DuplicateFileName.py** — Finds duplicates by *name*.  
  If a filename starts with a timestamp in the form `eejjmmdd` (optionally followed by `-mmss` / `_mmss`), the text **after** that timestamp is treated as the comparison name.  
  You can compare **with** or **without** the file extension.

Both apps:
- Start a local Flask server and open your browser with a waiting screen and progress bars.
- Exclude system folders and OneDrive folders by default.
- Skip OneDrive *Files-On-Demand* placeholders (to avoid auto-downloads), unless you opt in.
- Move deletions to the **Recycle Bin** (via `send2trash`) — no permanent deletion.

---

## Requirements

```bash
pip install -r requirements.txt
