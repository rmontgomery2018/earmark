import asyncio
import bisect
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from earmark.app_settings import get_effective_int, get_effective_str
from earmark.config import settings
from earmark.database import AsyncSessionLocal
from earmark.models import AbsEbookMapping, AlignmentJob, KosyncUser, ReadingProgress, User
from earmark.services.audiobookshelf import AudiobookshelfClient
from earmark.services.progress import write_reading_progress

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()

_SYNC_DEVICE = "earmark-sync"

# Serializes sync runs so a manual trigger can't overlap a scheduled one (or another
# manual one). APScheduler's max_instances only guards scheduled-vs-scheduled runs.
_sync_lock = asyncio.Lock()


class SyncStatus:
    """In-memory record of the most recent scheduled sync run."""

    last_run_at: datetime | None = None
    last_duration_seconds: float | None = None
    last_error: str | None = None
    last_synced_count: int | None = None


sync_status = SyncStatus()


# In-process cache of parsed sync maps, keyed by path. Each entry stores the
# file mtime it was parsed from so a regenerated map is reloaded automatically.
_SYNC_MAP_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _load_sync_map(path: str) -> list[dict[str, Any]] | None:
    p = Path(path)
    if not p.exists():
        logger.error("Sync map not found: %s", path)
        _SYNC_MAP_CACHE.pop(path, None)
        return None
    try:
        mtime = p.stat().st_mtime
        cached = _SYNC_MAP_CACHE.get(path)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        entries: list[dict[str, Any]] = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to load sync map %s: %s", path, exc)
        return None
    if not entries:
        logger.warning("Sync map is empty: %s", path)
        return None
    _SYNC_MAP_CACHE[path] = (mtime, entries)
    return entries


def _audio_to_kosync(
    current_time: float, duration: float, sync_map: list[dict[str, Any]]
) -> tuple[str, float]:
    starts = [e["audio_start"] for e in sync_map]
    idx = bisect.bisect_right(starts, current_time) - 1
    idx = max(0, min(idx, len(sync_map) - 1))
    entry = sync_map[idx]
    percentage = current_time / duration if duration > 0 else 0.0
    return entry["ebook_pos"], percentage


_DOCFRAG_RE = re.compile(r"/body/DocFragment\[(\d+)\]")
_BRACKET_IDX_RE = re.compile(r"\[(\d+)\]")
_TEXT_NODE_TAIL_RE = re.compile(r"/text\(\)(?:\[\d+\])?(?:\.\d+)?$")
_CHAR_OFFSET_TAIL_RE = re.compile(r"\.\d+$")
_INDEX_ONE_RE = re.compile(r"\[1\]")


def _normalize_xpath(xpath: str) -> str:
    """Bring a KOReader xpath and a sync_map ebook_pos into a comparable form.

    Strips: trailing /text() node selector (with optional [N] and .N),
    trailing .N char offset, and all [1] sibling indices (CRE omits the
    index when an element has no same-tag siblings; the sync map always
    emits one).
    """
    xpath = _TEXT_NODE_TAIL_RE.sub("", xpath)
    xpath = _CHAR_OFFSET_TAIL_RE.sub("", xpath)
    xpath = _INDEX_ONE_RE.sub("", xpath)
    return xpath


def _kosync_to_audio(xpath: str, sync_map: list[dict[str, Any]]) -> float | None:
    frag_match = _DOCFRAG_RE.search(xpath)
    if not frag_match:
        return None
    n = int(frag_match.group(1))

    candidates = [e for e in sync_map if f"DocFragment[{n}]" in e["ebook_pos"]]
    if not candidates:
        return None

    clean_xpath = _normalize_xpath(xpath)

    # Try exact match first (works when both sides use hierarchical XPaths).
    for entry in candidates:
        if _normalize_xpath(entry["ebook_pos"]) == clean_xpath:
            return float(entry["audio_start"])

    # Fallback: compare deepest bracketed index in the path after DocFragment[n].
    after_frag = clean_xpath.split(f"DocFragment[{n}]", 1)[-1]
    indices = _BRACKET_IDX_RE.findall(after_frag)
    m = int(indices[-1]) if indices else 1

    def _deepest_idx(entry: dict[str, Any]) -> int:
        af = entry["ebook_pos"].split(f"DocFragment[{n}]", 1)[-1]
        idxs = _BRACKET_IDX_RE.findall(af)
        return int(idxs[-1]) if idxs else 1

    best = min(candidates, key=lambda e: abs(_deepest_idx(e) - m))
    return float(best["audio_start"])


def _ensure_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _mapping_label(mapping: AbsEbookMapping) -> str:
    """Identify a mapping in logs by ABS item id, KOSync document id, and title."""
    parts = [f"abs={mapping.abs_item_id}"]
    if mapping.kosync_document:
        parts.append(f"doc={mapping.kosync_document}")
    if mapping.abs_title:
        parts.append(f'"{mapping.abs_title}"')
    return " ".join(parts)


async def _write_abs_to_kosync(
    mapping: AbsEbookMapping,
    abs_data: dict[str, Any],
    sync_map: list[dict[str, Any]],
    session: AsyncSession,
    idle_threshold: int,
    *,
    min_percentage: float | None = None,
) -> None:
    assert mapping.kosync_document  # caller guards this
    abs_updated_at = datetime.fromtimestamp(abs_data["lastUpdate"] / 1000, tz=UTC)
    idle = (datetime.now(UTC) - abs_updated_at).total_seconds()
    if idle < idle_threshold:
        # ABS lastUpdate is still advancing → playback is active. Defer the write so we
        # don't add a KOSync entry per sync cycle; a later cycle will write once playback
        # has stopped. Don't touch last_synced_at so the next cycle re-evaluates.
        logger.info(
            "Deferring ABS→KOSync for %s: idle %.0fs < %ds, still playing",
            _mapping_label(mapping),
            idle,
            idle_threshold,
        )
        return
    ebook_pos, new_pct = _audio_to_kosync(abs_data["currentTime"], abs_data["duration"], sync_map)
    if min_percentage is not None and new_pct <= min_percentage:
        logger.warning(
            "Skipping KOSync update for %s: new %.4f%% <= current %.4f%%",
            _mapping_label(mapping),
            new_pct,
            min_percentage,
        )
        return
    mapping.last_synced_at = datetime.now(UTC)
    # Write every linked KosyncUser and advance last_synced_at in one transaction,
    # so a failure mid-loop rolls the whole mapping back instead of leaving
    # last_synced_at advanced with progress only partially written.
    for ku in mapping.user.kosync_users:
        await write_reading_progress(
            session,
            kosync_user_id=ku.id,
            document=mapping.kosync_document,
            progress=ebook_pos,
            percentage=new_pct,
            device=_SYNC_DEVICE,
            device_id=_SYNC_DEVICE,
            title=mapping.abs_title,
            authors=mapping.abs_author,
            filename=mapping.ebook_filename,
            updated_at=abs_updated_at,
            commit=False,
        )
    await session.commit()
    logger.info("ABS→KOSync %s: %.4f%%", _mapping_label(mapping), new_pct)


async def _write_kosync_to_abs(
    mapping: AbsEbookMapping,
    ko_progress: ReadingProgress,
    abs_data: dict[str, Any] | None,
    abs_client: AudiobookshelfClient,
    sync_map: list[dict[str, Any]],
    session: AsyncSession,
) -> None:
    assert mapping.kosync_document  # caller guards this
    audio_time = _kosync_to_audio(ko_progress.progress, sync_map)
    if audio_time is None:
        frag = _DOCFRAG_RE.search(ko_progress.progress)
        logger.warning(
            "Cannot map KOSync XPath to ABS for %s: DocFragment[%s] not in sync map",
            _mapping_label(mapping),
            frag.group(1) if frag else "?",
        )
        return
    if abs_data is not None and audio_time <= abs_data["currentTime"]:
        duration = abs_data["duration"]
        logger.warning(
            "Skipping ABS update for %s: new %.4f%% <= current %.4f%%",
            _mapping_label(mapping),
            audio_time / duration * 100 if duration else 0.0,
            abs_data["currentTime"] / duration * 100 if duration else 0.0,
        )
        return
    try:
        await abs_client.update_progress(
            mapping.abs_item_id,
            current_time=audio_time,
            duration=abs_data["duration"] if abs_data else 0.0,
            progress=ko_progress.percentage,
        )
    except Exception as exc:
        ko_progress.abs_synced = False
        ko_progress.abs_synced_at = None
        ko_progress.abs_sync_error = str(exc)
        await session.commit()
        logger.error("KOSync→ABS failed for %s: %s", _mapping_label(mapping), exc)
        return
    ko_progress.abs_synced = True
    ko_progress.abs_synced_at = datetime.now(UTC)
    ko_progress.abs_sync_error = None
    mapping.last_synced_at = datetime.now(UTC)
    await session.commit()
    logger.info("KOSync→ABS %s: %.4f%%", _mapping_label(mapping), ko_progress.percentage)


async def _sync_mapping(
    mapping: AbsEbookMapping,
    abs_client: AudiobookshelfClient,
    session: AsyncSession,
    idle_threshold: int,
) -> None:
    abs_item_id = mapping.abs_item_id

    if not mapping.kosync_document:
        logger.warning("Skipping %s: no kosync_document", _mapping_label(mapping))
        return

    if not mapping.user or not mapping.user.kosync_users:
        logger.warning("Skipping user %s: no KosyncUsers linked", mapping.user_id)
        return

    job = mapping.alignment_job
    if not job or job.status not in ("complete", "complete_with_warnings") or not job.sync_map_path:
        logger.warning("Skipping %s: no completed alignment job", _mapping_label(mapping))
        return

    sync_map = await asyncio.to_thread(_load_sync_map, job.sync_map_path)
    if sync_map is None:
        return

    try:
        abs_data = await abs_client.get_progress(abs_item_id)
    except Exception as exc:
        logger.error("Error fetching ABS progress for %s: %s", _mapping_label(mapping), exc)
        return

    ko_result = await session.execute(
        select(ReadingProgress)
        .join(KosyncUser, ReadingProgress.kosync_user_id == KosyncUser.id)
        .where(
            KosyncUser.user_id == mapping.user_id,
            ReadingProgress.document == mapping.kosync_document,
            ReadingProgress.is_latest == True,  # noqa: E712
        )
        .order_by(ReadingProgress.updated_at.desc())
        .limit(1)
    )
    ko_progress = ko_result.scalar_one_or_none()

    if abs_data is None and ko_progress is None:
        return

    # Skip if neither side has changed since last sync
    if abs_data is not None and ko_progress is not None and mapping.last_synced_at is not None:
        abs_ts = datetime.fromtimestamp(abs_data["lastUpdate"] / 1000, tz=UTC)
        ko_ts = _ensure_utc(ko_progress.updated_at)
        last = _ensure_utc(mapping.last_synced_at)
        if abs_ts <= last and ko_ts <= last:
            logger.info(
                "Skipping %s: no change (abs=%s ko=%s last_synced=%s)",
                _mapping_label(mapping),
                abs_ts,
                ko_ts,
                last,
            )
            return

    if ko_progress is None:
        assert abs_data is not None
        await _write_abs_to_kosync(mapping, abs_data, sync_map, session, idle_threshold)
    elif abs_data is None:
        await _write_kosync_to_abs(mapping, ko_progress, None, abs_client, sync_map, session)
    else:
        abs_ts = datetime.fromtimestamp(abs_data["lastUpdate"] / 1000, tz=UTC)
        ko_ts = _ensure_utc(ko_progress.updated_at)
        if abs_ts == ko_ts:
            logger.info("Skipping %s: ABS and KOSync timestamps equal", _mapping_label(mapping))
            return
        if abs_ts > ko_ts:
            await _write_abs_to_kosync(
                mapping,
                abs_data,
                sync_map,
                session,
                idle_threshold,
                min_percentage=ko_progress.percentage,
            )
        else:
            await _write_kosync_to_abs(
                mapping, ko_progress, abs_data, abs_client, sync_map, session
            )


async def sync_progress(ignore_idle: bool = False) -> None:
    async with _sync_lock:
        await _run_sync_progress(ignore_idle)


async def _run_sync_progress(ignore_idle: bool) -> None:
    logger.info("Running progress sync")
    started_at = asyncio.get_event_loop().time()
    synced = 0
    run_error: str | None = None
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(AbsEbookMapping.id)
                .join(AlignmentJob, AbsEbookMapping.alignment_job_id == AlignmentJob.id)
                .where(
                    AbsEbookMapping.kosync_document.isnot(None),
                    AlignmentJob.status.in_(("complete", "complete_with_warnings")),
                    AlignmentJob.sync_map_path.isnot(None),
                )
            )
            mapping_ids: list[int] = list(result.scalars())

            # Resolve effective settings once per cycle (constant for all mappings).
            abs_url = await get_effective_str(
                "audiobookshelf_url", settings.audiobookshelf_url, session
            )
            abs_key = await get_effective_str(
                "audiobookshelf_api_key", settings.audiobookshelf_api_key, session
            )
            idle_threshold = await get_effective_int(
                "sync_abs_idle_seconds", settings.sync_abs_idle_seconds, session
            )
            if ignore_idle:
                # Manual "ignore ABS playing" run: threshold 0 makes the idle guard's
                # `idle < idle_threshold` check always false, so no write is deferred.
                idle_threshold = 0

        abs_client = AudiobookshelfClient(url=abs_url, api_key=abs_key)
        try:
            for mapping_id in mapping_ids:
                try:
                    async with AsyncSessionLocal() as session:
                        mapping_result = await session.execute(
                            select(AbsEbookMapping)
                            .options(
                                selectinload(AbsEbookMapping.user).selectinload(User.kosync_users)
                            )
                            .where(AbsEbookMapping.id == mapping_id)
                        )
                        mapping = mapping_result.scalar_one_or_none()
                        if mapping is None:
                            continue
                        await _sync_mapping(mapping, abs_client, session, idle_threshold)
                        synced += 1
                except Exception:
                    logger.exception("Error syncing mapping %s", mapping_id)
        finally:
            await abs_client.close()
    except Exception as exc:
        run_error = repr(exc)
        logger.exception("Progress sync failed")
    finally:
        elapsed = asyncio.get_event_loop().time() - started_at
        sync_status.last_run_at = datetime.now(UTC)
        sync_status.last_duration_seconds = elapsed
        sync_status.last_error = run_error
        sync_status.last_synced_count = synced
    logger.info("Progress sync complete (%.2fs, %d mappings)", elapsed, synced)


def start_scheduler(interval_seconds: int) -> None:
    scheduler.add_job(
        sync_progress,
        "interval",
        seconds=interval_seconds,
        id="sync_progress",
        replace_existing=True,
        max_instances=1,  # never run two syncs concurrently
        coalesce=True,  # collapse missed runs into one
        misfire_grace_time=interval_seconds,
        next_run_time=datetime.now(UTC),
    )
    scheduler.start()


def reschedule_sync_job(interval_seconds: int) -> None:
    scheduler.reschedule_job(
        "sync_progress",
        trigger="interval",
        seconds=interval_seconds,
    )


def stop_scheduler() -> None:
    scheduler.shutdown()
