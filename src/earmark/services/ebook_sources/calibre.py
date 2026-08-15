import base64
import logging
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from earmark.config import settings
from earmark.services.ebook_sources.base import EbookCandidate, normalize

logger = logging.getLogger(__name__)

_TITLE_STOPWORDS: frozenset[str] = frozenset(
    {"the", "a", "an", "of", "and", "or", "to"}
)

# Leading numeric/series prefixes on ABS titles, e.g. "09 - ", "01. ", "Book 12: ".
_LEADING_INDEX_RE = re.compile(
    r"^(?:book\s+)?\d+[\W_]+", re.IGNORECASE
)

# Leading volume markers on ABS titles incl. word-form numbers,
# e.g. "Book Ten: ", "Volume III - ", "Part 2. ". Conservative: one token then a separator.
_LEADING_VOLUME_RE = re.compile(
    r"^(?:book|volume|vol|part)\s+[\w'-]+\s*[:.\-]+\s*", re.IGNORECASE
)

_MIME_TO_FORMAT: dict[str, str] = {
    "application/epub+zip": "epub",
    "application/x-mobipocket-ebook": "mobi",
    "application/vnd.amazon.ebook": "azw3",
    "application/pdf": "pdf",
}

_FORMAT_PRIORITY: dict[str, int] = {"epub": 0, "azw3": 1, "mobi": 2, "pdf": 3}

# Strip leading series tags like "Wheel of Time [07]: " before normalization.
_SERIES_PREFIX_RE = re.compile(r"^[^\[\]]*\[\d+\][:\s]*", re.IGNORECASE)


class CalibreOpdsSource:
    """Searches and downloads ebooks via a Calibre Web OPDS server."""

    def __init__(
        self,
        base_url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url if base_url is not None else settings.cwa_url
        self._username = username if username is not None else settings.cwa_username
        self._password = password if password is not None else settings.cwa_password
        self._transport = transport

    def _client(self, timeout: httpx.Timeout | None = None) -> httpx.AsyncClient:
        kwargs: dict = {"base_url": self._base_url}
        if timeout is not None:
            kwargs["timeout"] = timeout
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    def _headers(self) -> dict[str, str]:
        if not self._username and not self._password:
            return {}
        creds = base64.b64encode(f"{self._username}:{self._password}".encode()).decode()
        return {"Authorization": f"Basic {creds}"}

    async def search(self, title: str, author: str | None) -> list[EbookCandidate]:
        if not self._base_url:
            raise RuntimeError("Calibre Web URL is not configured (CWA_URL).")

        core_title = _LEADING_VOLUME_RE.sub("", _LEADING_INDEX_RE.sub("", title)).strip()
        query = _build_search_query(core_title)
        norm_title = normalize(core_title)
        norm_author = normalize(author or "")
        if not query or not norm_title:
            return []

        timeout = httpx.Timeout(10.0, read=30.0)
        async with self._client(timeout=timeout) as client:
            resp = await client.get(
                f"/opds/search/{query}",
                headers=self._headers(),
                follow_redirects=True,
            )
            resp.raise_for_status()
            xml_text = resp.text

        return _parse_opds_feed(xml_text, norm_title, norm_author)

    def _safe_ref_url(self, ref: str) -> str:
        """Resolve an OPDS acquisition ref against the configured Calibre host.

        OPDS hrefs may be relative; resolve them against the base URL and reject
        anything that points to another host or a non-http(s) scheme, so a
        malicious/compromised feed can't drive a fetch to file:// or an internal
        service (SSRF).
        """
        resolved = urljoin(self._base_url, ref)
        parts = urlsplit(resolved)
        base = urlsplit(self._base_url)
        if parts.scheme not in ("http", "https"):
            raise ValueError(f"Refusing non-http(s) ebook ref: {ref!r}")
        if parts.netloc != base.netloc:
            raise ValueError(
                f"Refusing ebook ref outside the configured Calibre host: {ref!r}"
            )
        return resolved

    async def fetch(self, ref: str, dest: Path) -> None:
        if not self._base_url:
            raise RuntimeError("Calibre Web URL is not configured (CWA_URL).")
        url = self._safe_ref_url(ref)
        dest.parent.mkdir(parents=True, exist_ok=True)
        timeout = httpx.Timeout(10.0, read=300.0)
        async with self._client(timeout=timeout) as client:
            async with client.stream(
                "GET", url, headers=self._headers(), follow_redirects=True
            ) as resp:
                resp.raise_for_status()
                with dest.open("wb") as f:
                    async for chunk in resp.aiter_bytes(65536):
                        f.write(chunk)


def _build_search_query(title: str) -> str:
    """Build an OPDS-friendly search term from the raw ABS title.

    Calibre Web's OPDS search does case-insensitive substring matching against
    the stored title, but does NOT normalize away apostrophes or numeric
    prefixes. Sending the full normalized title (e.g. ``09 winters heart``)
    matches nothing, while a single significant token from the raw title
    (e.g. ``winter``) is a reliable substring of ``Winter's Heart``.
    """
    if not title:
        return ""
    stripped = _LEADING_INDEX_RE.sub("", title).strip()
    for raw_token in stripped.split():
        # Trim trailing punctuation, then drop anything after an apostrophe so
        # "Winter's" → "Winter" (matches Calibre's substring search).
        token = raw_token.strip(".,:;\"!?()[]{}").split("'")[0]
        token = token.strip("-_")
        if len(token) < 3:
            continue
        lower = token.lower()
        if not any(c.isalpha() for c in lower):
            continue
        if lower in _TITLE_STOPWORDS:
            continue
        return lower
    return ""


def _format_from_mime(mime: str) -> str:
    if mime in _MIME_TO_FORMAT:
        return _MIME_TO_FORMAT[mime]
    if "/" in mime:
        subtype = mime.split("/", 1)[1]
        return subtype.removeprefix("x-").removesuffix("+zip").removesuffix("-ebook")
    return mime or "unknown"


def _author_matches(norm_author: str, opds_authors: list[str]) -> bool:
    """Permissive author match: any overlap between ABS and OPDS authors passes."""
    if not norm_author:
        return True
    norm_opds = [normalize(a) for a in opds_authors if normalize(a)]
    if not norm_opds:
        return True  # entry has no author info; don't reject

    abs_tokens = norm_author.split()
    abs_surname = abs_tokens[-1] if abs_tokens else ""

    for opds in norm_opds:
        if opds == norm_author:
            return True
        opds_tokens = opds.split()
        if abs_surname and abs_surname in opds_tokens:
            return True
        if opds_tokens and opds_tokens[-1] in abs_tokens:
            return True
    return False


def _volume_numbers(norm_title: str) -> set[int]:
    """Numeric tokens of a normalized title — in practice the series volume.

    Compared as ints so zero-padding doesn't matter ('monsters 03' == 'monsters 3').
    """
    return {int(t) for t in norm_title.split() if t.isdigit()}


def _significant_tokens(norm_title: str) -> list[str]:
    return [
        t for t in norm_title.split()
        if len(t) >= 3 and not t.isdigit() and t not in _TITLE_STOPWORDS
    ]


def _title_match_tier(norm_abs_title: str, raw_opds_title: str) -> int | None:
    """Return 1/2/3/4 for the best matching tier, or None if no match."""
    norm_opds = normalize(raw_opds_title)
    if not norm_abs_title or not norm_opds:
        return None

    if norm_opds == norm_abs_title:
        return 1

    # Beyond an exact match, a stated volume number must agree — the word
    # tokens of "He Who Fights with Monsters 03" and "…Monsters 10" are
    # otherwise identical, and a whole-book mismatch is the worst outcome
    # the alignment pipeline can be handed. Only enforced when both sides
    # state a number; series prefixes like "Wheel of Time [09]" are paired
    # against ABS titles that carry no number at all.
    abs_numbers = _volume_numbers(norm_abs_title)
    opds_numbers = _volume_numbers(norm_opds)
    if abs_numbers and opds_numbers and not (abs_numbers & opds_numbers):
        return None

    stripped = _SERIES_PREFIX_RE.sub("", raw_opds_title)
    norm_stripped = normalize(stripped)
    if norm_stripped == norm_abs_title:
        return 2
    if norm_abs_title in norm_stripped or norm_abs_title in norm_opds:
        return 2

    abs_tokens = _significant_tokens(norm_abs_title)
    opds_tokens = set(norm_opds.split())
    if abs_tokens and all(t in opds_tokens for t in abs_tokens):
        return 3

    # Reverse containment: ABS titles often pile on subtitle/format noise
    # ("He Who Fights with Monsters 7 (Unabridged)") around the plain
    # Calibre title, so every Calibre token appearing in the ABS title is
    # also a match — ranked last, since it's the loosest test.
    opds_significant = _significant_tokens(norm_opds)
    abs_token_set = set(norm_abs_title.split())
    if opds_significant and all(t in abs_token_set for t in opds_significant):
        return 4
    return None


def _parse_opds_feed(
    xml_text: str, norm_title: str, norm_author: str
) -> list[EbookCandidate]:
    from bs4 import BeautifulSoup

    # Uses lxml's XML parser, which by default does not fetch external DTDs or
    # resolve external entities (no network), mitigating XXE. The feed source is
    # the admin-configured Calibre server.
    soup = BeautifulSoup(xml_text, "xml")
    # (tier, author_rank, format_priority, EbookCandidate)
    ranked: list[tuple[int, int, int, EbookCandidate]] = []

    for entry in soup.find_all("entry"):
        entry_title_tag = entry.find("title")
        entry_title = entry_title_tag.get_text() if entry_title_tag else ""

        tier = _title_match_tier(norm_title, entry_title)
        if tier is None:
            continue

        opds_authors = [
            name.get_text()
            for author in entry.find_all("author")
            for name in [author.find("name")]
            if name is not None
        ]
        # Author is a ranking signal, not a filter: ABS metadata sometimes stores
        # the series name (e.g. "The Wheel of Time") instead of the author, so a
        # title match with a mismatched author still surfaces, just ranked lower.
        author_rank = 0 if _author_matches(norm_author, opds_authors) else 1

        author_display = ", ".join(a for a in opds_authors if a) or None

        for link in entry.find_all(
            "link", attrs={"rel": "http://opds-spec.org/acquisition"}
        ):
            href = link.get("href")
            if not href:
                continue
            mime = link.get("type", "") or ""
            fmt = _format_from_mime(mime)
            candidate = EbookCandidate(
                ref=href,
                title=entry_title,
                author=author_display,
                format=fmt,
            )
            ranked.append((tier, author_rank, _FORMAT_PRIORITY.get(fmt, 99), candidate))

    ranked.sort(key=lambda x: (x[0], x[1], x[2]))
    return [c for *_rest, c in ranked]
