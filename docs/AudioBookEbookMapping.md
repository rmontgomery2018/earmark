# AudioBook–Ebook Forced Alignment

This document describes the pipeline that force-aligns an audiobook from Audiobookshelf (ABS) with its companion EPUB ebook, producing a JSON mapping from audio timestamps to EPUB paragraph positions. The output enables read-along synchronization between an audiobook player and an ebook reader (e.g., KOReader via KOSync).

## Table of Contents

1. [Overview](#1-overview)
2. [Database Schema](#2-database-schema)
3. [Audiobookshelf API Integration](#3-audiobookshelf-api-integration)
4. [Ebook Sourcing](#4-ebook-sourcing)
5. [Local Cache Storage Structure](#5-local-cache-storage-structure)
6. [EPUB Parsing & Extraction](#6-epub-parsing--extraction)
7. [Indexing](#7-indexing)
8. [The Core Mapping Mechanism](#8-the-core-mapping-mechanism)
9. [Multi-File Audio Handling](#9-multi-file-audio-handling)
10. [Audio Format Handling](#10-audio-format-handling)
11. [Audio Transcription (faster-whisper)](#11-audio-transcription-faster-whisper)
12. [Paragraph Matching (rapidfuzz)](#12-paragraph-matching-rapidfuzz)
13. [Final Assembly](#13-final-assembly)
14. [Output Schema](#14-output-schema)
15. [Dependencies](#15-dependencies)
16. [Error Handling](#16-error-handling)
    - [16a. Validation Warnings](#16a-validation-warnings)
17. [Verifying Alignment Against ABS](#17-verifying-alignment-against-abs)

---

## 1. Overview

**Input:** An Audiobookshelf library item ID (`abs_item_id`) that has both audio files and an ebook file attached.

**Output:** `sync_map.json` — an ordered array of paragraph-level alignment entries, each pairing an audio time range with an EPUB position.

**Pipeline stages:**

```
ABS API
  └─► fetch item metadata & cache to DB (abs_library_items)
        └─► download audio files → local cache
              └─► download ebook → local cache
                    └─► parse EPUB, classify spine, extract bodymatter paragraphs
                          └─► chunk audio (10-min WAV slices) → faster-whisper transcribe (word-level timestamps)
                                └─► fuzzy-match EPUB paragraphs to transcript (rapidfuzz)
                                      └─► validate + write sync_map.json (warnings → status)
```

Progress for each run is tracked in the `alignment_jobs` database table, updated at each stage so the job can be monitored or resumed after failure.

---

## 2. Database Schema

Two new tables are added to `earmark/models.py`, following the existing `Mapped`/`mapped_column` SQLAlchemy pattern. An Alembic migration is required to create them.

### `abs_library_items` — ABS item metadata

Mirrors the Audiobookshelf item record locally. Used for cache invalidation: the `abs_updated_at` field is compared against the live ABS `updatedAt` value at pipeline start — if ABS is newer, the cached files are discarded and re-fetched. `raw_metadata` stores the full ABS response JSON for debugging.

```python
class AbsLibraryItem(Base):
    __tablename__ = "abs_library_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    abs_item_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    library_id: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(500))
    author: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ebook_filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ebook_format: Mapped[str | None] = mapped_column(String(20), nullable=True)  # "epub", "pdf"
    audio_file_count: Mapped[int] = mapped_column(Integer)
    total_duration_seconds: Mapped[float] = mapped_column(Float)
    abs_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    raw_metadata: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    alignment_jobs: Mapped[list["AlignmentJob"]] = relationship(back_populates="library_item")
```

### `alignment_jobs` — pipeline progress tracking

One row per pipeline run. `status` is updated as the pipeline advances through each stage. A failed job is never modified after failure; a retry creates a new row.

```python
class AlignmentJob(Base):
    __tablename__ = "alignment_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    abs_item_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("abs_library_items.abs_item_id"), index=True
    )
    status: Mapped[str] = mapped_column(String(50), default="pending")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    audio_cache_dir: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    ebook_cache_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    sync_map_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    paragraph_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fragment_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    library_item: Mapped["AbsLibraryItem"] = relationship(back_populates="alignment_jobs")
```

**`status` lifecycle:**

```
pending
  └─► fetching_audio
        └─► fetching_ebook
              └─► parsing_epub
                    └─► aligning
                          └─► assembling
                                └─► complete
                                └─► failed  (from any stage)
```

Each transition writes `status` and `updated_at`. On failure, `error_message` is populated. `paragraph_count` is set after `parsing_epub`; `fragment_count` is set after `aligning`. A mismatch between them is logged as a warning but does not fail the job — alignment proceeds up to `min(paragraphs, fragments)`.

---

## 3. Audiobookshelf API Integration

The existing `earmark/services/audiobookshelf.py` client is extended with the methods below. Authentication uses a Bearer token from `settings.audiobookshelf_api_key` on every request.

```
Authorization: Bearer {api_key}
```

### 3a. Fetch Item Metadata

```
GET /api/items/{item_id}?expanded=1
```

Relevant response fields:

```json
{
  "id": "li_abc123",
  "libraryId": "lib_xyz",
  "updatedAt": 1715000000000,
  "mediaType": "book",
  "media": {
    "metadata": {
      "title": "A Tale of Two Cities",
      "authorName": "Charles Dickens"
    },
    "audioFiles": [
      {
        "index": 1,
        "filename": "Chapter01.mp3",
        "ext": ".mp3",
        "duration": 1823.4,
        "codec": "mp3",
        "bitrate": 64000,
        "channels": 2
      },
      {
        "index": 2,
        "filename": "Chapter02.mp3",
        "ext": ".mp3",
        "duration": 2104.8,
        "codec": "mp3"
      }
    ],
    "ebookFile": {
      "filename": "tale-of-two-cities.epub",
      "ext": ".epub"
    }
  }
}
```

`updatedAt` is a Unix millisecond timestamp. It is stored in `abs_library_items.abs_updated_at` and compared on every pipeline run to detect stale caches.

If `ebookFile` is `null`, the pipeline cannot proceed — the job is set to `failed` immediately.

### 3b. Download Audio Files

```
GET /api/items/{item_id}/file/{filename}
```

Stream the response body to disk. Files are sorted by their `index` field before downloading and stored with a zero-padded index prefix to preserve order:

```
audio/001_Chapter01.mp3
audio/002_Chapter02.mp3
```

### 3c. Library Discovery (batch mode)

```
GET /api/libraries
GET /api/libraries/{lib_id}/items?limit=0
```

For batch alignment runs, filter items where `media.ebookFile != null` (ABS-attached ebook) or where a CWA/local match is expected for the item's title.

---

## 4. Ebook Sourcing

The ebook for a mapping comes from one of two sources, chosen **per mapping** in the UI and stored on the `AbsEbookMapping` row as `ebook_source`:

- `"local"` — a file under `ebook_local_root` on disk (the default).
- `"calibre"` — an ebook fetched from a Calibre Web OPDS server, identified by the OPDS download href in `ebook_source_ref`.

The pipeline resolves the source before the `fetching_ebook` stage and always deposits the result at `.cache/earmark/{item_id}/ebook.epub`. Dispatch lives in `AlignmentPipeline._fetch_ebook_from_source` (`src/earmark/services/alignment.py`) and delegates to the implementations in `src/earmark/services/ebook_sources/`. See [CalibreWebIntegration.md](CalibreWebIntegration.md) for the design.

```python
# earmark/config.py
cwa_url: str = ""              # base URL of Calibre Web (OPDS)
cwa_username: str = ""
cwa_password: str = ""
ebook_local_root: str = "."    # root directory scanned for local ebooks
```

> **Removed:** the old `EBOOK_SOURCE` global env var no longer exists. If it is still set in your environment the app logs a deprecation warning at startup and ignores it. There is also a fallback `ABS-attached` path used only by CLI/legacy alignment jobs that have no mapping; mapping-driven jobs always go through `local` or `calibre`.

### Mode A — Local filesystem (`ebook_source = "local"`)

The EPUB already exists on a drive accessible to earmark, under `ebook_local_root`. The user picks the file in the mapping UI; the chosen path (relative to the root) is stored on the mapping as `ebook_path`.

At alignment time the pipeline copies `ebook_local_root / ebook_path` to `.cache/earmark/{item_id}/ebook.epub`. The source file is never modified.

`LocalEbookSource.search()` (used by the UI's local listing fallback and the CLI flow without an explicit path) walks `ebook_local_root` and ranks candidates against the ABS item's `title`/`authorName` (lowercased, punctuation stripped):

1. Filename matches normalized title exactly **and** parent directory matches normalized author
2. Filename matches normalized title exactly
3. Any path component contains the normalized title (fallback)

### Mode B — Calibre Web OPDS (`ebook_source = "calibre"`)

Calibre Web does not expose a traditional REST API. The pipeline uses its OPDS feed for both discovery and download.

**Step 1 — Discovery (frontend, before mapping creation)**

The mapping UI calls `GET /web/calibre-ebooks?abs_item_id=…`. The backend resolves the audiobook's title/author (via ABS if configured, falling back to the `abs_library_items` table) and queries the OPDS server:

```
GET /opds/search/{normalized_title}
Authorization: Basic base64({cwa_username}:{cwa_password})
```

The Atom XML feed is parsed by `_parse_opds_feed`. Matching is permissive (see [CalibreWebIntegration.md § 7](CalibreWebIntegration.md) for the full rules): multi-author entries pass on any author overlap; titles match via exact equality, series-prefix-stripped substring, or significant-token-set subset. Every `<link rel="http://opds-spec.org/acquisition">` becomes a candidate; the `format` field is derived from the MIME type and EPUB candidates sort first. The user picks one candidate; its `ref` is persisted on the mapping as `ebook_source_ref`.

**Step 2 — Download (alignment job)**

At alignment time `CalibreOpdsSource.fetch(ref, dest)` streams the EPUB from `{cwa_url}{ref}` with the same Basic auth, writing it to `.cache/earmark/{item_id}/ebook.epub`.

**Error handling:** `GET /web/calibre-ebooks` returns 503 if `CWA_URL` is unset, 502 if the OPDS server is unreachable, and 404 if the ABS item cannot be resolved.

---

## 5. Local Cache Storage Structure

All downloaded and intermediate files live under `.cache/earmark/` within the project root, keyed by ABS item ID.

```
.cache/earmark/
  {abs_item_id}/
    metadata.json          ← full ABS /api/items/{id}?expanded=1 response
    audio/
      001_Chapter01.mp3    ← zero-padded index prefix preserves playback order
      002_Chapter02.mp3
    chunks/<model>_<chunk_seconds>_<lang>/
      0000.json            ← per-chunk word lists; restart-resumable; deleted after transcript.json is written
      0001.json
    transcript.json        ← durable; cached consolidated word list (see below)
    ebook.epub
    sync_map.json          ← durable output artifact
```

**Cache invalidation:** On pipeline start, `metadata.json` is read and its `updatedAt` value is compared against the live ABS API. If ABS is newer, all cached files for that item are deleted and re-downloaded.

Per-chunk WAV slices are extracted to a process-local tempdir for transcription and removed as each chunk completes. There is no concatenated WAV — the previous pipeline produced a `concatenated.wav` at 16 kHz mono; the chunked pipeline never materializes the full track on disk.

`transcript.json` is the consolidated transcription output (word list + the `whisper_model` it was produced with). Subsequent runs skip the multi-minute transcription step when this file exists and the cached model name matches `settings.whisper_model`. Delete it manually to force a re-transcription (e.g. after changing to a larger model).

The `chunks/` directory holds per-chunk word lists keyed on `(model, chunk_seconds, language)`. If a job dies mid-transcription (container restart, OOM, NAS reboot), the next run reuses already-completed chunks and resumes from where it died. The directory is removed once `transcript.json` is finalized.

`sync_map.json` is the final artifact. Its path is written to `alignment_jobs.sync_map_path`.

---

## 6. EPUB Parsing & Extraction

**Libraries:** `ebooklib` to open the EPUB and iterate the spine; `BeautifulSoup` to parse each HTML document.

```python
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup

book = epub.read_epub(ebook_path)
first_body_pos, last_body_pos, _ = _classify_spine(book)  # see §6a

paragraphs = []
for spine_pos, (item_id, _attrs) in enumerate(book.spine, start=1):
    if spine_pos < first_body_pos or spine_pos > last_body_pos:
        continue
    item = book.get_item_with_id(item_id)
    soup = BeautifulSoup(item.get_content(), "html.parser")
    for p in soup.find_all("p"):
        text = p.get_text(separator=" ").strip()
        if text:
            paragraphs.append((item.file_name, p, text))
```

Paragraphs are collected in EPUB spine order, restricted to the **bodymatter range** `[first_body_pos, last_body_pos]` (1-based, inclusive). Empty paragraphs are skipped. Each surviving paragraph is assigned a sequential ID: `para_001`, `para_002`, …

The job's `status` is set to `parsing_epub` at the start of this stage, and `paragraph_count` is set to the final count on completion.

### 6a. Front/Back Matter Detection (`_classify_spine`)

`_classify_spine` walks the EPUB spine and classifies each item as `front`, `back`, `body`, `ambiguous`, or `skip`. The bodymatter range is the position of the first `body` item through the position of the last `body` item, inclusive — anything outside that range is dropped.

Classification uses four signals, in priority order:

1. **EPUB3 `nav` landmarks** — when a `<nav epub:type="landmarks">` element is present in the EPUB navigation document, an `<a>` whose `epub:type` is `frontmatter`, `bodymatter`, or `backmatter` is authoritative for that spine item. This is the cleanest signal and always wins.
2. **Spine `linear="no"`** — items the publisher flagged as non-linear (covers, supplementary pages) are classified `front`.
3. **TOC title and spine filename/id substrings** — the TOC title (lowercase, whitespace-collapsed) is tested against a phrase list (`_FRONT_PHRASES`, `_BACK_PHRASES` — "praise", "dedication", "contents", "copyright", "about the author", "acknowledgments", "glossary", "bibliography", …). The spine item's `file_name` and `id` (joined and lowercased with `-` → `_`) are tested against substring tokens (`_FRONT_FILE_TOKENS`, `_BACK_FILE_TOKENS` — "frontmatter", "copyright", "dedication", "halftitle", "acknowledg", "about_the_author", "backmatter", …). Tokens like `also_by` and `acknowledg` appear on both sides; an item that matches both is `ambiguous`.
4. **Content shape** — items dominated by short attribution lines (`— The New York Times`) or `<blockquote>`/`<cite>` content are `front` (`_is_blurb_shaped`). Items with fewer than two paragraphs of ≥40 characters are `ambiguous` (likely chapter dividers or end-of-book ad pages).

A strong **body** signal (an `<h1>`/`<h2>` that matches `prologue|epilogue|chapter|book|part|<digit>|<roman>`) overrides front-matter filename hints. This is what catches the common case where a publisher names a spine item `frontmatter02.xhtml` but it actually contains the Prologue.

When the EPUB ships with `nav` landmarks, classification is essentially exact. Without landmarks, the heuristic still handles the practical cases seen in commercial releases: drift in the heuristic shows up later as warnings (see [§16a](#16a-validation-warnings)).

---

## 7. Indexing

As paragraphs are extracted, an in-memory index maps each ID to its text and EPUB position:

```python
index: dict[str, dict] = {}

for seq, (doc_filename, element, text) in enumerate(paragraphs, start=1):
    para_id = f"para_{seq:03d}"
    spine_pos = spine_index[doc_filename]   # 1-based position in EPUB spine
    rel_path = _element_full_xpath(element) # full path from <body> to element

    index[para_id] = {
        "text": text,
        "ebook_pos": f"/body/DocFragment[{spine_pos}]{rel_path}",
        "chapter_file": doc_filename,
    }
```

**`ebook_pos` construction:**
- `DocFragment[N]` — the 1-based index of the HTML document within the EPUB spine
- The remainder is the **full hierarchical path** from `<body>` to the block element, built by `_element_full_xpath`. At each ancestor level, the element's position is counted among its same-tag siblings (1-based).

This produces a path that mirrors the actual DOM structure of the EPUB HTML, for example:

```
/body/DocFragment[2]/body/section[1]/div[2]/p[3]
```

This format is critical: KOReader's CRE engine navigates positions using the real DOM tree, so the XPath must match the actual nesting of the HTML. A flattened path like `/body/DocFragment[2]/body/p[3]` only works when `<p>` elements are direct children of `<body>`, which is uncommon in commercial EPUBs.

---

## 8. The Core Mapping Mechanism

This is the critical design invariant: **the sequential paragraph ID is the sole join key between the audio timeline and the EPUB position.**

EPUB paragraphs are matched to audio time ranges by fuzzy-matching paragraph text against the `faster-whisper` word-level transcript. Each paragraph is searched in a window centered on its **expected audio position**. When EPUB chapter headings map to ground-truth ABS chapter starts (the common case), that position is found by interpolating between the surrounding chapter anchors so the search stays inside the paragraph's own chapter (`_align_paragraphs_anchored`, §12). Only when no heading maps to a chapter does the matcher fall back to **global proportional positioning** — paragraph `i` of `n` searched in a window centered on the transcript character offset `total * i/n` (`_align_paragraphs_to_transcript`):

```
EPUB spine (reading order)
  │
  ▼
Extract paragraphs (bodymatter only) → assign IDs in order
  para_000  "It was the best of times..."     ebook_pos = /body/DocFragment[1]/body/section[1]/p[1]
  para_001  "it was the worst of times..."    ebook_pos = /body/DocFragment[1]/body/section[1]/p[2]
  │
  ▼
faster-whisper transcribes the audio → word-level timestamps
  [
    {"word": "it", "start": 0.10, "end": 0.18},
    {"word": "was", "start": 0.19, "end": 0.30},
    ...
  ]
  │
  ▼
Build a normalized transcript string + (char → time) mapping.
For each paragraph in order, rapidfuzz finds the best partial-ratio
substring match in a window centered on its expected position
(chapter-anchored when headings map to ABS, else proportional).
The matched char range maps back to (audio_start, audio_end).
Paragraphs whose best score is below `min_score=45` are recorded
as None and dropped.

  para_000: audio 0.10–5.21    ebook_pos /body/DocFragment[1]/body/section[1]/p[1]
  para_001: audio 5.30–12.10   ebook_pos /body/DocFragment[1]/body/section[1]/p[2]
```

**Invariant:** every paragraph is searched independently in a window around its expected position — there is no forward cursor that consecutive paragraphs share. This dropped an earlier failure mode where a single fuzzy mismatch near the start of a long book pushed the cursor far ahead of where later paragraphs actually appear, starving them of search range. Anchoring that window on ABS chapter boundaries (rather than a single global proportional estimate) keeps the search inside the right chapter: a global estimate assumes paragraph index maps linearly to audio position, which drifts by tens of minutes on books with dense front matter or an unusually long prologue and pulls early paragraphs into the wrong region. Monotonicity is restored later by the anchor + linear-interpolation post-pass (see §13). Paragraphs that don't appear in the audio (front-matter blurbs, back-matter acknowledgments) score below `min_score` and are dropped from the final map — they never poison the alignment of surrounding paragraphs.

---

## 9. Multi-File Audio Handling

Audiobookshelf commonly stores audiobooks as a folder of MP3 files — one per chapter. The transcription stage processes the audio in fixed-length chunks (default 600 s), so the files don't need to be physically concatenated. For each chunk index *i*, ffmpeg extracts a slice spanning `[i * chunk_seconds, (i+1) * chunk_seconds)` from the underlying audio (sorted by `audioFiles[].index`) into a temporary 16 kHz mono WAV that is fed to `faster_whisper`. The slice file is deleted as soon as that chunk completes.

If the audio is already a single `.mp3`, `.m4b`, or `.m4a` file, ffmpeg operates on it directly with the appropriate `-ss` / `-t` flags; multi-file inputs are handled via an ffmpeg concat list driving the same `-ss` / `-t` slice. Peak audio memory is one chunk (~19 MB at 600 s) rather than the full audiobook.

---

## 10. Audio Format Handling

| Format | Notes |
|--------|-------|
| `.mp3` | Single or multi-file. ffmpeg slices each chunk on demand. |
| `.m4b` | Single AAC audiobook container. Sliced via ffmpeg. |
| `.m4a` | Single AAC. Same as `.m4b`. |
| `.flac` / `.ogg` / `.opus` / `.aac` | Same chunked ffmpeg slicing path; each chunk emitted as 16 kHz mono PCM WAV. |

`-ar 16000 -ac 1` (16 kHz mono PCM) is what `faster-whisper` expects internally — emitting that format per-chunk avoids codec edge cases and keeps memory predictable across formats.

---

## 11. Audio Transcription (faster-whisper)

The pipeline uses **`faster-whisper`** directly to produce word-level timestamps from Whisper's own decoder (no wav2vec2 forced-alignment pass). It replaces the previous aeneas + chapter-rescaling pipeline. The key wins: alignment is text-based (not positional) — front matter, back matter, intro tracks, and split chapters no longer cascade errors through the rest of the book — and the chunked + cached design survives container restarts mid-job with bounded retry cost.

```python
from faster_whisper import WhisperModel

model = WhisperModel(model_name, device=device, compute_type=compute_type,
                     cpu_threads=cpu_threads)

# For each ffmpeg-extracted chunk WAV:
segments, _info = model.transcribe(
    str(chunk_path),
    beam_size=1, best_of=1,        # greedy decode — primary speed lever
    language=language,
    word_timestamps=True,
    vad_filter=True,
)
words = [
    {"word": w.word.strip(),
     "start": float(w.start) + chunk_offset,
     "end":   float(w.end)   + chunk_offset}
    for seg in segments for w in (seg.words or [])
    if w.start is not None and w.end is not None
]
```

Word timestamps are **absolute** seconds in the original audio (per-chunk offsets are added on emit). No pre-trim is needed; intros and credits just don't get matched to any EPUB paragraph. Whisper's word timestamps are coarser than wav2vec2's (≈±200 ms vs. ≈±50 ms), but the matcher (§12) runs `rapidfuzz.fuzz.partial_ratio_alignment` at `min_score=45`, which is fuzzy enough that the precision loss doesn't shift paragraph boundaries by more than ≈1 s.

**Chunked + resumable design.** Each chunk's word list is written to `chunks/<model>_<chunk_seconds>_<language>/<idx>.json` atomically as soon as it finishes. Before transcribing a chunk, the pipeline checks for an existing cache file; if present, the model call is skipped and the cached words are reused. After all chunks finish, the consolidated word list is written to `transcript.json` and the `chunks/` subdirectory is removed. On a container restart mid-job, the next run picks up at the first incomplete chunk.

**Configuration** (all via `Settings` / env vars):

| Setting | Default | Notes |
|---------|---------|-------|
| `WHISPER_MODEL` | `tiny.en` | `tiny.en` / `base.en` / `small.en` / `medium.en` / `large-v3` |
| `WHISPER_DEVICE` | `cpu` | `cuda` for NVIDIA GPU; `mps` experimentally on Apple Silicon |
| `WHISPER_COMPUTE_TYPE` | `int8` | `float16` on GPU |
| `WHISPER_CPU_THREADS` | `4` | ctranslate2/openblas thread count; set to host CPU count |
| `WHISPER_CHUNK_SECONDS` | `600` | Audio chunk length in seconds; lower on low-RAM hosts |
| `WHISPER_LANGUAGE` | `en` | Passed straight through to `model.transcribe(language=…)` |

The pipeline updates `status=aligning, progress=30` before transcription and `progress=85, fragment_count=len(words)` after. Transcription dominates wall-clock cost: roughly 0.02× real-time on Apple Silicon (M-series) with 8 threads + `tiny.en` (a 12 h audiobook → ~10 min) and 0.07–0.1× on a 4-core N100-class CPU (~50 min for the same book). CUDA cuts this another 5×+.

**Live progress.** `faster-whisper.transcribe()` returns a generator of segments. For each completed segment within a chunk, `_transcribe_audio_sync` emits a per-chunk percent through a progress callback, which a wrapper maps to the overall span `(chunk_idx + chunk_pct/100) / n_chunks * 100`. That percent goes onto a `queue.Queue`. A drain coroutine reads each event, maps it through `_stage_progress(percent)` to the job-progress range **30..85**, and writes monotonically-increasing values to `alignment_jobs.progress`, surfaced to the mappings poll loop (`mappings/+page.svelte:64`) and the CLI runner.

A concurrent **heartbeat coroutine** runs alongside the drain. Every 30 s of model silence it auto-increments `progress` by 1 (capped just below the stage ceiling). Every 60 s it logs an `INFO`-level line of the form `alignment still running: elapsed=Nm  progress=X`, so an operator tailing the server log can tell the job is alive without staring at the DB.

The cached-transcript path skips both the drain and the heartbeat — it jumps straight from 30 to 85 in milliseconds.

---

## 12. Paragraph Matching (rapidfuzz)

`faster-whisper` gives a stream of timestamped words. Each EPUB paragraph is mapped onto a contiguous run of those words by **fuzzy substring matching in a window around the paragraph's expected audio position**. A shared helper, `_match_in_window`, runs `rapidfuzz.fuzz.partial_ratio_alignment` over the transcript slice and maps the matched char range back to `(audio_start, audio_end)`; the two aligners differ only in how they choose the window center.

```python
def _build_transcript_index(words):
    # Concatenate normalized words into a single transcript; record each
    # word's (char_start, char_end, time_start, time_end).
    ...

def _match_in_window(p_norm, transcript, ranges, expected_char, min_score):
    from rapidfuzz import fuzz
    half = max(8_000, len(p_norm) * 5)
    win_start, win_end = max(0, expected_char - half), expected_char + half
    m = fuzz.partial_ratio_alignment(p_norm, transcript[win_start:win_end])
    if m.score < min_score:
        return None                                     # not narrated in audio
    t_start = ranges[_word_index_at_char(ranges, win_start + m.dest_start)][2]
    t_end   = ranges[_word_index_at_char(ranges, win_start + m.dest_end - 1)][3]
    return (t_start, t_end)
```

**Chapter-anchored positioning (`_align_paragraphs_anchored`, primary path).** EPUB chapter headings (`PROLOGUE` / `CHAPTER N` / `EPILOGUE`) that match a ground-truth ABS chapter start give an ordered list of `(paragraph_index, audio_time)` anchors, strictly increasing in both fields. A paragraph's expected audio time is the linear interpolation between its bracketing anchors (in paragraph-index space), converted to a char offset via the transcript index. A match is rejected (left `None` for the §13 interpolation pass to fill) if it lands outside the bracketing chapter span ± 30 s, so a phrase that repeats in a neighbouring chapter can't drag the paragraph there. Anchor heading paragraphs are always emitted so the §13 chapter snap can pin them, even when the audio timeline diverges from ABS.

**Global proportional positioning (`_align_paragraphs_to_transcript`, fallback).** When no heading maps to an ABS chapter, each paragraph is searched at `expected_char = int(total * i / n)` instead. This is robust to ABS audio splitting a chapter the EPUB keeps whole, but on books with dense front matter or a very long prologue the index→position assumption drifts badly, which is why the anchored path is preferred whenever chapters are available.

Two further properties matter:

1. **Independent windows, no shared cursor.** Each paragraph is searched on its own; consecutive paragraphs do not advance a shared cursor. An earlier implementation kept a forward cursor; on a long book one mismatched fuzzy hit could drift the cursor several thousand characters past expected, starving later paragraphs of search range. Independent windows fix that, at the cost of allowing local out-of-order matches inside a chapter (a phrase that repeats two pages apart can match either occurrence). The anchor + interpolation pass in §13 restores strict monotonicity.
2. **Per-paragraph score gate.** If the best match scores below `min_score=45`, the paragraph is recorded as `None` and dropped from the final sync map. This is how unnarrated back matter (acknowledgments, copyright pages) and unmapped front matter naturally fall out of the result. The threshold sits below the default `60` because `tiny.en`/`base.en` transcripts have non-trivial word-level noise; values much higher cause whole chapters to fail matching on small models.

Together with chapter snapping (§13), this lets the pipeline degrade gracefully when ABS audio splits a chapter that the EPUB keeps whole, or when either side has extra material.

The job's `status` is set to `assembling, progress=92` after transcription finishes and matching begins.

---

## 13. Final Assembly

Matched paragraphs are written to `sync_map.json` in EPUB order:

```python
sync_map = []
for para_id, alignment in zip(para_ids, alignments):
    if alignment is None:
        continue                                          # skipped — see §12
    audio_start, audio_end = alignment
    entry = index[para_id]
    sync_map.append({
        "id": para_id,
        "audio_start": audio_start,
        "audio_end": audio_end,
        "ebook_pos": entry["ebook_pos"],
        "text_snippet": entry["text"],
    })

sync_map_path.write_text(json.dumps(sync_map, indent=2, ensure_ascii=False))
```

Two refinement passes run on `sync_map` before it's persisted:

1. **Chapter snap.** Every entry whose `ebook_pos` ends in `/h[1-6][N]` and whose `text_snippet` matches `PROLOGUE` / `EPILOGUE` / `CHAPTER N` (Arabic or Roman) is forced to the corresponding ABS `chapters[i].start`. `_match_heading_to_abs_chapter` does the lookup by parsing the chapter number from the heading text and finding the ABS chapter whose `title` contains the same number (or the first `Prologue` chapter that doesn't look like a "Part 2+" split). ABS chapter starts are publisher-supplied ground truth; without this step, fuzzy-match drift accumulates to tens of minutes over a long book. Headings with no ABS counterpart get an `unmatched_chapter_heading` warning and are left at their fuzzy-matched time.
2. **Anchor + interpolation.** A forward scan picks every entry whose `audio_start ≥ previous_anchor.audio_end` as an anchor. Chapter-snapped headings (from either snap path) are **mandatory anchors**: they are ground truth and always survive this scan, and an ordinary entry can only become an anchor if it doesn't overshoot the next snapped heading's start — otherwise a fuzzy match running a few seconds past a chapter boundary would demote the snapped heading and re-interpolate it (a 3 s overshoot at a boundary used to become an 11 s heading error). Each run of non-anchor entries between two consecutive anchors is linearly re-distributed across the time span between them; entries before the first anchor are clamped to its start, entries after the last anchor to its end. This bounds local drift to a single chapter and guarantees monotonic timestamps.

After these passes, the pipeline runs `_validate_sync_map` (see [§16a](#16a-validation-warnings)) and any warnings are persisted on the `alignment_jobs.warnings` column (JSON-encoded `list[str]`). The job's terminal `status` is:

- `complete` — no warnings, sync map fully usable
- `complete_with_warnings` — warnings present but sync map is still readable through the API

```python
job.status = "complete_with_warnings" if warnings else "complete"
job.sync_map_path = str(sync_map_path)
job.fragment_count = len(sync_map)
job.warnings = json.dumps(warnings) if warnings else None
job.completed_at = datetime.utcnow()
```

There are no ephemeral artifacts in the cache directory on success — per-chunk WAV slices live in a process tempdir and are deleted as each chunk completes, and the `chunks/` cache subfolder is removed once `transcript.json` is consolidated. `transcript.json` (the cached word list) survives between runs — see §5. Whisper model weights are cached by `huggingface_hub` outside of the project tree.

---

## 14. Output Schema

`sync_map.json` is an ordered JSON array. Each entry corresponds to one paragraph from the ebook and its aligned position in the audio.

```json
[
  {
    "id": "para_001",
    "audio_start": 0.000,
    "audio_end": 5.210,
    "ebook_pos": "/body/DocFragment[1]/body/section[1]/p[1]",
    "text_snippet": "It was the best of times, it was the worst of times,"
  },
  {
    "id": "para_002",
    "audio_start": 5.210,
    "audio_end": 12.100,
    "ebook_pos": "/body/DocFragment[1]/body/section[1]/p[2]",
    "text_snippet": "it was the age of wisdom, it was the age of foolishness,"
  }
]
```

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Sequential paragraph ID (`para_001`, `para_002`, …) |
| `audio_start` | float | Start of aligned audio segment in seconds |
| `audio_end` | float | End of aligned audio segment in seconds |
| `ebook_pos` | string | Full hierarchical XPath to the element: `/body/DocFragment[spine_index]/body/<ancestor_path>/<tag>[sibling_index]` |
| `text_snippet` | string | The paragraph text as extracted from the EPUB |

**XPath format detail:** each step in `ebook_pos` after `DocFragment[N]` corresponds to a real ancestor element in the EPUB HTML, with the element's 1-based index among same-tag siblings at that level. KOReader's CRE engine uses this same format when recording its own reading position. The paths are compatible in both directions — earmark writes positions KOReader can navigate to, and earmark can parse positions KOReader sends back (after stripping any trailing character-offset suffix such as `.0`).

The array is ordered by `audio_start` (ascending), which matches EPUB reading order provided the ebook and audio are aligned chapter-for-chapter.

---

## 15. Dependencies

| Package | Purpose |
|---------|---------|
| `ebooklib` | Open and iterate EPUB spine documents |
| `beautifulsoup4` | Parse HTML within EPUB document items |
| `faster-whisper` (optional `align` extra) | Whisper transcription with word-level timestamps |
| `rapidfuzz` | Monotonic fuzzy substring matching of paragraphs to transcript |
| `ffmpeg-python` | Audio concatenation and format normalization |

`faster-whisper` is a core dependency. Its `ctranslate2` / `onnxruntime` transitive deps currently ship wheels only for Python ≤3.13, which is why `pyproject.toml` pins `requires-python<3.14`.

**System dependencies:**

- `ffmpeg` — audio decoding and concatenation

Install on macOS: `brew install ffmpeg`
Install on Debian/Ubuntu: `apt-get install ffmpeg`

---

## 16. Error Handling

| Scenario | Stage | Behavior |
|----------|-------|----------|
| `ebookFile` is `null` on ABS item (mode `abs`) | Pre-flight | Set `status=failed`, `error_message="No ebook file on ABS item {id}"` |
| CWA OPDS returns zero matches | `fetching_ebook` | Set `status=failed`, log search terms used |
| CWA OPDS returns multiple ambiguous matches | `fetching_ebook` | Set `status=failed`, log all candidate titles — do not guess |
| CWA login fails (bad credentials) | `fetching_ebook` | Set `status=failed`, `error_message="CWA authentication failed"` |
| No EPUB found in `ebook_local_root` | `fetching_ebook` | Set `status=failed`, log normalized search terms |
| Audio download HTTP error | `fetching_audio` | Retry 3× with exponential backoff; fail job after exhaustion |
| Ebook download HTTP error | `fetching_ebook` | Retry 3× with exponential backoff; fail job after exhaustion |
| Unsupported audio format | `fetching_audio` | Set `status=failed`, `error_message="Unsupported audio format: {ext}"` |
| ffmpeg concatenation error | `aligning` | Set `status=failed`, `error_message=str(e)` |
| Concatenated track shorter than the sum of inputs | `aligning` | Set `status=failed` with `"Audio concatenation truncated: …"`. The concat demuxer can skip an unreadable/odd path (e.g. an apostrophe in a filename) and still exit 0, silently dropping every file after it; the duration guard catches this before transcription wastes ~30 min on a partial track. Filenames are escaped per the concat-list syntax (`'` → `'\''`) so this should not trigger in normal operation. |
| `faster-whisper` raises an exception | `aligning` | Capture exception message; set `status=failed`, `error_message=str(e)` |
| Cache stale (`abs_updated_at` newer) | Pre-flight | Delete cached files; create a new `AlignmentJob` row; re-run from scratch |
| Partial cache (audio present, ebook missing) | Pre-flight | Re-download missing files only; reuse what is present |

### 16a. Validation Warnings

After matching, `_validate_sync_map` scores the result. Any warnings are stored as a JSON list on `AlignmentJob.warnings` and the job's status becomes `complete_with_warnings` (sync map is still readable through the API).

| Warning string | Trigger |
|---|---|
| `suspect_first_entry: '<snippet>'` | `sync_map[0].text_snippet` is shorter than 40 characters or matches the blurb attribution pattern `^[—\-–]\s*[A-Z]`. Usually means front matter slipped through `_classify_spine`. |
| `docfragment_gap: missing [N, …]` | A `DocFragment[N]` between the first and last mapped fragment had a substantive amount of EPUB text (≥ `_GAP_MIN_PARAS` paragraphs — i.e. a chapter) yet produced **no** aligned entries. Indicates a whole chapter was skipped — likely a misclassification or a transcript coverage problem. Blank/decorative spine pages (0 paragraphs) and one-line front matter such as a dedication or epigraph are ignored, since they have nothing narratable to align. |
| `low_transcript_coverage: N/M paragraphs unmatched` | More than 10% of EPUB paragraphs failed to fuzzy-match (`score < 45`). Usual causes: back matter still in the EPUB, transcript model too small for the speaker, or large audio gaps. |
| `unmatched_chapter_heading: '<text>'` | An EPUB chapter heading (h1/h2/h3 matching `PROLOGUE` / `EPILOGUE` / `CHAPTER N`) had no counterpart in the ABS `chapters` metadata, so it could not be snapped to a ground-truth start time. The entry is left at its fuzzy-matched position. Usual cause: the ABS chapter title omits the chapter number or uses an unusual format. |
| `audio_offset_excessive: …` | *(legacy)* the current pipeline never pre-trims audio (`audio_offset` is always `0`), so this rule never fires. Preserved in `_validate_sync_map` for the older aeneas API. |
| `chapter_rescale_extreme: …` | *(legacy)* the current pipeline doesn't do chapter rescaling, so its `scales` argument is always empty and this rule never fires. Preserved in `_validate_sync_map` for older callers. |

Operationally: a `complete_with_warnings` job is still consumable — the sync map is written and the API serves it. Warnings just surface what to check: usually re-classifying spine items or bumping `WHISPER_MODEL` to `base.en`/`small.en` resolves the underlying issue.

---

## 17. Verifying Alignment Against ABS

After a job completes, `testing/diff_chapters.py` compares each EPUB chapter heading in `sync_map.json` against the corresponding ABS chapter start. It picks one of two matching modes automatically:

- **`title`** — used when the ABS chapter titles carry chapter numbers (e.g. `Chapter 12: A Lily in Winter`). EPUB headings parse to `chapter 12` / `prologue` / `epilogue` (Arabic or Roman) and look up the ABS chapter by number.
- **`positional (offset=N)`** — used when fewer than half the headings match by title (e.g. ABS labels them `01-68 Remarkably Bright Creatures`). The script finds the offset `N` that minimises mean `|sync_start − abs_start|` across all headings and aligns the *k*-th EPUB heading with the *(N+k)*-th ABS chapter.

```bash
uv run python testing/diff_chapters.py --item-id <ABS_ITEM_ID> --threshold 5.0
```

Output is a per-chapter table with `sync_start`, `abs_start`, signed diff, and the ABS title; rows above `--threshold` (default 5 s) are marked `!`. Summary line reports `matched / total`, `max |diff|`, mean, and the count over threshold. Exit code `0` means everything is within threshold and matched; `2` means at least one heading drifted or didn't match.

Use it on a freshly-aligned book to confirm the chapter-snap step (§13) is doing its job — `Winter's Heart` should report `max |diff| = 0.00 s` after a clean run, while a book with generic ABS titles will fall back to positional mode and report whatever drift the proportional matcher produced.
