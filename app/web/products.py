from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.models import Listing, Product
from app.schemas.domain import now
from app.web.common import amount, get_product, protected, render, selection, validate_thresholds

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
    return render(request, "add.html", preferences=runtime.settings.get(), product=product)


@router.get("/products/{product_id}")
async def product_detail(request: Request, product_id: int):
    runtime = request.app.state.runtime
    return render(
        request, "product.html", **get_product(runtime, product_id), monitor=runtime.monitor
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
    from app.schemas.domain import Condition

    conditions = selection(form, "conditions", list(Condition))
    name = str(form.get("canonical_name", "")).strip()
    if not name or len(name) > 250:
        raise HTTPException(422, "A product name of 1–250 characters is required")
    with runtime.db.session() as session:
        product = session.get(Product, product_id)
        product.canonical_name = name
        product.target_price, product.insane_deal_price = target, insane
        product.allowed_conditions = conditions
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
