from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from httpx import AsyncClient

from earmark.services.ebook_sources import CalibreOpdsSource, LocalEbookSource
from earmark.services.ebook_sources.base import normalize

_ACQ = 'rel="http://opds-spec.org/acquisition"'

OPDS_XML_TWO_MATCHES = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>The Test Book</title>
    <author><name>Test Author</name></author>
    <link {_ACQ} type="application/epub+zip" href="/opds/download/1/test.epub"/>
  </entry>
  <entry>
    <title>The Test Book</title>
    <author><name>Test Author</name></author>
    <link {_ACQ} type="application/epub+zip" href="/opds/download/2/test.epub"/>
  </entry>
</feed>
"""

OPDS_XML_NO_MATCHES = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"></feed>
"""

OPDS_XML_AUTHOR_MISMATCH = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>The Test Book</title>
    <author><name>J.K. Rowling</name></author>
    <link {_ACQ} type="application/epub+zip" href="/opds/download/9/test.epub"/>
  </entry>
</feed>
"""

OPDS_XML_MULTI_AUTHOR = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Towers of Midnight</title>
    <author><name>Robert Jordan</name></author>
    <author><name>Brandon Sanderson</name></author>
    <link {_ACQ} type="application/x-mobipocket-ebook" href="/opds/download/16/mobi/"/>
  </entry>
</feed>
"""

OPDS_XML_SERIES_PREFIX = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Wheel of Time [07]: A Crown of Swords</title>
    <author><name>Robert Jordan</name></author>
    <link {_ACQ} type="application/epub+zip" href="/opds/download/7/cos.epub"/>
  </entry>
</feed>
"""

OPDS_XML_TOKEN_FALLBACK = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>The Name of the Wind: 10th Anniversary Edition</title>
    <author><name>Patrick Rothfuss</name></author>
    <link {_ACQ} type="application/epub+zip" href="/opds/download/3/notw.epub"/>
  </entry>
</feed>
"""

OPDS_XML_MULTI_FORMAT = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>The Test Book</title>
    <author><name>Test Author</name></author>
    <link {_ACQ} type="application/x-mobipocket-ebook" href="/opds/download/1/test.mobi"/>
    <link {_ACQ} type="application/epub+zip" href="/opds/download/1/test.epub"/>
  </entry>
</feed>
"""


def test_normalize_strips_punctuation_and_lowercases() -> None:
    assert normalize("Hello, World!") == "hello world"
    assert normalize("Café résumé") == "cafe resume"


# ── LocalEbookSource ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_local_source_exact_match_with_parent_author(tmp_path: Path) -> None:
    author_dir = tmp_path / "Test Author"
    author_dir.mkdir()
    (author_dir / "The Test Book.epub").write_bytes(b"EPUB")
    (tmp_path / "Unrelated.epub").write_bytes(b"OTHER")

    src = LocalEbookSource(root=tmp_path)
    results = await src.search("The Test Book", "Test Author")

    assert len(results) >= 1
    top = results[0]
    assert top.ref.endswith("The Test Book.epub")
    assert top.title == "The Test Book"


@pytest.mark.asyncio
async def test_local_source_fuzzy_only(tmp_path: Path) -> None:
    (tmp_path / "thetestbook companion.epub").write_bytes(b"EPUB")

    src = LocalEbookSource(root=tmp_path)
    results = await src.search("thetestbook", None)
    assert len(results) == 1
    assert results[0].ref.endswith("thetestbook companion.epub")


@pytest.mark.asyncio
async def test_local_source_empty_when_no_match(tmp_path: Path) -> None:
    (tmp_path / "Something Else.epub").write_bytes(b"EPUB")
    src = LocalEbookSource(root=tmp_path)
    results = await src.search("Unknown Title", None)
    assert results == []


@pytest.mark.asyncio
async def test_local_source_fetch_copies_file(tmp_path: Path) -> None:
    (tmp_path / "book.epub").write_bytes(b"CONTENT")
    dest = tmp_path / "out" / "book.epub"

    src = LocalEbookSource(root=tmp_path)
    await src.fetch("book.epub", dest)
    assert dest.read_bytes() == b"CONTENT"


# ── CalibreOpdsSource ──────────────────────────────────────────────────────────


def _opds_transport(body: str, *, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body, headers={"content-type": "application/xml"})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_calibre_source_returns_all_candidates() -> None:
    src = CalibreOpdsSource(
        base_url="http://calibre.test",
        username="u",
        password="p",
        transport=_opds_transport(OPDS_XML_TWO_MATCHES),
    )
    results = await src.search("The Test Book", "Test Author")
    assert len(results) == 2
    assert {r.ref for r in results} == {
        "/opds/download/1/test.epub",
        "/opds/download/2/test.epub",
    }


@pytest.mark.asyncio
async def test_calibre_source_no_matches() -> None:
    src = CalibreOpdsSource(
        base_url="http://calibre.test",
        transport=_opds_transport(OPDS_XML_NO_MATCHES),
    )
    results = await src.search("Anything", None)
    assert results == []


@pytest.mark.asyncio
async def test_calibre_source_author_mismatch_still_returned() -> None:
    """Author is a ranking signal, not a filter: a title match surfaces even when
    the author differs (ABS sometimes stores the series name as the author)."""
    src = CalibreOpdsSource(
        base_url="http://calibre.test",
        transport=_opds_transport(OPDS_XML_AUTHOR_MISMATCH),
    )
    results = await src.search("The Test Book", "Test Author")
    assert len(results) == 1
    assert results[0].ref == "/opds/download/9/test.epub"


@pytest.mark.asyncio
async def test_calibre_source_author_match_ranks_first() -> None:
    """Among equal title matches, the correct-author entry sorts ahead of a mismatch."""
    feed = f"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>The Test Book</title>
    <author><name>J.K. Rowling</name></author>
    <link {_ACQ} type="application/epub+zip" href="/opds/download/wrong.epub"/>
  </entry>
  <entry>
    <title>The Test Book</title>
    <author><name>Test Author</name></author>
    <link {_ACQ} type="application/epub+zip" href="/opds/download/right.epub"/>
  </entry>
</feed>
"""
    src = CalibreOpdsSource(
        base_url="http://calibre.test", transport=_opds_transport(feed)
    )
    results = await src.search("The Test Book", "Test Author")
    assert [r.ref for r in results] == [
        "/opds/download/right.epub",
        "/opds/download/wrong.epub",
    ]


@pytest.mark.asyncio
async def test_calibre_source_fetch_writes_file(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"EPUBBYTES")

    src = CalibreOpdsSource(
        base_url="http://calibre.test",
        transport=httpx.MockTransport(handler),
    )
    dest = tmp_path / "cache" / "ebook.epub"
    await src.fetch("/opds/download/1/test.epub", dest)
    assert dest.read_bytes() == b"EPUBBYTES"


@pytest.mark.asyncio
async def test_calibre_source_raises_without_base_url() -> None:
    src = CalibreOpdsSource(base_url="")
    with pytest.raises(RuntimeError):
        await src.search("title", None)


@pytest.mark.asyncio
async def test_calibre_source_multi_author_matches_any() -> None:
    """Towers-of-Midnight case: entry has two authors; ABS only knows one."""
    src = CalibreOpdsSource(
        base_url="http://calibre.test",
        transport=_opds_transport(OPDS_XML_MULTI_AUTHOR),
    )
    results = await src.search("Towers of Midnight", "Brandon Sanderson")
    assert len(results) == 1
    assert results[0].ref == "/opds/download/16/mobi/"
    assert results[0].format == "mobi"
    assert "Robert Jordan" in (results[0].author or "")
    assert "Brandon Sanderson" in (results[0].author or "")


@pytest.mark.asyncio
async def test_calibre_source_returns_mobi_when_no_epub() -> None:
    src = CalibreOpdsSource(
        base_url="http://calibre.test",
        transport=_opds_transport(OPDS_XML_MULTI_AUTHOR),
    )
    results = await src.search("Towers of Midnight", "Robert Jordan")
    assert len(results) == 1
    assert results[0].format == "mobi"


@pytest.mark.asyncio
async def test_calibre_source_series_prefix_title_matches() -> None:
    src = CalibreOpdsSource(
        base_url="http://calibre.test",
        transport=_opds_transport(OPDS_XML_SERIES_PREFIX),
    )
    results = await src.search("A Crown of Swords", "Robert Jordan")
    assert len(results) == 1
    assert results[0].ref == "/opds/download/7/cos.epub"
    assert results[0].format == "epub"


@pytest.mark.asyncio
async def test_calibre_source_token_fallback_matches() -> None:
    src = CalibreOpdsSource(
        base_url="http://calibre.test",
        transport=_opds_transport(OPDS_XML_TOKEN_FALLBACK),
    )
    results = await src.search("The Name of the Wind", "Patrick Rothfuss")
    assert len(results) == 1
    assert results[0].ref == "/opds/download/3/notw.epub"


@pytest.mark.asyncio
async def test_calibre_source_multi_format_sorts_epub_first() -> None:
    src = CalibreOpdsSource(
        base_url="http://calibre.test",
        transport=_opds_transport(OPDS_XML_MULTI_FORMAT),
    )
    results = await src.search("The Test Book", "Test Author")
    assert [r.format for r in results] == ["epub", "mobi"]


@pytest.mark.asyncio
async def test_calibre_source_builds_query_from_raw_title() -> None:
    """ABS title '09 - Winter's Heart' should send 'winter' (apostrophe-trimmed)."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(200, text=OPDS_XML_NO_MATCHES)

    src = CalibreOpdsSource(
        base_url="http://calibre.test",
        transport=httpx.MockTransport(handler),
    )
    await src.search("09 - Winter's Heart", "Robert Jordan")
    assert captured["path"] == "/opds/search/winter"


@pytest.mark.asyncio
async def test_calibre_source_query_strips_leading_index_and_stopwords() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(200, text=OPDS_XML_NO_MATCHES)

    src = CalibreOpdsSource(
        base_url="http://calibre.test",
        transport=httpx.MockTransport(handler),
    )
    # "A Crown of Swords" → drop "A" / "of" stopwords → first significant = "crown"
    await src.search("A Crown of Swords", "Robert Jordan")
    assert captured["path"] == "/opds/search/crown"


@pytest.mark.asyncio
async def test_calibre_source_winters_heart_end_to_end() -> None:
    """Towers-of-Midnight regression sibling: ABS '09 - Winter's Heart' finds the WoT entry."""

    feed = f"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Wheel of Time [09]: Winter's Heart</title>
    <author><name>Robert Jordan</name></author>
    <link {_ACQ} type="application/epub+zip" href="/opds/download/9/winters-heart.epub"/>
  </entry>
</feed>
"""
    src = CalibreOpdsSource(
        base_url="http://calibre.test", transport=_opds_transport(feed)
    )
    results = await src.search("09 - Winter's Heart", "Robert Jordan")
    assert len(results) == 1
    assert results[0].ref == "/opds/download/9/winters-heart.epub"


@pytest.mark.asyncio
async def test_calibre_source_token_match_requires_all_tokens() -> None:
    """Guard: 'Crossroads' alone must not match 'Crossroads of Twilight'."""
    feed = f"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Crossroads of Twilight</title>
    <author><name>Robert Jordan</name></author>
    <link {_ACQ} type="application/epub+zip" href="/opds/download/10/cot.epub"/>
  </entry>
</feed>
"""
    src = CalibreOpdsSource(
        base_url="http://calibre.test", transport=_opds_transport(feed)
    )
    # ABS title that should NOT match (missing the "of twilight" half).
    results = await src.search("Apocalypse", "Robert Jordan")
    assert results == []


@pytest.mark.asyncio
async def test_calibre_source_word_volume_prefix_builds_query() -> None:
    """ABS title 'Book Ten: Crossroads of Twilight' should send 'crossroads', not 'book'."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(200, text=OPDS_XML_NO_MATCHES)

    src = CalibreOpdsSource(
        base_url="http://calibre.test",
        transport=httpx.MockTransport(handler),
    )
    await src.search("Book Ten: Crossroads of Twilight", "Robert Jordan")
    assert captured["path"] == "/opds/search/crossroads"


@pytest.mark.asyncio
async def test_calibre_source_word_volume_prefix_matches() -> None:
    """Real-world regression: ABS title 'Book Ten: Crossroads of Twilight' with the
    series name stored as author ('The Wheel of Time') still finds the WoT entry whose
    real author is 'Robert Jordan'."""
    feed = f"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Wheel of Time [10]: Crossroads of Twilight</title>
    <author><name>Robert Jordan</name></author>
    <link {_ACQ} type="application/epub+zip" href="/opds/download/10/cot.epub"/>
  </entry>
</feed>
"""
    src = CalibreOpdsSource(
        base_url="http://calibre.test", transport=_opds_transport(feed)
    )
    results = await src.search(
        "Book Ten: Crossroads of Twilight", "The Wheel of Time"
    )
    assert len(results) == 1
    assert results[0].ref == "/opds/download/10/cot.epub"


@pytest.mark.asyncio
async def test_calibre_source_volume_prefix_does_not_mangle_plain_title() -> None:
    """'Book of the Dead' has no volume separator, so the prefix must not be stripped."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(200, text=OPDS_XML_NO_MATCHES)

    src = CalibreOpdsSource(
        base_url="http://calibre.test",
        transport=httpx.MockTransport(handler),
    )
    await src.search("Book of the Dead", "Some Author")
    assert captured["path"] == "/opds/search/book"


_HWFWM_FEED = f"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>He Who Fights with Monsters 10: A LitRPG Adventure</title>
    <author><name>Shirtaloon</name></author>
    <link {_ACQ} type="application/epub+zip" href="/opds/download/81/hwfwm10.epub"/>
  </entry>
  <entry>
    <title>He Who Fights with Monsters, 3</title>
    <author><name>Shirtaloon</name></author>
    <link {_ACQ} type="application/epub+zip" href="/opds/download/88/hwfwm3.epub"/>
  </entry>
</feed>
"""


@pytest.mark.asyncio
async def test_calibre_source_volume_number_must_agree() -> None:
    """Regression: ABS 'He Who Fights with Monsters 03' matched volume 10, because
    the word tokens are identical and the zero-padded number was ignored."""
    src = CalibreOpdsSource(
        base_url="http://calibre.test", transport=_opds_transport(_HWFWM_FEED)
    )
    results = await src.search("He Who Fights with Monsters 03", "Shirtaloon")
    assert [r.ref for r in results] == ["/opds/download/88/hwfwm3.epub"]


@pytest.mark.asyncio
async def test_calibre_source_matches_abs_title_with_extra_subtitle() -> None:
    """ABS titles pile subtitle/format noise around the plain Calibre title."""
    src = CalibreOpdsSource(
        base_url="http://calibre.test", transport=_opds_transport(_HWFWM_FEED)
    )
    results = await src.search(
        "He Who Fights with Monsters 3: A LitRPG Adventure (Unabridged)", "Shirtaloon"
    )
    assert [r.ref for r in results] == ["/opds/download/88/hwfwm3.epub"]


@pytest.mark.asyncio
async def test_calibre_source_series_prefix_number_is_not_a_conflict() -> None:
    """'Wheel of Time [09]' carries a number the ABS title doesn't — still a match."""
    feed = f"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Wheel of Time [09]: Winter's Heart</title>
    <author><name>Robert Jordan</name></author>
    <link {_ACQ} type="application/epub+zip" href="/opds/download/12/wh.epub"/>
  </entry>
</feed>
"""
    src = CalibreOpdsSource(
        base_url="http://calibre.test", transport=_opds_transport(feed)
    )
    results = await src.search("Winter's Heart", "Robert Jordan")
    assert [r.ref for r in results] == ["/opds/download/12/wh.epub"]


# ── Route integration ─────────────────────────────────────────────────────────


@pytest.fixture
async def jwt_headers(client: AsyncClient) -> dict[str, str]:
    await client.post(
        "/auth/register",
        json={
            "email": "src@example.com",
            "password": "secret",
            "kosync_username": "src_web",
            "kosync_password": "secret",
        },
    )
    resp = await client.post(
        "/auth/login", json={"email": "src@example.com", "password": "secret"}
    )
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_calibre_ebooks_endpoint_requires_cwa_url(
    client: AsyncClient, jwt_headers: dict[str, str]
) -> None:
    with patch("earmark.routers.mappings.settings.cwa_url", ""):
        resp = await client.get(
            "/web/calibre-ebooks?abs_item_id=li_x", headers=jwt_headers
        )
    assert resp.status_code == 503


async def _seed_library_item(
    db_session_factory, abs_item_id: str, title: str, author: str
) -> None:
    import json as _json

    from earmark.models import AbsLibraryItem

    async with db_session_factory() as session:
        session.add(
            AbsLibraryItem(
                abs_item_id=abs_item_id,
                library_id="lib_001",
                title=title,
                author=author,
                audio_file_count=0,
                total_duration_seconds=0.0,
                raw_metadata=_json.dumps({}),
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_calibre_ebooks_endpoint_returns_candidates(
    client: AsyncClient, jwt_headers: dict[str, str], db_session_factory
) -> None:
    from earmark.services.ebook_sources import CalibreOpdsSource as _Src
    from earmark.services.ebook_sources.base import EbookCandidate

    await _seed_library_item(db_session_factory, "li_x", "Test Book", "Test Author")

    async def fake_search(self, title, author):  # type: ignore[no-untyped-def]
        assert title == "Test Book"
        assert author == "Test Author"
        return [
            EbookCandidate(
                ref="/opds/download/1/test.epub",
                title="Test Book",
                author="Test Author",
                format="epub",
            )
        ]

    with (
        patch("earmark.routers.mappings.settings.cwa_url", "http://fake"),
        patch("earmark.routers.mappings.settings.audiobookshelf_url", ""),
        patch.object(_Src, "search", fake_search),
    ):
        resp = await client.get(
            "/web/calibre-ebooks?abs_item_id=li_x", headers=jwt_headers
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["ref"] == "/opds/download/1/test.epub"


@pytest.mark.asyncio
async def test_calibre_ebooks_endpoint_502_on_http_error(
    client: AsyncClient, jwt_headers: dict[str, str], db_session_factory
) -> None:
    from earmark.services.ebook_sources import CalibreOpdsSource as _Src

    await _seed_library_item(db_session_factory, "li_x", "Test Book", "Test Author")

    async def boom(self, title, author):  # type: ignore[no-untyped-def]
        raise httpx.ConnectError("nope")

    with (
        patch("earmark.routers.mappings.settings.cwa_url", "http://fake"),
        patch("earmark.routers.mappings.settings.audiobookshelf_url", ""),
        patch.object(_Src, "search", boom),
    ):
        resp = await client.get(
            "/web/calibre-ebooks?abs_item_id=li_x", headers=jwt_headers
        )

    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_create_mapping_calibre_requires_ref(
    client: AsyncClient, jwt_headers: dict[str, str]
) -> None:
    resp = await client.post(
        "/web/mappings",
        json={
            "abs_item_id": "li_x",
            "abs_title": "Test",
            "abs_author": None,
            "ebook_source": "calibre",
        },
        headers=jwt_headers,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_mapping_calibre_persists_ref(
    client: AsyncClient, jwt_headers: dict[str, str]
) -> None:
    resp = await client.post(
        "/web/mappings",
        json={
            "abs_item_id": "li_x",
            "abs_title": "Test Book",
            "abs_author": "Test Author",
            "ebook_source": "calibre",
            "ebook_source_ref": "/opds/download/1/test.epub",
        },
        headers=jwt_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["ebook_source"] == "calibre"
    assert body["ebook_source_ref"] == "/opds/download/1/test.epub"
    assert body["ebook_path"] is None
