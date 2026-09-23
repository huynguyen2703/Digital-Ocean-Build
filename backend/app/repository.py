"""Async persistence boundary for feature flags.

``BaseRepository`` provides standard async CRUD with typed error isolation.
``FlagRepository`` composes base repos and adds flag-domain operations
(unique name lookup, upsert targeting, parent-flag checks).

This module is the only layer that talks to SQLite. It never imports the service.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Generic, Optional, TypeVar

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from backend.app.models import Flag, UserFlagOverride, utc_now

logger = logging.getLogger("app.repository")

T = TypeVar("T", bound=SQLModel)


class RepositoryError(Exception):
    """Base class for repository-layer failures."""

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.cause = cause


class StorageError(RepositoryError):
    """Durable store failed unexpectedly (connectivity, driver, or internal DB error)."""


class NotFoundError(RepositoryError):
    """Requested flag or targeting row does not exist."""


class ConflictError(RepositoryError):
    """Write violates a uniqueness / integrity constraint (e.g. duplicate flag name)."""


class BaseRepository(Generic[T]):
    """Generic async CRUD wrapper for a single SQLModel table.

    Responsibilities:
    - Standard create / read / update / delete against ``self.model``.
    - Map ``IntegrityError`` → ``ConflictError`` and other SQLAlchemy failures
      → ``StorageError`` after a safe rollback.
    - Never crash the event loop on rollback failure.
    """

    def __init__(self, session: AsyncSession, model: type[T]) -> None:
        self._session = session
        self.model = model

    async def create(self, obj_in: T, *, commit: bool = True) -> T:
        """Insert a new row.

        Args:
            obj_in: Unpersisted SQLModel instance.
            commit: When True (default), commit and refresh; otherwise flush only.

        Returns:
            The persisted instance (with DB-generated fields when committed).

        Raises:
            ConflictError: Unique/check constraint violation.
            StorageError: Unexpected database failure after rollback.
        """
        try:
            self._session.add(obj_in)
            if commit:
                await self._session.commit()
                await self._session.refresh(obj_in)
            else:
                await self._session.flush()
            return obj_in
        except IntegrityError as exc:
            await self._safe_rollback()
            logger.warning(
                "create conflict model=%s: %s",
                self.model.__name__,
                exc,
            )
            raise ConflictError(
                f"conflict creating {self.model.__name__}",
                cause=exc,
            ) from exc
        except SQLAlchemyError as exc:
            await self._safe_rollback()
            logger.exception("create storage failure model=%s", self.model.__name__)
            raise StorageError(
                f"failed to create {self.model.__name__}",
                cause=exc,
            ) from exc

    async def get(self, id: Any) -> Optional[T]:
        """Load a row by primary key.

        Uses the session identity map when possible to avoid redundant I/O.

        Args:
            id: Primary key value.

        Returns:
            The row, or ``None`` if not found.

        Raises:
            StorageError: Unexpected database failure.
        """
        try:
            return await self._session.get(self.model, id)
        except SQLAlchemyError as exc:
            logger.exception(
                "get storage failure model=%s id=%s",
                self.model.__name__,
                id,
            )
            raise StorageError(
                f"failed to get {self.model.__name__} id={id}",
                cause=exc,
            ) from exc

    async def get_by(self, **filters: Any) -> Optional[T]:
        """Load the first row matching equality filters on model attributes.

        Args:
            **filters: Field name → value pairs (unknown keys are ignored).

        Returns:
            The first match, or ``None``.

        Raises:
            StorageError: Unexpected database failure.
        """
        try:
            statement = select(self.model)
            for key, value in filters.items():
                if hasattr(self.model, key):
                    statement = statement.where(getattr(self.model, key) == value)
            result = await self._session.exec(statement)
            return result.first()
        except SQLAlchemyError as exc:
            logger.exception(
                "get_by storage failure model=%s filters=%s",
                self.model.__name__,
                filters,
            )
            raise StorageError(
                f"failed to query {self.model.__name__}",
                cause=exc,
            ) from exc

    async def list(
        self,
        *,
        skip: int = 0,
        limit: int = 100,
        **filters: Any,
    ) -> Sequence[T]:
        """List rows with optional equality filters and pagination.

        Args:
            skip: Offset.
            limit: Max rows (applied in SQL, not in memory).
            **filters: Field name → value pairs (unknown keys ignored).

        Returns:
            Matching rows (possibly empty).

        Raises:
            StorageError: Unexpected database failure.
        """
        try:
            statement = select(self.model).offset(skip).limit(limit)
            for key, value in filters.items():
                if hasattr(self.model, key):
                    statement = statement.where(getattr(self.model, key) == value)
            result = await self._session.exec(statement)
            return result.all()
        except SQLAlchemyError as exc:
            logger.exception("list storage failure model=%s", self.model.__name__)
            raise StorageError(
                f"failed to list {self.model.__name__}",
                cause=exc,
            ) from exc

    async def update(
        self,
        db_obj: T,
        obj_in: dict[str, Any] | SQLModel,
        *,
        commit: bool = True,
    ) -> T:
        """Apply a partial update to an already-loaded instance.

        Only fields present in ``obj_in`` are written (``exclude_unset`` for
        SQLModel/Pydantic inputs), so PATCH-style payloads cannot wipe columns
        with accidental defaults.

        Args:
            db_obj: Attached instance to mutate.
            obj_in: Dict or model dump of fields to change.
            commit: When True, commit and refresh.

        Returns:
            The updated instance.

        Raises:
            ConflictError: Integrity violation on write.
            StorageError: Unexpected database failure after rollback.
        """
        update_data = (
            obj_in
            if isinstance(obj_in, dict)
            else obj_in.model_dump(exclude_unset=True)
        )
        for field, value in update_data.items():
            if hasattr(db_obj, field):
                setattr(db_obj, field, value)
        return await self.create(db_obj, commit=commit)

    async def delete(self, id: Any, *, commit: bool = True) -> bool:
        """Delete a row by primary key.

        Args:
            id: Primary key value.
            commit: When True, commit; otherwise flush.

        Returns:
            ``True`` if a row was deleted, ``False`` if the id was missing.

        Raises:
            StorageError: Unexpected database failure after rollback.
        """
        obj = await self.get(id)
        if obj is None:
            return False
        return await self.delete_obj(obj, commit=commit)

    async def delete_obj(self, obj: T, *, commit: bool = True) -> bool:
        """Delete an already-loaded instance.

        Args:
            obj: Attached instance to remove.
            commit: When True, commit; otherwise flush.

        Returns:
            ``True`` on success.

        Raises:
            StorageError: Unexpected database failure after rollback.
        """
        try:
            await self._session.delete(obj)
            if commit:
                await self._session.commit()
            else:
                await self._session.flush()
            return True
        except SQLAlchemyError as exc:
            await self._safe_rollback()
            logger.exception(
                "delete_obj storage failure model=%s",
                self.model.__name__,
            )
            raise StorageError(
                f"failed to delete {self.model.__name__}",
                cause=exc,
            ) from exc

    async def _safe_rollback(self) -> None:
        """Roll back the current transaction; never let rollback crash the loop."""
        try:
            await self._session.rollback()
        except SQLAlchemyError as exc:
            logger.exception("session rollback failed: %s", exc)


class FlagRepository:
    """Flag-domain repository built on ``BaseRepository`` for standard CRUD.

    Adds operations that are not generic:
    - create/get/update by unique ``name``
    - per-user override upsert / optional get / delete with parent-flag checks
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._flags = BaseRepository(session, Flag)
        self._overrides = BaseRepository(session, UserFlagOverride)

    async def create_flag(
        self,
        *,
        name: str,
        description: str = "",
        enabled: bool = False,
    ) -> Flag:
        """Persist a new feature flag definition via base ``create``.

        Args:
            name: Unique flag slug (validated upstream).
            description: Optional human-readable description.
            enabled: Initial global release state.

        Returns:
            The persisted ``Flag`` with generated id and timestamps.

        Raises:
            ConflictError: A flag with the same ``name`` already exists.
            StorageError: Unexpected database failure after rollback.
        """
        flag = Flag(name=name, description=description, enabled=enabled)
        try:
            return await self._flags.create(flag)
        except ConflictError as exc:
            raise ConflictError(
                f"flag '{name}' already exists",
                cause=exc.cause,
            ) from exc
        except StorageError as exc:
            raise StorageError(
                f"failed to create flag '{name}'",
                cause=exc.cause,
            ) from exc

    async def get_flag_by_name(self, name: str) -> Flag:
        """Load a flag by unique name via base ``get_by``.

        Args:
            name: Flag slug to look up.

        Returns:
            The matching ``Flag`` row.

        Raises:
            NotFoundError: No flag with this name exists.
            StorageError: Unexpected database failure.
        """
        try:
            flag = await self._flags.get_by(name=name)
        except StorageError as exc:
            raise StorageError(
                f"failed to read flag '{name}'",
                cause=exc.cause,
            ) from exc

        if flag is None:
            raise NotFoundError(f"flag '{name}' not found")
        return flag

    async def update_flag_enabled(self, name: str, enabled: bool) -> Flag:
        """Update the global enabled state using base ``update``.

        Args:
            name: Flag slug to update.
            enabled: New global release boolean (State pattern persists as bool).

        Returns:
            The updated ``Flag`` row.

        Raises:
            NotFoundError: Flag does not exist.
            StorageError: Unexpected database failure after rollback.
        """
        flag = await self.get_flag_by_name(name)
        try:
            return await self._flags.update(
                flag,
                {"enabled": enabled, "updated_at": utc_now()},
            )
        except StorageError as exc:
            raise StorageError(
                f"failed to update flag '{name}'",
                cause=exc.cause,
            ) from exc

    async def get_override(
        self,
        flag_name: str,
        user_id: str,
    ) -> Optional[UserFlagOverride]:
        """Fetch an optional per-user targeting rule via base ``get_by``.

        Missing overrides are normal (evaluate falls back to global) and return
        ``None`` rather than ``NotFoundError``.

        Args:
            flag_name: Parent flag slug.
            user_id: Opaque target user id.

        Returns:
            The override row, or ``None`` if no targeting rule exists.

        Raises:
            StorageError: Unexpected database failure.
        """
        try:
            return await self._overrides.get_by(flag_name=flag_name, user_id=user_id)
        except StorageError as exc:
            raise StorageError(
                f"failed to read override for flag '{flag_name}' user '{user_id}'",
                cause=exc.cause,
            ) from exc

    async def upsert_override(
        self,
        *,
        flag_name: str,
        user_id: str,
        enabled: bool,
    ) -> UserFlagOverride:
        """Create or replace per-user targeting using base create/update.

        Ensures the parent flag exists before writing. If an override already
        exists for ``(flag_name, user_id)``, its ``enabled`` value is updated.

        Args:
            flag_name: Existing flag slug.
            user_id: Opaque target user id.
            enabled: Targeted release state for this user.

        Returns:
            The inserted or updated ``UserFlagOverride``.

        Raises:
            NotFoundError: Parent flag does not exist.
            ConflictError: Integrity violation on insert (rare race).
            StorageError: Unexpected database failure after rollback.
        """
        await self.get_flag_by_name(flag_name)
        existing = await self.get_override(flag_name, user_id)

        try:
            if existing is not None:
                return await self._overrides.update(
                    existing,
                    {"enabled": enabled, "updated_at": utc_now()},
                )

            override = UserFlagOverride(
                flag_name=flag_name,
                user_id=user_id,
                enabled=enabled,
            )
            return await self._overrides.create(override)
        except ConflictError as exc:
            raise ConflictError(
                f"override conflict for flag '{flag_name}' user '{user_id}'",
                cause=exc.cause,
            ) from exc
        except StorageError as exc:
            raise StorageError(
                f"failed to upsert override for flag '{flag_name}' user '{user_id}'",
                cause=exc.cause,
            ) from exc

    async def delete_override(self, flag_name: str, user_id: str) -> None:
        """Remove a per-user targeting rule via base ``delete_obj``.

        Args:
            flag_name: Parent flag slug.
            user_id: Opaque target user id.

        Raises:
            NotFoundError: Flag or override does not exist.
            StorageError: Unexpected database failure after rollback.
        """
        await self.get_flag_by_name(flag_name)
        override = await self.get_override(flag_name, user_id)
        if override is None:
            raise NotFoundError(
                f"override for flag '{flag_name}' user '{user_id}' not found"
            )

        try:
            await self._overrides.delete_obj(override)
        except StorageError as exc:
            raise StorageError(
                f"failed to delete override for flag '{flag_name}' user '{user_id}'",
                cause=exc.cause,
            ) from exc
