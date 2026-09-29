# SoundSorta

**SoundSorta** is a lean, non-destructive desktop utility for auditioning, tagging, and managing large legacy sample libraries — built for music producers and sound designers who need to make sense of years of accumulated audio without ever touching the original files or folder structure.

Everything SoundSorta knows about your library — file locations, tags, colors — lives in a local SQLite database. Your samples themselves are never moved, renamed, or modified.

---

## Key Features

- **Non-Destructive Tagging**: Manage, organize, and search your sample library without altering or moving original files on disk.
- **Low-Latency Auditioning**: Powered by `pygame.mixer` for instant sample preview and playback, with a `soundfile`-based fallback for 24-bit/32-bit AIFF and other codecs `pygame` can't play directly.
- **Visual Waveform Display**: Render visual representations of sample assets for quick inspection. Toggleable if you want a lighter, faster list.
- **Staged Multi-Tag Apply**: Check any number of samples, tick any number of tags in the tag panel, and apply or remove all of them in one batch — no dragging files onto tags one at a time.
- **Checked vs. Selected, kept separate**: Checking a sample (for tagging or deletion) and selecting it (for playback or dragging out to your DAW) are two independent things, so batch-tagging a pile of samples doesn't disturb what you've got selected for drag-out, and vice versa.
- **DAW Drag-and-Drop**: Drag selected samples directly from the sample list into your DAW (e.g. FL Studio).
- **Color-Coded Tag System**: Assign each tag a color from a 12-swatch palette (right-click a tag) for fast visual scanning.
- **No Row Cap**: The sample list pages in as you scroll rather than capping out at a fixed count, and filename search runs against your full library, not just whatever's currently loaded on screen.
- **SQLite Database Architecture (WAL mode)**: Fast local indexing and querying, with write-ahead logging so the UI can keep reading while a scan is writing.
- **Threaded, Cancellable Scanning**: Background indexing for large directory trees, batched into transactions so it stays fast even on huge collections.
- **Multi-Select Batch Deletion**: Move any number of checked samples to the Recycle Bin at once — files go through `send2trash`, never a permanent delete.
- **Library Root Manager**: Configure and re-scan multiple root sample locations from one dialog, with a folder-tree browser for drilling into a specific subfolder.

---

## Tech Stack

- **Language**: Python 3.11+
- **UI Framework**: PyQt6
- **Audio Engine**: `pygame.mixer` (+ `soundfile` for broader format/bit-depth support)
- **Database**: SQLite3, WAL mode, foreign keys enforced
- **Safe Deletes**: `send2trash`
- **OS**: Windows 11 / cross-platform compatible

---

## Project Structure

```
SoundSorta/
├── soundsorta.py   # Application entry point, main window, and all UI widgets
├── database.py     # SQLite connection, schema, scanning/indexing, WAL setup
├── tags.py         # Tag CRUD, batched tag apply/remove, tag-state lookups
└── README.md       # Project documentation
```

---

## Installation & Setup

1. **Clone the Repository**:
   ```bash
   git clone https://github.com/perpetualemotionmachine/soundsorta.git
   cd soundsorta
   ```

2. **Set Up a Virtual Environment:**

   ```bash
   python -m venv venv
   # On Windows:
   venv\Scripts\activate
   # On macOS/Linux:
   source venv/bin/activate
   ```

3. **Install Dependencies:**

   ```bash
   pip install PyQt6 pygame soundfile send2trash
   ```

   `soundfile` and `send2trash` are optional at import time, but strongly recommended: without `soundfile` you lose waveform/duration display and playback for anything beyond plain 16-bit WAV, and without `send2trash` the delete buttons fall back to a permanent `os.remove` instead of the Recycle Bin.

4. **Run the Application:**

   ```bash
   python soundsorta.py
   ```

---

## How to Use

### Importing Audio Directories

Click **Scan Folder** to select and index a local sample directory, or use **Roots** to register one or more library root folders you'll come back to often. Scanning indexes files into the SQLite database without moving or renaming your original audio files, and can be cancelled mid-scan.

### Auditioning Samples

Click any sample card (or use the ► play button on it) to trigger instant playback. Up/Down/Space also replay the currently selected sample. Toggle **Waveforms** off for a lighter, faster-scrolling list on very large libraries.

### Tagging

1. **Check** the samples you want to tag — the checkbox on each card, or **Check All** / **Uncheck All** for everything matching the current filters.
2. In the tag panel, tick the checkbox next to each tag you want to add or remove. A filled box means the tag will be applied; an empty one (after you've explicitly unticked it) means it'll be removed; a dash means only *some* of your checked samples currently have that tag.
3. Click **Apply Staged Tags** to commit the changes to all checked samples at once, or **Clear Staged** to discard your ticks without applying anything.

Click a tag's **name** (not its checkbox) to toggle it as a view filter instead — filtering and staging are independent, so you can filter the list down to "Kicks" while staging a completely different tag to apply.

Right-click a tag for its color swatch menu or to delete it entirely.

### Filtering & Searching

Use the tag filters, the folder tree, and/or the filename search bar together — they combine. Search runs against your entire filtered library, not just what's currently scrolled into view.

### DAW Drag-and-Drop

Select one or more samples in the list (regular click/Ctrl-click/Shift-click — this is separate from checking them) and drag them out of SoundSorta into your DAW's arrangement or sampler.

### Deleting Samples

Check the samples you want gone and click **X Delete Checked**, or use the **X** on an individual card. Either way, files go to the Recycle Bin via `send2trash` (or a permanent delete if `send2trash` isn't installed) and their database records are removed — deletion never touches your other original files.

---

## Version

**v1.0** — non-destructive core, staged multi-tag workflow, checked/selected separation, uncapped paginated sample list, WAL-mode database.

---

## License

MIT License. See LICENSE for details.