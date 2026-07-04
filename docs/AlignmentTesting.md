# Alignment Pipeline Testing Guide

This guide explains how to set up and manually test the audiobook-ebook alignment pipeline end-to-end against a real Audiobookshelf server.

## Prerequisites

### 1. Install system and Python dependencies

```bash
uv sync                          # installs faster-whisper as a core dep (needs Python 3.12 or 3.13)
brew install ffmpeg              # macOS — required for audio decoding
# apt-get install ffmpeg         # Debian/Ubuntu equivalent
```

`faster-whisper`'s `ctranslate2` / `onnxruntime` deps currently ship wheels only for Python ≤3.13. `pyproject.toml` pins `requires-python = ">=3.12,<3.14"` to keep `uv sync` honest. If you previously created the venv on a newer Python, recreate it: `rm -rf .venv && uv venv --python 3.13`.

### 2. Configure environment

Copy `.env.example` to `.env` and set at minimum:

```
AUDIOBOOKSHELF_URL=https://your-abs-server
AUDIOBOOKSHELF_API_KEY=your-api-key
```

Tuning the transcription run (defaults in parentheses):

```
WHISPER_MODEL=tiny.en            # (tiny.en) other choices: base.en, small.en, medium.en, large-v3
WHISPER_DEVICE=cpu               # (cpu) — or "cuda" if you have a CUDA GPU, "mps" experimentally
WHISPER_COMPUTE_TYPE=int8        # (int8) — "float16" on GPU
WHISPER_CPU_THREADS=4            # (4) — set to host CPU count for best throughput
WHISPER_CHUNK_SECONDS=600        # (600) — lower on low-RAM hosts to cut peak RAM
WHISPER_LANGUAGE=en              # (en)
LOG_LEVEL=DEBUG                  # see stage-by-stage logging
```

### 3. Obtain an ABS item ID and an EPUB file

- **Item ID**: in the ABS UI, open a book — the ID is the UUID in the URL (`/item/d07beaed-…`). The item must have audio files attached.
- **EPUB file**: the ebook corresponding to that audiobook. Local files work; the ebook does not need to be attached to the ABS item.

---

## Running the Test Script

```bash
uv run python testing/test_alignment.py \
    --item-id  <ABS_ITEM_ID> \
    --ebook-file /path/to/book.epub
```

The script:
1. Initialises the database (creates tables if needed).
2. Creates an `AlignmentJob` row and starts the pipeline.
3. Polls and prints stage transitions every 2 seconds.
4. On completion, prints job statistics and a preview of the first 10 sync map entries.

### Example output

```
Initializing database...
Created alignment job #1 for item 'd07beaed-…'
EPUB: /path/to/book.epub

Pipeline progress:
  [17:07:06] Pending
  [17:07:08] Parsing EPUB and extracting paragraphs
             Paragraphs extracted: 3,434
  [17:07:24] Running Whisper transcription
  [17:07:24] progress: 30%
  [17:09:13] progress: 32%
  [17:11:42] progress: 35%
  …
  [17:54:08] progress: 57%
  [17:56:30] progress: 60%
  …
  [18:22:51] progress: 85%
  [18:23:11] Complete
             Fragments aligned:    3,420

Completed in 1h 16m
```

Transcription contributes job progress **30..85**. The drain coroutine pushes a DB write whenever the integer mapping advances, and a heartbeat coroutine auto-nudges progress every 30 s if the model has been silent in the meantime (so the bar never sits dead for more than half a minute). The server log also receives an `INFO` line every 60 s:

```
INFO  alignment still running: elapsed=12m04s progress=39
INFO  alignment still running: elapsed=13m05s progress=40
…
```

For an external view, watch the DB directly:

```bash
watch -n 5 'sqlite3 earmark.db "SELECT status, progress FROM alignment_jobs ORDER BY id DESC LIMIT 1;"'
```

Expect transcription to dominate runtime — on CPU with the `tiny.en` model, a 12-hour audiobook takes roughly an hour; `base.en` is roughly 2×, and `medium.en` is 5×+. A CUDA GPU brings this down by an order of magnitude.

---

## Cache

All downloaded and intermediate files are stored under `.cache/earmark/<item-id>/`:

```
.cache/earmark/<item-id>/
  audio/             ← downloaded MP3/M4B files (zero-padded index prefix)
  ebook.epub         ← copied from the mapping's source (local or Calibre OPDS); skipped when --ebook-file is used
  chunks/<model>_<chunk_seconds>_<lang>/
    0000.json        ← per-chunk word lists; restart-resumable; cleaned up on success
    0001.json
  transcript.json    ← durable; consolidated word list keyed on WHISPER_MODEL
  sync_map.json      ← final output
  .abs_updated_at    ← cache sentinel; compared against live ABS updatedAt on each run
```

**Cache invalidation:** if the ABS item's `updatedAt` timestamp has advanced since the last run, all cached files are deleted and re-downloaded. To force a clean run, delete `.cache/earmark/<item-id>/` manually.

**Transcript caching:** the transcribe step writes `transcript.json` and reuses it on subsequent runs while `WHISPER_MODEL` matches the cached value. Iterating on the matching algorithm (e.g. tuning `min_score`, the chapter-snap rules, or the anchor/interpolation pass) takes **~18 s** on a cache hit instead of the cold run, because the audio decoding + transcription steps are skipped. Change `WHISPER_MODEL` (or delete `transcript.json` manually) to force a re-transcription.

**Per-chunk caching:** during a cold run, each ~`WHISPER_CHUNK_SECONDS`-long chunk's word list is written to `chunks/<model>_<chunk_seconds>_<language>/<idx>.json` as soon as it finishes. If the container restarts mid-run (deploy, OOM, NAS reboot), the next run reuses the already-completed chunks and resumes from where it died. The directory is removed once `transcript.json` is consolidated.

---

## Validation Warnings

After transcription runs, the pipeline scores the sync map and records any warnings on `AlignmentJob.warnings`. A job with warnings finishes in status `complete_with_warnings` (the sync map is still readable through the API). Warning strings:

| Warning | Meaning |
|---------|---------|
| `suspect_first_entry: …` | The first sync-map entry is very short (non-heading element under 40 chars) or matches a blurb attribution pattern — usually means front matter leaked through. |
| `docfragment_gap: missing […]` | A `DocFragment[N]` that held a full chapter (≥ `_GAP_MIN_PARAS` EPUB paragraphs) produced no aligned entries — the pipeline silently skipped over a chapter. Blank/decorative pages and one-line front matter (dedication, epigraph) are ignored. |
| `low_transcript_coverage: N/M paragraphs unmatched` | More than 10% of EPUB paragraphs failed fuzzy-matching against the audio transcript (score < `min_score=45`). Usually indicates back-matter pollution or audio missing entire chapters. |
| `unmatched_chapter_heading: '<text>'` | An EPUB chapter heading (PROLOGUE / EPILOGUE / CHAPTER N) couldn't be matched to any ABS chapter title — so it wasn't snapped to a ground-truth audio start. Inspect the ABS chapter titles for that book and confirm the numbering is conventional. |

To exercise these in tests, run `uv run pytest tests/test_alignment.py -k validate -v`.

---

## Common Failures

| Symptom | Cause | Fix |
|---------|-------|-----|
| `ModuleNotFoundError: No module named 'faster_whisper'` | venv predates faster-whisper becoming a core dep | `uv sync` to install/refresh deps |
| `Distribution torch can't be installed … no wheels for cp314` | Python 3.14 venv | Recreate venv with Python 3.12 or 3.13 |
| First sync-map entry is praise/blurb text | EPUB front matter not detected as front matter | See `_classify_spine` in `src/earmark/services/alignment.py` — landmarks/title/filename/blurb signals |
| `low_transcript_coverage` warning on a clean book | Whisper model too small | Try `WHISPER_MODEL=base.en` or `small.en` |
| Run times out / OOM | Audio too long for available RAM | Use `int8` compute_type and `tiny.en` model; lower `WHISPER_CHUNK_SECONDS` (e.g. 300) |
| ffmpeg `No such file or directory` in concat list | Relative paths in concat list | Fixed in code (uses absolute paths) |

---

## Verifying Alignment Accuracy

The sync map preview printed at the end of each run shows the first 10 entries. To verify against a known audiobook, listen to the audio and note actual start times of the first paragraphs, then compare.

Transcription uses **faster-whisper directly** with `word_timestamps=True`, `beam_size=1`, `best_of=1`, and `vad_filter=True` — no wav2vec2 forced-alignment pass. Whisper's own word timestamps are coarser (≈±200 ms per word vs. ≈±50 ms for wav2vec2), but the downstream paragraph matcher (`rapidfuzz.fuzz.partial_ratio_alignment` at `min_score=45`) is fuzzy enough that this rarely shifts a paragraph boundary by more than a second. Expected per-paragraph accuracy: **±1–2 seconds** with `tiny.en`, **±1 second** with `base.en` or larger.

When ABS audio splits a chapter that the EPUB keeps whole (or vice versa), the old aeneas pipeline produced hours of offset error; the current text-based matcher handles this naturally because the match is content-driven, not positional.

---

## Verifying Chapter Timings (`testing/diff_chapters.py`)

After a job completes, run the chapter diff to compare each EPUB chapter heading in `sync_map.json` against the matching ABS chapter start:

```bash
uv run python testing/diff_chapters.py --item-id <ABS_ITEM_ID> [--threshold 5.0]
```

The script picks a matching mode automatically:

- **`title`** — when ABS chapter titles include chapter numbers (`Chapter 12: …`, `Prologue: Snow (Part 1)`), EPUB headings (`CHAPTER 12`, `PROLOGUE`) are looked up by number. *Winter's Heart* runs in this mode and should report `max |diff| = 0.00 s` after a clean run because of the chapter-snap step.
- **`positional (offset=N)`** — when fewer than half the headings match by title, the script finds the offset that minimises mean drift across the whole book and pairs the *k*-th EPUB heading with the *(N+k)*-th ABS chapter. *Remarkably Bright Creatures* runs in this mode (its ABS titles are generic, `01-68 Remarkably Bright Creatures`); expect a handful of outliers on short distinctive chapter titles.

Output is a per-chapter table with `sync_start`, `abs_start`, signed diff, and the ABS title; rows above `--threshold` are flagged. Exit code `0` means everything within threshold and matched; `2` means at least one chapter drifted or didn't match — handy for use in CI.

---

## Re-running After Failure

Each test run creates a new `AlignmentJob` row. Failed jobs are never modified. Re-run the script — if audio is already cached and the ABS timestamp hasn't changed, the download stage is skipped.

To reset everything:

```bash
rm -rf .cache/earmark/<item-id>/
uv run python testing/test_alignment.py --item-id <id> --ebook-file /path/to/book.epub
```
