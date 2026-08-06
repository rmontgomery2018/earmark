import json
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, EmailStr, field_serializer, field_validator


def _utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


class UserCreate(BaseModel):
    email: EmailStr
    password: str


class UserRegister(BaseModel):
    email: EmailStr
    password: str
    kosync_username: str
    kosync_password: str


class UserRead(BaseModel):
    id: int
    email: str
    created_at: datetime

    model_config = {"from_attributes": True}

    @field_serializer("created_at")
    def _ser_created_at(self, dt: datetime) -> str:
        return _utc_iso(dt)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class KosyncUserCreate(BaseModel):
    username: str
    password: str


class KosyncUserCreated(BaseModel):
    username: str


class MetadataIn(BaseModel):
    filename: str | None = None
    title: str | None = None
    authors: str | None = None


class ProgressUpsert(BaseModel):
    document: str
    progress: str
    percentage: float
    device: str
    device_id: str
    metadata: MetadataIn | None = None


class ProgressResponse(BaseModel):
    document: str
    progress: str
    percentage: float
    device: str
    device_id: str
    timestamp: int

    model_config = {"from_attributes": True}


class ProgressListItem(ProgressResponse):
    id: int
    filename: str | None = None
    title: str | None = None
    authors: str | None = None
    is_latest: bool | None = None
    abs_synced: bool | None = None
    abs_synced_at: int | None = None
    abs_synced_position_seconds: float | None = None
    abs_sync_error: str | None = None


class DocumentSummary(BaseModel):
    document: str
    title: str | None = None


class ProgressList(BaseModel):
    data: list[ProgressListItem]
    total: int
    page: int
    per_page: int


class RecordDelete(BaseModel):
    ids: list[int]


class AlignmentJobCreate(BaseModel):
    abs_item_id: str


class AlignmentJobRead(BaseModel):
    id: int
    abs_item_id: str
    status: str
    progress: int = 0
    error_message: str | None
    paragraph_count: int | None
    fragment_count: int | None
    audio_offset_seconds: float | None
    sync_map_path: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    warnings: list[str] = []

    @field_serializer("created_at", "updated_at", "completed_at")
    def _ser_datetimes(self, dt: datetime | None) -> str | None:
        return _utc_iso(dt) if dt is not None else None

    @field_validator("warnings", mode="before")
    @classmethod
    def _decode_warnings(cls, v: object) -> list[str]:
        if v is None or v == "":
            return []
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            try:
                decoded = json.loads(v)
                return decoded if isinstance(decoded, list) else []
            except json.JSONDecodeError:
                return []
        return []

    model_config = ConfigDict(from_attributes=True)


class SyncMapEntry(BaseModel):
    id: str
    audio_start: float
    audio_end: float
    ebook_pos: str
    text_snippet: str


class AbsItemSummary(BaseModel):
    abs_item_id: str
    title: str
    author: str | None = None


class EbookFileSummary(BaseModel):
    path: str
    filename: str
    title: str | None = None
    author: str | None = None


class EbookCandidate(BaseModel):
    ref: str
    title: str
    author: str | None = None
    format: str = "epub"


class MappingCreate(BaseModel):
    abs_item_id: str
    abs_title: str
    abs_author: str | None = None
    ebook_source: str = "local"
    ebook_path: str | None = None
    ebook_source_ref: str | None = None


class SettingRead(BaseModel):
    key: str
    label: str
    description: str
    value_type: str
    is_secret: bool
    has_db_value: bool
    display_value: str


class SettingUpdate(BaseModel):
    value: str


class LogEntry(BaseModel):
    timestamp: str | None = None
    level: str | None = None
    name: str | None = None
    message: str


class LogList(BaseModel):
    data: list[LogEntry]
    total: int
    page: int
    per_page: int


class LogFileInfo(BaseModel):
    name: str
    size_bytes: int
    modified_at: str


class MappingRead(BaseModel):
    id: int
    user_id: int
    abs_item_id: str
    abs_title: str
    abs_author: str | None
    ebook_source: str
    ebook_path: str | None
    ebook_filename: str | None
    ebook_source_ref: str | None
    kosync_document: str | None
    created_at: datetime
    alignment_job_id: int | None = None
    sync_status: str | None = None
    sync_progress: int | None = None
    sync_error: str | None = None
    sync_warnings: list[str] = []
    cache_intact: bool | None = None
    reading_percentage: float | None = None

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("created_at")
    def _ser_created_at(self, dt: datetime) -> str:
        return _utc_iso(dt)

    @field_validator("sync_warnings", mode="before")
    @classmethod
    def _decode_sync_warnings(cls, v: object) -> list[str]:
        if v is None or v == "":
            return []
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            try:
                decoded = json.loads(v)
                return decoded if isinstance(decoded, list) else []
            except json.JSONDecodeError:
                return []
        return []
