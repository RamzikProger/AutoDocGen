from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

try:
    from backend.database import Base
except ModuleNotFoundError:
    from database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    analysis_runs: Mapped[list["AnalysisRun"]] = relationship(back_populates="user")


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    model_name: Mapped[str] = mapped_column(String(120), nullable=False)
    output_format: Mapped[str] = mapped_column(String(20), default="md", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="done", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[Optional["User"]] = relationship(back_populates="analysis_runs")
    files: Mapped[list["AnalysisFile"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    result: Mapped[Optional["AnalysisResult"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", uselist=False
    )


class AnalysisFile(Base):
    __tablename__ = "analysis_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("analysis_runs.id"), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    extension: Mapped[str] = mapped_column(String(20), nullable=False)
    chars_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    run: Mapped["AnalysisRun"] = relationship(back_populates="files")


class AnalysisResult(Base):
    __tablename__ = "analysis_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("analysis_runs.id"), nullable=False, unique=True)
    markdown_content: Mapped[str] = mapped_column(Text, nullable=False)

    run: Mapped["AnalysisRun"] = relationship(back_populates="result")


class AnalysisReport(Base):
    __tablename__ = "analysis_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
