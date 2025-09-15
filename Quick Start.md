# Quick Start — DuplicateFileFinder

A minimal, copy‑paste friendly guide to install and run the tools.

---

## Requirements

- **Windows** (Explorer integration is Windows‑optimized)
- **Python 3.10+**
- `pip`

Install dependencies (from the project root):

```bash
pip install -r requirements.txt
```
or:
```bash
pip install flask send2trash
```

---

## Start the apps

> Both apps start a local Flask server and **open your browser** with a progress screen.
> Deletions go to the **Recycle Bin** (via `send2trash`).

### 1) Size‑based duplicate finder
Finds *exact* duplicates (same size + SHA‑256).

```bash
python DuplicateFileSizeFinder.py --root C:\ --min-size 10485760 --time-budget-min 60
```

**Key flags**

- `--root <PATH>` — start folder (default: `C:\`)
- `--min-size <BYTES>` — minimum size to consider (default: **10 MB** = `10485760`)
- `--time-budget-min <MINUTES>` — soft time budget (default **60**). If ETA exceeds the budget, the UI lets you continue or raise `--min-size` on the fly.
- `--no-excludes` — do **not** exclude system/OneDrive folders
- `--add-exclude "<PATH>"` — exclude an additional folder (repeatable)
- `--include-cloud` — include OneDrive *Files‑On‑Demand* placeholders (**may trigger downloads**)

### 2) Name‑based duplicate finder
Finds duplicates by *normalized name*. If a filename starts with a timestamp `eejjmmdd` (optionally followed by `-mmss`/`_mmss`), that leading timestamp is stripped before comparing.

```bash
python DuplicateFileName.py --root C:\ --ignore-ext --min-size 10485760 --time-budget-min 60
```

**Key flags**

- `--ignore-ext` *(default)* — compare names **without** extension
- `--keep-ext` — compare names **with** extension
- Other flags are the same as for the size‑based finder

**Name normalization examples**

- `250915_report-final.pdf` → `report final`
- `20250131-1201 my_file.TXT` → `my file`
- `2025-01-31__my.file.txt` → `my file`

---

## Tips

- Start with your **user folders** first, then expand to `C:\` if needed.
- Keep the default **10 MB** minimum to finish within an hour; raise to **20–50 MB** for very large trees.
- OneDrive placeholders are skipped by default; use `--include-cloud` only if you really need to scan those (it can download files).

---

## Troubleshooting

- **Permission errors**: try running the terminal/IDE as Administrator, or exclude restricted folders.
- **Browser didn’t open**: the UI is at `http://127.0.0.1:5000`.
- **Stop**: press `Ctrl+C` in the terminal or stop the Run/Debug session in PyCharm.
