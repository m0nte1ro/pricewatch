from app.models import Setting
from app.schemas.domain import Preferences


class SettingsService:
    def __init__(self, db):
        self.db = db

    def get(self) -> Preferences:
        with self.db.session() as session:
            row = session.get(Setting, "preferences")
            return Preferences.model_validate(row.value if row else {})

    def save(self, preferences: Preferences):
        with self.db.session() as session:
            session.merge(Setting(key="preferences", value=preferences.model_dump(mode="json")))
