from sqlalchemy import delete, select

from app.models import StoreRule
from app.schemas.domain import PriceRule


class RuleService:
    def __init__(self, db):
        self.db = db

    def get(self, host: str) -> PriceRule | None:
        with self.db.session() as session:
            row = session.get(StoreRule, host)
            return PriceRule.model_validate(row, from_attributes=True) if row else None

    def save(self, host: str, rule: PriceRule) -> None:
        with self.db.session() as session:
            session.merge(StoreRule(host=host, **rule.model_dump()))

    def delete(self, host: str) -> None:
        with self.db.session() as session:
            session.execute(delete(StoreRule).where(StoreRule.host == host))

    def all(self) -> dict[str, PriceRule]:
        with self.db.session() as session:
            rows = session.scalars(select(StoreRule).order_by(StoreRule.host))
            return {row.host: PriceRule.model_validate(row, from_attributes=True) for row in rows}
