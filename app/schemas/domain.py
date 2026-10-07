from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


def now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Condition(StrEnum):
    NEW = "new"
    OUTLET = "outlet_open_box"
    A = "outlet_grade_a"
    B = "outlet_grade_b"
    C = "outlet_grade_c"
    REFURBISHED = "refurbished"
    USED = "used"
    UNKNOWN = "unknown"


class Identity(BaseModel):
    name: str
    brand: str | None = None
    model: str | None = None
    size: str | None = None
    category: str = "general"
    identifiers: dict[str, str] = Field(default_factory=dict)


class Snapshot(BaseModel):
    retailer: str
    url: str
    retailer_product_id: str | None = None
    identity: Identity
    title: str
    price: Decimal | None = None
    original_price: Decimal | None = None
    currency: str = "EUR"
    availability: str = "unknown"
    condition: Condition = Condition.UNKNOWN
    seller: str | None = None
    observed_at: datetime = Field(default_factory=now)

    @field_validator("price", "original_price")
    @classmethod
    def valid_money(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and (not value.is_finite() or value <= 0):
            raise ValueError("Price must be a positive finite amount")
        return value.quantize(Decimal("0.01")) if value is not None else None


class PriceCandidate(BaseModel):
    selector: str
    price: Decimal
    text: str


class Match(BaseModel):
    level: str
    score: float
    reason: str


class Candidate(BaseModel):
    listing: Snapshot
    sources: list[str] = Field(default_factory=list)
    match: Match


class Preferences(BaseModel):
    polling_minutes: int = Field(60, ge=5, le=10080)
    retailer_intervals: dict[str, int] = Field(default_factory=dict)
    enabled_retailers: list[str] = Field(
        default_factory=lambda: ["worten", "fnac", "darty", "radiopopular", "amazon_es"]
    )
    allowed_conditions: list[Condition] = Field(default_factory=lambda: [Condition.NEW])
    user_agent: str = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    request_timeout: int = Field(20, ge=5, le=90)
    proxy: str = ""
    playwright_enabled: bool = False
    ntfy_url: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_token: str = ""

    @field_validator("retailer_intervals")
    @classmethod
    def valid_intervals(cls, value: dict[str, int]) -> dict[str, int]:
        if any(x < 5 or x > 10080 for x in value.values()):
            raise ValueError("Retailer polling intervals must be between 5 and 10080 minutes")
        return value
