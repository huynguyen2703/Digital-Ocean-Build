from collections.abc import Sequence
from typing import Any, Generic, TypeVar
from sqlmodel import Session, SQLModel, select

T = TypeVar("T", bound=SQLModel)


class BaseRepository(Generic[T]):
    """Generic CRUD persistence wrapper optimized for speed and simplicity."""

    def __init__(self, model: type[T]):
        self.model = model

    def create(self, db: Session, *, obj_in: T, commit: bool = True) -> T:
        # Enclosing operations in explicit try/rollback blocks guarantees session cleanup 
        # when bad payloads trigger unexpected database exceptions during rapid test execution.
        try:
            db.add(obj_in)
            if commit:
                db.commit()
                db.refresh(obj_in)
            else:
                db.flush()
            return obj_in
        except Exception:
            db.rollback()
            raise

    def get(self, db: Session, id: Any) -> T | None:
        # Session.get checks the local session Identity Map before querying the DB,
        # avoiding redundant network I/O for recently queried entities.
        return db.get(self.model, id)

    def get_by(self, db: Session, **filters: Any) -> T | None:
        # Constructing statements via direct attribute lookups avoids heavy SQLAlchemy mapper
        # reflection while keeping query generation fast and deterministic.
        statement = select(self.model)
        for key, value in filters.items():
            if hasattr(self.model, key):
                statement = statement.where(getattr(self.model, key) == value)
        return db.exec(statement).first()

    def list(
        self, db: Session, *, skip: int = 0, limit: int = 100, **filters: Any
    ) -> Sequence[T]:
        # Applying pagination constraints at the query execution level prevents in-memory 
        # list slicing performance bottlenecks on larger result sets.
        statement = select(self.model).offset(skip).limit(limit)
        for key, value in filters.items():
            if hasattr(self.model, key):
                statement = statement.where(getattr(self.model, key) == value)
        return db.exec(statement).all()

    def update(
        self,
        db: Session,
        *,
        db_obj: T,
        obj_in: dict[str, Any] | SQLModel,
        commit: bool = True,
    ) -> T:
        # Extracting unset attributes via model_dump prevents partial update payloads (PATCH)
        # from accidentally overwriting existing database fields with default None values.
        update_data = (
            obj_in if isinstance(obj_in, dict) else obj_in.model_dump(exclude_unset=True)
        )
        for field, value in update_data.items():
            if hasattr(db_obj, field):
                setattr(db_obj, field, value)
        return self.create(db, obj_in=db_obj, commit=commit)

    def delete(self, db: Session, id: Any, commit: bool = True) -> bool:
        # Returning a boolean status allows the service layer to translate deletion results 
        # directly into HTTP 204 or HTTP 404 responses without catching database exceptions.
        obj = self.get(db, id)
        if not obj:
            return False
        try:
            db.delete(obj)
            if commit:
                db.commit()
            else:
                db.flush()
            return True
        except Exception:
            db.rollback()
            raise