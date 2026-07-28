#!/usr/bin/env python
"""Compare sync_map chapter headings against ABS chapter metadata.

Reads the sync map for an ABS item, fetches the item's chapter list from
Audiobookshelf, and prints a per-chapter diff between the sync map's heading
timestamps and ABS's ground-truth chapter starts. Useful for verifying that
the chapter-snap step in the alignment pipeline is working correctly, and
for spotting books whose EPUB headings don't match the ABS chapter titles.

Usage:
    uv run python testing/diff_chapters.py --item-id <ABS_ITEM_ID>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())).strip()


# Drop-cap / small-caps chapter markup ("C<small>HAPTER</small> 1") extracts
# as "C HAPTER 1"; rejoin the leading letter so it matches like "CHAPTER 1".
_DROP_CAP_RE = re.compile(r"^\s*([A-Za-z])\s+(?=[A-Za-z])")


def _collapse_drop_cap(s: str) -> str:
    return _DROP_CAP_RE.sub(r"\1", s or "")


def _chapter_number(title: str) -> int | None:
    """Pull the chapter number out of an ABS title like 'Chapter 5: Flags'."""
    m = re.search(r"chapter\s*(\d+)", title or "", re.IGNORECASE)
    return int(m.group(1)) if m else None


def _build_abs_lookup(chapters: list[dict]) -> dict[str, dict]:
    """Build several lookup tables for matching sync_map headings to ABS chapters.

    Keys tried in order:
      'chapter <N>'  → first ABS chapter with that number in its title
      'prologue'     → first ABS chapter whose title contains 'prologue'
                       and does not look like a "(Part 2)" split
      'epilogue'     → first ABS chapter whose title contains 'epilogue'
      <normalized title>  → exact normalized match against ABS title
                            (catches "Day 1,299 of My Captivity"-style books)
    """
    out: dict[str, dict] = {}
    for ch in chapters:
        title = ch.get("title", "") or ""
        norm_title = _norm(title)
        if norm_title and norm_title not in out:
            out[norm_title] = ch
        num = _chapter_number(title)
        if num is not None:
            key = f"chapter {num}"
            out.setdefault(key, ch)
        low = title.lower()
        if "prologue" in low:
            part = re.search(r"part\s+(\d+)", low)
            if not part or int(part.group(1)) == 1:
                out.setdefault("prologue", ch)
        if "epilogue" in low:
            out.setdefault("epilogue", ch)
    return out


_HEADING_POS = re.compile(r"/h[1-6]\[\d+\]$")


def _heading_lookup_keys(entry: dict) -> list[str]:
    """Candidate lookup keys for matching a sync_map heading to ABS."""
    text = entry.get("text_snippet", "").strip()
    keys = []
    for norm in dict.fromkeys(
        n for n in (_norm(text), _norm(_collapse_drop_cap(text))) if n
    ):
        keys.append(norm)
        m = re.match(r"chapter\s+(\d+)", norm)
        if m:
            keys.append(f"chapter {int(m.group(1))}")
        if norm.startswith("prologue"):
            keys.append("prologue")
        if norm.startswith("epilogue"):
            keys.append("epilogue")
    return keys


def _is_heading(entry: dict) -> bool:
    return bool(_HEADING_POS.search(entry.get("ebook_pos", "")))


def _fmt_time(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


async def _fetch_chapters(item_id: str) -> list[dict]:
    """Fetch ABS chapters live so we don't depend on DB cache freshness."""
    from earmark.services.audiobookshelf import AudiobookshelfClient

    client = AudiobookshelfClient()
    try:
        item = await client.get_item(item_id)
    finally:
        await client.close()
    return item.get("media", {}).get("chapters", []) or []


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare sync_map chapter headings to ABS chapter starts.",
    )
    parser.add_argument("--item-id", required=True, help="ABS item UUID")
    parser.add_argument(
        "--cache-dir",
        default=".cache/earmark",
        help="Path to the alignment cache root (default: .cache/earmark)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=5.0,
        help="Mark rows |diff| > THRESHOLD seconds as BAD (default: 5.0)",
    )
    args = parser.parse_args()

    sync_path = Path(args.cache_dir) / args.item_id / "sync_map.json"
    if not sync_path.exists():
        print(f"Error: sync_map not found at {sync_path}", file=sys.stderr)
        return 1
    sync = json.loads(sync_path.read_text(encoding="utf-8"))

    chapters = await _fetch_chapters(args.item_id)
    if not chapters:
        print(f"Error: ABS returned no chapters for {args.item_id}", file=sys.stderr)
        return 1

    abs_lookup = _build_abs_lookup(chapters)
    all_headings = [e for e in sync if _is_heading(e)]

    # Title-based matching pass.
    primary: list[tuple[int, dict | None]] = []
    for i, h in enumerate(all_headings):
        ch = None
        for key in _heading_lookup_keys(h):
            ch = abs_lookup.get(key)
            if ch is not None:
                break
        primary.append((i, ch))

    title_hit = sum(1 for _, ch in primary if ch is not None)
    mode = "title"
    # In title mode, restrict to headings that look like primary chapter
    # markers (PROLOGUE / EPILOGUE / CHAPTER N). Books like Winter's Heart
    # split each chapter into two h2 elements — h2[1] is "CHAPTER 13" and
    # h2[2] is the chapter subtitle ("Wonderful News"). Subtitles aren't
    # chapter boundaries and we don't want to flag them as unmatched.
    if title_hit > 0:
        headings = []
        title_matches: list[tuple[int, dict | None]] = []
        for (i, ch), h in zip(primary, all_headings):
            looks_chapter = bool(
                re.match(r"^\s*(prologue|epilogue|chapter\s+\S+)",
                         _collapse_drop_cap(h.get("text_snippet", "")),
                         re.IGNORECASE)
            )
            if ch is not None or looks_chapter:
                headings.append(h)
                title_matches.append((i, ch))
    else:
        headings = all_headings
        title_matches = primary
    # Fall back to positional alignment when title matching covers less than half
    # of the headings. ABS chapter titles are sometimes generic ("01-68 …") with
    # no narrative names; in that case the Nth EPUB heading lines up with the
    # (N + offset)-th ABS chapter, where offset is chosen to minimise drift on
    # the first paragraph.
    if title_hit < len(headings) // 2 and headings and chapters:
        mode = "positional"
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
        positional = []
        for i, _ in enumerate(headings):
            abs_idx = offset + i
            positional.append(chapters[abs_idx] if abs_idx < len(chapters) else None)
        title_matches = list(zip(range(len(headings)), positional))
        mode = f"positional (offset={offset})"

    print(f"sync_map: {sync_path}")
    print(f"ABS chapters: {len(chapters)}   EPUB headings: {len(headings)}")
    print(f"match mode: {mode}   threshold: {args.threshold:.1f}s\n")
    print(
        f"  {'#':>3}  {'EPUB heading':30s}  {'sync_start':>12s}  "
        f"{'abs_start':>12s}  {'diff':>10s}  {'ABS title':30s}"
    )
    print("  " + "-" * 110)

    bad = 0
    unmatched = 0
    diffs: list[float] = []
    for (i, ch), h in zip(title_matches, headings):
        text = h.get("text_snippet", "").strip()
        sync_t = float(h["audio_start"])
        if ch is None:
            unmatched += 1
            print(
                f"  {i:>3}  {text[:30]:30s}  {_fmt_time(sync_t):>12s}  "
                f"{'??':>12s}  {'??':>10s}  (no ABS match)"
            )
            continue
        abs_t = float(ch["start"])
        diff = sync_t - abs_t
        diffs.append(abs(diff))
        flag = " " if abs(diff) <= args.threshold else "!"
        if abs(diff) > args.threshold:
            bad += 1
        print(
            f"{flag} {i:>3}  {text[:30]:30s}  {_fmt_time(sync_t):>12s}  "
            f"{_fmt_time(abs_t):>12s}  {diff:+10.2f}  {(ch.get('title') or '')[:30]:30s}"
        )

    matched = len(headings) - unmatched
    print()
    print(f"matched: {matched}/{len(headings)}   unmatched: {unmatched}")
    if diffs:
        print(
            f"|diff| max={max(diffs):.2f}s   mean={sum(diffs) / len(diffs):.2f}s   "
            f"over threshold: {bad}"
        )
    return 0 if bad == 0 and unmatched == 0 else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
