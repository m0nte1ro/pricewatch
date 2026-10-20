from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select, update

from app.models import Alert, OutletFeed, OutletItem, OutletPrice, OutletRule
from app.retailers.http import user_waiting
from app.retailers.parsing import ScrapeError
from app.services.matching import normalize
from app.web.common import full_https, protected, render

router = APIRouter()


def interval(form) -> int:
    value = str(form.get("check_interval_minutes", "30")).strip()
    if not value.isdigit() or not 5 <= int(value) <= 10080:
        raise HTTPException(422, "Check every 5 to 10080 minutes")
    return int(value)


def get_feed(runtime, feed_id: int) -> OutletFeed:
    with runtime.db.session() as session:
        feed = session.get(OutletFeed, feed_id)
    if feed is None:
        raise HTTPException(404, "Outlet page not found")
    return feed


async def read_now(runtime, feed: OutletFeed):
    # Someone is waiting: a store in cooldown gets one more try and a 429 fails at once.
    runtime.registry.fetcher.retry_now({feed.retailer})
    with user_waiting():
        await runtime.outlet.run(feed.id, force=True)


@router.get("/outlet")
async def outlet_page(request: Request):
    return render(request, "outlet.html", **request.app.state.runtime.outlet.overview())


@router.post("/outlet/feeds", dependencies=[Depends(protected)])
async def add_feed(request: Request):
    runtime = request.app.state.runtime
    form = await request.form()
    url = str(form.get("url", "")).strip()
    if len(url) > 2048 or not full_https(url):
        raise HTTPException(422, "Outlet links must be full https:// URLs")
    minutes = interval(form)
    try:
        adapter = runtime.registry.for_feed(url)
        url = adapter.normalize_url(url)
    except (ScrapeError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from None
    with runtime.db.session() as session:
        if session.scalar(select(OutletFeed.id).where(OutletFeed.url == url)):
            raise HTTPException(422, "This page is already on the Outlet tab")
        feed = OutletFeed(retailer=adapter.name, url=url, check_interval_minutes=minutes)
        session.add(feed)
    await read_now(runtime, feed)
    return RedirectResponse("/outlet", 303)


@router.post("/outlet/feeds/{feed_id}/check", dependencies=[Depends(protected)])
async def check_feed(request: Request, feed_id: int):
    runtime = request.app.state.runtime
    await read_now(runtime, get_feed(runtime, feed_id))
    return RedirectResponse("/outlet", 303)


@router.post("/outlet/feeds/{feed_id}/edit", dependencies=[Depends(protected)])
async def edit_feed(request: Request, feed_id: int):
    runtime = request.app.state.runtime
    get_feed(runtime, feed_id)
    minutes = interval(await request.form())
    with runtime.db.session() as session:
        session.get(OutletFeed, feed_id).check_interval_minutes = minutes
    return RedirectResponse("/outlet", 303)


@router.post("/outlet/feeds/{feed_id}/delete", dependencies=[Depends(protected)])
async def delete_feed(request: Request, feed_id: int):
    runtime = request.app.state.runtime
    get_feed(runtime, feed_id)
    with runtime.db.session() as session:
        items = select(OutletItem.id).where(OutletItem.feed_id == feed_id)
        # Past alerts stay in Activity without their item.
        session.execute(
            update(Alert).where(Alert.outlet_item_id.in_(items)).values(outlet_item_id=None)
        )
        session.execute(delete(OutletPrice).where(OutletPrice.item_id.in_(items)))
        session.execute(delete(OutletItem).where(OutletItem.feed_id == feed_id))
        session.execute(delete(OutletFeed).where(OutletFeed.id == feed_id))
    return RedirectResponse("/outlet", 303)


@router.post("/outlet/items/{item_id}/bell", dependencies=[Depends(protected)])
async def toggle_bell(request: Request, item_id: int):
    with request.app.state.runtime.db.session() as session:
        item = session.get(OutletItem, item_id)
        if item is None:
            raise HTTPException(404, "Outlet item not found")
        item.belled = not item.belled
    return RedirectResponse(f"/outlet#item-{item_id}", 303)


@router.post("/outlet/rules", dependencies=[Depends(protected)])
async def add_rule(request: Request):
    form = await request.form()
    text = " ".join(str(form.get("text", "")).split())
    if not normalize(text) or len(text) > 100:
        raise HTTPException(422, "Enter 1–100 characters with at least one letter or digit")
    with request.app.state.runtime.db.session() as session:
        session.add(OutletRule(text=text))
    return RedirectResponse("/outlet", 303)


@router.post("/outlet/rules/{rule_id}/delete", dependencies=[Depends(protected)])
async def delete_rule(request: Request, rule_id: int):
    with request.app.state.runtime.db.session() as session:
        session.execute(delete(OutletRule).where(OutletRule.id == rule_id))
    return RedirectResponse("/outlet", 303)
