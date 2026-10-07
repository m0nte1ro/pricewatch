from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.models import DiscoveryDraft
from app.retailers.parsing import ScrapeError
from app.web.common import (
    amount,
    confirmed_price,
    get_product,
    protected,
    render,
    selection,
    validate_thresholds,
)

router = APIRouter()


def full_https(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:  # e.g. "https://www.[::1]/p"
        return False
    return parts.scheme == "https" and bool(parts.hostname)


@router.post("/discoveries", dependencies=[Depends(protected)])
async def start_discovery(request: Request):
    runtime = request.app.state.runtime
    form = await request.form()
    name = str(form.get("name", "")).strip()
    urls = [str(url).strip() for url in form.getlist("urls") if str(url).strip()]
    if len(name) > 250 or len(urls) > 20 or any(len(url) > 2048 for url in urls):
        raise HTTPException(422, "Use a name up to 250 characters and at most 20 product URLs")
    if not name and not urls:
        raise HTTPException(422, "Enter a product name/model or at least one retailer URL")
    # Only the shape is checked here. A link that cannot be read (a non-public host, a page
    # that fails to load) becomes a discovery note, so one bad link never discards the others.
    if not all(full_https(url) for url in urls):
        raise HTTPException(422, "Product links must be full https:// URLs")
    target, insane = amount(form, "target_price"), amount(form, "insane_deal_price")
    validate_thresholds(target, insane)
    retailers = selection(form, "retailers", runtime.registry.adapters)
    if not urls and not retailers:
        raise HTTPException(422, "Add at least one link, or pick stores to search")
    product_id = int(form["product_id"]) if str(form.get("product_id", "")).isdigit() else None
    category = str(form.get("category", "general")).strip()[:100] or "general"
    if product_id:
        product = get_product(runtime, product_id)["product"]
        name, category = (
            " ".join(filter(None, [product.brand, product.model])) or product.canonical_name,
            product.category,
        )
    payload = {
        "name": name,
        "urls": urls,
        "category": category,
        "target_price": target,
        "insane_deal_price": insane,
        "retailers": retailers,
    }
    draft_id = runtime.discovery.create(payload, product_id)
    runtime.spawn(runtime.discovery.run(draft_id))
    return RedirectResponse(f"/discoveries/{draft_id}", 303)


@router.get("/discoveries/{draft_id}")
async def discovery_preview(request: Request, draft_id: str):
    runtime = request.app.state.runtime
    with runtime.db.session() as session:
        draft = session.get(DiscoveryDraft, draft_id)
        if draft is None:
            raise HTTPException(404, "Discovery not found")
    template = (
        "partials/discovery_result.html" if request.headers.get("HX-Request") else "discovery.html"
    )
    return render(request, template, draft=draft)


@router.post("/discoveries/{draft_id}/confirm", dependencies=[Depends(protected)])
async def confirm_discovery(request: Request, draft_id: str):
    runtime = request.app.state.runtime
    form = await request.form()
    try:
        selected = [int(x) for x in form.getlist("selected")]
        product_id = runtime.discovery.confirm(draft_id, selected)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    runtime.spawn(runtime.notifications.deliver())
    return RedirectResponse(f"/products/{product_id}", 303)


@router.post("/discoveries/{draft_id}/teach/{index}", dependencies=[Depends(protected)])
async def teach_discovery(request: Request, draft_id: str, index: int):
    runtime = request.app.state.runtime
    price, availability = confirmed_price(await request.form())
    try:
        await runtime.discovery.teach(draft_id, index, price, availability)
    except (ValueError, ScrapeError) as exc:
        raise HTTPException(422, str(exc)) from None
    return RedirectResponse(f"/discoveries/{draft_id}", 303)
