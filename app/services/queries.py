from datetime import timedelta
from decimal import Decimal

from sqlalchemy import and_, case, func, select

from app.models import Alert, Listing, PriceHistory, Product
from app.schemas.domain import lower_price, now

# Unconfirmed (heuristic) prices are charted but may be a wrong element's amount, so they never
# become a historical low, high or first price.
CONFIRMED_EUR = (PriceHistory.currency == "EUR", PriceHistory.method != "heuristic")
# What the owner could have paid at that moment: the promo-code price when it was lower.
DEAL = case(
    (
        and_(PriceHistory.promo_price.is_not(None), PriceHistory.promo_price < PriceHistory.price),
        PriceHistory.promo_price,
    ),
    else_=PriceHistory.price,
)


def summary(product: Product, low: Decimal | None = None, high: Decimal | None = None) -> dict:
    active = [x for x in product.listings if x.enabled]
    eligible = [
        x
        for x in active
        if x.deal_price is not None
        and x.currency == "EUR"
        and x.availability == "in_stock"
        and not x.last_error
        and x.extraction_method != "heuristic"
    ]
    best = min(eligible, key=lambda x: x.deal_price, default=None)
    # Shown when nothing is buyable: the price is still recorded, but clearly not a deal.
    unavailable = min(
        (
            x
            for x in active
            if x.deal_price is not None
            and x.currency == "EUR"
            and x.availability != "in_stock"
            and x.extraction_method != "heuristic"
        ),
        key=lambda x: x.deal_price,
        default=None,
    )
    status = "WATCHING"
    if active and all(x.availability == "out_of_stock" and not x.last_error for x in active):
        status = "OUT OF STOCK"
    if best:
        price = best.deal_price
        if product.insane_deal_price is not None and price <= product.insane_deal_price:
            status = "INSANE DEAL"
        elif product.target_price is not None and price <= product.target_price:
            status = "TARGET HIT"
        elif product.target_price is not None and price <= product.target_price * Decimal("1.10"):
            status = "NEAR TARGET"
    return {
        "product": product,
        "best": best,
        "unavailable": None if best else unavailable,
        "low": low,
        # Only news once the price has been higher: a first reading is trivially the lowest.
        "at_low": bool(best and low is not None and high is not None and high > low)
        and best.deal_price <= low,
        "status": status,
        "active_count": len(active),
        "last_checked": max((x.last_checked_at for x in active if x.last_checked_at), default=None),
        "review": sum(bool(x.last_error) or x.extraction_method == "heuristic" for x in active),
    }


class QueryService:
    def __init__(self, db):
        self.db = db

    def dashboard(self, archived: bool = False) -> list[dict]:
        with self.db.session() as session:
            products = session.scalars(
                select(Product)
                .where(Product.archived == archived)
                .order_by(Product.created_at.desc())
            ).all()
            ranges = {
                product_id: (low, high)
                for product_id, low, high in session.execute(
                    select(Listing.product_id, func.min(DEAL), func.max(DEAL))
                    .join(PriceHistory)
                    .where(*CONFIRMED_EUR)
                    .group_by(Listing.product_id)
                )
            }
            return [summary(product, *ranges.get(product.id, ())) for product in products]

    def detail(self, product_id: int) -> dict | None:
        with self.db.session() as session:
            product = session.get(Product, product_id)
            if product is None:
                return None
            eligible = (
                Listing.product_id == product_id,
                *CONFIRMED_EUR,
                PriceHistory.price.is_not(None),
            )
            low, high = session.execute(
                select(func.min(DEAL), func.max(DEAL)).join(Listing).where(*eligible)
            ).one()
            first = session.scalar(
                select(DEAL)
                .join(Listing)
                .where(*eligible)
                .order_by(PriceHistory.timestamp)
                .limit(1)
            )
            result = summary(product, low, high)
            result["listing_urls"] = {x.id: x.url for x in product.listings}
            low_time = session.scalar(
                select(func.max(PriceHistory.timestamp)).join(Listing).where(*eligible, DEAL == low)
            )
            result.update(
                high=high,
                first=first,
                days_since_low=(now() - low_time).days if low_time else None,
                difference=((result["best"].deal_price / low - 1) * 100)
                if low and result["best"]
                else None,
                events=session.scalars(
                    select(Alert)
                    .where(Alert.product_id == product_id)
                    .order_by(Alert.id.desc())
                    .limit(30)
                ).all(),
            )
            return result

    def history(self, product_id: int, days: int) -> dict:
        with self.db.session() as session:
            listings = session.scalars(
                select(Listing).where(Listing.product_id == product_id)
            ).all()
            series = []
            for listing in listings:
                query = select(PriceHistory).where(PriceHistory.listing_id == listing.id)
                if days:
                    query = query.where(PriceHistory.timestamp >= now() - timedelta(days=days))
                history = session.scalars(query.order_by(PriceHistory.timestamp)).all()
                # Bound the chart payload; full records stay in SQLite.
                stride = max(1, (len(history) + 1499) // 1500)
                sampled = history[::stride]
                if history and (not sampled or sampled[-1] != history[-1]):
                    sampled.append(history[-1])
                series.append(
                    {
                        "label": f"{listing.retailer} · {listing.condition} · {listing.seller or 'unknown seller'} · #{listing.id}",
                        "currency": listing.currency,
                        "data": [
                            {
                                "x": h.timestamp.isoformat() + "Z",
                                "y": float(deal)
                                if (deal := lower_price(h.price, h.promo_price)) is not None
                                else None,
                            }
                            for h in sampled
                        ],
                    }
                )
            return {"series": series}
