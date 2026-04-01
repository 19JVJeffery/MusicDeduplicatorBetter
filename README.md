# Music Deduplicator

A Python tool that finds and removes duplicate audio files from your music library. It uses **audio fingerprinting** (via [AcoustID](https://acoustid.org/)) plus fuzzy metadata matching to identify duplicates — even when file names or tags differ.

It ships with an **interactive TUI** (terminal user interface) that guides you step-by-step through scanning, reviewing, and acting on duplicates, as well as a fully-featured **CLI** for scripting and automation.

---

## Features

- **Interactive TUI** — guided workflow with progress bars, live log, and duplicate review before any action is taken.
- **Audio fingerprinting** — identifies duplicates regardless of file name or metadata using AcoustID/Chromaprint.
- **Fuzzy metadata matching** — fast pre-screening via artist/title/album similarity.
- **Three actions**: list (safe), move, or delete duplicates.
- **Dry-run mode** — preview what would happen without touching any files.
- **Caching** — fingerprints and metadata are cached in a local SQLite database, making repeat scans much faster.
- **Multiprocessing** — optional parallel directory hashing (capped at 2 workers to respect API rate limits).
- **Batch processing** — configurable batch size prevents excessive memory use on large libraries.
- **Safety prompts** — destructive delete requires an explicit confirmation step.
- **Detailed logging** — full log written to `music_deduplicate.log`.

---

## Requirements

- **Python 3.8 or higher**
- **pip**
- **fpcalc** (Chromaprint) — for audio fingerprinting
- **ffmpeg** — required by Chromaprint on some systems
- An **AcoustID API key** (free) — for fingerprint lookups

---

## Installation

### 1 — Install system dependencies

**Debian / Ubuntu:**

```bash
sudo apt-get update
sudo apt-get install ffmpeg libchromaprint-tools
```

**macOS (Homebrew):**

```bash
brew install ffmpeg chromaprint
```

**Windows:**

1. Download and install [FFmpeg](https://ffmpeg.org/download.html).
2. Download [fpcalc](https://acoustid.org/chromaprint) and place it somewhere on your `PATH`.

Verify `fpcalc` is available:

```bash
fpcalc -version
```

### 2 — Clone the repository

```bash
git clone https://github.com/19JVJeffery/MusicDeduplicatorBetter.git
cd MusicDeduplicatorBetter
```

### 3 — Create a virtual environment and install Python dependencies

Using a virtual environment is **strongly recommended** to avoid conflicts with system-managed Python packages (especially on macOS with Homebrew or on Ubuntu 23.04+):

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

> **Note:** Remember to activate the virtual environment (`source .venv/bin/activate`) each time you open a new terminal before running the tool.

### 4 — Obtain an AcoustID API key

1. Register at <https://acoustid.org/api-key>.
2. Copy your key — you will be prompted for it the first time you run the tool, or you can add it to `config.json` manually (see *Configuration* below).

---

## Running the TUI (recommended)

Simply run the script with no arguments to launch the interactive terminal UI:

```bash
python3 musicorganise.py
```

Or launch the TUI directly:

```bash
python3 music_tui.py
```

The TUI walks you through:

1. **Setup** — choose your music directory, action, and options.
2. **Settings** — enter/update your AcoustID API key and thresholds.
3. **Scanning** — live progress bar and log while the library is fingerprinted.
4. **Review** — inspect every duplicate set before any action is taken.
5. **Processing** — apply the chosen action with real-time progress.
6. **Summary** — see how many files were processed and storage reclaimed.

### Keyboard shortcuts

| Key | Action |
|-----|--------|
| `q` | Quit |
| `Ctrl+C` | Quit |

---

## CLI Usage

Pass at least `--path` and `--action` to skip the TUI and run non-interactively:

```bash
python3 musicorganise.py --path "/path/to/music" --action ACTION [OPTIONS]
```

### Options

| Flag | Description |
|------|-------------|
| `-p, --path PATH` | **(Required)** Music directory to scan. |
| `-a, --action ACTION` | **(Required)** `list`, `move`, or `delete`. |
| `-m, --move-dir DIR` | Destination for moved files (required when `--action move`). |
| `-v, --verbose` | Show tqdm progress bars in the terminal. |
| `--log-level LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` (default: `INFO`). |
| `--no-multiprocessing` | Disable parallel hashing (useful for debugging). |
| `--dry-run` | Preview what would happen without modifying any files. |
| `--clear-cache` | Wipe the fingerprint cache before running. |
| `-y, --yes` | Skip the delete-confirmation prompt. |

### Examples

**List duplicates (safe, no changes):**

```bash
python3 musicorganise.py --path "/media/music" --action list --verbose
```

**Move duplicates to another folder:**

```bash
python3 musicorganise.py --path "/media/music" --action move --move-dir "/media/duplicates" --verbose
```

**Dry-run delete (preview only):**

```bash
python3 musicorganise.py --path "/media/music" --action delete --dry-run --verbose
```

**Delete duplicates (with confirmation prompt):**

```bash
python3 musicorganise.py --path "/media/music" --action delete --verbose
```

**Delete without confirmation (automation/scripting):**

```bash
python3 musicorganise.py --path "/media/music" --action delete --yes
```

---

## Configuration

Settings are stored in `config.json` in the script directory. The file is created automatically the first time you run the tool.

```json
{
    "acoustid_api_key": "YOUR_API_KEY_HERE",
    "fuzzy_threshold": 90,
    "batch_size": 1000,
    "supported_extensions": [".mp3", ".flac", ".ogg", ".wav", ".m4a", ".aac"]
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `acoustid_api_key` | — | Your AcoustID API key. |
| `fuzzy_threshold` | `90` | Metadata similarity score (0–100) above which files are considered potential duplicates. |
| `batch_size` | `1000` | Number of directories processed per batch. Reduce if you hit "too many open files" errors. |
| `supported_extensions` | see above | Audio formats to scan. |

> **Security note:** `config.json` is listed in `.gitignore` to prevent your API key from being committed.

---

## How It Works

1. **File scanning** — walks the specified directory tree and collects every supported audio file.
2. **Metadata extraction** — reads artist, title, album, track number, and file size using *mutagen*.
3. **Audio fingerprinting** — runs `fpcalc` to generate a Chromaprint fingerprint, then looks it up via the AcoustID API to obtain a stable recording ID.
4. **Directory hashing** — each directory's recording IDs (or metadata as a fallback) are concatenated and hashed with SHA-256. Directories with the same hash are flagged as duplicates.
5. **Duplicate resolution** — for each duplicate set the "best" copy is kept (FLAC preferred; otherwise largest total size). The other copies are listed, moved, or deleted. Intra-directory duplicates (same AcoustID within a single folder) are also resolved by keeping the highest-quality format.
6. **Caching** — results are stored in `file_cache.db` keyed by file path and mtime, so unchanged files are not re-fingerprinted on subsequent runs.

---

## Logging

- Logs are written to `music_deduplicate.log` in the script directory.
- The log level can be changed with `--log-level`.
- The TUI also shows a live in-app log during scanning and processing.

---

## Uninstalling / Removing

### Remove the tool

```bash
# If you cloned into a dedicated folder, just delete it:
rm -rf /path/to/MusicDeduplicatorBetter

# If you installed into a virtual environment:
deactivate
rm -rf .venv
```

### Remove generated files only

```bash
rm -f config.json file_cache.db music_deduplicate.log
```

### Uninstall Python dependencies

```bash
pip uninstall -r requirements.txt -y
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `fpcalc: command not found` | Install `libchromaprint-tools` (Linux) or `chromaprint` (macOS/Homebrew). |
| `AcoustID lookup failed` | Check your API key in `config.json`. Check your internet connection. |
| `Too many open files` | Reduce `batch_size` in `config.json`. |
| `No duplicates found` despite obvious duplicates | Ensure your files have valid metadata or that fpcalc can read them. Try `--log-level DEBUG`. |
| TUI does not start | Make sure `textual>=0.47.0` is installed: `pip install textual`. |
| Multiprocessing errors | Run with `--no-multiprocessing` or uncheck it in the TUI Settings. |
| `Cannot perform a --user install` error inside venv | Your shell has a global `PIP_USER=1` environment variable. Clear it before installing: `PIP_USER="" pip install -r requirements.txt` |
| `pip install` fails with `externally-managed-environment` | You are on a system-managed Python (e.g. macOS Homebrew, Ubuntu 23.04+). Use a virtual environment as described in step 3 of installation. |

---

## Limitations

- AcoustID API calls are rate-limited to **3 per second per process** to respect the API's terms of service.
- Chromaprint cannot fingerprint DRM-protected or zero-length files; these are silently skipped.
- Always **back up your music library** before running with `--action delete`.

---

## Contributing

Pull requests and issue reports are welcome.

---

## License

MIT

