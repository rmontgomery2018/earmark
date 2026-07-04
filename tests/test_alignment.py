import copy
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from bs4 import BeautifulSoup
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from earmark.models import AlignmentJob
from earmark.services.alignment import (
    _align_paragraphs_to_transcript,
    _build_transcript_index,
    _classify_spine_item,
    _element_full_xpath,
    _is_blurb_shaped,
    _match_heading_to_abs_chapter,
    _stage_progress,
    _validate_sync_map,
    recover_orphaned_jobs,
    run_alignment_job,
)

# ── _element_full_xpath ────────────────────────────────────────────────────────


def _find(html: str, tag: str, **kwargs: str) -> object:
    return BeautifulSoup(html, "html.parser").find(tag, **kwargs)


def test_element_full_xpath_direct_child_of_body() -> None:
    html = "<html><body><p>text</p></body></html>"
    el = BeautifulSoup(html, "html.parser").find("p")
    assert _element_full_xpath(el) == "/body/p[1]"


def test_element_full_xpath_sibling_index() -> None:
    html = "<html><body><p>one</p><p>two</p><p>three</p></body></html>"
    soup = BeautifulSoup(html, "html.parser")
    ps = soup.find_all("p")
    assert _element_full_xpath(ps[0]) == "/body/p[1]"
    assert _element_full_xpath(ps[1]) == "/body/p[2]"
    assert _element_full_xpath(ps[2]) == "/body/p[3]"


def test_element_full_xpath_nested_structure() -> None:
    html = "<html><body><section><div><p>text</p></div></section></body></html>"
    el = BeautifulSoup(html, "html.parser").find("p")
    assert _element_full_xpath(el) == "/body/section[1]/div[1]/p[1]"


def test_element_full_xpath_mixed_siblings() -> None:
    html = (
        "<html><body>"
        "<section><div><p>a</p><p>b</p></div><div><p>c</p></div></section>"
        "</body></html>"
    )
    soup = BeautifulSoup(html, "html.parser")
    ps = soup.find_all("p")
    assert _element_full_xpath(ps[0]) == "/body/section[1]/div[1]/p[1]"
    assert _element_full_xpath(ps[1]) == "/body/section[1]/div[1]/p[2]"
    assert _element_full_xpath(ps[2]) == "/body/section[1]/div[2]/p[1]"


# ── fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
async def jwt_headers(client: AsyncClient) -> dict[str, str]:
    await client.post(
        "/auth/register",
        json={
            "email": "align@example.com",
            "password": "secret",
            "kosync_username": "align_web",
            "kosync_password": "secret",
        },
    )
    resp = await client.post(
        "/auth/login", json={"email": "align@example.com", "password": "secret"}
    )
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


ABS_ITEM_ID = "li_test123"

ABS_METADATA: dict[str, Any] = {
    "id": ABS_ITEM_ID,
    "libraryId": "lib_001",
    "updatedAt": 1715000000000,
    "media": {
        "metadata": {"title": "Test Book", "authorName": "Test Author"},
        "audioFiles": [
            {
                "index": 1,
                "ino": "test_ino_001",
                "metadata": {"filename": "chapter01.mp3"},
                "filename": "chapter01.mp3",
                "duration": 1000.0,
            },
        ],
        "chapters": [
            # Short intro track triggers _find_audio_trim_seconds → trim to 10s.
            {"id": 0, "start": 0.0, "end": 10.0, "title": "Intro"},
            {"id": 1, "start": 10.0, "end": 1000.0, "title": "Content"},
        ],
        "ebookFile": {"filename": "test.epub", "ext": ".epub"},
    },
}

# Paragraphs must be ≥40 chars and not blurb-shaped so the validator stays quiet.
FAKE_PARAGRAPHS = [
    "First narrative paragraph with substantive content that looks like prose.",
    "Second narrative paragraph also substantive enough to pass validation.",
]
FAKE_INDEX = {
    "para_000": {
        "text": FAKE_PARAGRAPHS[0],
        "ebook_pos": "/body/DocFragment[1]/body/section[1]/p[1]",
    },
    "para_001": {
        "text": FAKE_PARAGRAPHS[1],
        "ebook_pos": "/body/DocFragment[1]/body/section[1]/p[2]",
    },
}


def _words_for(paragraphs: list[str], step: float = 0.4) -> list[dict[str, float]]:
    """Synthesize WhisperX-style word timings: every word gets a fixed slot.

    Paragraphs are concatenated in order, so fuzzy matching lines each one up
    to a contiguous slice of the transcript. Step in seconds between words.
    """
    import re as _re
    out: list[dict[str, float]] = []
    t = 1.0  # start at 1s to keep audio_offset 0 sane
    for para in paragraphs:
        for tok in _re.findall(r"[A-Za-z0-9]+", para):
            out.append({"word": tok.lower(), "start": t, "end": t + step})
            t += step
        t += step  # small gap between paragraphs
    return out


async def _run_pipeline(
    job_id: int,
    session_factory: async_sessionmaker,  # type: ignore[type-arg]
    cache_dir: Path,
    abs_metadata_override: dict[str, Any] | None = None,
    paragraphs_override: list[str] | None = None,
    index_override: dict[str, dict[str, str]] | None = None,
    words_override: list[dict[str, float]] | None = None,
) -> None:
    """Run the full pipeline with all blocking calls mocked."""
    metadata = abs_metadata_override if abs_metadata_override is not None else ABS_METADATA
    paragraphs = paragraphs_override if paragraphs_override is not None else FAKE_PARAGRAPHS
    index = index_override if index_override is not None else FAKE_INDEX
    words = words_override if words_override is not None else _words_for(paragraphs)

    async def fake_get_item(self: Any, item_id: str) -> dict[str, Any]:
        return metadata

    async def fake_download_audio(self: Any, item_id: str, filename: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake_audio")

    async def fake_download_ebook(self: Any, item_id: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake_epub")

    def fake_parse_epub(
        epub_path: Path,
    ) -> tuple[list[str], dict[str, dict[str, str]], int, int]:
        last_pos = max(
            (
                int(m.group(1))
                for m in (
                    __import__("re").match(r"/body/DocFragment\[(\d+)\]", e["ebook_pos"])
                    for e in index.values()
                )
                if m
            ),
            default=1,
        )
        return paragraphs, index, 1, last_pos

    def fake_ffmpeg_concat(audio_files: list[Path], output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake_wav")

    async def fake_transcribe(self: Any, audio_path: Path) -> list[dict[str, float]]:
        return words

    with (
        patch(
            "earmark.services.audiobookshelf.AudiobookshelfClient.get_item",
            fake_get_item,
        ),
        patch(
            "earmark.services.audiobookshelf.AudiobookshelfClient.download_audio_file",
            fake_download_audio,
        ),
        patch(
            "earmark.services.audiobookshelf.AudiobookshelfClient.download_ebook",
            fake_download_ebook,
        ),
        patch("earmark.services.alignment._parse_epub_sync", fake_parse_epub),
        patch("earmark.services.alignment._ffmpeg_concat_sync", fake_ffmpeg_concat),
        patch("earmark.services.alignment.AlignmentPipeline._transcribe", fake_transcribe),
        patch("earmark.config.settings.alignment_cache_dir", str(cache_dir)),
    ):
        await run_alignment_job(job_id, session_factory=session_factory)


# ── tests ──────────────────────────────────────────────────────────────────────


async def test_create_job_returns_202(client: AsyncClient, jwt_headers: dict[str, str]) -> None:
    resp = await client.post(
        "/alignment/jobs", json={"abs_item_id": ABS_ITEM_ID}, headers=jwt_headers
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["abs_item_id"] == ABS_ITEM_ID
    assert data["status"] == "pending"
    assert data["id"] is not None


async def test_create_job_requires_auth(client: AsyncClient) -> None:
    resp = await client.post("/alignment/jobs", json={"abs_item_id": ABS_ITEM_ID})
    assert resp.status_code in (401, 403)


async def test_create_job_duplicate_active_returns_409(
    client: AsyncClient, jwt_headers: dict[str, str]
) -> None:
    await client.post(
        "/alignment/jobs", json={"abs_item_id": ABS_ITEM_ID}, headers=jwt_headers
    )
    resp = await client.post(
        "/alignment/jobs", json={"abs_item_id": ABS_ITEM_ID}, headers=jwt_headers
    )
    assert resp.status_code == 409


async def test_get_job_not_found(client: AsyncClient, jwt_headers: dict[str, str]) -> None:
    resp = await client.get("/alignment/jobs/9999", headers=jwt_headers)
    assert resp.status_code == 404


async def test_get_sync_map_not_found(client: AsyncClient, jwt_headers: dict[str, str]) -> None:
    resp = await client.get("/alignment/jobs/9999/sync-map", headers=jwt_headers)
    assert resp.status_code == 404


async def test_get_sync_map_returns_409_while_pending(
    client: AsyncClient, jwt_headers: dict[str, str]
) -> None:
    resp = await client.post(
        "/alignment/jobs", json={"abs_item_id": ABS_ITEM_ID}, headers=jwt_headers
    )
    job_id = resp.json()["id"]
    resp = await client.get(f"/alignment/jobs/{job_id}/sync-map", headers=jwt_headers)
    assert resp.status_code == 409


async def test_list_jobs(client: AsyncClient, jwt_headers: dict[str, str]) -> None:
    await client.post(
        "/alignment/jobs", json={"abs_item_id": "li_aaa"}, headers=jwt_headers
    )
    resp = await client.get("/alignment/jobs", headers=jwt_headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
    assert len(resp.json()) >= 1


async def test_list_jobs_pagination(client: AsyncClient, jwt_headers: dict[str, str]) -> None:
    for i in range(3):
        await client.post(
            "/alignment/jobs", json={"abs_item_id": f"li_{i:03d}"}, headers=jwt_headers
        )
    resp = await client.get("/alignment/jobs?page=1&per_page=2", headers=jwt_headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 2


async def test_jobs_are_scoped_to_creator(
    client: AsyncClient, jwt_headers: dict[str, str]
) -> None:
    # User A creates a job.
    created = await client.post(
        "/alignment/jobs", json={"abs_item_id": "li_owned_by_a"}, headers=jwt_headers
    )
    job_id = created.json()["id"]

    # User B authenticates separately.
    await client.post(
        "/auth/register",
        json={
            "email": "other@example.com",
            "password": "secret",
            "kosync_username": "other_web",
            "kosync_password": "secret",
        },
    )
    login = await client.post(
        "/auth/login", json={"email": "other@example.com", "password": "secret"}
    )
    b_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    # B cannot see A's job in the list, by id, or its sync-map.
    listing = await client.get("/alignment/jobs", headers=b_headers)
    assert listing.status_code == 200
    assert all(j["id"] != job_id for j in listing.json())
    assert (await client.get(f"/alignment/jobs/{job_id}", headers=b_headers)).status_code == 404
    assert (
        await client.get(f"/alignment/jobs/{job_id}/sync-map", headers=b_headers)
    ).status_code == 404


async def test_full_pipeline_happy_path(
    client: AsyncClient,
    jwt_headers: dict[str, str],
    db_session_factory: async_sessionmaker,  # type: ignore[type-arg]
    tmp_path: Path,
) -> None:
    resp = await client.post(
        "/alignment/jobs", json={"abs_item_id": ABS_ITEM_ID}, headers=jwt_headers
    )
    assert resp.status_code == 202
    job_id = resp.json()["id"]

    await _run_pipeline(job_id, db_session_factory, tmp_path)

    resp = await client.get(f"/alignment/jobs/{job_id}", headers=jwt_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "complete"

    resp = await client.get(f"/alignment/jobs/{job_id}/sync-map", headers=jwt_headers)
    assert resp.status_code == 200
    entries = resp.json()
    assert len(entries) == 2
    assert entries[0]["id"] == "para_000"
    assert entries[0]["ebook_pos"] == "/body/DocFragment[1]/body/section[1]/p[1]"
    # WhisperX gives absolute audio timestamps directly; words start at 1s.
    assert entries[0]["audio_start"] >= 1.0
    assert entries[0]["audio_end"] > entries[0]["audio_start"]

    resp = await client.get(f"/alignment/jobs/{job_id}", headers=jwt_headers)
    # WhisperX produces absolute timestamps — no offset is applied.
    assert resp.json()["audio_offset_seconds"] is None


async def test_pipeline_no_offset_without_chapters(
    client: AsyncClient,
    jwt_headers: dict[str, str],
    db_session_factory: async_sessionmaker,  # type: ignore[type-arg]
    tmp_path: Path,
) -> None:
    metadata_no_chapters = copy.deepcopy(ABS_METADATA)
    del metadata_no_chapters["media"]["chapters"]

    resp = await client.post(
        "/alignment/jobs", json={"abs_item_id": ABS_ITEM_ID}, headers=jwt_headers
    )
    job_id = resp.json()["id"]

    await _run_pipeline(
        job_id, db_session_factory, tmp_path, abs_metadata_override=metadata_no_chapters
    )

    resp = await client.get(f"/alignment/jobs/{job_id}/sync-map", headers=jwt_headers)
    entries = resp.json()
    assert entries[0]["audio_start"] >= 1.0

    resp = await client.get(f"/alignment/jobs/{job_id}", headers=jwt_headers)
    assert resp.json()["audio_offset_seconds"] is None


async def test_pipeline_fails_on_abs_error(
    client: AsyncClient,
    jwt_headers: dict[str, str],
    db_session_factory: async_sessionmaker,  # type: ignore[type-arg]
    tmp_path: Path,
) -> None:
    import httpx as _httpx

    resp = await client.post(
        "/alignment/jobs", json={"abs_item_id": "li_bad"}, headers=jwt_headers
    )
    job_id = resp.json()["id"]

    async def bad_get_item(self: Any, item_id: str) -> dict[str, Any]:
        raise _httpx.HTTPStatusError(
            "404", request=MagicMock(), response=MagicMock(status_code=404)
        )

    with (
        patch(
            "earmark.services.audiobookshelf.AudiobookshelfClient.get_item",
            bad_get_item,
        ),
        patch("earmark.config.settings.alignment_cache_dir", str(tmp_path)),
    ):
        await run_alignment_job(job_id, session_factory=db_session_factory)

    resp = await client.get(f"/alignment/jobs/{job_id}", headers=jwt_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "failed"
    assert data["error_message"] is not None


async def test_pipeline_preserves_paragraph_order_for_1000plus_paragraphs(
    client: AsyncClient,
    jwt_headers: dict[str, str],
    db_session_factory: async_sessionmaker,  # type: ignore[type-arg]
    tmp_path: Path,
) -> None:
    """Books with ≥1000 paragraphs must not be re-ordered by lexicographic id sort."""
    n = 1100
    paragraphs = [
        f"Paragraph number {i} with enough unique content to fuzzy-match cleanly."
        for i in range(n)
    ]
    index = {
        f"para_{i:03d}": {
            "text": paragraphs[i],
            "ebook_pos": f"/body/DocFragment[1]/body/section[1]/p[{i + 1}]",
        }
        for i in range(n)
    }

    resp = await client.post(
        "/alignment/jobs", json={"abs_item_id": ABS_ITEM_ID}, headers=jwt_headers
    )
    job_id = resp.json()["id"]

    await _run_pipeline(
        job_id, db_session_factory, tmp_path,
        paragraphs_override=paragraphs, index_override=index,
    )

    resp = await client.get(f"/alignment/jobs/{job_id}/sync-map", headers=jwt_headers)
    entries = resp.json()
    # All paragraphs match the synthetic transcript that contains them in order.
    assert len(entries) == n
    for i, entry in enumerate(entries):
        assert entry["id"] == f"para_{i:03d}", f"position {i}: got {entry['id']}"
        # Timestamps must be monotonically non-decreasing.
        if i > 0:
            assert entry["audio_start"] >= entries[i - 1]["audio_start"]


async def test_pipeline_drops_unmatched_back_matter(
    client: AsyncClient,
    jwt_headers: dict[str, str],
    db_session_factory: async_sessionmaker,  # type: ignore[type-arg]
    tmp_path: Path,
) -> None:
    """EPUB paragraphs that don't appear in the audio transcript are dropped."""
    paragraphs = [
        "First narrative paragraph that appears in the audio.",
        "Second narrative paragraph that also appears in the audio.",
        "xxxxxx yyyyyy zzzzzz qqqqqq wwwwww eeeeee uuuuuu iiiiii oooooo",
    ]
    index = {
        "para_000": {
            "text": paragraphs[0],
            "ebook_pos": "/body/DocFragment[1]/body/section[1]/p[1]",
        },
        "para_001": {
            "text": paragraphs[1],
            "ebook_pos": "/body/DocFragment[1]/body/section[1]/p[2]",
        },
        "para_002": {
            "text": paragraphs[2],
            "ebook_pos": "/body/DocFragment[2]/body/section[1]/p[1]",
        },
    }
    # Synthesize a transcript that contains only the first two paragraphs.
    words = _words_for(paragraphs[:2])

    resp = await client.post(
        "/alignment/jobs", json={"abs_item_id": ABS_ITEM_ID}, headers=jwt_headers
    )
    job_id = resp.json()["id"]

    await _run_pipeline(
        job_id, db_session_factory, tmp_path,
        paragraphs_override=paragraphs, index_override=index, words_override=words,
    )

    resp = await client.get(f"/alignment/jobs/{job_id}/sync-map", headers=jwt_headers)
    entries = resp.json()
    ids = [e["id"] for e in entries]
    assert "para_000" in ids and "para_001" in ids
    assert "para_002" not in ids  # back matter not in transcript → dropped
    assert all(e["audio_start"] != e["audio_end"] for e in entries)


async def test_recover_orphaned_jobs_marks_active_as_failed(
    db_session_factory: async_sessionmaker,  # type: ignore[type-arg]
) -> None:
    async with db_session_factory() as session:
        for status in ("pending", "fetching_audio", "aligning"):
            session.add(AlignmentJob(
                abs_item_id="li_orphan", status=status, progress=5,
            ))
        session.add(AlignmentJob(
            abs_item_id="li_done", status="complete", progress=100,
        ))
        await session.commit()

    n = await recover_orphaned_jobs(session_factory=db_session_factory)
    assert n == 3

    async with db_session_factory() as session:
        result = await session.execute(select(AlignmentJob))
        jobs = list(result.scalars().all())
        statuses = sorted(j.status for j in jobs)
        assert statuses == ["complete", "failed", "failed", "failed"]
        for j in jobs:
            if j.status == "failed":
                assert j.error_message == "Interrupted by server restart"


# ── classifier helpers ─────────────────────────────────────────────────────────


def _stub_book(items: list[tuple[str, str, str | None, dict | None]]) -> object:
    """Tiny mock EPUB book.

    `items` is [(spine_id, file_name, html, attrs)]. html=None marks a non-document.
    """

    class _StubItem:
        def __init__(self, sid: str, file_name: str, html: str | None) -> None:
            self.id = sid
            self.file_name = file_name
            self._html = html

        def get_type(self) -> int:
            import ebooklib
            return ebooklib.ITEM_DOCUMENT if self._html is not None else 0

        def get_content(self) -> bytes:
            return (self._html or "").encode()

    class _StubBook:
        def __init__(self) -> None:
            self._items = {sid: _StubItem(sid, fn, html) for sid, fn, html, _ in items}
            self.spine = [(sid, attrs or {}) for sid, _, _, attrs in items]
            self.toc: list = []

        def get_item_with_id(self, sid: str) -> object | None:
            return self._items.get(sid)

        def get_items_of_type(self, _t: int) -> list:
            return []

        def get_items(self) -> list:
            return list(self._items.values())

    return _StubBook()


def _classify(html: str, *, item_id: str = "x", file_name: str = "x.xhtml",
              attrs: dict | None = None, toc_title: str = "") -> str:
    book = _stub_book([(item_id, file_name, html, attrs)])
    toc_titles = {file_name: toc_title.lower()}
    return _classify_spine_item(book, item_id, attrs or {}, toc_titles, {})


def test_classify_skips_praise_title_in_filename() -> None:
    # WoT spine id "frontmatter" with blurb-shaped praise content.
    html = (
        "<html><body>"
        "<p>Praise for THE WHEEL OF TIME®</p>"
        "<p>"
        + "The battle scenes have the breathless urgency of firsthand experience, " * 10
        + "</p>"
        "<p>— The New York Times</p>"
        "<p>— Chicago Sun-Times</p>"
        "<p>— Booklist</p>"
        "</body></html>"
    )
    assert _classify(html, item_id="frontmatter", file_name="xhtml/frontmatter.html") == "front"


def test_classify_keeps_prologue_even_when_filename_says_frontmatter() -> None:
    # WoT spine id "frontmatter02" actually contains the Prologue.
    html = (
        "<html><body><h2>PROLOGUE</h2>"
        "<p>" + "Real narrative content " * 20 + "</p>"
        "<p>" + "More real content here " * 20 + "</p>"
        "</body></html>"
    )
    assert (
        _classify(html, item_id="frontmatter02", file_name="xhtml/frontmatter02.html")
        == "body"
    )


def test_classify_respects_epub3_landmarks() -> None:
    # epub:type=bodymatter overrides any front-matter filename hint.
    book = _stub_book([("x", "praise.xhtml", "<html><body><p>x</p></body></html>", None)])
    landmarks = {"praise.xhtml": "bodymatter"}
    assert _classify_spine_item(book, "x", {}, {"praise.xhtml": "praise"}, landmarks) == "body"


def test_classify_linear_no_is_front() -> None:
    html = "<html><body><p>Cover</p></body></html>"
    assert _classify(html, item_id="cover_id", file_name="cover.xhtml",
                     attrs={"linear": "no"}) == "front"


def test_classify_drop_cap_title_page_is_front() -> None:
    # Drop-cap markup (<span>C</span>ROSSROADS…) must not gain an injected
    # space in the heading text — "c rossroads…" classifies as body via the
    # Roman-numeral alternative in _BODY_HEADING_RE matching the lone "c".
    html = (
        "<html><body>"
        "<h1><span>C</span>ROSSROADS OF <span>T</span>WILIGHT</h1>"
        "<p>ROBERT JORDAN</p>"
        "<p>A TOM DOHERTY ASSOCIATES BOOK NEW YORK</p>"
        "</body></html>"
    )
    assert _classify(html, item_id="title", file_name="xhtml/title.html") == "front"


def test_classify_about_the_author_is_back() -> None:
    html = (
        "<html><body><h1>About the Author</h1>"
        "<p>" + "Author biography goes here. " * 10 + "</p>"
        "</body></html>"
    )
    assert _classify(html, item_id="x", file_name="About_the_Author.xhtml",
                     toc_title="about the author") == "back"


def test_is_blurb_shaped_short_attribution() -> None:
    soup = BeautifulSoup(
        "<html><body>"
        "<p>Praise for the book</p>"
        "<p>— The New York Times</p>"
        "<p>— Chicago Sun-Times</p>"
        "<p>— Booklist</p>"
        "</body></html>",
        "html.parser",
    )
    assert _is_blurb_shaped(soup) is True


def test_is_blurb_shaped_normal_paragraph() -> None:
    soup = BeautifulSoup(
        "<html><body><p>"
        + "Just a normal narrative paragraph with substantive content. " * 5
        + "</p></body></html>",
        "html.parser",
    )
    assert _is_blurb_shaped(soup) is False


# ── transcript building + fuzzy alignment ──────────────────────────────────────


def test_build_transcript_index_round_trip() -> None:
    words = [
        {"word": "Hello", "start": 0.0, "end": 0.5},
        {"word": "world", "start": 0.6, "end": 1.0},
    ]
    transcript, ranges = _build_transcript_index(words)
    assert transcript == "hello world"
    assert ranges == [(0, 5, 0.0, 0.5), (6, 11, 0.6, 1.0)]


def test_align_paragraphs_matches_transcript_segments() -> None:
    words = _words_for([
        "Hello world this is the first paragraph.",
        "And here is the second paragraph in the audio.",
    ])
    transcript, ranges = _build_transcript_index(words)
    results = _align_paragraphs_to_transcript(
        ["Hello world this is the first paragraph.",
         "And here is the second paragraph in the audio."],
        transcript, ranges,
    )
    assert results[0] is not None and results[1] is not None
    assert results[0][0] < results[1][0]
    assert results[0][1] <= results[1][0]


def test_align_paragraphs_returns_none_for_unmatched() -> None:
    words = _words_for(["Hello world this is the only paragraph in the audio."])
    transcript, ranges = _build_transcript_index(words)
    results = _align_paragraphs_to_transcript(
        ["Hello world this is the only paragraph in the audio.",
         "xxxxxx yyyyyy zzzzzz qqqqqq wwwwww eeeeee uuuuuu iiiiii oooooo"],
        transcript, ranges,
    )
    assert results[0] is not None
    assert results[1] is None


# ── _validate_sync_map ─────────────────────────────────────────────────────────


def _sm(snippet: str = "A normal paragraph that is long enough to pass.",
        spine_pos: int = 1) -> dict:
    return {
        "id": "para_000",
        "audio_start": 0.0,
        "audio_end": 5.0,
        "ebook_pos": f"/body/DocFragment[{spine_pos}]/body/p[1]",
        "text_snippet": snippet,
    }


def test_validate_clean() -> None:
    sm = [_sm(spine_pos=p) for p in (1, 2, 3)]
    warnings = _validate_sync_map(sm, {1: 1.0, 2: 1.1}, total_duration=1000.0, audio_offset=10.0)
    assert warnings == []


def test_validate_extreme_rescale_scale() -> None:
    sm = [_sm()]
    warnings = _validate_sync_map(sm, {3: 3.5}, total_duration=1000.0, audio_offset=0.0)
    assert any("chapter_rescale_extreme" in w for w in warnings)


def test_validate_suspect_first_entry_short() -> None:
    sm = [_sm(snippet="Praise.")]
    warnings = _validate_sync_map(sm, {}, total_duration=1000.0, audio_offset=0.0)
    assert any("suspect_first_entry" in w for w in warnings)


def test_validate_suspect_first_entry_blurb_attribution() -> None:
    sm = [_sm(snippet="— The New York Times Bestseller list of all time honors")]
    warnings = _validate_sync_map(sm, {}, total_duration=1000.0, audio_offset=0.0)
    assert any("suspect_first_entry" in w for w in warnings)


def test_validate_audio_offset_excessive() -> None:
    sm = [_sm()]
    warnings = _validate_sync_map(sm, {}, total_duration=1000.0, audio_offset=100.0)
    assert any("audio_offset_excessive" in w for w in warnings)


def test_validate_docfragment_gap() -> None:
    # Mapped fragments 1, 2, 10; fragment 5 had a full chapter (>= _GAP_MIN_PARAS
    # paragraphs) in the EPUB but produced nothing → real gap.
    sm = [_sm(spine_pos=1), _sm(spine_pos=2), _sm(spine_pos=10)]
    epub_frag_counts = {1: 80, 2: 80, 5: 120, 10: 80}
    warnings = _validate_sync_map(
        sm, {}, total_duration=1000.0, audio_offset=0.0,
        epub_frag_counts=epub_frag_counts,
    )
    assert any("docfragment_gap: missing [5]" in w for w in warnings)


def test_validate_docfragment_gap_ignores_blank_and_frontmatter() -> None:
    # Regression for "Knife of Dreams": the unmapped fragments between first and
    # last mapped are a blank page (0 paragraphs) and a one-line dedication
    # (1 paragraph) — neither should trip the gap warning.
    sm = [_sm(spine_pos=6), _sm(spine_pos=8), _sm(spine_pos=11)]
    epub_frag_counts = {6: 19, 7: 1, 8: 42, 10: 0, 11: 1}
    warnings = _validate_sync_map(
        sm, {}, total_duration=1000.0, audio_offset=0.0,
        epub_frag_counts=epub_frag_counts,
    )
    assert not any("docfragment_gap" in w for w in warnings)


# ── pipeline status branching ──────────────────────────────────────────────────


async def test_pipeline_status_complete_with_warnings(
    client: AsyncClient,
    jwt_headers: dict[str, str],
    db_session_factory: async_sessionmaker,  # type: ignore[type-arg]
    tmp_path: Path,
) -> None:
    # Force a warning by giving a suspiciously short first snippet.
    paragraphs = ["Praise.", "Real second paragraph with substantive content here."]
    index = {
        "para_000": {"text": "Praise.", "ebook_pos": "/body/DocFragment[1]/body/p[1]"},
        "para_001": {
            "text": paragraphs[1],
            "ebook_pos": "/body/DocFragment[1]/body/p[2]",
        },
    }

    resp = await client.post(
        "/alignment/jobs", json={"abs_item_id": ABS_ITEM_ID}, headers=jwt_headers
    )
    job_id = resp.json()["id"]

    await _run_pipeline(
        job_id, db_session_factory, tmp_path,
        paragraphs_override=paragraphs, index_override=index,
    )

    resp = await client.get(f"/alignment/jobs/{job_id}", headers=jwt_headers)
    body = resp.json()
    assert body["status"] == "complete_with_warnings"
    assert any("suspect_first_entry" in w for w in body["warnings"])

    # Sync map still readable through the API.
    resp = await client.get(f"/alignment/jobs/{job_id}/sync-map", headers=jwt_headers)
    assert resp.status_code == 200


async def test_pipeline_low_transcript_coverage_warning(
    client: AsyncClient,
    jwt_headers: dict[str, str],
    db_session_factory: async_sessionmaker,  # type: ignore[type-arg]
    tmp_path: Path,
) -> None:
    """When most EPUB paragraphs don't match the transcript, flag low_transcript_coverage."""
    # First paragraph in audio + nine totally disjoint back-matter paragraphs
    # with no shared vocabulary, so partial_ratio can't latch onto common
    # tokens.
    in_audio = "Hello world this is the only narrated paragraph in the audio."
    distinct = [
        "wholly distinct vocabulary mountain pebble enchant garden fortress cottage",
        "completely separate alphabet zigzag mythical wizard dragon tale moonlight",
        "another unrelated theme spaceships nebula asteroid quasar pulsar comet",
        "fourth different domain saxophone violin cello drum piano harpsichord",
        "fifth unrelated content blueberry strawberry raspberry blackberry cherry",
        "sixth subject matter penguin walrus seal otter dolphin narwhal beluga",
        "seventh different topic engineer architect surgeon dentist plumber chef",
        "eighth distinct theme algebra calculus geometry trigonometry topology",
        "ninth unrelated paragraph painter sculptor dancer singer actor poet",
    ]
    paragraphs = [in_audio, *distinct]
    index = {
        f"para_{i:03d}": {
            "text": paragraphs[i],
            "ebook_pos": f"/body/DocFragment[1]/body/p[{i + 1}]",
        }
        for i in range(len(paragraphs))
    }
    # Transcript only contains the first paragraph — 9/10 should be unmatched.
    words = _words_for(paragraphs[:1])

    resp = await client.post(
        "/alignment/jobs", json={"abs_item_id": ABS_ITEM_ID}, headers=jwt_headers
    )
    job_id = resp.json()["id"]

    await _run_pipeline(
        job_id, db_session_factory, tmp_path,
        paragraphs_override=paragraphs, index_override=index, words_override=words,
    )

    resp = await client.get(f"/alignment/jobs/{job_id}", headers=jwt_headers)
    body = resp.json()
    assert body["status"] == "complete_with_warnings"
    assert any("low_transcript_coverage" in w for w in body["warnings"])


# ── _match_heading_to_abs_chapter ──────────────────────────────────────────────


_WOT_CHAPTERS = [
    {"id": 0, "start": 0.0, "end": 5964.5, "title": "Prologue: Snow (Part 1)"},
    {"id": 1, "start": 5964.5, "end": 10064.0, "title": "Prologue: Snow (Part 2)"},
    {"id": 2, "start": 10064.0, "end": 11611.8, "title": "Chapter 1: Leaving the Prophet"},
    {"id": 3, "start": 11611.8, "end": 13641.1, "title": "Chapter 2: Taken"},
    {"id": 4, "start": 13641.1, "end": 15320.4, "title": "Chapter 3: Customs"},
    {"id": 5, "start": 15320.4, "end": 17794.6, "title": "Chapter 4: Offers"},
    {"id": 6, "start": 17794.6, "end": 19354.4, "title": "Chapter 5: Flags"},
    {"id": 36, "start": 83400.0, "end": 84000.0, "title": "Epilogue"},
]


def test_match_heading_chapter_arabic() -> None:
    assert _match_heading_to_abs_chapter("CHAPTER 5", _WOT_CHAPTERS) == 6
    assert _match_heading_to_abs_chapter("Chapter 1", _WOT_CHAPTERS) == 2


def test_match_heading_chapter_roman() -> None:
    assert _match_heading_to_abs_chapter("Chapter V", _WOT_CHAPTERS) == 6
    assert _match_heading_to_abs_chapter("CHAPTER IV", _WOT_CHAPTERS) == 5


def test_match_heading_prologue_part1() -> None:
    # Must pick Part 1, not Part 2.
    assert _match_heading_to_abs_chapter("PROLOGUE", _WOT_CHAPTERS) == 0


def test_match_heading_epilogue() -> None:
    assert _match_heading_to_abs_chapter("Epilogue", _WOT_CHAPTERS) == 7


def test_match_heading_no_match_returns_none() -> None:
    # No "Chapter 99" exists.
    assert _match_heading_to_abs_chapter("Chapter 99", _WOT_CHAPTERS) is None
    # Random text isn't a chapter heading at all.
    assert _match_heading_to_abs_chapter("Acknowledgments", _WOT_CHAPTERS) is None


# ── chapter snap end-to-end in the pipeline ────────────────────────────────────


async def test_pipeline_snaps_chapter_heading_to_abs_start(
    client: AsyncClient,
    jwt_headers: dict[str, str],
    db_session_factory: async_sessionmaker,  # type: ignore[type-arg]
    tmp_path: Path,
) -> None:
    """Chapter-heading paragraphs are forced to ABS chapter.start times."""
    paragraphs = [
        "First narrative paragraph of chapter one with substantive content.",
        "CHAPTER 2",
        "Second chapter opens with substantive content for fuzzy matching.",
    ]
    index = {
        "para_000": {
            "text": paragraphs[0],
            "ebook_pos": "/body/DocFragment[1]/body/section[1]/p[1]",
        },
        "para_001": {
            "text": paragraphs[1],
            "ebook_pos": "/body/DocFragment[2]/body/h2[1]",
        },
        "para_002": {
            "text": paragraphs[2],
            "ebook_pos": "/body/DocFragment[2]/body/section[1]/p[1]",
        },
    }
    metadata = copy.deepcopy(ABS_METADATA)
    # Insert two body chapters whose starts diverge from where fuzzy match
    # would put them — the snap should win.
    metadata["media"]["chapters"] = [
        {"id": 0, "start": 0.0, "end": 5.0, "title": "Intro"},
        {"id": 1, "start": 5.0, "end": 500.0, "title": "Chapter 1: Opening"},
        {"id": 2, "start": 500.0, "end": 1000.0, "title": "Chapter 2: The Middle"},
    ]
    words = _words_for(paragraphs)

    resp = await client.post(
        "/alignment/jobs", json={"abs_item_id": ABS_ITEM_ID}, headers=jwt_headers
    )
    job_id = resp.json()["id"]

    await _run_pipeline(
        job_id, db_session_factory, tmp_path,
        abs_metadata_override=metadata,
        paragraphs_override=paragraphs, index_override=index, words_override=words,
    )

    resp = await client.get(f"/alignment/jobs/{job_id}/sync-map", headers=jwt_headers)
    entries = resp.json()
    chapter_2 = next(e for e in entries if e["id"] == "para_001")
    assert chapter_2["audio_start"] == pytest.approx(500.0)
    # Heading audio_end is clamped to start+5s.
    assert chapter_2["audio_end"] == pytest.approx(505.0)


async def test_pipeline_interpolates_around_snapped_chapter(
    client: AsyncClient,
    jwt_headers: dict[str, str],
    db_session_factory: async_sessionmaker,  # type: ignore[type-arg]
    tmp_path: Path,
) -> None:
    """Paragraphs flanking a snapped heading interpolate to fall between anchors."""
    paragraphs = [
        "Anchor first narrative paragraph with substantive content for matching.",
        "Filler paragraph one between anchor and chapter two heading content.",
        "Filler paragraph two between anchor and chapter two heading content.",
        "CHAPTER 2",
        "After-chapter narrative paragraph with substantive content for matching.",
    ]
    index = {
        f"para_{i:03d}": {
            "text": paragraphs[i],
            "ebook_pos": (
                "/body/DocFragment[2]/body/h2[1]" if i == 3
                else f"/body/DocFragment[{1 if i < 3 else 2}]/body/section[1]/p[{i+1}]"
            ),
        }
        for i in range(len(paragraphs))
    }
    metadata = copy.deepcopy(ABS_METADATA)
    metadata["media"]["chapters"] = [
        {"id": 0, "start": 0.0, "end": 5.0, "title": "Intro"},
        {"id": 1, "start": 5.0, "end": 600.0, "title": "Chapter 1: Opening"},
        {"id": 2, "start": 600.0, "end": 1200.0, "title": "Chapter 2: Next"},
    ]
    words = _words_for(paragraphs)

    resp = await client.post(
        "/alignment/jobs", json={"abs_item_id": ABS_ITEM_ID}, headers=jwt_headers
    )
    job_id = resp.json()["id"]

    await _run_pipeline(
        job_id, db_session_factory, tmp_path,
        abs_metadata_override=metadata,
        paragraphs_override=paragraphs, index_override=index, words_override=words,
    )

    resp = await client.get(f"/alignment/jobs/{job_id}/sync-map", headers=jwt_headers)
    entries = resp.json()
    by_id = {e["id"]: e for e in entries}
    # CHAPTER 2 heading is pinned to ABS chapter.start = 600.0
    assert by_id["para_003"]["audio_start"] == pytest.approx(600.0)
    # Strict monotonicity across the whole sync map
    for prev, cur in zip(entries, entries[1:]):
        assert cur["audio_start"] >= prev["audio_start"]


async def test_pipeline_snapped_heading_survives_overshooting_predecessor(
    client: AsyncClient,
    jwt_headers: dict[str, str],
    db_session_factory: async_sessionmaker,  # type: ignore[type-arg]
    tmp_path: Path,
) -> None:
    """A fuzzy match that overshoots the next chapter start must not demote the snap.

    The last paragraph of a chapter often fuzzy-matches a few seconds past the
    ABS start of the next chapter; the snapped heading is ground truth and must
    stay pinned, with the overshooting predecessor clamped instead.
    """
    import re as _re

    paragraphs = [
        "Opening chapter paragraph with plenty of substantive narrative content "
        "that keeps the synthesized transcript going for a while longer here.",
        "CHAPTER 2",
        "Second chapter opens with substantive content for fuzzy matching.",
    ]
    index = {
        "para_000": {
            "text": paragraphs[0],
            "ebook_pos": "/body/DocFragment[1]/body/section[1]/p[1]",
        },
        "para_001": {
            "text": paragraphs[1],
            "ebook_pos": "/body/DocFragment[2]/body/h2[1]",
        },
        "para_002": {
            "text": paragraphs[2],
            "ebook_pos": "/body/DocFragment[2]/body/section[1]/p[1]",
        },
    }
    words = _words_for(paragraphs)
    # _words_for lays out p0's words from t=1.0 at 0.4s each; place the ABS
    # chapter-2 start 2s before p0's matched end so p0 overshoots it.
    n0 = len(_re.findall(r"[A-Za-z0-9]+", paragraphs[0]))
    ch2_start = (1.0 + 0.4 * n0) - 2.0
    metadata = copy.deepcopy(ABS_METADATA)
    metadata["media"]["chapters"] = [
        {"id": 0, "start": 0.0, "end": ch2_start, "title": "Chapter 1: Opening"},
        {"id": 1, "start": ch2_start, "end": ch2_start + 3.0, "title": "Chapter 2: The Middle"},
        {"id": 2, "start": ch2_start + 3.0, "end": 1000.0, "title": "Chapter 3: Later"},
    ]

    resp = await client.post(
        "/alignment/jobs", json={"abs_item_id": ABS_ITEM_ID}, headers=jwt_headers
    )
    job_id = resp.json()["id"]

    await _run_pipeline(
        job_id, db_session_factory, tmp_path,
        abs_metadata_override=metadata,
        paragraphs_override=paragraphs, index_override=index, words_override=words,
    )

    resp = await client.get(f"/alignment/jobs/{job_id}/sync-map", headers=jwt_headers)
    entries = resp.json()
    by_id = {e["id"]: e for e in entries}
    # The snapped heading stays pinned to the ABS chapter start...
    assert by_id["para_001"]["audio_start"] == pytest.approx(ch2_start)
    assert by_id["para_001"]["audio_end"] == pytest.approx(ch2_start + 3.0)
    # ...and the overshooting predecessor is clamped to it, not vice versa.
    assert by_id["para_000"]["audio_start"] == pytest.approx(ch2_start)
    # The following paragraph keeps its genuine fuzzy-matched time.
    assert by_id["para_002"]["audio_start"] > ch2_start + 3.0
    for prev, cur in zip(entries, entries[1:]):
        assert cur["audio_start"] >= prev["audio_start"]


async def test_pipeline_positional_snap_survives_overshoot(
    client: AsyncClient,
    jwt_headers: dict[str, str],
    db_session_factory: async_sessionmaker,  # type: ignore[type-arg]
    tmp_path: Path,
) -> None:
    """Positionally-snapped headings are also mandatory anchors in the repair pass."""
    import re as _re

    paragraphs = [
        "Opening narrative paragraph with substantive content for fuzzy matching.",
        "STRANGE BUSINESS",
        "Short paragraph right after the first narrative chapter heading here.",
        "A much longer middle paragraph with plenty of substantive narrative "
        "content that runs on long enough to overshoot the next chapter start.",
        "LOCAL CUISINE",
        "Trailing narrative paragraph with substantive content for fuzzy matching.",
    ]
    index = {
        f"para_{i:03d}": {
            "text": paragraphs[i],
            "ebook_pos": (
                f"/body/DocFragment[{(1, 2, 2, 2, 3, 3)[i]}]/body/"
                + ("h2[1]" if i in (1, 4) else "section[1]/p[1]")
            ),
        }
        for i in range(len(paragraphs))
    }
    words = _words_for(paragraphs)
    starts = {w["word"]: w["start"] for w in words}
    # ABS titles are generic, so no heading matches by title → positional snap.
    # Chapter 2 starts exactly at the first heading's spoken time; chapter 3
    # starts 1s before the long middle paragraph's matched end (overshoot).
    n_before_h2 = len(_re.findall(r"[A-Za-z0-9]+", " ".join(paragraphs[:4])))
    p3_end = 1.0 + 0.4 * n_before_h2 + 0.4 * 3  # + inter-paragraph gaps
    s1 = starts["strange"]
    s2 = p3_end - 1.0
    metadata = copy.deepcopy(ABS_METADATA)
    metadata["media"]["chapters"] = [
        {"id": 0, "start": 0.0, "end": s1, "title": "01"},
        {"id": 1, "start": s1, "end": s2, "title": "02"},
        {"id": 2, "start": s2, "end": 1000.0, "title": "03"},
    ]

    resp = await client.post(
        "/alignment/jobs", json={"abs_item_id": ABS_ITEM_ID}, headers=jwt_headers
    )
    job_id = resp.json()["id"]

    await _run_pipeline(
        job_id, db_session_factory, tmp_path,
        abs_metadata_override=metadata,
        paragraphs_override=paragraphs, index_override=index, words_override=words,
    )

    resp = await client.get(f"/alignment/jobs/{job_id}/sync-map", headers=jwt_headers)
    entries = resp.json()
    by_id = {e["id"]: e for e in entries}
    assert by_id["para_001"]["audio_start"] == pytest.approx(s1)
    assert by_id["para_004"]["audio_start"] == pytest.approx(s2)
    for prev, cur in zip(entries, entries[1:]):
        assert cur["audio_start"] >= prev["audio_start"]


async def test_pipeline_warns_on_unmatched_chapter_heading(
    client: AsyncClient,
    jwt_headers: dict[str, str],
    db_session_factory: async_sessionmaker,  # type: ignore[type-arg]
    tmp_path: Path,
) -> None:
    """A chapter heading with no ABS counterpart emits unmatched_chapter_heading."""
    paragraphs = [
        "Opening paragraph that fuzzy-matches the synthesized audio nicely.",
        "CHAPTER 99",
        "Trailing paragraph that fuzzy-matches the synthesized audio nicely.",
    ]
    index = {
        "para_000": {"text": paragraphs[0], "ebook_pos": "/body/DocFragment[1]/body/p[1]"},
        "para_001": {"text": paragraphs[1], "ebook_pos": "/body/DocFragment[2]/body/h2[1]"},
        "para_002": {"text": paragraphs[2], "ebook_pos": "/body/DocFragment[2]/body/p[2]"},
    }
    metadata = copy.deepcopy(ABS_METADATA)
    metadata["media"]["chapters"] = [
        {"id": 0, "start": 0.0, "end": 5.0, "title": "Intro"},
        {"id": 1, "start": 5.0, "end": 600.0, "title": "Chapter 1: Only One"},
    ]
    words = _words_for(paragraphs)

    resp = await client.post(
        "/alignment/jobs", json={"abs_item_id": ABS_ITEM_ID}, headers=jwt_headers
    )
    job_id = resp.json()["id"]

    await _run_pipeline(
        job_id, db_session_factory, tmp_path,
        abs_metadata_override=metadata,
        paragraphs_override=paragraphs, index_override=index, words_override=words,
    )

    resp = await client.get(f"/alignment/jobs/{job_id}", headers=jwt_headers)
    body = resp.json()
    assert body["status"] == "complete_with_warnings"
    assert any("unmatched_chapter_heading" in w for w in body["warnings"])


# ── Transcription progress mapping ────────────────────────────────────────────


def test_stage_progress_endpoints() -> None:
    assert _stage_progress(0) == 30
    assert _stage_progress(100) == 85


def test_stage_progress_clamps() -> None:
    assert _stage_progress(-10) == 30
    assert _stage_progress(200) == 85


def test_stage_progress_monotonic() -> None:
    prev = -1
    for pct in range(0, 101, 5):
        v = _stage_progress(pct)
        assert v >= prev, f"regression at {pct}: {v} < {prev}"
        prev = v


# ── MappingRead.sync_warnings decoding ────────────────────────────────────────


def _mapping_read_with_warnings(raw: object) -> "list[str]":
    from datetime import UTC, datetime

    from earmark.schemas import MappingRead

    m = MappingRead(
        id=1,
        user_id=1,
        abs_item_id="x",
        abs_title="t",
        abs_author=None,
        ebook_source="local",
        ebook_path=None,
        ebook_filename=None,
        ebook_source_ref=None,
        kosync_document=None,
        created_at=datetime.now(UTC),
        sync_warnings=raw,
    )
    return m.sync_warnings


def test_mapping_read_decodes_json_warnings_string() -> None:
    # AlignmentJob.warnings is stored as a JSON-encoded string column.
    raw = '["suspect_first_entry: \'X\'", "docfragment_gap: missing [7]"]'
    assert _mapping_read_with_warnings(raw) == [
        "suspect_first_entry: 'X'",
        "docfragment_gap: missing [7]",
    ]


def test_mapping_read_warnings_none_and_empty() -> None:
    assert _mapping_read_with_warnings(None) == []
    assert _mapping_read_with_warnings("") == []


def test_mapping_read_warnings_malformed_json() -> None:
    assert _mapping_read_with_warnings("{not json") == []


def test_mapping_read_warnings_passthrough_list() -> None:
    assert _mapping_read_with_warnings(["a", "b"]) == ["a", "b"]
