from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select, update

from app.models import Alert, MarketListing, MarketSearch
from app.retailers.http import user_waiting
from app.retailers.parsing import ScrapeError, money
from app.services.market import Filters
from app.web.common import full_https, protected, render

router = APIRouter()


def interval(form) -> int:
    value = str(form.get("check_interval_minutes", "15")).strip()
    if not value.isdigit() or not 5 <= int(value) <= 10080:
        raise HTTPException(422, "Check every 5 to 10080 minutes")
    return int(value)


def get_search(runtime, search_id: int) -> MarketSearch:
    with runtime.db.session() as session:
        search = session.get(MarketSearch, search_id)
    if search is None:
        raise HTTPException(404, "Search not found")
    return search


async def read_now(runtime, search: MarketSearch):
    # Someone is waiting: a marketplace in cooldown gets one more try and a 429 fails at once.
    runtime.fetcher.retry_now({search.provider})
    with user_waiting():
        await runtime.market.run(search.id, force=True)


def filters(params) -> Filters:
    """The table's filters from the query string; anything malformed is ignored."""
    search = params.get("search", "")
    return Filters(
        q=" ".join(params.get("q", "").split())[:100],
        search=int(search) if search.isdigit() else None,
        max_price=money(params.get("max_price")),
        fresh=params.get("new") == "on",
        hide_reserved=params.get("reserved") == "hide",
    )


@router.get("/market")
async def market_page(request: Request, sort: str = ""):
    params = request.query_params

    def link(**changes) -> str:
        """This page's URL with some parameters changed; empty values drop out."""
        query = {key: value for key, value in params.items() if key not in changes}
        query.update({key: value for key, value in changes.items() if value})
        return "/market" + (f"?{urlencode(query)}" if query else "")

    runtime = request.app.state.runtime
    overview = runtime.market.overview(sort, filters(params))
    return render(request, "market.html", **overview, link=link, providers=runtime.marketplaces)


@router.post("/market/searches", dependencies=[Depends(protected)])
async def add_search(request: Request):
    runtime = request.app.state.runtime
    form = await request.form()
    url = str(form.get("url", "")).strip()
    if len(url) > 2048 or not full_https(url):
        raise HTTPException(422, "Search links must be full https:// URLs")
    minutes = interval(form)
    try:
        provider = runtime.marketplaces.for_url(url)
        url = provider.normalize_url(url)
    except (ScrapeError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from None
    with runtime.db.session() as session:
        if session.scalar(select(MarketSearch.id).where(MarketSearch.url == url)):
            raise HTTPException(422, "This search is already on the Marketplace tab")
        search = MarketSearch(
            provider=provider.name,
            url=url,
            label=provider.describe(url)[:200],
            check_interval_minutes=minutes,
        )
        session.add(search)
    await read_now(runtime, search)
    return RedirectResponse("/market", 303)


@router.post("/market/check", dependencies=[Depends(protected)])
async def check_all(request: Request):
    runtime = request.app.state.runtime
    with runtime.db.session() as session:
        searches = session.scalars(select(MarketSearch)).all()
    runtime.fetcher.retry_now({search.provider for search in searches})
    with user_waiting():
        await runtime.market.run(force=True)
    return RedirectResponse("/market", 303)


@router.post("/market/searches/{search_id}/check", dependencies=[Depends(protected)])
async def check_search(request: Request, search_id: int):
    runtime = request.app.state.runtime
    await read_now(runtime, get_search(runtime, search_id))
    return RedirectResponse("/market", 303)


@router.post("/market/searches/{search_id}/edit", dependencies=[Depends(protected)])
async def edit_search(request: Request, search_id: int):
    runtime = request.app.state.runtime
    get_search(runtime, search_id)
    minutes = interval(await request.form())
    with runtime.db.session() as session:
        session.get(MarketSearch, search_id).check_interval_minutes = minutes
    return RedirectResponse("/market", 303)


@router.post("/market/searches/{search_id}/delete", dependencies=[Depends(protected)])
async def delete_search(request: Request, search_id: int):
    runtime = request.app.state.runtime
    get_search(runtime, search_id)
    with runtime.db.session() as session:
        listings = select(MarketListing.id).where(MarketListing.search_id == search_id)
        # Past alerts stay in Activity without their listing.
        session.execute(
            update(Alert)
            .where(Alert.market_listing_id.in_(listings))
            .values(market_listing_id=None)
        )
        session.execute(delete(MarketListing).where(MarketListing.search_id == search_id))
        session.execute(delete(MarketSearch).where(MarketSearch.id == search_id))
    return RedirectResponse("/market", 303)
