import asyncio
import logging
import uuid
from decimal import Decimal
from urllib.parse import urlsplit

from sqlalchemy import select

from app.models import DiscoveryDraft, Product
from app.retailers.generic import GenericAdapter
from app.retailers.parsing import ScrapeError
from app.schemas.domain import Candidate, Condition, Identity
from app.services.listings import add_candidates
from app.services.matching import deduplicate, identify, match_identity

log = logging.getLogger(__name__)


class DiscoveryService:
    def __init__(self, db, registry, settings):
        self.db, self.registry, self.settings = db, registry, settings
        self.capacity = asyncio.Semaphore(2)

    def create(self, payload: dict, product_id: int | None = None) -> str:
        draft_id = str(uuid.uuid4())
        with self.db.session() as session:
            session.add(DiscoveryDraft(id=draft_id, payload=payload, product_id=product_id))
        return draft_id

    async def run(self, draft_id: str):
        async with self.capacity:
            try:
                await self._discover(draft_id)
            except asyncio.CancelledError:
                self._fail(draft_id, "Discovery interrupted by shutdown. Please retry.")
                raise
            except Exception:
                log.exception("discovery_failed")
                self._fail(draft_id, "Discovery failed. Check the application log and retry.")

    def _fail(self, draft_id: str, message: str):
        with self.db.session() as session:
            draft = session.get(DiscoveryDraft, draft_id)
            draft.status, draft.results = "failed", {"errors": [message]}

    async def _discover(self, draft_id: str):
        with self.db.session() as session:
            draft = session.get(DiscoveryDraft, draft_id)
            payload = draft.payload
            draft.status = "running"
        manual, automatic, errors = [], [], []
        # The user asked for this now: try stores in cooldown once instead of skipping them,
        # above all the store of a URL they pasted.
        retailers = set(payload["retailers"])
        for url in payload["urls"]:
            try:
                retailers.add(self.registry.for_url(url).name)
            except (ScrapeError, ValueError):
                pass
        self.registry.fetcher.retry_now(retailers)
        # Manual sources seed identity but never replace the independent retailer searches.
        seen_urls = set()
        for url in payload["urls"]:
            try:
                adapter = self.registry.for_url(url)
                url = adapter.normalize_url(url)
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                manual.extend(await adapter.fetch_listing(url, alternatives=True))
            except (ScrapeError, ValueError) as exc:
                errors.append(f"Manual URL ({urlsplit(url).hostname}): {exc}")
            except Exception:
                log.exception("manual_adapter_failed")
                errors.append("A manual URL adapter failed; other sources continued.")
        # Outlet/refurbished pages may carry a retailer barcode; only new stock defines identity.
        reference = [m for m in manual if m.condition in (Condition.NEW, Condition.UNKNOWN)]
        identity = (
            identify(payload["name"], category=payload["category"])
            if payload["name"]
            else (reference or manual)[0].identity.model_copy(deep=True)
            if manual
            else None
        )
        if identity is not None and not payload["name"] and not reference:
            identity.identifiers = {}
        if identity is None:
            self._fail(
                draft_id,
                "Could not identify a product from the supplied URLs. Add a name/model and retry. "
                + " ".join(errors),
            )
            return
        if payload["category"] != "general":
            identity.category = payload["category"]
        if payload["name"] and manual:
            enriched = next(
                (
                    item.identity
                    for item in reference
                    if match_identity(identity, item.identity).level in ("EXACT", "HIGH")
                ),
                None,
            )
            if enriched:
                identity = identity.model_copy(
                    update={
                        "brand": identity.brand or enriched.brand,
                        "size": identity.size or enriched.size,
                        "identifiers": enriched.identifiers,
                    }
                )

        async def search(retailer: str):
            adapter = self.registry.adapters[retailer]
            try:
                urls = await adapter.search_product(identity)
                if not urls:
                    errors.append(
                        f"{adapter.label}: no product links returned (possibly JavaScript-only or no results)."
                    )
                for url in urls:
                    try:
                        automatic.extend(await adapter.fetch_listing(url))
                    except ScrapeError as exc:
                        errors.append(f"{adapter.label}: {exc}")
            except (ScrapeError, ValueError) as exc:
                errors.append(f"{adapter.label}: {exc}")
            except Exception:
                log.exception("retailer_discovery_failed", extra={"retailer": retailer})
                errors.append(f"{adapter.label}: adapter failed; other retailers continued.")

        await asyncio.gather(
            *(search(name) for name in payload["retailers"] if name in self.registry.adapters)
        )
        candidates = deduplicate(
            [
                Candidate(
                    listing=snapshot,
                    sources=[source],
                    match=match_identity(identity, snapshot.identity, snapshot.condition),
                )
                for source, snapshots in (("manual", manual), ("discovered", automatic))
                for snapshot in snapshots
            ]
        )
        for candidate in candidates:
            log.info(
                "matching_decision",
                extra={"retailer": candidate.listing.retailer, "confidence": candidate.match.level},
            )
        with self.db.session() as session:
            draft = session.get(DiscoveryDraft, draft_id)
            draft.results = {
                "identity": identity.model_dump(mode="json"),
                "candidates": [c.model_dump(mode="json") for c in candidates],
                "errors": errors,
            }
            draft.status = "ready"
        log.info("discovery_complete", extra={"count": len(candidates)})

    async def teach(
        self, draft_id: str, index: int, price: Decimal, availability: str | None
    ) -> None:
        with self.db.session() as session:
            draft = session.get(DiscoveryDraft, draft_id)
            if draft is None:
                raise ValueError("Discovery not found")
            if draft.status != "ready":
                raise ValueError("Discovery is not ready")
            if not 0 <= index < len(draft.results["candidates"]):
                raise ValueError("Invalid listing selection")
            url = draft.results["candidates"][index]["listing"]["url"]
        adapter = self.registry.for_url(url)
        if not isinstance(adapter, GenericAdapter):
            raise ValueError("Only generic stores can be taught")
        html = await adapter.fetcher.get(url, adapter.name, adapter.hosts)
        snapshot = adapter.learn(html, url, price, availability)
        with self.db.session() as session:
            draft = session.get(DiscoveryDraft, draft_id)
            identity = Identity.model_validate(draft.results["identity"])
            candidates = list(draft.results["candidates"])
            candidates[index] = {
                **candidates[index],
                "listing": snapshot.model_dump(mode="json"),
                "match": match_identity(identity, snapshot.identity, snapshot.condition).model_dump(
                    mode="json"
                ),
            }
            # A new dict: the JSON column does not track changes made inside the old one.
            draft.results = {**draft.results, "candidates": candidates}
        log.info("store_rule_taught", extra={"retailer": adapter.name})

    def confirm(self, draft_id: str, selected: list[int]) -> int:
        preferences = self.settings.get()
        with self.db.session() as session:
            draft = session.get(DiscoveryDraft, draft_id)
            if draft is None:
                raise ValueError("Discovery not found")
            if draft.status == "confirmed":
                return draft.product_id
            if draft.status != "ready":
                raise ValueError("Discovery is not ready")
            identity = Identity.model_validate(draft.results["identity"])
            candidates = [Candidate.model_validate(c) for c in draft.results["candidates"]]
            if any(i < 0 or i >= len(candidates) for i in selected):
                raise ValueError("Invalid listing selection")
            chosen = [candidates[i] for i in set(selected)]
            # A link the user pasted is saved as pasted; only search results must match.
            if any(c.match.level == "CONFLICT" and "manual" not in c.sources for c in chosen):
                raise ValueError("Conflicting models cannot be merged into this product")
            product = session.get(Product, draft.product_id) if draft.product_id else None
            if product is None and identity.model:
                matches = session.scalars(
                    select(Product).where(
                        Product.model == identity.model, Product.archived.is_(False)
                    )
                ).all()
                product = next(
                    (p for p in matches if p.brand == identity.brand and p.size == identity.size),
                    None,
                )
            if product and product.archived:
                raise ValueError("Restore the archived product before adding listings")
            if product is None:
                product = Product(
                    canonical_name=identity.name,
                    brand=identity.brand,
                    model=identity.model,
                    size=identity.size,
                    category=identity.category,
                    specifications=identity.identifiers,
                    target_price=Decimal(draft.payload["target_price"])
                    if draft.payload["target_price"]
                    else None,
                    insane_deal_price=Decimal(draft.payload["insane_deal_price"])
                    if draft.payload["insane_deal_price"]
                    else None,
                    retailers=draft.payload["retailers"],
                )
                session.add(product)
                session.flush()
            add_candidates(session, product, chosen, preferences)
            draft.status, draft.product_id = "confirmed", product.id
            return product.id
