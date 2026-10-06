"""Opt-in live cross-store validation. Uses a temporary database; never sends notifications."""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from app.config import Config
from app.database import Base, Database
from app.models import DiscoveryDraft
from app.runtime import Runtime


async def main():
    with TemporaryDirectory(prefix="pricewatch-live-") as directory:
        db = Database(f"sqlite:///{directory}/pricewatch.db")
        Base.metadata.create_all(db.engine)
        runtime = Runtime(Config(data_dir=Path(directory), scheduler_enabled=False), db)
        draft_id = runtime.discovery.create(
            {
                "name": "",
                "category": "tv",
                "target_price": None,
                "insane_deal_price": None,
                "conditions": ["new"],
                "retailers": ["worten", "darty", "radiopopular"],
                "urls": [
                    "https://www.darty.pt/products/smart-tv-tcl-55p8l-qd-mini-led-55-uhd-4k-google-tv-140cm-5901292530204"
                ],
            }
        )
        try:
            await runtime.discovery.run(draft_id)
            with db.session() as session:
                draft = session.get(DiscoveryDraft, draft_id)
                print(
                    json.dumps(
                        {
                            "status": draft.status,
                            "identity": draft.results.get("identity"),
                            "candidates": [
                                {
                                    "retailer": c["listing"]["retailer"],
                                    "model": c["listing"]["identity"]["model"],
                                    "condition": c["listing"]["condition"],
                                    "match": c["match"]["level"],
                                    "sources": c["sources"],
                                }
                                for c in draft.results.get("candidates", [])
                            ],
                            "notes": draft.results.get("errors", []),
                        },
                        indent=2,
                    )
                )
                assert draft.status == "ready"
                assert draft.results["identity"]["model"] == "55P8L"
        finally:
            await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
