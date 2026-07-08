"""Tests for the sync_progress() orchestration loop in earmark.scheduler.

The per-mapping logic (_sync_mapping and its helpers) is covered in test_sync.py.
These tests exercise the top-level loop: which mappings it selects, that it
isolates per-mapping failures, and that it records run status.
"""

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from earmark import scheduler
from earmark.models import (
    AbsEbookMapping,
    AbsLibraryItem,
    AlignmentJob,
    KosyncUser,
    ReadingProgress,
    User,
)

DOCUMENT = "aabbccdd" * 4
ABS_ITEM_ID = "li_sched001"
DURATION = 400.0

SYNC_MAP = [
    {"id": "p0", "audio_start": 0.0, "audio_end": 200.0,
     "ebook_pos": "/body/DocFragment[1]/body/section[1]/p[1]", "text_snippet": "A"},
    {"id": "p1", "audio_start": 200.0, "audio_end": 400.0,
     "ebook_pos": "/body/DocFragment[2]/body/section[1]/p[1]", "text_snippet": "B"},
]


def _mock_abs_client(progress_data: dict | None = None) -> MagicMock:
    client = MagicMock()
    client.get_progress = AsyncMock(return_value=progress_data)
    client.update_progress = AsyncMock()
    client.close = AsyncMock()
    return client


async def _add_mapping(
    session,
    *,
    abs_item_id: str = ABS_ITEM_ID,
    document: str = DOCUMENT,
    job_status: str = "complete",
    sync_map_path: str | None = None,
) -> None:
    session.add(
        AbsLibraryItem(
            abs_item_id=abs_item_id,
            library_id="lib1",
            title="Test Book",
            audio_file_count=1,
            total_duration_seconds=DURATION,
            raw_metadata="{}",
        )
    )
    job = AlignmentJob(abs_item_id=abs_item_id, status=job_status, sync_map_path=sync_map_path)
    session.add(job)
    user = User(email=f"{abs_item_id}@example.com", password_hash="x")
    session.add(user)
    await session.flush()
    session.add(KosyncUser(username=f"ku_{abs_item_id}", password_hash="x", user_id=user.id))
    session.add(
        AbsEbookMapping(
            user_id=user.id,
            abs_item_id=abs_item_id,
            abs_title="Test Book",
            ebook_path="test.epub",
            ebook_filename="test.epub",
            kosync_document=document,
            alignment_job_id=job.id,
        )
    )
    await session.commit()


@pytest.fixture
def patch_scheduler(monkeypatch, db_session_factory: async_sessionmaker):
    """Point sync_progress at the in-memory test DB and a default abs client."""
    monkeypatch.setattr(scheduler, "AsyncSessionLocal", db_session_factory)
    client = _mock_abs_client()
    monkeypatch.setattr(scheduler, "AudiobookshelfClient", lambda **kwargs: client)
    # Reset the shared status record between tests.
    scheduler.sync_status.last_run_at = None
    scheduler.sync_status.last_error = None
    scheduler.sync_status.last_synced_count = None
    return client


async def test_sync_progress_writes_kosync_and_records_status(
    patch_scheduler, db_session_factory: async_sessionmaker, tmp_path: Path
) -> None:
    sync_map_file = tmp_path / "sync_map.json"
    sync_map_file.write_text(json.dumps(SYNC_MAP))
    patch_scheduler.get_progress = AsyncMock(
        return_value={
            "currentTime": 100.0,
            "duration": DURATION,
            # Idle past the threshold (playback stopped) so ABS→KOSync writes.
            "lastUpdate": (int(time.time()) - 3600) * 1000,
        }
    )

    async with db_session_factory() as session:
        await _add_mapping(session, sync_map_path=str(sync_map_file))

    await scheduler.sync_progress()

    async with db_session_factory() as session:
        records = (
            await session.execute(
                select(ReadingProgress).where(ReadingProgress.document == DOCUMENT)
            )
        ).scalars().all()
    assert len(records) == 1
    assert records[0].device == "earmark-sync"

    assert scheduler.sync_status.last_run_at is not None
    assert scheduler.sync_status.last_error is None
    assert scheduler.sync_status.last_synced_count == 1
    patch_scheduler.close.assert_awaited()


async def test_sync_progress_defers_when_recently_playing(
    patch_scheduler, db_session_factory: async_sessionmaker, tmp_path: Path
) -> None:
    sync_map_file = tmp_path / "sync_map.json"
    sync_map_file.write_text(json.dumps(SYNC_MAP))
    patch_scheduler.get_progress = AsyncMock(
        return_value={
            "currentTime": 100.0,
            "duration": DURATION,
            # lastUpdate is now → within the idle threshold, so ABS is "still playing".
            "lastUpdate": int(time.time()) * 1000,
        }
    )

    async with db_session_factory() as session:
        await _add_mapping(session, sync_map_path=str(sync_map_file))

    await scheduler.sync_progress()

    async with db_session_factory() as session:
        records = (
            await session.execute(
                select(ReadingProgress).where(ReadingProgress.document == DOCUMENT)
            )
        ).scalars().all()
    # ABS→KOSync was deferred because playback looks active.
    assert records == []


async def test_sync_progress_ignore_idle_writes_anyway(
    patch_scheduler, db_session_factory: async_sessionmaker, tmp_path: Path
) -> None:
    sync_map_file = tmp_path / "sync_map.json"
    sync_map_file.write_text(json.dumps(SYNC_MAP))
    patch_scheduler.get_progress = AsyncMock(
        return_value={
            "currentTime": 100.0,
            "duration": DURATION,
            # Recent lastUpdate that would normally trigger the idle deferral.
            "lastUpdate": int(time.time()) * 1000,
        }
    )

    async with db_session_factory() as session:
        await _add_mapping(session, sync_map_path=str(sync_map_file))

    await scheduler.sync_progress(ignore_idle=True)

    async with db_session_factory() as session:
        records = (
            await session.execute(
                select(ReadingProgress).where(ReadingProgress.document == DOCUMENT)
            )
        ).scalars().all()
    # Idle guard bypassed → the write happens despite the recent lastUpdate.
    assert len(records) == 1
    assert records[0].device == "earmark-sync"


async def test_sync_progress_skips_incomplete_jobs(
    patch_scheduler, db_session_factory: async_sessionmaker, tmp_path: Path
) -> None:
    sync_map_file = tmp_path / "sync_map.json"
    sync_map_file.write_text(json.dumps(SYNC_MAP))

    async with db_session_factory() as session:
        await _add_mapping(session, job_status="pending", sync_map_path=str(sync_map_file))

    await scheduler.sync_progress()

    async with db_session_factory() as session:
        records = (
            await session.execute(
                select(ReadingProgress).where(ReadingProgress.document == DOCUMENT)
            )
        ).scalars().all()
    assert records == []
    assert scheduler.sync_status.last_synced_count == 0
    assert scheduler.sync_status.last_error is None


async def test_sync_progress_isolates_per_mapping_errors(
    patch_scheduler, db_session_factory: async_sessionmaker, tmp_path: Path, monkeypatch
) -> None:
    sync_map_file = tmp_path / "sync_map.json"
    sync_map_file.write_text(json.dumps(SYNC_MAP))

    async with db_session_factory() as session:
        await _add_mapping(
            session, abs_item_id="li_a", document="a" * 32, sync_map_path=str(sync_map_file)
        )
        await _add_mapping(
            session, abs_item_id="li_b", document="b" * 32, sync_map_path=str(sync_map_file)
        )

    calls: list[str] = []

    async def fake_sync_mapping(mapping, abs_client, session, idle_threshold):
        calls.append(mapping.abs_item_id)
        if mapping.abs_item_id == "li_a":
            raise RuntimeError("boom")

    monkeypatch.setattr(scheduler, "_sync_mapping", fake_sync_mapping)

    # Must not raise even though one mapping fails.
    await scheduler.sync_progress()

    # Both mappings were attempted; the failure was isolated.
    assert set(calls) == {"li_a", "li_b"}
    assert scheduler.sync_status.last_synced_count == 1
    assert scheduler.sync_status.last_error is None
