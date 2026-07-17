# Bidirectional ABS ↔ KOSync Progress Sync

This document describes the scheduled sync job that keeps AudioBookshelf (ABS) audio progress and KOSync ebook progress in agreement. It relies on the sync map produced by the alignment pipeline (see [`docs/AudioBookEbookMapping.md`](AudioBookEbookMapping.md)) to convert positions between the two systems.

## Table of Contents

1. [Overview](#1-overview)
2. [Sync Rules](#2-sync-rules)
3. [Position Conversion](#3-position-conversion)
   - [3a. ABS → KOSync](#3a-abs--kosync)
   - [3b. KOSync → ABS](#3b-kosync--abs)
4. [ABS API Integration](#4-abs-api-integration)
5. [KOSync Write Logic](#5-kosync-write-logic)
6. [Warning and Skip Conditions](#6-warning-and-skip-conditions)
7. [Scheduler Integration](#7-scheduler-integration)
8. [Error Handling](#8-error-handling)

---

## 1. Overview

earmark runs a periodic sync job (default every 5 minutes) that compares reading progress between ABS and KOSync for every active mapping. The side with the newer timestamp wins and its position is pushed to the other side. Only forward progress is allowed — neither side is ever updated to an earlier position.

**Prerequisites for a mapping to be synced:**

- `AbsEbookMapping.kosync_document` is set (the MD5 hash linking to `ReadingProgress.document`)
- `AbsEbookMapping.alignment_job_id` points to an `AlignmentJob` with `status = "complete"` and a non-null `sync_map_path`
- The earmark `User` who owns the mapping has at least one linked `KosyncUser`

---

## 2. Sync Rules

For each eligible mapping, the sync job performs the following steps:

**Step 1 — Fetch both sides**

| Side | Field | Type | Source |
|------|-------|------|--------|
| ABS | `currentTime` | float (seconds) | `GET /api/me/progress/{abs_item_id}` |
| ABS | `lastUpdate` | int (Unix ms) | same response |
| ABS | `duration` | float (seconds) | same response |
| KOSync | `progress` | string (XPath) | latest `ReadingProgress` row for `kosync_document` |
| KOSync | `percentage` | float (0–1) | same row |
| KOSync | `updated_at` | datetime | same row |

**Step 2 — Compare timestamps**

Convert ABS `lastUpdate` (Unix ms) to a UTC datetime and compare against KOSync `updated_at`.

```
abs_ts  = datetime.fromtimestamp(lastUpdate / 1000, tz=UTC)
ko_ts   = ReadingProgress.updated_at (UTC-aware)
```

**Step 3 — Determine direction and apply forward-only guard**

| Condition | Action |
|-----------|--------|
| `abs_ts > ko_ts` | ABS is newer → attempt to update KOSync |
| `ko_ts > abs_ts` | KOSync is newer → attempt to update ABS |
| Timestamps equal | No-op |
| ABS has no progress | KOSync has no progress | No-op |
| ABS has progress, KOSync has none | Write ABS position to KOSync unconditionally |
| KOSync has progress, ABS has none | Write KOSync position to ABS unconditionally |

**Idle guard (ABS → KOSync only, applied before the forward-only guard):**

While an audiobook is actively playing, ABS's `lastUpdate` advances every sync cycle, which
would otherwise produce a new KOSync entry each cycle. To collapse a listening session into a
single write, ABS → KOSync is deferred until ABS has been idle for at least
`SYNC_ABS_IDLE_SECONDS` (default 360). If `now - abs_lastUpdate < SYNC_ABS_IDLE_SECONDS`, the
update is skipped for this run (and `last_synced_at` is left untouched so the next cycle
re-evaluates). KOSync → ABS is unaffected.

**Forward-only guard (applied before writing):**

- ABS → KOSync: if `new_percentage ≤ current_kosync_percentage`, log WARN and skip.
- KOSync → ABS: if `new_current_time ≤ abs_current_time`, log WARN and skip.

**Manual trigger:**

Besides the scheduled interval, a sync can be run on demand:

- `POST /web/sync/run` — fire-and-forget. Starts `sync_progress()` as a background task and
  returns immediately: `{"started": true, "already_running": false}`. If a sync (scheduled or
  manual) is already in flight, it returns `{"started": false, "already_running": true}` — runs
  are serialized by a module-level lock in `scheduler.py`. The outcome of the run is observed
  via `GET /web/sync-status`.
- Query flag `ignore_abs_playing` (default `false`): when `true`, the run sets the idle
  threshold to `0`, bypassing the "still playing" deferral above so ABS → KOSync writes
  regardless of how recently ABS updated.

This is surfaced in the UI as the **Manual Sync** card on the Settings page, with a **Sync now**
button and an **Ignore ABS playing** checkbox that maps to `ignore_abs_playing`.

---

## 3. Position Conversion

The sync map (`AlignmentJob.sync_map_path`) is a JSON array of entries linking audio timestamps to EPUB XPath positions:

```json
[
  {
    "id": "para_001",
    "audio_start": 142.3,
    "audio_end": 148.7,
    "ebook_pos": "/body/DocFragment[3]/body/section[1]/div[2]/p[1]",
    "text_snippet": "It was the best of times, it was the worst of times,"
  }
]
```

**XPath format:** `ebook_pos` values reflect the **actual DOM hierarchy** of the EPUB HTML — every intermediate container element (`section`, `div`, `article`, etc.) is included and indexed among its same-tag siblings. This matches the format KOReader's CRE engine uses internally, so the position can be navigated to directly.

For example, a paragraph nested inside a section and div produces:
```
/body/DocFragment[3]/body/section[1]/div[2]/p[1]
```

Not the flattened shorthand `/body/DocFragment[3]/body/p[N]`, which only works when `<p>` elements are direct children of `<body>`.

### 3a. ABS → KOSync

**Input:** `currentTime` (float, seconds into the audiobook), `duration` (float, total seconds)

**Algorithm:**

1. Binary-search the sync map for the entry where `audio_start ≤ currentTime < audio_end`. If `currentTime` exceeds the last entry's `audio_end`, use the last entry.
2. Return:
   - `ebook_pos` — the XPath string from that entry (used as `ReadingProgress.progress`)
   - `percentage = currentTime / duration`

**Example:**

```
currentTime = 145.0, duration = 3600.0
→ matches para_001 (audio_start=142.3, audio_end=148.7)
→ ebook_pos = "/body/DocFragment[3]/body/section[1]/div[2]/p[1]"
→ percentage = 145.0 / 3600.0 = 0.0403
```

### 3b. KOSync → ABS

**Input:** KOSync XPath string from KOReader (e.g. `/body/DocFragment[15]/body/section[1]/div[3].41`)

KOReader's CRE engine records positions in a hierarchical XPath that differs from the sync map format in two ways:

1. **Trailing `/text()` node selector** with optional `[N]` sibling index and `.N` character offset — points at a character inside a text node. The sync map only records the block element.
   ```
   /body/DocFragment[11]/body/section[1]/p[3]/text().42
   ```
2. **Omitted `[1]` sibling index** for single-sibling tags (CRE convention). The sync map always emits the index (1-based among same-tag siblings).
   ```
   KOReader  : /body/DocFragment[19]/body/section/p[11]
   sync map  : /body/DocFragment[19]/body/section[1]/p[11]
   ```

earmark normalizes both paths before comparison: strip `/text()(\[N\])?(\.N)?` and `.N` from the tail, and strip all `[1]` indices anywhere in the path. Non-`[1]` indices survive unchanged.

**Algorithm:**

1. Parse the DocFragment index `N` from the XPath using `/body/DocFragment\[(\d+)\]/`.
2. Collect all sync map entries whose `ebook_pos` contains `DocFragment[N]`.
3. Normalize the input XPath and each candidate's `ebook_pos` (rules above). Try exact match — if found, return that entry's `audio_start`.
4. Fallback: extract the deepest bracketed index from the path segment after `DocFragment[N]`. Find the sync map entry within `DocFragment[N]` whose deepest index is closest.
5. Return `audio_start` of the best match.

If step 2 produces no entries (DocFragment not found in sync map), log WARN and return `None` to skip the update.

**Example:**

```
XPath        = "/body/DocFragment[19]/body/section/p[11]/text().0"
→ N          = 19
→ normalize  → "/body/DocFragment[19]/body/section/p[11]"
sync_map[k]  = "/body/DocFragment[19]/body/section[1]/p[11]"
→ normalize  → "/body/DocFragment[19]/body/section/p[11]"
→ exact match → return that entry's audio_start
```

**Note on DocFragment numbering:** `ebook_pos` values in the sync map use the 1-based index of each item in the EPUB spine (all items, including non-XHTML), matching KOReader's crengine DocFragment numbering. Large logical chapters are sometimes split across multiple spine HTML files by publishers, so a single logical chapter may span several DocFragment indices.

---

## 4. ABS API Integration

All requests use the shared Bearer token from `settings.audiobookshelf_api_key`.

### 4a. Fetch Progress

```
GET /api/me/progress/{libraryItemId}
Authorization: Bearer {api_key}
```

**Response (200 OK):**

```json
{
  "id": "...",
  "libraryItemId": "li_abc123",
  "currentTime": 145.0,
  "duration": 3600.0,
  "progress": 0.0403,
  "isFinished": false,
  "lastUpdate": 1715000000000
}
```

| Field | Type | Description |
|-------|------|-------------|
| `currentTime` | float | Playback position in seconds |
| `duration` | float | Total audiobook duration in seconds |
| `progress` | float | Fraction 0.0–1.0 |
| `lastUpdate` | int | Unix timestamp in milliseconds (UTC) |

Returns **404** if the user has no progress for this item — treat as no progress, not an error.

### 4b. Update Progress

```
PATCH /api/me/progress/{libraryItemId}
Authorization: Bearer {api_key}
Content-Type: application/json

{
  "currentTime": 310.5,
  "duration": 3600.0,
  "progress": 0.0863
}
```

No meaningful response body. Any non-2xx status is logged as an error and the sync for that mapping is aborted.

---

## 5. KOSync Write Logic

Progress is written directly to the database via a shared helper function extracted from `routers/progress.py`. The upsert logic must not be duplicated — both the HTTP endpoint (`PUT /syncs/progress`) and the sync job call the same function so that any future changes (new fields, `is_latest` behaviour, etc.) apply everywhere.

**Shared helper — `earmark/services/progress.py`:**

```python
async def write_reading_progress(
    session: AsyncSession,
    *,
    kosync_user_id: int,
    document: str,
    progress: str,
    percentage: float,
    device: str,
    device_id: str,
    title: str | None = None,
    authors: str | None = None,
    filename: str | None = None,
) -> ReadingProgress:
    await session.execute(
        update(ReadingProgress)
        .where(
            ReadingProgress.kosync_user_id == kosync_user_id,
            ReadingProgress.document == document,
            ReadingProgress.is_latest == True,
        )
        .values(is_latest=False)
    )
    record = ReadingProgress(
        kosync_user_id=kosync_user_id,
        document=document,
        progress=progress,
        percentage=percentage,
        device=device,
        device_id=device_id,
        title=title,
        authors=authors,
        filename=filename,
        is_latest=True,
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record
```

`routers/progress.py:upsert_progress` is refactored to call `write_reading_progress` instead of containing the upsert inline. The sync job calls the same function.

**Chatty-client de-bloat:** inbound KOSync pushes (`PUT /syncs/progress`) pass `min_movement = sync_min_movement` (a reading fraction, default `0.005`). A push whose percentage differs from the stored latest by less than this is ignored — no new row — so aggressive clients (e.g. Readest) don't bloat `reading_progress`. Each stored record is the new baseline, so a run of small moves still persists once cumulative drift crosses the threshold; `0` disables the guard. The guard applies only to client pushes — the sync job's ABS → KOSync writes pass no `min_movement`, since its audio-time fraction is not comparable to a client's page fraction.

**Device identity:** all sync-job writes use `device = "earmark-sync"` and `device_id = "earmark-sync"`. This identifies the sync job as its own virtual device in the KOSync history, separate from KOReader devices.

**Which KosyncUsers:** when syncing ABS → KOSync, write to **all** `KosyncUser` records owned by the earmark `User` (i.e., `KosyncUser.user_id == mapping.user_id`). This ensures all KOReader devices pick up the updated position on their next sync.

---

## 6. Warning and Skip Conditions

The sync job logs `logger.warning(...)` and silently skips the update in the following cases:

| Condition | Log message |
|-----------|-------------|
| ABS → KOSync: ABS `lastUpdate` idle for less than `SYNC_ABS_IDLE_SECONDS` (still playing) | `"Deferring ABS→KOSync for {abs_item_id}: idle {x:.0f}s < {n}s, still playing"` |
| ABS → KOSync: new `percentage ≤` current KOSync `percentage` | `"Skipping KOSync update for {abs_item_id}: new percentage {x:.4f} ≤ current {y:.4f}"` |
| KOSync → ABS: new `currentTime ≤` current ABS `currentTime` | `"Skipping ABS update for {abs_item_id}: new position {x:.1f}s ≤ current {y:.1f}s"` |
| KOSync → ABS: DocFragment not found in sync map | `"Cannot map KOSync XPath to ABS for {abs_item_id}: DocFragment[{N}] not in sync map"` |
| Mapping has no completed alignment job | `"Skipping {abs_item_id}: no completed alignment job"` |
| Mapping has no `kosync_document` | `"Skipping {abs_item_id}: no kosync_document"` |
| User has no KosyncUsers | `"Skipping user {user_id}: no KosyncUsers linked"` |

---

## 7. Scheduler Integration

The sync job runs on a fixed interval (default 5 minutes, configurable via the `SYNC_INTERVAL_SECONDS` env var) using APScheduler:

```python
# earmark/scheduler.py
async def sync_progress() -> None:
    logger.info("Running progress sync")
    # Collect eligible mapping IDs in one session, then process each in its own
    # session so a failure in one mapping doesn't corrupt the session for others.
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(AbsEbookMapping.id)
            .join(AlignmentJob, AbsEbookMapping.alignment_job_id == AlignmentJob.id)
            .where(
                AbsEbookMapping.kosync_document.isnot(None),
                AlignmentJob.status == "complete",
                AlignmentJob.sync_map_path.isnot(None),
            )
        )
        mapping_ids = list(result.scalars())

    abs_client = AudiobookshelfClient()
    try:
        for mapping_id in mapping_ids:
            try:
                async with AsyncSessionLocal() as session:
                    mapping = (await session.execute(
                        select(AbsEbookMapping)
                        .options(selectinload(AbsEbookMapping.user).selectinload(User.kosync_users))
                        .where(AbsEbookMapping.id == mapping_id)
                    )).scalar_one_or_none()
                    if mapping:
                        await _sync_mapping(mapping, abs_client, session)
            except Exception:
                logger.exception("Error syncing mapping %s", mapping_id)
    finally:
        await abs_client.close()
```

`_sync_mapping` handles one mapping end-to-end: fetching both sides, comparing timestamps, converting positions, applying the forward-only guard, and writing. Each mapping runs inside its own `AsyncSessionLocal` context so that a session error on one mapping does not affect subsequent ones.

The `AsyncSessionLocal` session factory is imported from `earmark.database`.

---

## 8. Error Handling

| Scenario | Behavior |
|----------|----------|
| ABS `GET /api/me/progress` returns 404 | Treat as no ABS progress — sync from KOSync to ABS if KOSync has a position |
| ABS `GET /api/me/progress` returns other error | Log error, skip this mapping for this run |
| ABS `PATCH /api/me/progress` fails | Log error, skip this mapping for this run |
| `sync_map_path` file missing from disk | Log error (`"Sync map not found: {path}"`), skip mapping |
| `sync_map_path` contains malformed JSON | Log error, skip mapping |
| Sync map is empty | Log warning, skip mapping |
| DocFragment[N] not found in sync map (KOSync → ABS) | Log warning (see §6), skip update |
| Exception inside `_sync_mapping` | Log exception with mapping ID, continue to next mapping |
| KosyncUser has no progress for `kosync_document` | Treat as no KOSync progress — sync from ABS to KOSync if ABS has a position |
