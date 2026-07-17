import asyncio
import logging
import shutil
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from earmark.app_settings import get_effective_str
from earmark.config import settings
from earmark.database import get_session
from earmark.earmark_auth import get_current_earmark_user
from earmark.models import (
    AbsEbookMapping,
    AbsLibraryItem,
    AlignmentJob,
    EbookMetadataCache,
    KosyncUser,
    ReadingProgress,
    User,
)
from earmark.schemas import (
    AbsItemSummary,
    EbookCandidate,
    EbookFileSummary,
    MappingCreate,
    MappingRead,
)
from earmark.services.alignment import ACTIVE_STATUSES, run_alignment_job
from earmark.services.audiobookshelf import AudiobookshelfClient
from earmark.services.ebook_sources import CalibreOpdsSource
from earmark.services.progress import backfill_progress_titles
from earmark.utils import partial_md5, safe_subpath

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/web", tags=["mappings"])

_EBOOK_EXTENSIONS = {".epub", ".pdf", ".mobi", ".azw3"}


@router.get("/sync-status")
async def get_sync_status(
    _user: User = Depends(get_current_earmark_user),
) -> dict[str, object]:
    """Report the outcome of the most recent scheduled progress sync."""
    from earmark.scheduler import sync_status

    return {
        "last_run_at": sync_status.last_run_at.isoformat() if sync_status.last_run_at else None,
        "last_duration_seconds": sync_status.last_duration_seconds,
        "last_synced_count": sync_status.last_synced_count,
        "last_error": sync_status.last_error,
        "interval_seconds": settings.sync_interval_seconds,
    }


@router.post("/sync/run")
async def run_sync_now(
    ignore_abs_playing: bool = False,
    _user: User = Depends(get_current_earmark_user),
) -> dict[str, bool]:
    """Fire off a progress sync in the background and return immediately.

    Fire-and-forget: the run's outcome is observable via GET /web/sync-status.
    When ignore_abs_playing is set, the run bypasses the "still playing" idle guard.
    """
    from earmark.scheduler import _sync_lock, sync_progress

    if _sync_lock.locked():
        return {"started": False, "already_running": True}
    asyncio.create_task(sync_progress(ignore_idle=ignore_abs_playing))
    return {"started": True, "already_running": False}


def _check_cache_intact(abs_item_id: str, lib_item: AbsLibraryItem | None) -> bool | None:
    if lib_item is None or lib_item.abs_updated_at is None:
        return None
    sentinel = Path(settings.alignment_cache_dir) / abs_item_id / ".abs_updated_at"
    if not sentinel.exists():
        return False
    return sentinel.read_text().strip() == lib_item.abs_updated_at.isoformat()


def _mapping_to_schema(
    m: AbsEbookMapping,
    lib_item: AbsLibraryItem | None,
    reading_percentage: float | None = None,
) -> MappingRead:
    job = m.alignment_job
    return MappingRead(
        id=m.id,
        user_id=m.user_id,
        abs_item_id=m.abs_item_id,
        abs_title=m.abs_title,
        abs_author=m.abs_author,
        ebook_source=m.ebook_source,
        ebook_path=m.ebook_path,
        ebook_filename=m.ebook_filename,
        ebook_source_ref=m.ebook_source_ref,
        kosync_document=m.kosync_document,
        created_at=m.created_at,
        alignment_job_id=job.id if job else None,
        sync_status=job.status if job else None,
        sync_progress=job.progress if job else None,
        sync_error=job.error_message if job else None,
        sync_warnings=job.warnings if job else None,
        cache_intact=_check_cache_intact(m.abs_item_id, lib_item),
        reading_percentage=reading_percentage,
    )


def _extract_epub_metadata(path: Path) -> tuple[str | None, str | None]:
    try:
        from ebooklib import epub

        book = epub.read_epub(str(path), options={"ignore_ncx": True})
        title = book.get_metadata("DC", "title")
        author = book.get_metadata("DC", "creator")
        return (
            title[0][0] if title else None,
            author[0][0] if author else None,
        )
    except Exception:
        logger.warning("Failed to extract EPUB metadata from %s", path, exc_info=True)
        return None, None


def _extract_pdf_metadata(path: Path) -> tuple[str | None, str | None]:
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        info = reader.metadata
        if info is None:
            return None, None
        return info.title or None, info.author or None
    except Exception:
        logger.warning("Failed to extract PDF metadata from %s", path, exc_info=True)
        return None, None


@router.get("/abs-items", response_model=list[AbsItemSummary])
async def list_abs_items(
    _user: User = Depends(get_current_earmark_user),
    session: AsyncSession = Depends(get_session),
) -> list[AbsItemSummary]:
    abs_url = await get_effective_str(
        "audiobookshelf_url", settings.audiobookshelf_url, session
    )
    abs_key = await get_effective_str(
        "audiobookshelf_api_key", settings.audiobookshelf_api_key, session
    )
    if abs_url and abs_key:
        try:
            client = AudiobookshelfClient(url=abs_url, api_key=abs_key)
            try:
                libraries = await client.list_libraries()
                items: list[AbsItemSummary] = []
                for lib in libraries:
                    raw_items = await client.list_library_items(lib["id"])
                    for item in raw_items:
                        if item.get("mediaType") != "book":
                            continue
                        metadata = item.get("media", {}).get("metadata", {})
                        items.append(
                            AbsItemSummary(
                                abs_item_id=item["id"],
                                title=metadata.get("title", item["id"]),
                                author=metadata.get("authorName") or None,
                            )
                        )
                return items
            finally:
                await client.close()
        except Exception:
            logger.error(
                "Failed to fetch library items from Audiobookshelf at %s",
                abs_url,
                exc_info=True,
            )
            raise HTTPException(
                status_code=503, detail="Failed to fetch library items from Audiobookshelf"
            )

    result = await session.execute(select(AbsLibraryItem).order_by(AbsLibraryItem.title))
    rows = result.scalars().all()
    return [
        AbsItemSummary(abs_item_id=r.abs_item_id, title=r.title, author=r.author) for r in rows
    ]


@router.get("/abs-items/{abs_item_id}/cover")
async def get_abs_item_cover(
    abs_item_id: str,
    _user: User = Depends(get_current_earmark_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    abs_url = await get_effective_str(
        "audiobookshelf_url", settings.audiobookshelf_url, session
    )
    abs_key = await get_effective_str(
        "audiobookshelf_api_key", settings.audiobookshelf_api_key, session
    )
    if not (abs_url and abs_key):
        raise HTTPException(status_code=404, detail="Audiobookshelf not configured")
    client = AudiobookshelfClient(url=abs_url, api_key=abs_key)
    try:
        content, content_type = await client.get_item_cover(abs_item_id)
    except Exception:
        logger.error("Failed to fetch cover for %s", abs_item_id, exc_info=True)
        raise HTTPException(status_code=404, detail="Cover not found")
    finally:
        await client.close()
    return Response(
        content=content, media_type=content_type, headers={"Cache-Control": "max-age=3600"}
    )


@router.get("/ebook-files", response_model=list[EbookFileSummary])
async def list_ebook_files(
    _user: User = Depends(get_current_earmark_user),
    session: AsyncSession = Depends(get_session),
) -> list[EbookFileSummary]:
    root_str = settings.ebook_local_root
    if not root_str:
        return []
    root = Path(root_str)
    if not root.is_dir():
        return []

    cache_result = await session.execute(select(EbookMetadataCache))
    cache_map: dict[str, EbookMetadataCache] = {
        row.path: row for row in cache_result.scalars().all()
    }

    def _scan(
        root: Path, cache: dict[str, EbookMetadataCache]
    ) -> list[dict]:  # type: ignore[type-arg]
        results = []
        for file in root.rglob("*"):
            if file.suffix.lower() not in _EBOOK_EXTENSIONS:
                continue
            try:
                stat = file.stat()
            except OSError:
                logger.debug("Cannot stat %s, skipping", file)
                continue
            path_rel = file.relative_to(root).as_posix()
            cached = cache.get(path_rel)
            if cached and cached.file_mtime == stat.st_mtime and cached.file_size == stat.st_size:
                title, author = cached.title, cached.author
                needs_update = False
            else:
                ext = file.suffix.lower()
                if ext == ".epub":
                    title, author = _extract_epub_metadata(file)
                elif ext == ".pdf":
                    title, author = _extract_pdf_metadata(file)
                else:
                    title, author = None, None
                needs_update = True
            results.append(
                {
                    "path_rel": path_rel,
                    "filename": file.name,
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                    "title": title,
                    "author": author,
                    "needs_update": needs_update,
                }
            )
        return results

    scanned = await asyncio.to_thread(_scan, root, cache_map)

    scanned_paths = {item["path_rel"] for item in scanned}
    stale_paths = set(cache_map.keys()) - scanned_paths
    if stale_paths:
        await session.execute(
            delete(EbookMetadataCache).where(EbookMetadataCache.path.in_(stale_paths))
        )

    for item in scanned:
        if not item["needs_update"]:
            continue
        cached = cache_map.get(item["path_rel"])
        if cached is None:
            session.add(
                EbookMetadataCache(
                    path=item["path_rel"],
                    title=item["title"],
                    author=item["author"],
                    file_mtime=item["mtime"],
                    file_size=item["size"],
                )
            )
        else:
            cached.title = item["title"]
            cached.author = item["author"]
            cached.file_mtime = item["mtime"]
            cached.file_size = item["size"]
    await session.commit()

    return [
        EbookFileSummary(
            path=item["path_rel"],
            filename=item["filename"],
            title=item["title"],
            author=item["author"],
        )
        for item in sorted(scanned, key=lambda x: x["path_rel"])
    ]


@router.get("/mappings", response_model=list[MappingRead])
async def list_mappings(
    user: User = Depends(get_current_earmark_user),
    session: AsyncSession = Depends(get_session),
) -> list[MappingRead]:
    result = await session.execute(
        select(AbsEbookMapping)
        .where(AbsEbookMapping.user_id == user.id)
        .order_by(AbsEbookMapping.created_at.desc())
    )
    mappings = list(result.scalars().all())

    abs_item_ids = {m.abs_item_id for m in mappings}
    lib_result = await session.execute(
        select(AbsLibraryItem).where(AbsLibraryItem.abs_item_id.in_(abs_item_ids))
    )
    lib_by_id = {li.abs_item_id: li for li in lib_result.scalars().all()}

    kosync_docs = [m.kosync_document for m in mappings if m.kosync_document]
    progress_by_doc: dict[str, float] = {}
    if kosync_docs:
        progress_result = await session.execute(
            select(ReadingProgress)
            .join(KosyncUser, ReadingProgress.kosync_user_id == KosyncUser.id)
            .where(
                KosyncUser.user_id == user.id,
                ReadingProgress.document.in_(kosync_docs),
                ReadingProgress.is_latest.is_(True),
            )
        )
        progress_by_doc = {r.document: r.percentage for r in progress_result.scalars().all()}

    return [
        _mapping_to_schema(
            m, lib_by_id.get(m.abs_item_id), progress_by_doc.get(m.kosync_document or "")
        )
        for m in mappings
    ]


@router.get("/calibre-ebooks", response_model=list[EbookCandidate])
async def list_calibre_ebooks(
    abs_item_id: str,
    _user: User = Depends(get_current_earmark_user),
    session: AsyncSession = Depends(get_session),
) -> list[EbookCandidate]:
    cwa_url = await get_effective_str("cwa_url", settings.cwa_url, session)
    if not cwa_url:
        raise HTTPException(
            status_code=503, detail="Calibre Web is not configured (CWA_URL is unset)."
        )

    abs_url = await get_effective_str(
        "audiobookshelf_url", settings.audiobookshelf_url, session
    )
    abs_key = await get_effective_str(
        "audiobookshelf_api_key", settings.audiobookshelf_api_key, session
    )

    title: str = ""
    author: str | None = None
    if abs_url and abs_key:
        client = AudiobookshelfClient(url=abs_url, api_key=abs_key)
        try:
            item = await client.get_item(abs_item_id)
            metadata = item.get("media", {}).get("metadata", {})
            title = metadata.get("title", "")
            author = metadata.get("authorName") or None
        except Exception:
            logger.error("Failed to fetch ABS item %s", abs_item_id, exc_info=True)
        finally:
            await client.close()

    if not title:
        lib_result = await session.execute(
            select(AbsLibraryItem).where(AbsLibraryItem.abs_item_id == abs_item_id)
        )
        lib_item = lib_result.scalar_one_or_none()
        if lib_item is not None:
            title = lib_item.title
            author = lib_item.author or author

    if not title:
        raise HTTPException(status_code=404, detail="Unknown ABS item")

    cwa_username = await get_effective_str("cwa_username", settings.cwa_username, session)
    cwa_password = await get_effective_str("cwa_password", settings.cwa_password, session)
    source = CalibreOpdsSource(base_url=cwa_url, username=cwa_username, password=cwa_password)
    try:
        return await source.search(title, author)
    except httpx.HTTPStatusError as exc:
        logger.error("Calibre Web OPDS returned %s", exc.response.status_code, exc_info=True)
        raise HTTPException(
            status_code=502,
            detail=f"Calibre Web returned HTTP {exc.response.status_code}",
        )
    except httpx.HTTPError as exc:
        logger.error("Calibre Web OPDS request failed", exc_info=True)
        raise HTTPException(
            status_code=502, detail=f"Calibre Web is unreachable: {exc}"
        )


@router.post("/mappings", response_model=MappingRead, status_code=201)
async def create_mapping(
    body: MappingCreate,
    user: User = Depends(get_current_earmark_user),
    session: AsyncSession = Depends(get_session),
) -> MappingRead:
    if body.ebook_source not in ("local", "calibre"):
        raise HTTPException(status_code=422, detail="Unknown ebook_source")

    if body.ebook_source == "local":
        if not body.ebook_path:
            raise HTTPException(
                status_code=422, detail="ebook_path is required for local mapping"
            )
    else:
        if not body.ebook_source_ref:
            raise HTTPException(
                status_code=422, detail="ebook_source_ref is required for calibre mapping"
            )

    existing_query = select(AbsEbookMapping).where(
        AbsEbookMapping.user_id == user.id,
        AbsEbookMapping.abs_item_id == body.abs_item_id,
        AbsEbookMapping.ebook_source == body.ebook_source,
    )
    if body.ebook_source == "local":
        existing_query = existing_query.where(AbsEbookMapping.ebook_path == body.ebook_path)
    else:
        existing_query = existing_query.where(
            AbsEbookMapping.ebook_source_ref == body.ebook_source_ref
        )

    existing = await session.execute(existing_query)
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="Mapping already exists")

    kosync_document: str | None = None
    ebook_filename: str | None = None
    if body.ebook_source == "local":
        if body.ebook_path is None:
            raise HTTPException(status_code=400, detail="ebook_path is required for local source")
        full_path = safe_subpath(settings.ebook_local_root, body.ebook_path)
        if full_path is None:
            raise HTTPException(status_code=400, detail="Invalid ebook_path")
        try:
            kosync_document = await asyncio.to_thread(partial_md5, full_path)
        except OSError:
            logger.error("Cannot read ebook file: %s", full_path, exc_info=True)
            raise HTTPException(status_code=500, detail="Could not read ebook file")
        ebook_filename = Path(body.ebook_path).name

    mapping = AbsEbookMapping(
        user_id=user.id,
        abs_item_id=body.abs_item_id,
        abs_title=body.abs_title,
        abs_author=body.abs_author,
        ebook_source=body.ebook_source,
        ebook_path=body.ebook_path,
        ebook_filename=ebook_filename,
        ebook_source_ref=body.ebook_source_ref,
        kosync_document=kosync_document,
    )
    session.add(mapping)
    await session.commit()
    await session.refresh(mapping)

    if kosync_document is not None:
        kosync_user_result = await session.execute(
            select(KosyncUser).where(KosyncUser.user_id == user.id)
        )
        for kosync_user in kosync_user_result.scalars().all():
            await backfill_progress_titles(
                session,
                kosync_user_id=kosync_user.id,
                document=kosync_document,
                title=body.abs_title,
            )
        await session.commit()

    return _mapping_to_schema(mapping, None)


@router.delete("/mappings/{mapping_id}")
async def delete_mapping(
    mapping_id: int,
    user: User = Depends(get_current_earmark_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    result = await session.execute(
        select(AbsEbookMapping).where(
            AbsEbookMapping.id == mapping_id,
            AbsEbookMapping.user_id == user.id,
        )
    )
    mapping = result.scalar_one_or_none()
    if mapping is None:
        raise HTTPException(status_code=404, detail="Mapping not found")
    abs_item_id = mapping.abs_item_id
    await session.delete(mapping)
    await session.commit()
    cache_path = safe_subpath(settings.alignment_cache_dir, abs_item_id)
    if cache_path is not None:
        shutil.rmtree(cache_path, ignore_errors=True)
    else:
        logger.warning("Refusing to remove cache for unsafe abs_item_id: %r", abs_item_id)
    return {"deleted": mapping_id}


@router.post("/mappings/{mapping_id}/sync", response_model=MappingRead, status_code=202)
async def sync_mapping(
    mapping_id: int,
    user: User = Depends(get_current_earmark_user),
    session: AsyncSession = Depends(get_session),
) -> MappingRead:
    result = await session.execute(
        select(AbsEbookMapping).where(
            AbsEbookMapping.id == mapping_id,
            AbsEbookMapping.user_id == user.id,
        )
    )
    mapping = result.scalar_one_or_none()
    if mapping is None:
        raise HTTPException(status_code=404, detail="Mapping not found")

    any_active = await session.execute(
        select(AlignmentJob).where(AlignmentJob.status.in_(ACTIVE_STATUSES)).limit(1)
    )
    if any_active.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Another sync is already running")

    job = AlignmentJob(
        abs_item_id=mapping.abs_item_id,
        created_by_user_id=user.id,
        status="pending",
        progress=0,
        ebook_path=mapping.ebook_path,
        ebook_source=mapping.ebook_source,
        ebook_source_ref=mapping.ebook_source_ref,
    )
    session.add(job)
    await session.flush()
    mapping.alignment_job_id = job.id
    await session.commit()
    await session.refresh(mapping)

    lib_result = await session.execute(
        select(AbsLibraryItem).where(AbsLibraryItem.abs_item_id == mapping.abs_item_id)
    )
    lib_item = lib_result.scalar_one_or_none()

    asyncio.create_task(run_alignment_job(job.id))
    return _mapping_to_schema(mapping, lib_item)
