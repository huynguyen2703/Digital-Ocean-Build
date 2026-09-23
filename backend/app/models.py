"""Persistence models (SQLModel) and API DTOs (Pydantic)."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, field_validator
from sqlalchemy import UniqueConstraint
from sqlmodel import Field as SQLField
from sqlmodel import SQLModel

FLAG_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_flag_name(value: str) -> str:
    if not FLAG_NAME_PATTERN.fullmatch(value):
        raise ValueError(
            "name must match ^[a-z][a-z0-9_]{1,63}$ (lowercase slug, 2–64 chars)"
        )
    return value


def _validate_user_id(value: str) -> str:
    trimmed = value.strip()
    if not trimmed:
        raise ValueError("user_id must be non-empty after trim")
    if len(trimmed) > 128:
        raise ValueError("user_id must be at most 128 characters")
    return trimmed


# --- Persistence (SQLModel tables) ---


class Flag(SQLModel, table=True):
    __tablename__ = "flags"

    id: Optional[int] = SQLField(default=None, primary_key=True)
    name: str = SQLField(index=True, unique=True, max_length=64)
    description: str = SQLField(default="", max_length=512)
    enabled: bool = SQLField(default=False)
    created_at: datetime = SQLField(default_factory=utc_now)
    updated_at: datetime = SQLField(default_factory=utc_now)


class UserFlagOverride(SQLModel, table=True):
    __tablename__ = "user_flag_overrides"
    __table_args__ = (
        UniqueConstraint("flag_name", "user_id", name="uq_flag_user_override"),
    )

    id: Optional[int] = SQLField(default=None, primary_key=True)
    flag_name: str = SQLField(
        foreign_key="flags.name",
        index=True,
        max_length=64,
    )
    user_id: str = SQLField(index=True, max_length=128)
    enabled: bool = SQLField(default=False)
    updated_at: datetime = SQLField(default_factory=utc_now)


# --- API DTOs (Pydantic) ---


class FlagCreate(BaseModel):
    name: str
    description: str = ""
    enabled: bool = False

    @field_validator("name")
    @classmethod
    def name_must_be_slug(cls, value: str) -> str:
        return _validate_flag_name(value)

    @field_validator("description")
    @classmethod
    def description_max_length(cls, value: str) -> str:
        if len(value) > 512:
            raise ValueError("description must be at most 512 characters")
        return value


class FlagUpdate(BaseModel):
    enabled: bool


class FlagRead(BaseModel):
    name: str
    description: str
    enabled: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class UserOverrideUpsert(BaseModel):
    enabled: bool


class UserOverrideRead(BaseModel):
    flag_name: str
    user_id: str
    enabled: bool
    updated_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("user_id")
    @classmethod
    def user_id_rules(cls, value: str) -> str:
        return _validate_user_id(value)


class EvaluationResponse(BaseModel):
    flag: str
    user_id: str
    enabled: bool
    source: Literal["override", "global"]

    @field_validator("user_id")
    @classmethod
    def user_id_rules(cls, value: str) -> str:
        return _validate_user_id(value)


class ErrorBody(BaseModel):
    detail: str
    trace_id: str
