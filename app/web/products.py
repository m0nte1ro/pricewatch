from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import joinedload

from app.models import Listing, Product
from app.retailers.parsing import ScrapeError
from app.schemas.domain import now
from app.services.listings import add_link, confirm_listing_price, verify_offer
from app.web.common import (
    amount,
    confirmed_price,
    full_https,
    get_product,
    protected,
    render,
    validate_thresholds,
)

router = APIRouter()


@router.get("/")
async def dashboard(request: Request, archived: bool = False):
    runtime = request.app.state.runtime
    return render(
        request,
        "dashboard.html",
        items=runtime.queries.dashboard(archived),
        archived=archived,
        monitor=runtime.monitor,
        scheduler_enabled=runtime.config.scheduler_enabled,
    )


@router.get("/products/new")
async def new_product(request: Request, product_id: int | None = None):
    runtime = request.app.state.runtime
    product = get_product(runtime, product_id)["product"] if product_id else None
    return render(request, "add.html", product=product)


@router.get("/products/{product_id}")
async def product_detail(request: Request, product_id: int, added: int | None = None):
    runtime = request.app.state.runtime
    return render(
        request,
        "product.html",
        **get_product(runtime, product_id),
        monitor=runtime.monitor,
        added=added,
    )


@router.get("/products/{product_id}/history")
async def product_history(request: Request, product_id: int, days: int = 30):
    runtime = request.app.state.runtime
    get_product(runtime, product_id)
    if days not in (0, 7, 30, 90, 365):
        raise HTTPException(422, "Invalid history range")
    return runtime.queries.history(product_id, days)


@router.post("/products/{product_id}/edit", dependencies=[Depends(protected)])
async def edit_product(request: Request, product_id: int):
    runtime = request.app.state.runtime
    get_product(runtime, product_id)
    form = await request.form()
    target, insane = amount(form, "target_price"), amount(form, "insane_deal_price")
    validate_thresholds(target, insane)
    name = str(form.get("canonical_name", "")).strip()
    if not name or len(name) > 250:
        raise HTTPException(422, "A product name of 1–250 characters is required")
    with runtime.db.session() as session:
        product = session.get(Product, product_id)
        product.canonical_name = name
        product.target_price, product.insane_deal_price = target, insane
        product.enabled = form.get("enabled") == "on"
    return RedirectResponse(f"/products/{product_id}", 303)


@router.post("/products/{product_id}/archive", dependencies=[Depends(protected)])
async def archive_product(request: Request, product_id: int):
    runtime = request.app.state.runtime
    get_product(runtime, product_id)
    with runtime.db.session() as session:
        product = session.get(Product, product_id)
        product.archived = not product.archived
    return RedirectResponse("/", 303)


@router.post("/products/{product_id}/links", dependencies=[Depends(protected)])
async def add_product_link(request: Request, product_id: int):
    runtime = request.app.state.runtime
    get_product(runtime, product_id)
    form = await request.form()
    url = str(form.get("url", "")).strip()
    if len(url) > 2048 or not full_https(url):
        raise HTTPException(422, "Product links must be full https:// URLs")
    try:
        added = await add_link(
            runtime.db, runtime.registry, product_id, url, runtime.settings.get()
        )
    except (ScrapeError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from None
    runtime.spawn(runtime.notifications.deliver())
    return RedirectResponse(f"/products/{product_id}?added={added}", 303)


@router.post("/listings/{listing_id}/toggle", dependencies=[Depends(protected)])
async def toggle_listing(request: Request, listing_id: int):
    runtime = request.app.state.runtime
    with runtime.db.session() as session:
        listing = session.get(Listing, listing_id)
        if listing is None:
            raise HTTPException(404, "Listing not found")
        listing.enabled = not listing.enabled
        product_id = listing.product_id
    return RedirectResponse(f"/products/{product_id}", 303)


@router.post("/listings/{listing_id}/interval", dependencies=[Depends(protected)])
async def listing_interval(request: Request, listing_id: int):
    runtime = request.app.state.runtime
    value = str((await request.form()).get("minutes", "")).strip()
    minutes = None
    if value:
        try:
            minutes = int(value)
            if not 5 <= minutes <= 10080:
                raise ValueError
        except ValueError:
            raise HTTPException(422, "Interval must be between 5 and 10080 minutes") from None
    with runtime.db.session() as session:
        listing = session.get(Listing, listing_id)
        if listing is None:
            raise HTTPException(404, "Listing not found")
        listing.check_interval_minutes = minutes
        if minutes is not None:
            listing.next_check_at = min(listing.next_check_at, now() + timedelta(minutes=minutes))
        product_id = listing.product_id
    return RedirectResponse(f"/products/{product_id}", 303)


def generic_listing(runtime, listing_id: int) -> Listing:
    with runtime.db.session() as session:
        listing = session.get(Listing, listing_id, options=[joinedload(Listing.product)])
    if listing is None:
        raise HTTPException(404, "Listing not found")
    if listing.retailer in runtime.registry.adapters:
        raise HTTPException(404, "Only generic store listings can be taught")
    if not listing.enabled or not listing.product.enabled or listing.product.archived:
        raise HTTPException(
            422, "This listing is not monitored: enable it, and resume or restore its product"
        )
    return listing


@router.get("/listings/{listing_id}/confirm-price")
async def listing_confirm_form(request: Request, listing_id: int):
    runtime = request.app.state.runtime
    listing = generic_listing(runtime, listing_id)
    context = {"listing": listing, "product": listing.product, "title": listing.title}
    try:
        adapter = runtime.registry[listing.retailer]
        snapshots = await adapter.fetch_listing(listing.url, alternatives=True)
    except ScrapeError as exc:
        return render(request, "listing_confirm.html", **context, error=str(exc))
    snapshot, error = verify_offer(listing, snapshots)
    # Show what the page names now: after a model change the saved title would hide it.
    context["title"] = snapshots[0].title
    if error:
        return render(request, "listing_confirm.html", **context, error=error)
    return render(
        request, "listing_confirm.html", **context, snapshot=snapshot.model_dump(mode="json")
    )


@router.post("/listings/{listing_id}/confirm-price", dependencies=[Depends(protected)])
async def confirm_listing(request: Request, listing_id: int):
    runtime = request.app.state.runtime
    listing = generic_listing(runtime, listing_id)
    price, availability = confirmed_price(await request.form())
    preferences = runtime.settings.get()
    try:
        adapter = runtime.registry[listing.retailer]
        await confirm_listing_price(runtime.db, adapter, listing, price, availability, preferences)
    except (ScrapeError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from None
    runtime.spawn(runtime.notifications.deliver())
    return RedirectResponse(f"/products/{listing.product_id}", 303)


@router.post("/checks", dependencies=[Depends(protected)])
async def run_checks(request: Request):
    runtime = request.app.state.runtime
    form = await request.form()
    product_id = int(form["product_id"]) if str(form.get("product_id", "")).isdigit() else None
    if product_id:
        get_product(runtime, product_id)
    runtime.spawn(runtime.monitor.run(product_id, force=True))
    if request.headers.get("HX-Request"):
        return render(request, "partials/check_status.html", monitor=runtime.monitor, queued=True)
    return RedirectResponse(f"/products/{product_id}" if product_id else "/", 303)


@router.get("/checks/status")
async def check_status(request: Request):
    monitor = request.app.state.runtime.monitor
    response = render(request, "partials/check_status.html", monitor=monitor, queued=False)
    if request.headers.get("HX-Request") and not monitor.lock.locked():
        response.headers["HX-Refresh"] = "true"
    return response
