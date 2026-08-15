from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from earmark.database import Base


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    label: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    value_type: Mapped[str] = mapped_column(String(20))
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    kosync_users: Mapped[list["KosyncUser"]] = relationship(back_populates="user")
    ebook_mappings: Mapped[list["AbsEbookMapping"]] = relationship(back_populates="user")


class KosyncUser(Base):
    __tablename__ = "kosync_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    user: Mapped["User | None"] = relationship(back_populates="kosync_users")
    progress: Mapped[list["ReadingProgress"]] = relationship(back_populates="kosync_user")


class ReadingProgress(Base):
    __tablename__ = "reading_progress"
    __table_args__ = (
        # At most one "latest" row per (user, document). Enforces the invariant
        # that write_reading_progress maintains, turning a concurrent double-write
        # into a hard error rather than silent duplicate-latest corruption.
        Index(
            "uq_reading_progress_latest",
            "kosync_user_id",
            "document",
            unique=True,
            sqlite_where=text("is_latest = 1"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kosync_user_id: Mapped[int] = mapped_column(ForeignKey("kosync_users.id"), index=True)
    document: Mapped[str] = mapped_column(String(500), index=True)
    progress: Mapped[str] = mapped_column(String(1000))
    percentage: Mapped[float] = mapped_column(Float)
    device: Mapped[str] = mapped_column(String(255))
    device_id: Mapped[str] = mapped_column(String(255))
    filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    authors: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_latest: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    abs_synced: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    abs_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    abs_synced_position_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    abs_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    kosync_user: Mapped["KosyncUser"] = relationship(back_populates="progress")


class AbsLibraryItem(Base):
    __tablename__ = "abs_library_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    abs_item_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    library_id: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(500))
    author: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ebook_filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ebook_format: Mapped[str | None] = mapped_column(String(20), nullable=True)
    audio_file_count: Mapped[int] = mapped_column(Integer)
    total_duration_seconds: Mapped[float] = mapped_column(Float)
    abs_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    raw_metadata: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    alignment_jobs: Mapped[list["AlignmentJob"]] = relationship(back_populates="library_item")


class AlignmentJob(Base):
    __tablename__ = "alignment_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    abs_item_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("abs_library_items.abs_item_id"), index=True
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(50), default="pending")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    audio_cache_dir: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    ebook_cache_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    sync_map_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    paragraph_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fragment_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    audio_offset_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    ebook_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    ebook_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    ebook_source_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    warnings: Mapped[str | None] = mapped_column(Text, nullable=True)

    library_item: Mapped["AbsLibraryItem"] = relationship(back_populates="alignment_jobs")


class AbsEbookMapping(Base):
    __tablename__ = "abs_ebook_mappings"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    abs_item_id: Mapped[str] = mapped_column(String(255), index=True)
    abs_title: Mapped[str] = mapped_column(String(500))
    abs_author: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ebook_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    ebook_filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ebook_source: Mapped[str] = mapped_column(String(20), server_default="local")
    ebook_source_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    kosync_document: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    alignment_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("alignment_jobs.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped["User"] = relationship(back_populates="ebook_mappings")
    alignment_job: Mapped["AlignmentJob | None"] = relationship(lazy="joined")


class EbookMetadataCache(Base):
    __tablename__ = "ebook_metadata_cache"

    id: Mapped[int] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(String(1000), unique=True, index=True)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    author: Mapped[str | None] = mapped_column(String(500), nullable=True)
    file_mtime: Mapped[float] = mapped_column(Float)
    file_size: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
