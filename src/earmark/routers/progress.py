from datetime import UTC
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from earmark.app_settings import get_effective_float, get_effective_str
from earmark.auth import get_current_user
from earmark.config import settings
from earmark.database import get_session
from earmark.earmark_auth import get_current_earmark_user
from earmark.models import KosyncUser, ReadingProgress, User
from earmark.schemas import (
    DocumentSummary,
    ProgressList,
    ProgressListItem,
    ProgressResponse,
    ProgressUpsert,
    RecordDelete,
)
from earmark.services.progress import write_reading_progress

router = APIRouter(prefix="/syncs", tags=["syncs"])


def _to_response(r: ReadingProgress) -> ProgressResponse:
    return ProgressResponse(
        document=r.document,
        progress=r.progress,
        percentage=r.percentage,
        device=r.device,
        device_id=r.device_id,
        timestamp=int(r.updated_at.replace(tzinfo=UTC).timestamp()),
    )


def _to_list_item(r: ReadingProgress) -> ProgressListItem:
    return ProgressListItem(
        id=r.id,
        document=r.document,
        progress=r.progress,
        percentage=r.percentage,
        device=r.device,
        device_id=r.device_id,
        timestamp=int(r.updated_at.replace(tzinfo=UTC).timestamp()),
        filename=r.filename,
        title=r.title,
        authors=r.authors,
        is_latest=r.is_latest,
        abs_synced=r.abs_synced,
        abs_synced_at=(
            int(r.abs_synced_at.replace(tzinfo=UTC).timestamp()) if r.abs_synced_at else None
        ),
        abs_synced_position_seconds=r.abs_synced_position_seconds,
        abs_sync_error=r.abs_sync_error,
    )


@router.put("/progress", response_model=ProgressResponse)
async def upsert_progress(
    body: ProgressUpsert,
    user: KosyncUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ProgressResponse:
    min_movement = await get_effective_float(
        "sync_min_movement", settings.sync_min_movement, session
    )
    record = await write_reading_progress(
        session,
        kosync_user_id=user.id,
        document=body.document,
        progress=body.progress,
        percentage=body.percentage,
        device=body.device,
        device_id=body.device_id,
        filename=body.metadata.filename if body.metadata else None,
        title=body.metadata.title if body.metadata else None,
        authors=body.metadata.authors if body.metadata else None,
        min_movement=min_movement,
    )
    return _to_response(record)


@router.get("/progress/{document}", response_model=ProgressResponse)
async def get_progress(
    document: str,
    user: KosyncUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ProgressResponse:
    result = await session.execute(
        select(ReadingProgress).where(
            ReadingProgress.kosync_user_id == user.id,
            ReadingProgress.document == document,
            ReadingProgress.is_latest == True,  # noqa: E712
        )
    )
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail="Not found")
    return _to_response(record)


@router.get("/progress", response_model=ProgressList)
async def list_progress(
    document: str = Query(...),
    user: KosyncUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=100),
) -> ProgressList:
    where = [
        ReadingProgress.kosync_user_id == user.id,
        ReadingProgress.document == document,
    ]
    count_result = await session.execute(
        select(func.count()).select_from(ReadingProgress).where(*where)
    )
    total = count_result.scalar_one()

    rows_result = await session.execute(
        select(ReadingProgress)
        .where(*where)
        .order_by(ReadingProgress.updated_at.desc(), ReadingProgress.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    records = rows_result.scalars().all()

    return ProgressList(
        data=[_to_list_item(r) for r in records],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.delete("/progress/{document}")
async def delete_progress(
    document: str,
    user: KosyncUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    exists = await session.execute(
        select(func.count())
        .select_from(ReadingProgress)
        .where(
            ReadingProgress.kosync_user_id == user.id,
            ReadingProgress.document == document,
        )
    )
    if exists.scalar_one() == 0:
        raise HTTPException(status_code=404, detail="Not found")
    await session.execute(
        delete(ReadingProgress).where(
            ReadingProgress.kosync_user_id == user.id,
            ReadingProgress.document == document,
        )
    )
    await session.commit()
    return {"deleted": document}


_SORT_COLUMNS = {
    "title": ReadingProgress.title,
    "percentage": ReadingProgress.percentage,
    "progress": ReadingProgress.progress,
    "device": ReadingProgress.device,
    "is_latest": ReadingProgress.is_latest,
    "updated_at": ReadingProgress.updated_at,
}

web_router = APIRouter(prefix="/web", tags=["web"])


@web_router.get("/config")
async def web_config(session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    tz = await get_effective_str("timezone", settings.timezone, session)
    return {"timezone": tz}


@web_router.get("/documents", response_model=list[DocumentSummary])
async def web_list_documents(
    user: User = Depends(get_current_earmark_user),
    session: AsyncSession = Depends(get_session),
) -> list[DocumentSummary]:
    result = await session.execute(
        select(ReadingProgress.document, func.max(ReadingProgress.title).label("title"))
        .join(KosyncUser, ReadingProgress.kosync_user_id == KosyncUser.id)
        .where(KosyncUser.user_id == user.id)
        .group_by(ReadingProgress.document)
        .order_by(ReadingProgress.document)
    )
    rows = result.all()
    return [DocumentSummary(document=r.document, title=r.title) for r in rows]


@web_router.get("/progress", response_model=ProgressList)
async def web_list_progress(
    user: User = Depends(get_current_earmark_user),
    session: AsyncSession = Depends(get_session),
    document: str | None = Query(default=None),
    sort_by: Literal[
        "title", "percentage", "progress", "device", "is_latest", "updated_at"
    ] = Query(default="updated_at"),
    sort_dir: Literal["asc", "desc"] = Query(default="desc"),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=100),
) -> ProgressList:
    where = [KosyncUser.user_id == user.id]
    if document is not None:
        where.append(ReadingProgress.document == document)

    col = _SORT_COLUMNS[sort_by]
    order_col = col.asc() if sort_dir == "asc" else col.desc()

    count_result = await session.execute(
        select(func.count())
        .select_from(ReadingProgress)
        .join(KosyncUser, ReadingProgress.kosync_user_id == KosyncUser.id)
        .where(*where)
    )
    total = count_result.scalar_one()

    rows_result = await session.execute(
        select(ReadingProgress)
        .join(KosyncUser, ReadingProgress.kosync_user_id == KosyncUser.id)
        .where(*where)
        .order_by(order_col, ReadingProgress.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    records = rows_result.scalars().all()

    return ProgressList(
        data=[_to_list_item(r) for r in records],
        total=total,
        page=page,
        per_page=per_page,
    )


@web_router.post("/records/delete")
async def web_delete_records(
    body: RecordDelete,
    user: User = Depends(get_current_earmark_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, list[int]]:
    if not body.ids:
        return {"deleted": []}

    result = await session.execute(
        select(ReadingProgress)
        .join(KosyncUser, ReadingProgress.kosync_user_id == KosyncUser.id)
        .where(ReadingProgress.id.in_(body.ids), KosyncUser.user_id == user.id)
    )
    records = result.scalars().all()
    deleted_ids = [r.id for r in records]

    # (kosync_user_id, document) pairs where a *latest* record was removed
    affected = {(r.kosync_user_id, r.document) for r in records if r.is_latest}

    for record in records:
        await session.delete(record)
    await session.flush()

    for kosync_user_id, document in affected:
        next_result = await session.execute(
            select(ReadingProgress)
            .where(
                ReadingProgress.kosync_user_id == kosync_user_id,
                ReadingProgress.document == document,
            )
            .order_by(ReadingProgress.updated_at.desc(), ReadingProgress.id.desc())
            .limit(1)
        )
        next_record = next_result.scalar_one_or_none()
        if next_record is not None:
            next_record.is_latest = True

    await session.commit()
    return {"deleted": deleted_ids}
