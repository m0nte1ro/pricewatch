import secrets
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates

from app.retailers.parsing import money
from app.schemas.domain import Condition

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))
TEMPLATES.env.filters["money"] = lambda value: f"€{value:,.2f}" if value is not None else "—"
TEMPLATES.env.filters["date"] = lambda value: (
    value.strftime("%d %b %H:%M UTC") if value else "Never"
)


def render(request: Request, template: str, **context):
    return TEMPLATES.TemplateResponse(
        request=request,
        name=template,
        context={
            "csrf": request.state.csrf,
            "retailers": request.app.state.runtime.registry,
            "conditions": [str(c) for c in Condition],
            **context,
        },
    )


async def protected(request: Request):
    form = await request.form(max_fields=200)
    token = str(form.get("csrf", ""))
    cookie = request.cookies.get("pricewatch_csrf", "")
    if not cookie or not secrets.compare_digest(token, cookie):
        raise HTTPException(403, "Form expired. Reload the page and try again.")
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "Cross-origin form rejected")


def amount(form, name: str) -> str | None:
    value = str(form.get(name, "")).strip()
    if not value:
        return None
    try:
        result = Decimal(value)
        if not result.is_finite() or result <= 0 or result > 999999999:
            raise ValueError
        return str(result.quantize(Decimal(".01")))
    except (InvalidOperation, ValueError):
        raise HTTPException(422, "Prices must be positive finite amounts") from None


def confirmed_price(form) -> tuple[Decimal, str | None]:
    price = money(form.get("price"))
    if price is None:
        raise HTTPException(422, "Enter the price as shown on the page, e.g. 1299,99")
    availability = str(form.get("availability", ""))
    if availability not in ("in_stock", "out_of_stock", "unknown"):
        raise HTTPException(422, "Invalid availability")
    return price, None if availability == "unknown" else availability


def validate_thresholds(target, insane):
    if target and insane and Decimal(insane) > Decimal(target):
        raise HTTPException(422, "The insane-deal threshold must not exceed the target price")


def selection(form, name: str, choices) -> list[str]:
    values = list(dict.fromkeys(str(v) for v in form.getlist(name)))
    if any(v not in choices for v in values):
        raise HTTPException(422, f"Invalid {name}")
    return values


def get_product(runtime, product_id: int):
    detail = runtime.queries.detail(product_id)
    if detail is None:
        raise HTTPException(404, "Product not found")
    return detail
