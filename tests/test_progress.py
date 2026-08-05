import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from conftest import PROGRESS_PAYLOAD
from earmark.models import AbsEbookMapping, User

DOC = PROGRESS_PAYLOAD["document"]


async def test_put_progress_creates_record(client: AsyncClient, alice: dict[str, str]) -> None:
    response = await client.put("/syncs/progress", json=PROGRESS_PAYLOAD, headers=alice)
    assert response.status_code == 200
    data = response.json()
    assert data["document"] == DOC
    assert data["percentage"] == pytest.approx(0.2082)
    assert data["device"] == "boox"
    assert "timestamp" in data


async def test_put_progress_creates_new_record_on_repeat(
    client: AsyncClient, alice: dict[str, str]
) -> None:
    await client.put("/syncs/progress", json=PROGRESS_PAYLOAD, headers=alice)
    higher = {**PROGRESS_PAYLOAD, "percentage": 0.5, "progress": "/body/div[2]"}
    response = await client.put("/syncs/progress", json=higher, headers=alice)
    assert response.status_code == 200
    assert response.json()["percentage"] == pytest.approx(0.5)
    assert response.json()["progress"] == "/body/div[2]"
    # GET returns the latest entry, not the first
    get_response = await client.get(f"/syncs/progress/{DOC}", headers=alice)
    assert get_response.json()["percentage"] == pytest.approx(0.5)


async def test_put_progress_lower_percentage_still_becomes_latest(
    client: AsyncClient, alice: dict[str, str]
) -> None:
    high = {**PROGRESS_PAYLOAD, "percentage": 0.9, "progress": "/body/div[99]"}
    await client.put("/syncs/progress", json=high, headers=alice)
    lower = {**PROGRESS_PAYLOAD, "percentage": 0.1, "progress": "/body/div[1]"}
    response = await client.put("/syncs/progress", json=lower, headers=alice)
    assert response.status_code == 200
    assert response.json()["percentage"] == pytest.approx(0.1)
    assert response.json()["progress"] == "/body/div[1]"


async def test_put_progress_same_xpath_lower_percentage_is_noop(
    client: AsyncClient, alice: dict[str, str]
) -> None:
    high = {**PROGRESS_PAYLOAD, "percentage": 0.9, "progress": "/body/div[99]"}
    await client.put("/syncs/progress", json=high, headers=alice)
    lower = {**PROGRESS_PAYLOAD, "percentage": 0.1, "progress": "/body/div[99]"}
    response = await client.put("/syncs/progress", json=lower, headers=alice)
    assert response.status_code == 200
    assert response.json()["percentage"] == pytest.approx(0.9)
    assert response.json()["progress"] == "/body/div[99]"
    list_response = await client.get(f"/syncs/progress?document={DOC}", headers=alice)
    assert list_response.json()["total"] == 1


async def test_put_progress_sub_threshold_move_ignored(
    client: AsyncClient, alice: dict[str, str]
) -> None:
    # Default sync_min_movement is 0.005; a smaller advance is dropped (chatty client).
    await client.put("/syncs/progress", json=PROGRESS_PAYLOAD, headers=alice)
    tiny = {**PROGRESS_PAYLOAD, "percentage": 0.2090, "progress": "/body/div[2]"}
    response = await client.put("/syncs/progress", json=tiny, headers=alice)
    assert response.status_code == 200
    # The stored latest is unchanged and no new row was written.
    assert response.json()["percentage"] == pytest.approx(0.2082)
    assert response.json()["progress"] == PROGRESS_PAYLOAD["progress"]
    list_response = await client.get(f"/syncs/progress?document={DOC}", headers=alice)
    assert list_response.json()["total"] == 1


async def test_put_progress_above_threshold_move_stored(
    client: AsyncClient, alice: dict[str, str]
) -> None:
    await client.put("/syncs/progress", json=PROGRESS_PAYLOAD, headers=alice)
    bigger = {**PROGRESS_PAYLOAD, "percentage": 0.22, "progress": "/body/div[2]"}
    response = await client.put("/syncs/progress", json=bigger, headers=alice)
    assert response.status_code == 200
    assert response.json()["percentage"] == pytest.approx(0.22)
    list_response = await client.get(f"/syncs/progress?document={DOC}", headers=alice)
    assert list_response.json()["total"] == 2


async def test_put_progress_sub_threshold_backward_move_stored(
    client: AsyncClient, alice: dict[str, str]
) -> None:
    # The threshold is forward-only: a small jump back is deliberate, not chatty drift.
    await client.put("/syncs/progress", json=PROGRESS_PAYLOAD, headers=alice)
    back = {**PROGRESS_PAYLOAD, "percentage": 0.2078, "progress": "/body/div[1]"}
    response = await client.put("/syncs/progress", json=back, headers=alice)
    assert response.status_code == 200
    assert response.json()["percentage"] == pytest.approx(0.2078)
    list_response = await client.get(f"/syncs/progress?document={DOC}", headers=alice)
    assert list_response.json()["total"] == 2


async def test_min_movement_skipped_when_baseline_written_by_sync_job(
    db_session_factory: async_sessionmaker,  # type: ignore[type-arg]
) -> None:
    """A sync-job baseline stores an audio-time fraction, so the page-fraction
    threshold must not be applied against it — that would drop real reading moves."""
    from sqlalchemy import func, select

    from earmark.models import KosyncUser, ReadingProgress
    from earmark.services.progress import SYNC_DEVICE, write_reading_progress

    async with db_session_factory() as session:
        ku = KosyncUser(username="carol", password_hash="x")
        session.add(ku)
        await session.flush()

        # Baseline written by the sync job, as _write_abs_to_kosync does.
        await write_reading_progress(
            session,
            kosync_user_id=ku.id,
            document=DOC,
            progress="/body/p[1]",
            percentage=0.5000,
            device=SYNC_DEVICE,
            device_id=SYNC_DEVICE,
        )
        # A client push that would be sub-threshold on the mismatched scale is stored.
        await write_reading_progress(
            session,
            kosync_user_id=ku.id,
            document=DOC,
            progress="/body/p[2]",
            percentage=0.5001,
            device="boox",
            device_id="i",
            min_movement=0.01,
        )
        total = await session.scalar(
            select(func.count()).select_from(ReadingProgress).where(ReadingProgress.document == DOC)
        )
        assert total == 2

        # The same tiny move against a client baseline is still dropped.
        await write_reading_progress(
            session,
            kosync_user_id=ku.id,
            document=DOC,
            progress="/body/p[3]",
            percentage=0.5002,
            device="boox",
            device_id="i",
            min_movement=0.01,
        )
        total = await session.scalar(
            select(func.count()).select_from(ReadingProgress).where(ReadingProgress.document == DOC)
        )
        assert total == 2


async def test_dropped_pushes_are_logged_at_info(
    db_session_factory: async_sessionmaker,  # type: ignore[type-arg]
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An ignored push must be visible in the Logs tab (INFO reaches the file handler),
    otherwise it's indistinguishable from a push that never arrived."""
    from earmark.models import KosyncUser
    from earmark.services.progress import write_reading_progress

    async with db_session_factory() as session:
        ku = KosyncUser(username="dave", password_hash="x")
        session.add(ku)
        await session.flush()

        await write_reading_progress(
            session,
            kosync_user_id=ku.id,
            document=DOC,
            progress="/body/p[1]",
            percentage=0.2000,
            device="boox",
            device_id="i",
        )

        with caplog.at_level("INFO", logger="earmark.services.progress"):
            # Unchanged position.
            await write_reading_progress(
                session,
                kosync_user_id=ku.id,
                document=DOC,
                progress="/body/p[1]",
                percentage=0.2000,
                device="boox",
                device_id="i",
            )
            # Sub-threshold forward move.
            await write_reading_progress(
                session,
                kosync_user_id=ku.id,
                document=DOC,
                progress="/body/p[2]",
                percentage=0.2001,
                device="boox",
                device_id="i",
                min_movement=0.01,
            )

    messages = [r.getMessage() for r in caplog.records]
    assert any("position unchanged" in m and DOC in m for m in messages)
    assert any("sub-threshold" in m and DOC in m for m in messages)


async def test_write_reading_progress_min_movement_disabled_stores_every_push(
    db_session_factory: async_sessionmaker,  # type: ignore[type-arg]
) -> None:
    from sqlalchemy import func, select

    from earmark.models import KosyncUser, ReadingProgress
    from earmark.services.progress import write_reading_progress

    async with db_session_factory() as session:
        ku = KosyncUser(username="bob", password_hash="x")
        session.add(ku)
        await session.flush()

        # min_movement=None: every distinct XPath is stored, even tiny moves.
        await write_reading_progress(
            session,
            kosync_user_id=ku.id,
            document=DOC,
            progress="/body/p[1]",
            percentage=0.2000,
            device="d",
            device_id="i",
            min_movement=None,
        )
        await write_reading_progress(
            session,
            kosync_user_id=ku.id,
            document=DOC,
            progress="/body/p[2]",
            percentage=0.2001,
            device="d",
            device_id="i",
            min_movement=None,
        )
        total = await session.scalar(
            select(func.count()).select_from(ReadingProgress).where(ReadingProgress.document == DOC)
        )
        assert total == 2

        # min_movement set: the same tiny move is ignored.
        await write_reading_progress(
            session,
            kosync_user_id=ku.id,
            document=DOC,
            progress="/body/p[3]",
            percentage=0.2002,
            device="d",
            device_id="i",
            min_movement=0.01,
        )
        total = await session.scalar(
            select(func.count()).select_from(ReadingProgress).where(ReadingProgress.document == DOC)
        )
        assert total == 2


async def test_put_progress_with_duplicate_mapping_documents(
    client: AsyncClient,
    alice: dict[str, str],
    db_session_factory: async_sessionmaker,  # type: ignore[type-arg]
) -> None:
    # The same ebook can be mapped more than once, so kosync_document is not unique.
    # A push for a duplicated document must not crash on MultipleResultsFound.
    async with db_session_factory() as session:
        user = User(email="dup@example.com", password_hash="x")
        session.add(user)
        await session.flush()
        for i in range(2):
            session.add(
                AbsEbookMapping(
                    user_id=user.id,
                    abs_item_id=f"item-{i}",
                    abs_title=f"Book {i}",
                    ebook_path=f"book{i}.epub",
                    kosync_document=DOC,
                )
            )
        await session.commit()

    response = await client.put("/syncs/progress", json=PROGRESS_PAYLOAD, headers=alice)
    assert response.status_code == 200

    # The lowest-id mapping wins the title deterministically.
    list_response = await client.get(f"/syncs/progress?document={DOC}", headers=alice)
    assert list_response.json()["data"][0]["title"] == "Book 0"


async def test_put_progress_with_metadata(client: AsyncClient, alice: dict[str, str]) -> None:
    payload = {
        **PROGRESS_PAYLOAD,
        "metadata": {
            "filename": "gatsby.epub",
            "title": "The Great Gatsby",
            "authors": "F. Scott Fitzgerald",
        },
    }
    await client.put("/syncs/progress", json=payload, headers=alice)
    await client.get(f"/syncs/progress/{DOC}", headers=alice)
    # The GET single-record endpoint does not return metadata fields,
    # but the list endpoint does.
    list_response = await client.get(f"/syncs/progress?document={DOC}", headers=alice)
    item = list_response.json()["data"][0]
    assert item["filename"] == "gatsby.epub"
    assert item["title"] == "The Great Gatsby"
    assert item["authors"] == "F. Scott Fitzgerald"


async def test_get_progress(client: AsyncClient, alice: dict[str, str]) -> None:
    await client.put("/syncs/progress", json=PROGRESS_PAYLOAD, headers=alice)
    response = await client.get(f"/syncs/progress/{DOC}", headers=alice)
    assert response.status_code == 200
    assert response.json()["document"] == DOC


async def test_get_progress_not_found(client: AsyncClient, alice: dict[str, str]) -> None:
    response = await client.get("/syncs/progress/nonexistent", headers=alice)
    assert response.status_code == 404


async def test_list_progress(client: AsyncClient, alice: dict[str, str]) -> None:
    await client.put("/syncs/progress", json=PROGRESS_PAYLOAD, headers=alice)
    # A second PUT for the same document creates a new historical entry
    higher = {
        **PROGRESS_PAYLOAD,
        "percentage": 0.5,
        "progress": "/body/DocFragment[20]/body/div[1]",
    }
    await client.put("/syncs/progress", json=higher, headers=alice)
    response = await client.get(f"/syncs/progress?document={DOC}", headers=alice)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["page"] == 1
    assert len(body["data"]) == 2
    # Most recent entry comes first
    assert body["data"][0]["percentage"] == pytest.approx(0.5)
    assert body["data"][1]["percentage"] == pytest.approx(0.2082)


async def test_list_progress_pagination(client: AsyncClient, alice: dict[str, str]) -> None:
    # Create 5 entries for the same document
    for i in range(5):
        payload = {
            **PROGRESS_PAYLOAD,
            "percentage": i * 0.1,
            "progress": f"/body/DocFragment[{i + 1}]/body/p[1]",
        }
        await client.put("/syncs/progress", json=payload, headers=alice)
    response = await client.get(f"/syncs/progress?document={DOC}&page=1&per_page=3", headers=alice)
    body = response.json()
    assert body["total"] == 5
    assert body["per_page"] == 3
    assert len(body["data"]) == 3
    # Page 2
    response = await client.get(f"/syncs/progress?document={DOC}&page=2&per_page=3", headers=alice)
    body = response.json()
    assert len(body["data"]) == 2


async def test_delete_progress(client: AsyncClient, alice: dict[str, str]) -> None:
    await client.put("/syncs/progress", json=PROGRESS_PAYLOAD, headers=alice)
    response = await client.delete(f"/syncs/progress/{DOC}", headers=alice)
    assert response.status_code == 200
    assert response.json() == {"deleted": DOC}
    assert (await client.get(f"/syncs/progress/{DOC}", headers=alice)).status_code == 404


async def test_delete_progress_not_found(client: AsyncClient, alice: dict[str, str]) -> None:
    response = await client.delete("/syncs/progress/nonexistent", headers=alice)
    assert response.status_code == 404


async def test_put_progress_auth_required(client: AsyncClient) -> None:
    response = await client.put("/syncs/progress", json=PROGRESS_PAYLOAD)
    assert response.status_code == 422


async def test_get_progress_auth_required(client: AsyncClient) -> None:
    response = await client.get(f"/syncs/progress/{DOC}")
    assert response.status_code == 422


async def test_list_progress_auth_required(client: AsyncClient) -> None:
    response = await client.get(f"/syncs/progress?document={DOC}")
    assert response.status_code == 422


async def test_delete_progress_auth_required(client: AsyncClient) -> None:
    response = await client.delete(f"/syncs/progress/{DOC}")
    assert response.status_code == 422
