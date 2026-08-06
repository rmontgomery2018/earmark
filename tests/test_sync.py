import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import selectinload

from earmark.models import (
    AbsEbookMapping,
    AbsLibraryItem,
    AlignmentJob,
    KosyncUser,
    ReadingProgress,
    User,
)
from earmark.scheduler import _audio_to_kosync, _kosync_to_audio, _load_sync_map, _sync_mapping
from earmark.services.progress import write_reading_progress

DOCUMENT = "aabbccdd" * 4  # 32-char fake MD5
ABS_ITEM_ID = "li_test001"

SYNC_MAP = [
    {"id": "p0", "audio_start": 0.0, "audio_end": 100.0, "ebook_pos": "/body/DocFragment[1]/body/section[1]/p[1]", "text_snippet": "A"},  # noqa: E501
    {"id": "p1", "audio_start": 100.0, "audio_end": 200.0, "ebook_pos": "/body/DocFragment[2]/body/section[1]/p[1]", "text_snippet": "B"},  # noqa: E501
    {"id": "p2", "audio_start": 200.0, "audio_end": 300.0, "ebook_pos": "/body/DocFragment[3]/body/section[1]/p[2]", "text_snippet": "C"},  # noqa: E501
    {"id": "p3", "audio_start": 300.0, "audio_end": 400.0, "ebook_pos": "/body/DocFragment[3]/body/section[1]/p[4]", "text_snippet": "D"},  # noqa: E501
]

DURATION = 400.0

# Matches the config default (settings.sync_abs_idle_seconds); the scenarios below use
# lastUpdate values well outside this window so the stopped/playing distinction is unambiguous.
IDLE_THRESHOLD = 360


def _stopped_ms() -> int:
    """ABS lastUpdate (Unix ms) old enough to count as stopped (past the idle threshold)."""
    return (int(time.time()) - 3600) * 1000


# ---------------------------------------------------------------------------
# write_reading_progress
# ---------------------------------------------------------------------------


async def test_write_reading_progress_creates_record(
    db_session_factory: async_sessionmaker,
) -> None:
    async with db_session_factory() as session:
        ku = KosyncUser(username="bob", password_hash="x")
        session.add(ku)
        await session.commit()
        await session.refresh(ku)

        record = await write_reading_progress(
            session,
            kosync_user_id=ku.id,
            document=DOCUMENT,
            progress="/body/DocFragment[1]/body/p[1]",
            percentage=0.1,
            device="testdev",
            device_id="testdev",
        )

    assert record.id is not None
    assert record.is_latest is True
    assert record.percentage == pytest.approx(0.1)


async def test_write_reading_progress_demotes_previous(
    db_session_factory: async_sessionmaker,
) -> None:
    async with db_session_factory() as session:
        ku = KosyncUser(username="bob", password_hash="x")
        session.add(ku)
        await session.commit()
        await session.refresh(ku)

        first = await write_reading_progress(
            session,
            kosync_user_id=ku.id,
            document=DOCUMENT,
            progress="/body/DocFragment[1]/body/p[1]",
            percentage=0.1,
            device="testdev",
            device_id="testdev",
        )
        await write_reading_progress(
            session,
            kosync_user_id=ku.id,
            document=DOCUMENT,
            progress="/body/DocFragment[2]/body/p[1]",
            percentage=0.5,
            device="testdev",
            device_id="testdev",
        )

        result = await session.execute(
            select(ReadingProgress).where(ReadingProgress.id == first.id)
        )
        old = result.scalar_one()
        assert old.is_latest is False


# ---------------------------------------------------------------------------
# _audio_to_kosync
# ---------------------------------------------------------------------------


def test_audio_to_kosync_mid_entry() -> None:
    pos, pct = _audio_to_kosync(50.0, DURATION, SYNC_MAP)
    assert pos == "/body/DocFragment[1]/body/section[1]/p[1]"
    assert pct == pytest.approx(50.0 / DURATION)


def test_audio_to_kosync_exact_boundary() -> None:
    pos, pct = _audio_to_kosync(100.0, DURATION, SYNC_MAP)
    assert pos == "/body/DocFragment[2]/body/section[1]/p[1]"


def test_audio_to_kosync_past_end_clamps_to_last() -> None:
    pos, pct = _audio_to_kosync(999.0, DURATION, SYNC_MAP)
    assert pos == "/body/DocFragment[3]/body/section[1]/p[4]"
    assert pct == pytest.approx(999.0 / DURATION)


def test_audio_to_kosync_start_of_map() -> None:
    pos, pct = _audio_to_kosync(0.0, DURATION, SYNC_MAP)
    assert pos == "/body/DocFragment[1]/body/section[1]/p[1]"
    assert pct == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# _kosync_to_audio
# ---------------------------------------------------------------------------


def test_kosync_to_audio_exact_match() -> None:
    # Hierarchical XPath matching exactly an entry in the sync map.
    xpath = "/body/DocFragment[2]/body/section[1]/p[1]"
    result = _kosync_to_audio(xpath, SYNC_MAP)
    assert result == pytest.approx(100.0)


def test_kosync_to_audio_char_offset_stripped_before_match() -> None:
    # KOReader appends a character offset (.N) to its positions; it must be
    # stripped before the exact-match attempt.
    xpath = "/body/DocFragment[2]/body/section[1]/p[1].41"
    result = _kosync_to_audio(xpath, SYNC_MAP)
    assert result == pytest.approx(100.0)


def test_kosync_to_audio_closest_element() -> None:
    # DocFragment[3] has section[1]/p[2] (200s) and section[1]/p[4] (300s).
    # An xpath with deepest index 3 is equidistant; min() picks the first candidate.
    xpath = "/body/DocFragment[3]/body/section[1]/div[3]"
    result = _kosync_to_audio(xpath, SYNC_MAP)
    # |3-2| == 1, |3-4| == 1 — tie broken by min() which picks first
    assert result == pytest.approx(200.0)


def test_kosync_to_audio_koreader_fallback_different_tag() -> None:
    # KOReader may send a position whose tag doesn't exist in the sync map for
    # that DocFragment. The fallback uses the deepest bracketed index to find
    # the closest entry.
    xpath = "/body/DocFragment[1]/body/div[1]/text()[1].41"
    result = _kosync_to_audio(xpath, SYNC_MAP)
    assert result == pytest.approx(0.0)


def test_kosync_to_audio_strips_text_node_and_char_offset() -> None:
    # KOReader appends /text().N to point at a character inside a text node;
    # the sync map only records the block element.
    xpath = "/body/DocFragment[2]/body/section[1]/p[1]/text().0"
    assert _kosync_to_audio(xpath, SYNC_MAP) == pytest.approx(100.0)


def test_kosync_to_audio_matches_when_koreader_omits_index_one() -> None:
    # CRE omits [1] for single-sibling tags; the sync map always emits it.
    xpath = "/body/DocFragment[2]/body/section/p[1]"
    assert _kosync_to_audio(xpath, SYNC_MAP) == pytest.approx(100.0)


def test_kosync_to_audio_strips_text_and_omitted_index_one() -> None:
    # Both gaps at once (the reported real-world case).
    xpath = "/body/DocFragment[2]/body/section/p[1]/text().0"
    assert _kosync_to_audio(xpath, SYNC_MAP) == pytest.approx(100.0)


def test_kosync_to_audio_keeps_non_one_indices() -> None:
    # Normalization must not strip indices other than [1].
    xpath = "/body/DocFragment[3]/body/section/p[4]/text().5"
    assert _kosync_to_audio(xpath, SYNC_MAP) == pytest.approx(300.0)


def test_kosync_to_audio_missing_docfragment_returns_none() -> None:
    xpath = "/body/DocFragment[99]/body/p[1]"
    result = _kosync_to_audio(xpath, SYNC_MAP)
    assert result is None


def test_kosync_to_audio_invalid_xpath_returns_none() -> None:
    result = _kosync_to_audio("not-an-xpath", SYNC_MAP)
    assert result is None


# ---------------------------------------------------------------------------
# _load_sync_map
# ---------------------------------------------------------------------------


def test_load_sync_map_success(tmp_path: Path) -> None:
    p = tmp_path / "sync_map.json"
    p.write_text(json.dumps(SYNC_MAP))
    result = _load_sync_map(str(p))
    assert result is not None
    assert len(result) == 4


def test_load_sync_map_missing_file(tmp_path: Path) -> None:
    result = _load_sync_map(str(tmp_path / "nonexistent.json"))
    assert result is None


def test_load_sync_map_malformed_json(tmp_path: Path) -> None:
    p = tmp_path / "sync_map.json"
    p.write_text("not json")
    result = _load_sync_map(str(p))
    assert result is None


def test_load_sync_map_empty_list(tmp_path: Path) -> None:
    p = tmp_path / "sync_map.json"
    p.write_text("[]")
    result = _load_sync_map(str(p))
    assert result is None


# ---------------------------------------------------------------------------
# _sync_mapping helpers
# ---------------------------------------------------------------------------


def _make_abs_client(
    progress_data: dict | None = None, update_raises: Exception | None = None
) -> MagicMock:
    client = MagicMock()
    client.get_progress = AsyncMock(return_value=progress_data)
    if update_raises:
        client.update_progress = AsyncMock(side_effect=update_raises)
    else:
        client.update_progress = AsyncMock()
    return client


async def _setup_mapping(
    session,
    sync_map_path: str,
    *,
    num_kosync_users: int = 1,
) -> AbsEbookMapping:
    lib_item = AbsLibraryItem(
        abs_item_id=ABS_ITEM_ID,
        library_id="lib1",
        title="Test Book",
        audio_file_count=1,
        total_duration_seconds=DURATION,
        raw_metadata="{}",
    )
    session.add(lib_item)
    await session.flush()

    job = AlignmentJob(
        abs_item_id=ABS_ITEM_ID,
        status="complete",
        sync_map_path=sync_map_path,
    )
    session.add(job)
    await session.flush()

    user = User(email="test@example.com", password_hash="x")
    session.add(user)
    await session.flush()

    for i in range(num_kosync_users):
        ku = KosyncUser(username=f"ku{i}", password_hash="x", user_id=user.id)
        session.add(ku)
    await session.commit()

    mapping = AbsEbookMapping(
        user_id=user.id,
        abs_item_id=ABS_ITEM_ID,
        abs_title="Test Book",
        ebook_path="test.epub",
        ebook_filename="test.epub",
        kosync_document=DOCUMENT,
        alignment_job_id=job.id,
    )
    session.add(mapping)
    await session.commit()

    result = await session.execute(
        select(AbsEbookMapping)
        .options(selectinload(AbsEbookMapping.user).selectinload(User.kosync_users))
        .where(AbsEbookMapping.id == mapping.id)
    )
    return result.scalar_one()


# ---------------------------------------------------------------------------
# _sync_mapping scenarios
# ---------------------------------------------------------------------------


async def test_sync_abs_newer_writes_kosync(
    db_session_factory: async_sessionmaker, tmp_path: Path
) -> None:
    sync_map_file = tmp_path / "sync_map.json"
    sync_map_file.write_text(json.dumps(SYNC_MAP))

    async with db_session_factory() as session:
        mapping = await _setup_mapping(session, str(sync_map_file))

        # ABS has progress; KOSync has none → unconditional write to KOSync.
        # lastUpdate is well past the idle threshold (playback stopped).
        abs_data = {"currentTime": 50.0, "duration": DURATION, "lastUpdate": _stopped_ms()}
        client = _make_abs_client(abs_data)

        await _sync_mapping(mapping, client, session, IDLE_THRESHOLD)

        result = await session.execute(
            select(ReadingProgress)
            .where(ReadingProgress.document == DOCUMENT, ReadingProgress.is_latest == True)  # noqa: E712
        )
        records = result.scalars().all()
        assert len(records) == 1
        assert records[0].device == "earmark-sync"
        assert records[0].percentage == pytest.approx(50.0 / DURATION)


async def test_sync_abs_newer_writes_all_kosync_users(
    db_session_factory: async_sessionmaker, tmp_path: Path
) -> None:
    sync_map_file = tmp_path / "sync_map.json"
    sync_map_file.write_text(json.dumps(SYNC_MAP))

    async with db_session_factory() as session:
        mapping = await _setup_mapping(session, str(sync_map_file), num_kosync_users=2)

        abs_data = {"currentTime": 50.0, "duration": DURATION, "lastUpdate": _stopped_ms()}
        client = _make_abs_client(abs_data)

        await _sync_mapping(mapping, client, session, IDLE_THRESHOLD)

        result = await session.execute(
            select(ReadingProgress)
            .where(ReadingProgress.document == DOCUMENT, ReadingProgress.is_latest == True)  # noqa: E712
        )
        records = result.scalars().all()
        assert len(records) == 2


async def test_sync_abs_playing_defers_kosync_write(
    db_session_factory: async_sessionmaker, tmp_path: Path
) -> None:
    sync_map_file = tmp_path / "sync_map.json"
    sync_map_file.write_text(json.dumps(SYNC_MAP))

    async with db_session_factory() as session:
        mapping = await _setup_mapping(session, str(sync_map_file))

        # ABS lastUpdate is recent → playback is active → defer (no KOSync write).
        now_ms = int(time.time() * 1000)
        abs_data = {"currentTime": 50.0, "duration": DURATION, "lastUpdate": now_ms}
        client = _make_abs_client(abs_data)

        await _sync_mapping(mapping, client, session, IDLE_THRESHOLD)

        result = await session.execute(
            select(ReadingProgress)
            .where(ReadingProgress.document == DOCUMENT, ReadingProgress.is_latest == True)  # noqa: E712
        )
        assert result.scalars().all() == []
        assert mapping.last_synced_at is None


async def test_sync_kosync_newer_writes_abs_even_while_abs_playing(
    db_session_factory: async_sessionmaker, tmp_path: Path
) -> None:
    # The idle guard only gates ABS→KOSync; a newer KOSync position must still
    # push to ABS regardless of how recently ABS was playing.
    sync_map_file = tmp_path / "sync_map.json"
    sync_map_file.write_text(json.dumps(SYNC_MAP))

    async with db_session_factory() as session:
        mapping = await _setup_mapping(session, str(sync_map_file))

        ko_user_result = await session.execute(
            select(KosyncUser).where(KosyncUser.username == "ku0")
        )
        ko_user = ko_user_result.scalar_one()
        # KOSync is newer (now) and ahead; ABS is older and recently playing.
        await write_reading_progress(
            session,
            kosync_user_id=ko_user.id,
            document=DOCUMENT,
            progress="/body/DocFragment[3]/body/section[1]/p[2]",  # 200s
            percentage=0.5,
            device="koreader",
            device_id="koreader",
        )

        # ABS played 10s ago (recent → "playing", inside the idle threshold) but is
        # older than the KOSync record, so the direction is KOSync→ABS.
        abs_data = {
            "currentTime": 10.0,
            "duration": DURATION,
            "lastUpdate": (int(time.time()) - 10) * 1000,
        }
        client = _make_abs_client(abs_data)

        await _sync_mapping(mapping, client, session, IDLE_THRESHOLD)

        client.update_progress.assert_awaited_once()


async def test_sync_kosync_newer_writes_abs(
    db_session_factory: async_sessionmaker, tmp_path: Path
) -> None:
    sync_map_file = tmp_path / "sync_map.json"
    sync_map_file.write_text(json.dumps(SYNC_MAP))

    async with db_session_factory() as session:
        mapping = await _setup_mapping(session, str(sync_map_file))

        # Write a KOSync record; ABS has no progress
        ko_user_result = await session.execute(
            select(KosyncUser).where(KosyncUser.username == "ku0")
        )
        ko_user = ko_user_result.scalar_one()
        await write_reading_progress(
            session,
            kosync_user_id=ko_user.id,
            document=DOCUMENT,
            progress="/body/DocFragment[2]/body/p[1]",
            percentage=0.5,
            device="koreader",
            device_id="koreader",
        )

        client = _make_abs_client(None)  # ABS has no progress

        await _sync_mapping(mapping, client, session, IDLE_THRESHOLD)

        client.update_progress.assert_awaited_once()
        call_kwargs = client.update_progress.call_args
        assert call_kwargs.args[0] == ABS_ITEM_ID
        assert call_kwargs.kwargs["current_time"] == pytest.approx(100.0)

        synced = (
            await session.execute(
                select(ReadingProgress).where(
                    ReadingProgress.kosync_user_id == ko_user.id,
                    ReadingProgress.is_latest == True,  # noqa: E712
                )
            )
        ).scalar_one()
        assert synced.abs_synced is True
        assert synced.abs_synced_position_seconds == pytest.approx(100.0)


async def test_sync_kosync_to_abs_failure_leaves_no_position(
    db_session_factory: async_sessionmaker, tmp_path: Path
) -> None:
    sync_map_file = tmp_path / "sync_map.json"
    sync_map_file.write_text(json.dumps(SYNC_MAP))

    async with db_session_factory() as session:
        mapping = await _setup_mapping(session, str(sync_map_file))

        ko_user_result = await session.execute(
            select(KosyncUser).where(KosyncUser.username == "ku0")
        )
        ko_user = ko_user_result.scalar_one()
        await write_reading_progress(
            session,
            kosync_user_id=ko_user.id,
            document=DOCUMENT,
            progress="/body/DocFragment[2]/body/p[1]",
            percentage=0.5,
            device="koreader",
            device_id="koreader",
        )

        client = _make_abs_client(None)
        client.update_progress.side_effect = RuntimeError("abs down")

        await _sync_mapping(mapping, client, session, IDLE_THRESHOLD)

        synced = (
            await session.execute(
                select(ReadingProgress).where(
                    ReadingProgress.kosync_user_id == ko_user.id,
                    ReadingProgress.is_latest == True,  # noqa: E712
                )
            )
        ).scalar_one()
        assert synced.abs_synced is False
        assert synced.abs_synced_position_seconds is None
        assert synced.abs_sync_error == "abs down"


async def test_sync_forward_only_guard_abs_to_kosync(
    db_session_factory: async_sessionmaker, tmp_path: Path
) -> None:
    sync_map_file = tmp_path / "sync_map.json"
    sync_map_file.write_text(json.dumps(SYNC_MAP))

    async with db_session_factory() as session:
        mapping = await _setup_mapping(session, str(sync_map_file))

        ko_user_result = await session.execute(
            select(KosyncUser).where(KosyncUser.username == "ku0")
        )
        ko_user = ko_user_result.scalar_one()
        # KOSync already at 0.9 (older); ABS is newer but at only 0.1 → should skip
        await write_reading_progress(
            session,
            kosync_user_id=ko_user.id,
            document=DOCUMENT,
            progress="/body/DocFragment[3]/body/p[4]",
            percentage=0.9,
            device="koreader",
            device_id="koreader",
            updated_at=datetime.now(UTC) - timedelta(hours=2),
        )

        # ABS is newer than the KOSync record and stopped (idle past threshold),
        # but at a lower position → forward-only guard skips it.
        abs_data = {"currentTime": 10.0, "duration": DURATION, "lastUpdate": _stopped_ms()}
        client = _make_abs_client(abs_data)

        await _sync_mapping(mapping, client, session, IDLE_THRESHOLD)

        # No new record should be written (only the existing koreader one exists)
        result = await session.execute(
            select(ReadingProgress)
            .where(ReadingProgress.document == DOCUMENT, ReadingProgress.is_latest == True)  # noqa: E712
        )
        records = result.scalars().all()
        assert len(records) == 1
        assert records[0].device == "koreader"


async def test_sync_forward_only_guard_kosync_to_abs(
    db_session_factory: async_sessionmaker, tmp_path: Path
) -> None:
    sync_map_file = tmp_path / "sync_map.json"
    sync_map_file.write_text(json.dumps(SYNC_MAP))

    async with db_session_factory() as session:
        mapping = await _setup_mapping(session, str(sync_map_file))

        ko_user_result = await session.execute(
            select(KosyncUser).where(KosyncUser.username == "ku0")
        )
        ko_user = ko_user_result.scalar_one()
        # KOSync is newer but at DocFragment[1] which maps to 0.0s; ABS is already at 200s → skip
        await write_reading_progress(
            session,
            kosync_user_id=ko_user.id,
            document=DOCUMENT,
            progress="/body/DocFragment[1]/body/p[1]",
            percentage=0.1,
            device="koreader",
            device_id="koreader",
        )

        # ABS is older (past timestamp) but further ahead
        past_ms = (int(time.time()) - 3600) * 1000
        abs_data = {"currentTime": 200.0, "duration": DURATION, "lastUpdate": past_ms}
        client = _make_abs_client(abs_data)

        await _sync_mapping(mapping, client, session, IDLE_THRESHOLD)

        client.update_progress.assert_not_awaited()


async def test_sync_no_kosync_users_skips(
    db_session_factory: async_sessionmaker, tmp_path: Path
) -> None:
    sync_map_file = tmp_path / "sync_map.json"
    sync_map_file.write_text(json.dumps(SYNC_MAP))

    async with db_session_factory() as session:
        mapping = await _setup_mapping(session, str(sync_map_file), num_kosync_users=0)
        client = _make_abs_client({"currentTime": 50.0, "duration": DURATION, "lastUpdate": 0})

        await _sync_mapping(mapping, client, session, IDLE_THRESHOLD)

        client.get_progress.assert_not_awaited()


async def test_sync_docfragment_not_in_map_skips_abs_update(
    db_session_factory: async_sessionmaker, tmp_path: Path
) -> None:
    sync_map_file = tmp_path / "sync_map.json"
    sync_map_file.write_text(json.dumps(SYNC_MAP))

    async with db_session_factory() as session:
        mapping = await _setup_mapping(session, str(sync_map_file))

        ko_user_result = await session.execute(
            select(KosyncUser).where(KosyncUser.username == "ku0")
        )
        ko_user = ko_user_result.scalar_one()
        await write_reading_progress(
            session,
            kosync_user_id=ko_user.id,
            document=DOCUMENT,
            progress="/body/DocFragment[99]/body/p[1]",  # not in sync map
            percentage=0.5,
            device="koreader",
            device_id="koreader",
        )

        past_ms = (int(time.time()) - 3600) * 1000
        abs_data = {"currentTime": 10.0, "duration": DURATION, "lastUpdate": past_ms}
        client = _make_abs_client(abs_data)

        await _sync_mapping(mapping, client, session, IDLE_THRESHOLD)

        client.update_progress.assert_not_awaited()
