import asyncio
import json
import logging
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import ffmpeg
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from earmark.config import settings
from earmark.database import AsyncSessionLocal
from earmark.models import AbsEbookMapping, AbsLibraryItem, AlignmentJob
from earmark.services.audiobookshelf import AudiobookshelfClient
from earmark.services.ebook_sources import CalibreOpdsSource, LocalEbookSource
from earmark.services.progress import link_progress_to_mapping
from earmark.utils import partial_md5, safe_subpath

logger = logging.getLogger(__name__)

# ── synchronous helpers (run via asyncio.to_thread) ────────────────────────────

# Upper bound on the diagnostic raw_metadata blob persisted per library item.
_MAX_RAW_METADATA_CHARS = 256 * 1024

_BLOCK_TAGS: list[str] = ["p", "h1", "h2", "h3", "h4", "h5", "h6"]

ACTIVE_STATUSES: frozenset[str] = frozenset({
    "pending", "fetching_audio", "fetching_ebook",
    "parsing_epub", "aligning", "assembling",
})

# Substring phrases tested against the normalized TOC title.
_FRONT_PHRASES: tuple[str, ...] = (
    "cover", "title page", "half title", "halftitle", "half-title",
    "dedication", "contents", "table of contents",
    "copyright", "imprint", "colophon",
    "preface", "foreword", "introduction",
    "acknowledgments", "acknowledgements",
    "epigraph", "maps",
    "dramatis personae", "cast of characters", "characters",
    "praise", "advance praise", "reviews",
    "also by", "by the same author", "books by",
    "front matter", "frontmatter",
)
_BACK_PHRASES: tuple[str, ...] = (
    "about the author", "about the publisher",
    "acknowledgments", "acknowledgements",
    "advertisement", "newsletter",
    "excerpt", "teaser", "sample chapter", "preview",
    "notes", "endnotes", "appendix", "bibliography",
    "glossary", "credits", "colophon",
    "back matter", "backmatter",
    "also by",
)
# Substring tokens tested against (file_name + " " + item_id).lower().replace("-","_")
_FRONT_FILE_TOKENS: tuple[str, ...] = (
    "cover", "title", "halftitle", "half_title",
    "dedication", "contents", "toc",
    "copyright", "imprint", "colophon",
    "preface", "foreword", "introduction",
    "epigraph", "map", "frontmatter",
    "praise", "alsoby", "also_by",
)
_BACK_FILE_TOKENS: tuple[str, ...] = (
    "about_the_author", "abouttheauthor",
    "about_the_publisher", "aboutthepublisher",
    "acknowledg", "advertisement", "newsletter",
    "excerpt", "teaser", "sample", "preview",
    "endnote", "appendix", "bibliography",
    "glossary", "credits", "backmatter",
)
# Heading text that strongly identifies bodymatter
_BODY_HEADING_RE = re.compile(
    r"^\s*(prologue|epilogue|chapter\b|book\b|part\b|\d+\b|[ivxlcdm]+\.?\s+\S)",
    re.IGNORECASE,
)
# Heading patterns suggesting body even without chapter wording
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _toc_title_map(book: object) -> dict[str, str]:
    """Map spine file_name → TOC title (normalized lower)."""
    out: dict[str, str] = {}

    def walk(items: list) -> None:  # type: ignore[type-arg]
        for it in items:
            if isinstance(it, tuple):
                head, kids = it
                if hasattr(head, "href") and hasattr(head, "title"):
                    href = head.href.split("#")[0]
                    out.setdefault(href, _norm(head.title))
                walk(kids)
            elif hasattr(it, "href"):
                href = it.href.split("#")[0]
                out.setdefault(href, _norm(it.title))

    walk(book.toc)  # type: ignore[union-attr]
    return out


def _landmarks_from_nav(book: object) -> dict[str, str]:
    """Return {href → epub:type role} from the EPUB3 nav landmarks, if any."""
    import ebooklib
    from bs4 import BeautifulSoup

    nav_items = list(book.get_items_of_type(ebooklib.ITEM_NAVIGATION))  # type: ignore[union-attr]
    if not nav_items:
        nav_items = [
            it for it in book.get_items()  # type: ignore[union-attr]
            if "nav" in (getattr(it, "properties", None) or [])
        ]
    out: dict[str, str] = {}
    for nav in nav_items:
        soup = BeautifulSoup(nav.get_content(), "html.parser")
        for nav_el in soup.find_all("nav"):
            ep_type = (nav_el.get("epub:type") or "").strip().lower()
            if "landmarks" not in ep_type:
                continue
            for a in nav_el.find_all("a", href=True):
                role = (a.get("epub:type") or "").strip().lower()
                if role in ("frontmatter", "bodymatter", "backmatter"):
                    href = a["href"].split("#")[0]
                    out[href] = role
    return out


def _is_blurb_shaped(soup: object) -> bool:
    """True when most content is blurbs/attribution (front-matter praise pages)."""
    ps = [p.get_text(" ", strip=True) for p in soup.find_all("p")]  # type: ignore[union-attr]
    ps = [p for p in ps if p]
    if not ps:
        return False
    short_attr = sum(1 for p in ps if len(p) < 80 and re.match(r"^[—\-–]\s*[A-Z]", p))
    if short_attr >= max(2, len(ps) // 3):
        return True
    bq_chars = sum(
        len(bq.get_text(" ", strip=True))
        for bq in soup.find_all(["blockquote", "cite"])  # type: ignore[union-attr]
    )
    total = sum(len(p) for p in ps)
    return bool(total) and bq_chars / total >= 0.5


def _is_substantive(soup: object) -> bool:
    """At least 2 paragraphs of ≥40 chars — likely real narrative."""
    ps = [p.get_text(" ", strip=True) for p in soup.find_all("p")]  # type: ignore[union-attr]
    return sum(1 for p in ps if len(p) >= 40) >= 2


def _heading_text(soup: object) -> str:
    # No separator between text nodes: drop-cap markup (<span>C</span>ROSSROADS)
    # must not gain an injected space — "c rossroads…" matches the
    # Roman-numeral alternative in _BODY_HEADING_RE via the lone "c".
    parts = [
        h.get_text().strip()
        for h in soup.find_all(["h1", "h2", "h3"])  # type: ignore[union-attr]
    ]
    return " ".join(p for p in parts if p).strip()


def _classify_spine_item(
    book: object,
    item_id: str,
    attrs: dict,  # type: ignore[type-arg]
    toc_titles: dict[str, str],
    landmarks: dict[str, str],
) -> str:
    """Return one of {'front', 'back', 'body', 'ambiguous', 'skip'} for a spine entry."""
    import ebooklib
    from bs4 import BeautifulSoup

    item = book.get_item_with_id(item_id)  # type: ignore[union-attr]
    if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
        return "skip"

    href = item.file_name
    role = landmarks.get(href) or landmarks.get(href.split("/")[-1])
    if role == "frontmatter":
        return "front"
    if role == "backmatter":
        return "back"
    if role == "bodymatter":
        return "body"

    linear = (attrs.get("linear") if isinstance(attrs, dict) else None) or "yes"
    soup = BeautifulSoup(item.get_content(), "html.parser")
    heading = _norm(_heading_text(soup))
    toc_title = toc_titles.get(href, "")
    fn_id = (href + " " + item_id).lower().replace("-", "_")

    if heading and _BODY_HEADING_RE.match(heading):
        return "body"
    if linear == "no":
        return "front"

    blurb = _is_blurb_shaped(soup)
    front_hit = (
        any(p in toc_title for p in _FRONT_PHRASES)
        or any(t in fn_id for t in _FRONT_FILE_TOKENS)
        or blurb
    )
    back_hit = (
        any(p in toc_title for p in _BACK_PHRASES)
        or any(t in fn_id for t in _BACK_FILE_TOKENS)
    )

    if front_hit and back_hit:
        return "ambiguous"
    if front_hit:
        return "front"
    if back_hit:
        return "back"
    if not _is_substantive(soup):
        return "ambiguous"
    return "body"


def _classify_spine(book: object) -> tuple[int, int, list[str]]:
    """Walk the spine and return (first_body_pos, last_body_pos, classes).

    Positions are 1-based, inclusive. classes[i] aligns with spine index i (0-based).
    """
    toc_titles = _toc_title_map(book)
    landmarks = _landmarks_from_nav(book)
    spine = book.spine  # type: ignore[union-attr]

    classes: list[str] = []
    for entry in spine:
        if isinstance(entry, tuple):
            iid, attrs = entry
        else:
            iid, attrs = entry, {}
        if not isinstance(attrs, dict):
            attrs = {"linear": attrs}
        classes.append(_classify_spine_item(book, iid, attrs, toc_titles, landmarks))

    body_positions = [i + 1 for i, c in enumerate(classes) if c == "body"]
    first_body = body_positions[0] if body_positions else 1
    last_body = body_positions[-1] if body_positions else len(classes)
    return first_body, last_body, classes


def _element_full_xpath(element: object) -> str:
    """Return the XPath from <body> to element, matching KOReader's CRE format.

    Each step counts the element's 1-based position among same-tag siblings at
    that level, e.g. /body/section[1]/div[2]/p[3].
    """
    from bs4 import Tag
    parts: list[str] = []
    node = element
    while isinstance(node, Tag) and node.name not in ("body", "html", "[document]"):
        parent = node.parent
        if not isinstance(parent, Tag) or parent.name in ("html", "[document]"):
            break
        tag = node.name
        siblings = [s for s in parent.children if isinstance(s, Tag) and s.name == tag]
        idx = siblings.index(node) + 1  # 1-based
        parts.append(f"{tag}[{idx}]")
        node = parent
    parts.reverse()
    return "/body/" + "/".join(parts)


def _parse_epub_sync(
    epub_path: Path,
) -> tuple[list[str], dict[str, dict[str, str]], int, int]:
    import ebooklib
    from bs4 import BeautifulSoup
    from ebooklib import epub

    book = epub.read_epub(str(epub_path))
    spine_items = [
        (entry[0] if isinstance(entry, tuple) else entry)
        for entry in book.spine
    ]

    first_body_pos, last_body_pos, _ = _classify_spine(book)

    paragraphs: list[str] = []
    index: dict[str, dict[str, str]] = {}
    seq = 0

    for spine_pos, item_id in enumerate(spine_items, start=1):
        if spine_pos < first_body_pos or spine_pos > last_body_pos:
            continue
        item = book.get_item_with_id(item_id)
        if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        soup = BeautifulSoup(item.get_content(), "html.parser")
        # Skip table-of-contents pages — they're not narrated in audiobooks.
        if soup.find(attrs={"role": "doc-toc"}):
            continue
        for element in soup.find_all(_BLOCK_TAGS):
            text = element.get_text(separator=" ").strip()
            if not text:
                continue
            para_id = f"para_{seq:03d}"
            rel_path = _element_full_xpath(element)
            ebook_pos = f"/body/DocFragment[{spine_pos}]{rel_path}"
            index[para_id] = {"text": text, "ebook_pos": ebook_pos}
            paragraphs.append(text)
            seq += 1

    return paragraphs, index, first_body_pos, last_body_pos


def _ffmpeg_concat_sync(audio_files: list[Path], output_path: Path) -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        concat_list = Path(f.name)
        for p in audio_files:
            # The concat demuxer quotes each path in single quotes; a literal
            # apostrophe in a filename (e.g. "Blacksmith's Puzzle.mp3") would
            # otherwise terminate the quote early, making ffmpeg skip that file
            # and every file after it — silently truncating the track while
            # still exiting 0. Escape ' as '\'' per the concat-list syntax.
            escaped = str(p.absolute()).replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")

    try:
        (
            ffmpeg.input(str(concat_list), format="concat", safe=0)
            .output(str(output_path), ar=16000, ac=1, acodec="pcm_s16le")
            .overwrite_output()
            .run(quiet=True)
        )
    finally:
        concat_list.unlink(missing_ok=True)

    # Guard against silent truncation: the concat demuxer can drop trailing
    # files (e.g. an unreadable path) and still exit 0. Verify the output
    # length matches the sum of the inputs before we spend ~30 min
    # transcribing a track that's missing most of the book.
    expected = sum(float(ffmpeg.probe(str(p))["format"]["duration"]) for p in audio_files)
    actual = float(ffmpeg.probe(str(output_path))["format"]["duration"])
    if actual < expected - 5.0:
        raise RuntimeError(
            f"Audio concatenation truncated: expected ~{expected:.0f}s from "
            f"{len(audio_files)} files but got {actual:.0f}s. "
            "A source file may be unreadable or its path malformed."
        )


async def _run_transcribe_worker(
    audio_path: Path,
    chunk_cache_dir: Path,
    progress_cb: object,  # Callable[[float], None]
) -> int:
    """Spawn ``earmark.services.transcribe_worker`` and stream its progress.

    Returns the chunk count reported by the worker, which the caller uses
    to assemble the final ``words`` list from the on-disk per-chunk cache.
    Raises ``RuntimeError`` if the worker exits non-zero.

    Running the heavy faster-whisper / ctranslate2 / onnxruntime stack in a
    short-lived child process keeps the main FastAPI worker's resident set
    flat across jobs — native buffers (~230 MB on tiny.en) cannot be freed
    from Python and would otherwise accumulate.
    """
    import os
    import sys

    cmd = [
        sys.executable, "-m", "earmark.services.transcribe_worker",
        "--audio-path", str(audio_path),
        "--model", settings.whisper_model,
        "--device", settings.whisper_device,
        "--compute-type", settings.whisper_compute_type,
        "--cpu-threads", str(settings.whisper_cpu_threads),
        "--language", settings.whisper_language,
        "--chunk-seconds", str(settings.whisper_chunk_seconds),
        "--chunk-cache-dir", str(chunk_cache_dir),
    ]
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    assert proc.stdout is not None and proc.stderr is not None

    n_chunks_seen = 0

    async def _read_stdout() -> None:
        nonlocal n_chunks_seen
        while True:
            line = await proc.stdout.readline()  # type: ignore[union-attr]
            if not line:
                return
            try:
                event = json.loads(line.decode("utf-8").strip())
            except Exception:
                logger.debug("worker stdout (unparsed): %s", line)
                continue
            kind = event.get("event")
            if kind == "start":
                n_chunks_seen = int(event.get("n_chunks", 0))
            elif kind == "progress":
                progress_cb(float(event["percent"]))  # type: ignore[operator]

    async def _read_stderr() -> None:
        while True:
            line = await proc.stderr.readline()  # type: ignore[union-attr]
            if not line:
                return
            logger.info("transcribe_worker: %s", line.decode("utf-8").rstrip())

    stdout_task = asyncio.create_task(_read_stdout())
    stderr_task = asyncio.create_task(_read_stderr())
    try:
        returncode = await proc.wait()
    finally:
        await stdout_task
        await stderr_task
    if returncode != 0:
        raise RuntimeError(f"transcribe_worker exited {returncode}")
    return n_chunks_seen


def _consolidate_chunk_cache(
    chunk_cache_dir: Path, n_chunks: int,
) -> list[dict]:  # type: ignore[type-arg]
    """Read per-chunk word lists written by the worker into one flat list."""
    words: list[dict] = []  # type: ignore[type-arg]
    for i in range(n_chunks):
        path = chunk_cache_dir / f"{i:04d}.json"
        if not path.exists():
            raise RuntimeError(f"missing chunk cache file: {path}")
        words.extend(json.loads(path.read_text(encoding="utf-8"))["words"])
    return words


def _normalize_text(s: str) -> str:
    """Lowercase, strip non-alphanumeric to spaces, collapse whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", s.lower())).strip()


# Job-progress range the transcription step occupies. Allocating 30..85
# keeps room for parse_epub (≤25), assembly (~92), and final state (100).
_WHISPER_PROGRESS_LO = 30
_WHISPER_PROGRESS_HI = 85


def _stage_progress(percent: float) -> int:
    """Map ``0..100`` transcription percent to the job's 30..85 range."""
    p = max(0.0, min(100.0, float(percent)))
    span = _WHISPER_PROGRESS_HI - _WHISPER_PROGRESS_LO
    return _WHISPER_PROGRESS_LO + int(p * span / 100)


# Minimum EPUB paragraphs a fully-unmapped DocFragment must have had to count
# as a real `docfragment_gap` (a dropped chapter). Heading-only fragments have
# 1 paragraph, dedications/epigraphs 1–3, blank pages 0 — so 5 cleanly excludes
# benign front matter while still catching a lost chapter (dozens+ paragraphs).
_GAP_MIN_PARAS = 5

# EPUB chapter-heading detection ──────────────────────────────────────────────

_EPUB_HEADING_RE = re.compile(r"/h[1-6]\[\d+\]$")
_CHAPTER_HEADING_RE = re.compile(
    r"^\s*(prologue|epilogue|chapter\s+\S+)", re.IGNORECASE
)
_ROMAN_VALUES = {
    "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7,
    "viii": 8, "ix": 9, "x": 10, "xi": 11, "xii": 12, "xiii": 13,
    "xiv": 14, "xv": 15, "xvi": 16, "xvii": 17, "xviii": 18, "xix": 19,
    "xx": 20, "xxi": 21, "xxii": 22, "xxiii": 23, "xxiv": 24, "xxv": 25,
    "xxvi": 26, "xxvii": 27, "xxviii": 28, "xxix": 29, "xxx": 30,
    "xxxi": 31, "xxxii": 32, "xxxiii": 33, "xxxiv": 34, "xxxv": 35,
    "xxxvi": 36, "xxxvii": 37, "xxxviii": 38, "xxxix": 39, "xl": 40,
}


def _parse_chapter_number(token: str) -> int | None:
    """Parse a chapter number from a token — Arabic (12) or Roman (XII)."""
    token = token.strip().rstrip(".,:;").lower()
    if not token:
        return None
    if token.isdigit():
        return int(token)
    return _ROMAN_VALUES.get(token)


def _match_heading_to_abs_chapter(
    heading_text: str,
    chapters: list,  # type: ignore[type-arg]
) -> int | None:
    """Return the ABS chapters[] index that matches a heading like 'CHAPTER 5'.

    Matching rules (case-insensitive against the ABS chapter title):
      - 'prologue' → first ABS chapter whose title contains 'prologue' and
        does NOT look like a "(part 2)" / "(part N>1)" split.
      - 'epilogue' → first ABS chapter whose title contains 'epilogue'.
      - 'chapter <n>' → ABS chapter whose title contains 'chapter <n>'
        (with word boundaries), where <n> can be Arabic or Roman.
    Returns None when no chapter matches.
    """
    text = heading_text.strip().lower()
    if not text:
        return None

    if text.startswith("prologue"):
        for i, ch in enumerate(chapters):
            t = (ch.get("title") or "").lower()
            if "prologue" not in t:
                continue
            part = re.search(r"part\s+(\d+)", t)
            if part and int(part.group(1)) > 1:
                continue
            return i
        return None

    if text.startswith("epilogue"):
        for i, ch in enumerate(chapters):
            if "epilogue" in (ch.get("title") or "").lower():
                return i
        return None

    m = re.match(r"chapter\s+(\S+)", text)
    if not m:
        return None
    n = _parse_chapter_number(m.group(1))
    if n is None:
        return None

    pattern = re.compile(rf"\bchapter\s+{n}\b", re.IGNORECASE)
    for i, ch in enumerate(chapters):
        if pattern.search(ch.get("title") or ""):
            return i
    return None


def _build_transcript_index(
    words: list[dict],  # type: ignore[type-arg]
) -> tuple[str, list[tuple[int, int, float, float]]]:
    """Concatenate words into a normalized transcript and return char→time mapping.

    Returns (transcript, ranges) where ranges[i] = (char_start, char_end, t_start, t_end).
    """
    parts: list[str] = []
    ranges: list[tuple[int, int, float, float]] = []
    pos = 0
    for w in words:
        text = _normalize_text(w["word"])
        if not text:
            continue
        if parts:
            pos += 1
        start = pos
        pos += len(text)
        parts.append(text)
        ranges.append((start, pos, float(w["start"]), float(w["end"])))
    return " ".join(parts), ranges


def _word_index_at_char(ranges: list[tuple[int, int, float, float]], char_pos: int) -> int:
    """Binary search: index of the first word whose char-end is > char_pos."""
    lo, hi = 0, len(ranges)
    while lo < hi:
        mid = (lo + hi) // 2
        if ranges[mid][1] <= char_pos:
            lo = mid + 1
        else:
            hi = mid
    return min(lo, max(0, len(ranges) - 1))


def _char_at_time(
    ranges: list[tuple[int, int, float, float]],
    time_starts: list[float],
    t: float,
) -> int:
    """Char offset of the first transcript word at/after audio time ``t``."""
    import bisect

    j = bisect.bisect_right(time_starts, t) - 1
    j = max(0, min(j, len(ranges) - 1))
    return ranges[j][0]


def _match_in_window(
    p_norm: str,
    transcript: str,
    ranges: list[tuple[int, int, float, float]],
    expected_char: int,
    min_score: float,
) -> tuple[float, float] | None:
    """Fuzzy-match one normalized paragraph in a window around ``expected_char``.

    Returns the matched (audio_start, audio_end) or None when no span scores
    above ``min_score``. The window half-width covers roughly a chapter so a
    slightly-off expected position still finds the paragraph.
    """
    from rapidfuzz import fuzz

    total = len(transcript)
    half = max(8_000, len(p_norm) * 5)
    win_start = max(0, expected_char - half)
    win_end = min(total, expected_char + half)
    if win_start >= total:
        return None

    window = transcript[win_start:win_end]
    match = fuzz.partial_ratio_alignment(p_norm, window)
    if match.score < min_score:
        return None

    m_start = win_start + match.dest_start
    m_end = win_start + match.dest_end
    start_idx = _word_index_at_char(ranges, m_start)
    end_idx = max(_word_index_at_char(ranges, max(m_end - 1, m_start)), start_idx)
    t_start = ranges[start_idx][2]
    t_end = ranges[end_idx][3]
    if t_end < t_start:
        t_end = t_start
    return t_start, t_end


def _align_paragraphs_to_transcript(
    paragraphs: list[str],
    transcript: str,
    ranges: list[tuple[int, int, float, float]],
    min_score: float = 45.0,
) -> list[tuple[float, float] | None]:
    """For each paragraph, return its (audio_start, audio_end) via fuzzy match.

    Each paragraph is searched in a window centered on its **proportional
    expected position** (paragraph i out of n → char i/n through the
    transcript). The window is wide enough to absorb non-uniform ratios of
    EPUB paragraphs to spoken words across chapters.

    Used as the fallback when no EPUB heading maps to an ABS chapter (see
    ``_align_paragraphs_anchored`` for the anchored path). Returns None for
    paragraphs that don't fuzzy-match (front/back matter not narrated, ad
    inserts, etc.).
    """
    n = len(paragraphs)
    total = len(transcript)
    if total == 0 or not ranges:
        return [None] * n

    results: list[tuple[float, float] | None] = []
    for i, p in enumerate(paragraphs):
        p_norm = _normalize_text(p)
        if not p_norm:
            results.append(None)
            continue
        expected = int(total * (i / n))
        results.append(_match_in_window(p_norm, transcript, ranges, expected, min_score))

    return results


def _align_paragraphs_anchored(
    paragraphs: list[str],
    transcript: str,
    ranges: list[tuple[int, int, float, float]],
    anchors: list[tuple[int, float]],
    min_score: float = 45.0,
) -> list[tuple[float, float] | None]:
    """Position each paragraph's search window using ABS chapter anchors.

    ``anchors`` is a list of ``(paragraph_index, audio_time)`` pairs, strictly
    increasing in both fields, derived from EPUB chapter headings matched to
    ground-truth ABS chapter starts. A paragraph's expected audio time is the
    linear interpolation between its bracketing anchors (in paragraph-index
    space), which keeps the search inside the right chapter instead of relying
    on a single global proportional estimate — the latter drifts by tens of
    minutes on books with dense front matter or an unusually long prologue.

    A match is rejected (left None for the interpolation pass to fill) when it
    lands outside the bracketing chapter span, so a repeated phrase can't drag
    a paragraph into a neighbouring chapter. Heading paragraphs that are anchors
    are always emitted so the chapter-snap step can pin them to the exact ABS
    start, even when the audio timeline diverges from ABS.
    """
    import bisect

    n = len(paragraphs)
    total = len(transcript)
    if total == 0 or not ranges or not anchors:
        return [None] * n

    time_starts = [r[2] for r in ranges]
    audio_end = ranges[-1][3]
    a_idx = [a[0] for a in anchors]
    a_time = [a[1] for a in anchors]
    anchor_time_by_idx = dict(anchors)

    results: list[tuple[float, float] | None] = []
    for i, p in enumerate(paragraphs):
        p_norm = _normalize_text(p)
        if not p_norm:
            results.append(None)
            continue

        # An anchor heading must always land in the sync map so the snap step
        # can pin it; its fuzzy position is irrelevant (the snap overwrites it).
        if i in anchor_time_by_idx:
            results.append((anchor_time_by_idx[i], anchor_time_by_idx[i]))
            continue

        # Find the bracketing anchors and the allowed [lo_t, hi_t] audio span.
        if i < a_idx[0]:
            p0, t0, p1, t1 = 0, 0.0, a_idx[0], a_time[0]
        elif i >= a_idx[-1]:
            p0, t0, p1, t1 = a_idx[-1], a_time[-1], n - 1, audio_end
        else:
            k = bisect.bisect_right(a_idx, i) - 1
            p0, t0, p1, t1 = a_idx[k], a_time[k], a_idx[k + 1], a_time[k + 1]
        lo_t, hi_t = t0, t1
        et = t0 + (t1 - t0) * ((i - p0) / (p1 - p0)) if p1 > p0 else t0
        expected_char = _char_at_time(ranges, time_starts, et)

        res = _match_in_window(p_norm, transcript, ranges, expected_char, min_score)
        if res is None or res[0] < lo_t - 30.0 or res[0] > hi_t + 30.0:
            results.append(None)
            continue
        results.append(res)

    return results


def _validate_sync_map(
    sync_map: list[dict],  # type: ignore[type-arg]
    scales: dict[int, float],
    total_duration: float,
    audio_offset: float,
    epub_frag_counts: dict[int, int] | None = None,
) -> list[str]:
    """Return a list of human-readable warnings about a finished sync map."""
    warnings: list[str] = []
    if not sync_map:
        return warnings

    for spine_pos, scale in scales.items():
        if scale < 0.5 or scale > 2.0:
            warnings.append(
                f"chapter_rescale_extreme: DocFragment[{spine_pos}] scale={scale:.2f}"
            )

    first_text = sync_map[0].get("text_snippet", "")
    first_pos = sync_map[0].get("ebook_pos", "")
    # Headings (h1/h2/h3) are legitimately short; only flag short snippets
    # that land on paragraph/inline elements where front matter typically lives.
    is_heading = bool(re.search(r"/h[1-6]\[\d+\]$", first_pos))
    if (not is_heading and len(first_text) < 40) or re.match(r"^[—\-–]\s*[A-Z]", first_text):
        warnings.append(f"suspect_first_entry: {first_text[:80]!r}")

    if total_duration > 0 and audio_offset / total_duration > 0.05:
        warnings.append(
            f"audio_offset_excessive: {audio_offset:.0f}s / {total_duration:.0f}s"
        )

    # A DocFragment is only a real gap if the EPUB had substantive narratable
    # text there (a chapter) yet nothing aligned. Blank/decorative spine pages
    # contribute zero paragraphs and can never appear in the map; one-line
    # front matter (dedication, epigraph) is legitimately unnarrated. Both used
    # to trip this warning when the older heuristic counted every integer
    # position between the first and last mapped fragment as an expected chapter.
    mapped = {
        int(m.group(1))
        for entry in sync_map
        if (m := re.match(r"/body/DocFragment\[(\d+)\]/", entry["ebook_pos"]))
    }
    if mapped and epub_frag_counts:
        lo, hi = min(mapped), max(mapped)
        dropped = sorted(
            pos
            for pos, n in epub_frag_counts.items()
            if lo <= pos <= hi and pos not in mapped and n >= _GAP_MIN_PARAS
        )
        if dropped:
            warnings.append(f"docfragment_gap: missing {dropped}")

    return warnings


# ── pipeline class ──────────────────────────────────────────────────────────────


class AlignmentPipeline:
    def __init__(self, job: AlignmentJob, session: AsyncSession) -> None:
        self.job = job
        self.session = session
        self._abs = AudiobookshelfClient()

    async def run(self) -> None:
        try:
            item_metadata = await self._fetch_abs_metadata()
            cache_dir = self._cache_dir()
            cache_dir.mkdir(parents=True, exist_ok=True)

            chapters = item_metadata.get("media", {}).get("chapters", [])

            audio_dir = await self._download_audio_files(cache_dir, item_metadata)
            ebook_path = await self._download_ebook(cache_dir, item_metadata)
            await self._ensure_kosync_document(ebook_path)
            _paragraphs, index, _first_body_pos, _last_body_pos = await self._parse_epub(ebook_path)

            audio_path = await self._prepare_audio(audio_dir)
            words = await self._transcribe(audio_path)
            await self._assemble_sync_map(cache_dir, words, index, chapters)
        except Exception as exc:
            logger.exception("Alignment job %d failed", self.job.id)
            await self._fail(str(exc))
        finally:
            await self._abs.close()

    # ── stages ─────────────────────────────────────────────────────────────────

    async def _fetch_abs_metadata(self) -> dict:  # type: ignore[type-arg]
        await self._update_status("fetching_audio", progress=5)
        item = await self._abs.get_item(self.job.abs_item_id)

        media = item.get("media", {})
        audio_files = media.get("audioFiles", [])
        ebook_file = media.get("ebookFile")
        metadata = media.get("metadata", {})
        abs_updated_at_ms: int | None = item.get("updatedAt")
        abs_updated_at = (
            datetime.fromtimestamp(abs_updated_at_ms / 1000, tz=UTC)
            if abs_updated_at_ms
            else None
        )

        # Upsert AbsLibraryItem
        result = await self.session.execute(
            select(AbsLibraryItem).where(
                AbsLibraryItem.abs_item_id == self.job.abs_item_id
            )
        )
        lib_item = result.scalar_one_or_none()
        if lib_item is None:
            lib_item = AbsLibraryItem(abs_item_id=self.job.abs_item_id)
            self.session.add(lib_item)

        lib_item.library_id = item.get("libraryId", "")
        lib_item.title = metadata.get("title", "")
        lib_item.author = metadata.get("authorName")
        lib_item.ebook_filename = ebook_file["filename"] if ebook_file else None
        lib_item.ebook_format = (
            ebook_file["ext"].lstrip(".").lower() if ebook_file else None
        )
        lib_item.audio_file_count = len(audio_files)
        lib_item.total_duration_seconds = sum(
            f.get("duration", 0) for f in audio_files
        )
        lib_item.abs_updated_at = abs_updated_at
        # Cap the stored blob so a hostile/oversized ABS payload can't bloat the
        # DB. raw_metadata is diagnostic-only (never parsed back by the app).
        raw = json.dumps(item)
        if len(raw) > _MAX_RAW_METADATA_CHARS:
            raw = json.dumps({"_truncated": True, "bytes": len(raw)})
        lib_item.raw_metadata = raw
        await self.session.commit()

        # Invalidate stale cache
        sentinel = self._cache_dir() / ".abs_updated_at"
        if sentinel.exists() and abs_updated_at:
            cached_ts = sentinel.read_text().strip()
            if cached_ts != abs_updated_at.isoformat():
                shutil.rmtree(self._cache_dir(), ignore_errors=True)
                logger.info("Cache invalidated for %s", self.job.abs_item_id)

        return item  # type: ignore[return-value]

    async def _download_audio_files(
        self, cache_dir: Path, item_metadata: dict  # type: ignore[type-arg]
    ) -> Path:
        audio_dir = cache_dir / "audio"
        audio_files = item_metadata.get("media", {}).get("audioFiles", [])
        sorted_files = sorted(audio_files, key=lambda f: f.get("index", 0))
        width = max(3, len(str(len(sorted_files))))

        for i, af in enumerate(sorted_files):
            ino = af.get("ino", "")
            filename = af.get("metadata", {}).get("filename") or af.get("filename", "")
            dest = audio_dir / f"{i:0{width}}_{filename}"
            if dest.exists():
                continue
            for attempt in range(3):
                try:
                    await self._abs.download_audio_file(
                        self.job.abs_item_id, ino, dest
                    )
                    break
                except httpx.HTTPError as exc:
                    if attempt == 2:
                        raise
                    logger.warning("Audio download attempt %d/3 failed: %s", attempt + 1, exc)
                    await asyncio.sleep(2**attempt)

        await self._update_status("fetching_audio", progress=12, audio_cache_dir=str(audio_dir))

        # Write cache sentinel
        abs_updated_at = item_metadata.get("updatedAt")
        if abs_updated_at:
            sentinel = cache_dir / ".abs_updated_at"
            dt = datetime.fromtimestamp(abs_updated_at / 1000, tz=UTC)
            sentinel.write_text(dt.isoformat())

        return audio_dir

    async def _download_ebook(
        self, cache_dir: Path, item_metadata: dict  # type: ignore[type-arg]
    ) -> Path:
        await self._update_status("fetching_ebook", progress=15)
        ebook_path = cache_dir / "ebook.epub"

        # CLI override: ebook_cache_path already set, copy into standard location
        if self.job.ebook_cache_path and self.job.ebook_cache_path != str(ebook_path):
            src = Path(self.job.ebook_cache_path)
            if not ebook_path.exists():
                shutil.copy2(src, ebook_path)
            await self._update_status(
                "fetching_ebook", progress=18, ebook_cache_path=str(ebook_path)
            )
            return ebook_path

        if ebook_path.exists():
            await self._update_status(
                "fetching_ebook", progress=18, ebook_cache_path=str(ebook_path)
            )
            return ebook_path

        await self._fetch_ebook_from_source(ebook_path, item_metadata)

        await self._update_status("fetching_ebook", progress=18, ebook_cache_path=str(ebook_path))
        return ebook_path

    async def _fetch_ebook_from_source(
        self, dest: Path, item_metadata: dict  # type: ignore[type-arg]
    ) -> None:
        source = self.job.ebook_source

        # Legacy / CLI path: explicit local path on the job.
        if source in (None, "local") and self.job.ebook_path:
            src = safe_subpath(settings.ebook_local_root, self.job.ebook_path)
            if src is None:
                raise ValueError(
                    f"ebook_path escapes the local root: {self.job.ebook_path!r}"
                )
            await asyncio.to_thread(shutil.copy2, src, dest)
            return

        if source == "calibre":
            ref = self.job.ebook_source_ref
            if not ref:
                raise ValueError("Calibre source has no ebook_source_ref")
            await CalibreOpdsSource().fetch(ref, dest)
            return

        if source == "local":
            # Reachable only for legacy/CLI jobs with no explicit ebook_path —
            # mapping-driven jobs always carry one (the POST /web/mappings
            # endpoint requires it for local source).
            media = item_metadata.get("media", {})
            metadata = media.get("metadata", {})
            title = metadata.get("title", "")
            author = metadata.get("authorName")
            local_source = LocalEbookSource()
            candidates = await local_source.search(title, author)
            if not candidates:
                raise ValueError(
                    f"No EPUB found in {settings.ebook_local_root} for "
                    f"title={title!r} author={author!r}"
                )
            await local_source.fetch(candidates[0].ref, dest)
            return

        # Fallback: pull from the ABS item itself.
        await self._download_ebook_from_abs(dest, item_metadata)

    async def _ensure_kosync_document(self, ebook_path: Path) -> None:
        """Fill the mapping's KOReader partial-MD5 from the downloaded epub.

        Calibre mappings can't compute this at creation time (no local file), so
        we do it here once the epub is on disk, then link any pushed progress that
        was waiting on a matching ``kosync_document``.
        """
        result = await self.session.execute(
            select(AbsEbookMapping).where(
                AbsEbookMapping.alignment_job_id == self.job.id
            )
        )
        # A job can back more than one mapping, so handle every one missing a hash.
        mappings = [m for m in result.scalars().all() if not m.kosync_document]
        if not mappings:
            return

        doc = await asyncio.to_thread(partial_md5, ebook_path)
        for mapping in mappings:
            mapping.kosync_document = doc
        await self.session.commit()
        for mapping in mappings:
            await link_progress_to_mapping(self.session, mapping)
        await self.session.commit()

    async def _download_ebook_from_abs(
        self, dest: Path, item_metadata: dict  # type: ignore[type-arg]
    ) -> None:
        ebook_file = item_metadata.get("media", {}).get("ebookFile")
        if not ebook_file:
            raise ValueError(
                f"No ebook file on ABS item {self.job.abs_item_id}"
            )
        for attempt in range(3):
            try:
                await self._abs.download_ebook(self.job.abs_item_id, dest)
                return
            except httpx.HTTPError as exc:
                if attempt == 2:
                    raise
                logger.warning("Ebook download attempt %d/3 failed: %s", attempt + 1, exc)
                await asyncio.sleep(2**attempt)

    async def _parse_epub(
        self, epub_path: Path
    ) -> tuple[list[str], dict[str, dict[str, str]], int, int]:
        await self._update_status("parsing_epub", progress=20)
        paragraphs, index, first_body_pos, last_body_pos = await asyncio.to_thread(
            _parse_epub_sync, epub_path
        )
        await self._update_status("parsing_epub", progress=25, paragraph_count=len(paragraphs))
        return paragraphs, index, first_body_pos, last_body_pos

    async def _prepare_audio(self, audio_dir: Path) -> Path:
        """Concatenate audio files into a single track for transcription."""
        audio_files = sorted(audio_dir.glob("*"))
        audio_files = [f for f in audio_files if f.is_file()]
        concat_path = audio_dir.parent / "concatenated.wav"

        if len(audio_files) == 1 and audio_files[0].suffix.lower() in (".mp3", ".m4b", ".m4a"):
            return audio_files[0]

        await asyncio.to_thread(_ffmpeg_concat_sync, audio_files, concat_path)
        return concat_path

    async def _transcribe(self, audio_path: Path) -> list[dict]:  # type: ignore[type-arg]
        """Run faster-whisper in a worker thread and return word-level timestamps.

        Caches the consolidated transcript at `<cache_dir>/transcript.json`
        keyed on the whisper model — iterating on the matching algorithm
        doesn't require re-running the multi-minute transcription step.
        Per-chunk word lists are also written to
        ``<cache_dir>/chunks/<model>_<chunk_seconds>_<lang>/`` so a
        mid-job restart resumes from the last completed chunk.

        While transcription runs, per-chunk progress events are forwarded
        through a thread-safe queue and written to ``alignment_jobs.progress``
        so the UI poll loop sees the job tick smoothly across 30 → 85. A
        concurrent heartbeat coroutine auto-nudges progress when the model
        goes silent for too long and emits an ``INFO`` log line every minute
        so operators watching the server log can tell the job is still alive.
        """
        await self._update_status("aligning", progress=_WHISPER_PROGRESS_LO)
        cache_path = self._cache_dir() / "transcript.json"
        if cache_path.exists():
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("model") == settings.whisper_model:
                words = cached["words"]
                logger.info("Using cached transcript (%d words)", len(words))
                await self._update_status(
                    "aligning", progress=_WHISPER_PROGRESS_HI, fragment_count=len(words),
                )
                return words

        import queue as _queue

        # The queue carries percent floats from the worker thread plus a
        # single None sentinel to cleanly terminate the drain coroutine. Bounded
        # so a fast producer can't grow it without limit; progress is monotonic,
        # so dropping an update when full is harmless.
        prog_q: _queue.Queue = _queue.Queue(maxsize=512)

        def _cb(percent: float) -> None:
            try:
                prog_q.put_nowait(float(percent))
            except _queue.Full:  # drop — a later update supersedes it
                pass
            except Exception:  # pragma: no cover — never raise out of the worker
                pass

        state = {"last_event": asyncio.get_running_loop().time()}

        async def _drain() -> None:
            last_written = _WHISPER_PROGRESS_LO
            while True:
                try:
                    item = await asyncio.to_thread(prog_q.get, True, 0.5)
                except _queue.Empty:
                    continue
                if item is None:
                    return
                state["last_event"] = asyncio.get_running_loop().time()
                mapped = _stage_progress(item)
                if mapped > last_written:
                    last_written = mapped
                    await self._update_status("aligning", progress=mapped)

        async def _heartbeat() -> None:
            start = asyncio.get_running_loop().time()
            last_log = start
            while True:
                await asyncio.sleep(15)
                now = asyncio.get_running_loop().time()
                cap = _WHISPER_PROGRESS_HI - 1
                if now - state["last_event"] >= 30 and (self.job.progress or 0) < cap:
                    await self._update_status("aligning", progress=(self.job.progress or 0) + 1)
                    state["last_event"] = now
                if now - last_log >= 60:
                    elapsed = int(now - start)
                    logger.info(
                        "alignment still running: elapsed=%dm%02ds progress=%d",
                        elapsed // 60, elapsed % 60, self.job.progress or 0,
                    )
                    last_log = now

        chunk_key = (
            f"{settings.whisper_model}_{settings.whisper_chunk_seconds}_"
            f"{settings.whisper_language}"
        )
        chunk_cache_dir = self._cache_dir() / "chunks" / chunk_key

        drain_task = asyncio.create_task(_drain())
        heartbeat_task = asyncio.create_task(_heartbeat())
        n_chunks = 0
        try:
            n_chunks = await _run_transcribe_worker(
                audio_path=audio_path,
                chunk_cache_dir=chunk_cache_dir,
                progress_cb=_cb,
            )
        finally:
            # Blocking put so the sentinel is never dropped on a full queue
            # (a dropped sentinel would leave _drain looping forever).
            await asyncio.to_thread(prog_q.put, None)  # sentinel — ends the drain loop
            heartbeat_task.cancel()
            await drain_task
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

        words = _consolidate_chunk_cache(chunk_cache_dir, n_chunks)

        cache_path.write_text(
            json.dumps({"model": settings.whisper_model, "words": words}),
            encoding="utf-8",
        )
        # The per-chunk cache is redundant once the consolidated transcript is on disk.
        shutil.rmtree(chunk_cache_dir, ignore_errors=True)
        await self._update_status(
            "aligning", progress=_WHISPER_PROGRESS_HI, fragment_count=len(words),
        )
        return words

    async def _assemble_sync_map(
        self,
        cache_dir: Path,
        words: list[dict],  # type: ignore[type-arg]
        index: dict[str, dict[str, str]],
        chapters: list[dict],  # type: ignore[type-arg]
        warnings: list[str] | None = None,
    ) -> None:
        warnings = list(warnings or [])
        await self._update_status("assembling", progress=92)

        para_count = len(index)
        para_ids = list(index.keys())
        paragraphs = [index[pid]["text"] for pid in para_ids]

        transcript, ranges = await asyncio.to_thread(_build_transcript_index, words)

        # Derive chapter anchors: EPUB headings (Prologue / Chapter N /
        # Epilogue) matched by text to a ground-truth ABS chapter start. These
        # let us position each paragraph's search window inside its own chapter
        # rather than against a single global proportional estimate, which
        # drifts badly on books with heavy front matter or a long prologue.
        # Kept strictly increasing in both paragraph index and time.
        chapter_anchors: list[tuple[int, float]] = []
        if chapters:
            for i, pid in enumerate(para_ids):
                entry = index[pid]
                if not _EPUB_HEADING_RE.search(entry["ebook_pos"]):
                    continue
                m = _CHAPTER_HEADING_RE.match(entry["text"])
                if not m:
                    continue
                abs_idx = _match_heading_to_abs_chapter(m.group(1), chapters)
                if abs_idx is None:
                    continue
                t = float(chapters[abs_idx]["start"])
                if not chapter_anchors or (
                    i > chapter_anchors[-1][0] and t > chapter_anchors[-1][1]
                ):
                    chapter_anchors.append((i, t))

        if chapter_anchors:
            alignments = await asyncio.to_thread(
                _align_paragraphs_anchored,
                paragraphs, transcript, ranges, chapter_anchors,
            )
        else:
            alignments = await asyncio.to_thread(
                _align_paragraphs_to_transcript, paragraphs, transcript, ranges
            )

        unmatched = sum(1 for a in alignments if a is None)
        if unmatched:
            logger.info(
                "Paragraphs not matched in transcript: %d/%d (likely EPUB-only content)",
                unmatched, para_count,
            )

        sync_map: list[dict] = []  # type: ignore[type-arg]
        for para_id, alignment in zip(para_ids, alignments):
            if alignment is None:
                continue
            audio_start, audio_end = alignment
            entry = index[para_id]
            sync_map.append({
                "id": para_id,
                "audio_start": audio_start,
                "audio_end": audio_end,
                "ebook_pos": entry["ebook_pos"],
                "text_snippet": entry["text"],
            })

        # Snap EPUB chapter headings (Prologue / Chapter N / Epilogue) to the
        # ground-truth ABS chapter starts. Without this, fuzzy matching drift
        # accumulates across long books (tens of minutes by mid-book on WoT).
        # The forced timestamps then act as hard anchors for the interpolation
        # pass below, bounding drift to within a single chapter.
        unmatched_headings: list[str] = []
        headings: list[dict] = []  # type: ignore[type-arg]
        snapped_ids: set[str] = set()
        title_hit = 0
        if chapters:
            for entry in sync_map:
                if not _EPUB_HEADING_RE.search(entry["ebook_pos"]):
                    continue
                headings.append(entry)
                m = _CHAPTER_HEADING_RE.match(entry["text_snippet"])
                if not m:
                    continue
                abs_idx = _match_heading_to_abs_chapter(m.group(1), chapters)
                if abs_idx is None:
                    unmatched_headings.append(entry["text_snippet"][:40])
                    continue
                ch_start = float(chapters[abs_idx]["start"])
                ch_end = float(chapters[abs_idx].get("end", ch_start))
                entry["audio_start"] = ch_start
                entry["audio_end"] = min(ch_start + 5.0, ch_end)
                snapped_ids.add(entry["id"])
                title_hit += 1

        # Positional fallback: when no EPUB heading matched a numbered ABS
        # chapter title (e.g. narrative-titled chapters paired with generic
        # ABS titles like "01-68 …"), pick the integer offset that minimises
        # mean |fuzzy_audio_start − chapters[off+i].start| and snap each
        # heading to chapters[off+i].start. Same algorithm as
        # testing/diff_chapters.py — see §17 of docs/AudioBookEbookMapping.md.
        if chapters and title_hit == 0 and headings:
            sync_starts = [float(h["audio_start"]) for h in headings]

            def _mean_abs_diff(off: int) -> float:
                total = 0.0
                cnt = 0
                for i, s in enumerate(sync_starts):
                    idx = off + i
                    if 0 <= idx < len(chapters):
                        total += abs(float(chapters[idx]["start"]) - s)
                        cnt += 1
                return total / cnt if cnt else float("inf")

            max_off = max(1, len(chapters) - len(headings) + 1)
            offset = min(range(max_off), key=_mean_abs_diff)
            snapped = 0
            for i, entry in enumerate(headings):
                abs_idx = offset + i
                if abs_idx >= len(chapters):
                    break
                ch_start = float(chapters[abs_idx]["start"])
                ch_end = float(chapters[abs_idx].get("end", ch_start))
                entry["audio_start"] = ch_start
                entry["audio_end"] = min(ch_start + 5.0, ch_end)
                snapped_ids.add(entry["id"])
                snapped += 1
            logger.info(
                "Positional chapter snap applied: offset=%d, snapped=%d/%d",
                offset, snapped, len(headings),
            )

        # Fuzzy matching can pick the wrong occurrence of a repeated phrase
        # inside a chapter (common in formulaic prose), producing local
        # backward jumps. We use the forward running-max as anchors and
        # linearly interpolate timestamps for regressing runs between
        # consecutive anchors. This keeps the sync map non-decreasing and
        # gives each paragraph a distinct estimated timestamp instead of
        # collapsing whole runs onto a single second.
        # An entry qualifies as an anchor only if its audio_start is at or
        # after the previous anchor's audio_end — that guarantees strictly
        # monotonic timestamps across consecutive anchors, so interpolated
        # spans always have non-negative width.
        #
        # Snapped chapter headings are ground truth and must survive this
        # pass: without special treatment, a fuzzy match that overshoots the
        # next chapter start by even a few seconds would demote the snapped
        # heading to a "regressing" entry and re-interpolate it (e.g. a 3 s
        # overshoot at a chapter boundary became an 11 s heading error).
        # They are therefore mandatory anchors, and an ordinary entry may
        # only become an anchor if it doesn't overshoot the next mandatory
        # one — which also keeps interpolation spans non-negative.
        mandatory: list[int] = []
        for i, entry in enumerate(sync_map):
            if entry["id"] in snapped_ids and (
                not mandatory
                or float(entry["audio_start"])
                > float(sync_map[mandatory[-1]]["audio_start"])
            ):
                mandatory.append(i)

        anchors: list[int] = []
        last_end = -1.0
        mand_pos = 0
        for i, entry in enumerate(sync_map):
            if mand_pos < len(mandatory) and mandatory[mand_pos] == i:
                anchors.append(i)
                last_end = float(entry["audio_end"])
                mand_pos += 1
                continue
            next_mand_start = (
                float(sync_map[mandatory[mand_pos]]["audio_start"])
                if mand_pos < len(mandatory)
                else None
            )
            if entry["audio_start"] >= last_end and (
                next_mand_start is None
                or float(entry["audio_end"]) <= next_mand_start
            ):
                anchors.append(i)
                last_end = entry["audio_end"]

        regressions = len(sync_map) - len(anchors)
        for a, b in zip(anchors, anchors[1:]):
            if b == a + 1:
                continue
            t0 = sync_map[a]["audio_end"]
            t1 = sync_map[b]["audio_start"]
            span = max(0.0, t1 - t0)
            count = b - a
            for k in range(1, count):
                frac = k / count
                sync_map[a + k]["audio_start"] = t0 + span * frac
                sync_map[a + k]["audio_end"] = t0 + span * ((k + 0.5) / count)
        # Leading non-anchors (possible when the first anchor is a snapped
        # heading that earlier entries overshoot): clamp to its start.
        if anchors and anchors[0] > 0:
            head_t = sync_map[anchors[0]]["audio_start"]
            for i in range(anchors[0]):
                sync_map[i]["audio_start"] = head_t
                sync_map[i]["audio_end"] = head_t
        # Trailing non-anchors: clamp to last anchor's end.
        if anchors and anchors[-1] < len(sync_map) - 1:
            last_a = anchors[-1]
            tail_t = sync_map[last_a]["audio_end"]
            for i in range(last_a + 1, len(sync_map)):
                sync_map[i]["audio_start"] = tail_t
                sync_map[i]["audio_end"] = tail_t

        if regressions:
            logger.info(
                "Interpolated %d regressing entries between %d anchors",
                regressions, len(anchors),
            )

        sync_map_path = cache_dir / "sync_map.json"
        sync_map_path.write_text(
            json.dumps(sync_map, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # Clean up ephemeral files
        for ephemeral in ["concatenated.wav"]:
            (cache_dir / ephemeral).unlink(missing_ok=True)

        total_duration = float(chapters[-1].get("end", 0) or 0) if chapters else 0.0
        # Per-DocFragment EPUB paragraph counts, so the gap check can tell a
        # dropped chapter from a blank page or a one-line dedication.
        epub_frag_counts: dict[int, int] = {}
        for entry in index.values():
            m = re.match(r"/body/DocFragment\[(\d+)\]/", entry["ebook_pos"])
            if m:
                pos = int(m.group(1))
                epub_frag_counts[pos] = epub_frag_counts.get(pos, 0) + 1
        # No audio trim → audio_offset is always 0.
        warnings.extend(
            _validate_sync_map(sync_map, {}, total_duration, 0.0, epub_frag_counts)
        )
        # Coverage warning: lots of EPUB paragraphs that didn't match audio.
        if para_count and unmatched / para_count > 0.10:
            warnings.append(
                f"low_transcript_coverage: {unmatched}/{para_count} paragraphs unmatched"
            )
        for h in unmatched_headings:
            warnings.append(f"unmatched_chapter_heading: {h!r}")

        final_status = "complete_with_warnings" if warnings else "complete"
        if warnings:
            logger.warning("Sync map completed with %d warnings: %s", len(warnings), warnings)

        await self._update_status(
            final_status,
            progress=100,
            sync_map_path=str(sync_map_path),
            fragment_count=len(sync_map),
            paragraph_count=para_count,
            audio_offset_seconds=None,
            completed_at=datetime.now(tz=UTC),
            warnings=json.dumps(warnings) if warnings else None,
        )

    # ── helpers ─────────────────────────────────────────────────────────────────

    def _cache_dir(self) -> Path:
        return Path(settings.alignment_cache_dir) / self.job.abs_item_id

    async def _update_status(
        self, status: str, progress: int | None = None, **kwargs: object
    ) -> None:
        self.job.status = status
        if progress is not None:
            self.job.progress = progress
        for k, v in kwargs.items():
            setattr(self.job, k, v)
        logger.debug("_update_status: status=%s progress=%s", status, progress)
        await self.session.commit()

    async def _fail(self, message: str) -> None:
        self.job.status = "failed"
        self.job.error_message = message
        await self.session.commit()


# ── module-level entry point ────────────────────────────────────────────────────


async def recover_orphaned_jobs(
    session_factory: async_sessionmaker | None = None,  # type: ignore[type-arg]
) -> int:
    """Mark any active-status jobs as failed at startup.

    No alignment task can survive a process restart (asyncio tasks die with the
    interpreter), so any job still in an active status must be an orphan.
    Returns the number of jobs marked failed.
    """
    factory = session_factory if session_factory is not None else AsyncSessionLocal
    async with factory() as session:
        result = await session.execute(
            select(AlignmentJob).where(AlignmentJob.status.in_(ACTIVE_STATUSES))
        )
        orphans = list(result.scalars().all())
        for job in orphans:
            job.status = "failed"
            job.error_message = "Interrupted by server restart"
        if orphans:
            await session.commit()
            logger.warning("Marked %d orphaned alignment jobs as failed", len(orphans))
        return len(orphans)


async def run_alignment_job(
    job_id: int,
    session_factory: async_sessionmaker | None = None,  # type: ignore[type-arg]
) -> None:
    """Entry point for the alignment pipeline. Opens its own session.

    The optional session_factory is used in tests to inject the test DB session.
    In production, the module-level AsyncSessionLocal is used.
    """
    factory = session_factory if session_factory is not None else AsyncSessionLocal
    async with factory() as session:
        result = await session.execute(
            select(AlignmentJob).where(AlignmentJob.id == job_id)
        )
        job = result.scalar_one()
        await AlignmentPipeline(job, session).run()
