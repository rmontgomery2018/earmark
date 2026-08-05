import logging
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from earmark.models import AbsEbookMapping, KosyncUser, ReadingProgress

logger = logging.getLogger(__name__)

# Device recorded on rows written by the ABS -> KOSync sync job (see scheduler.py).
# Their `percentage` is an audio-time fraction, not a KOReader page fraction.
SYNC_DEVICE = "earmark-sync"


def _progress_label(document: str, title: str | None) -> str:
    """Identify a document in logs by KOSync document id and title."""
    parts = [f"doc={document}"]
    if title:
        parts.append(f'"{title}"')
    return " ".join(parts)


async def get_mapping_for_document(
    session: AsyncSession, document: str
) -> AbsEbookMapping | None:
    """Return one mapping for a kosync document hash.

    kosync_document is not unique (the same ebook can be mapped more than once),
    so pick the lowest-id match deterministically rather than assuming uniqueness.
    """
    result = await session.execute(
        select(AbsEbookMapping)
        .where(AbsEbookMapping.kosync_document == document)
        .order_by(AbsEbookMapping.id)
        .limit(1)
    )
    return result.scalars().first()


async def resolve_title_from_mapping(session: AsyncSession, document: str) -> str | None:
    mapping = await get_mapping_for_document(session, document)
    return mapping.abs_title if mapping is not None else None


async def backfill_progress_titles(
    session: AsyncSession, *, kosync_user_id: int, document: str, title: str
) -> None:
    await session.execute(
        update(ReadingProgress)
        .where(
            ReadingProgress.kosync_user_id == kosync_user_id,
            ReadingProgress.document == document,
        )
        .values(title=title)
    )


async def link_progress_to_mapping(
    session: AsyncSession, mapping: AbsEbookMapping
) -> None:
    """Associate existing reading progress with a newly-hashed mapping.

    For every KosyncUser that has progress under ``mapping.kosync_document``, claim
    it for the mapping owner (if not already owned) and relabel that progress with
    the mapping title. Mirrors the linking rule in ``write_reading_progress``.
    """
    if not mapping.kosync_document:
        return

    result = await session.execute(
        select(ReadingProgress.kosync_user_id)
        .where(ReadingProgress.document == mapping.kosync_document)
        .distinct()
    )
    kosync_user_ids = list(result.scalars().all())

    for kosync_user_id in kosync_user_ids:
        kosync_user = await session.get(KosyncUser, kosync_user_id)
        if kosync_user is not None and kosync_user.user_id is None:
            kosync_user.user_id = mapping.user_id
            session.add(kosync_user)
        await backfill_progress_titles(
            session,
            kosync_user_id=kosync_user_id,
            document=mapping.kosync_document,
            title=mapping.abs_title,
        )


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
    updated_at: datetime | None = None,
    commit: bool = True,
    min_movement: float | None = None,
) -> ReadingProgress:
    existing_latest = (
        await session.execute(
            select(ReadingProgress).where(
                ReadingProgress.kosync_user_id == kosync_user_id,
                ReadingProgress.document == document,
                ReadingProgress.is_latest == True,  # noqa: E712
            )
        )
    ).scalar_one_or_none()

    label = _progress_label(document, existing_latest.title if existing_latest else title)

    if existing_latest is not None and existing_latest.progress == progress:
        logger.info(
            "Ignoring KOSync push for %s (device=%s): position unchanged (%r)",
            label,
            device,
            progress,
        )
        return existing_latest

    if min_movement and existing_latest is not None:
        # The sync job stores an audio-time fraction as `percentage`, which isn't
        # comparable to a client's page fraction — comparing them would drop real
        # moves. Only the exact-position check above applies against such a baseline.
        if existing_latest.device == SYNC_DEVICE:
            logger.info(
                "Not applying movement threshold for %s (device=%s): "
                "baseline was written by %s, percentages aren't comparable",
                label,
                device,
                SYNC_DEVICE,
            )
        # Forward-only: a deliberate jump back is always stored, since the threshold
        # exists to de-bloat chatty forward drift.
        elif percentage >= existing_latest.percentage and (
            percentage - existing_latest.percentage < min_movement
        ):
            logger.info(
                "Ignoring sub-threshold KOSync push for %s (device=%s): "
                "percentage %.4f%% -> %.4f%% (Δ%.4f < %.4f)",
                label,
                device,
                existing_latest.percentage,
                percentage,
                percentage - existing_latest.percentage,
                min_movement,
            )
            return existing_latest

    mapping = await get_mapping_for_document(session, document)
    mapped_title = mapping.abs_title if mapping is not None else None
    resolved_title = mapped_title or title or document

    if mapping is not None:
        kosync_user = await session.get(KosyncUser, kosync_user_id)
        if kosync_user is not None and kosync_user.user_id is None:
            kosync_user.user_id = mapping.user_id
            session.add(kosync_user)
            await session.flush()

    await session.execute(
        update(ReadingProgress)
        .where(
            ReadingProgress.kosync_user_id == kosync_user_id,
            ReadingProgress.document == document,
            ReadingProgress.is_latest == True,  # noqa: E712
        )
        .values(is_latest=False)
    )

    if mapped_title is not None:
        await session.execute(
            update(ReadingProgress)
            .where(
                ReadingProgress.kosync_user_id == kosync_user_id,
                ReadingProgress.document == document,
            )
            .values(title=resolved_title)
        )

    logger.info(
        "KOSync progress update for %s (device=%s): position %r -> %r, percentage %.4f%% -> %.4f%%",
        _progress_label(document, resolved_title),
        device,
        existing_latest.progress if existing_latest else None,
        progress,
        existing_latest.percentage if existing_latest else 0.0,
        percentage,
    )

    record = ReadingProgress(
        kosync_user_id=kosync_user_id,
        document=document,
        progress=progress,
        percentage=percentage,
        device=device,
        device_id=device_id,
        title=resolved_title,
        authors=authors,
        filename=filename,
        is_latest=True,
        updated_at=updated_at or datetime.now(UTC),
    )
    session.add(record)
    if commit:
        await session.commit()
        await session.refresh(record)
    else:
        await session.flush()
    return record
