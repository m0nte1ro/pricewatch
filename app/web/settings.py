from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select

from app.models import Alert, Listing, RetailerState
from app.schemas.domain import Preferences
from app.web.common import protected, render, selection

router = APIRouter()


@router.get("/settings")
async def settings_page(request: Request, saved: bool = False):
    runtime = request.app.state.runtime
    with runtime.db.session() as session:
        states = {s.name: s for s in session.scalars(select(RetailerState))}
    return render(
        request,
        "settings.html",
        preferences=runtime.settings.get(),
        states=states,
        rules=runtime.rules.all(),
        saved=saved,
    )


@router.post("/settings", dependencies=[Depends(protected)])
async def save_settings(request: Request):
    runtime = request.app.state.runtime
    form = await request.form()
    previous = runtime.settings.get()
    try:
        intervals = {
            name: int(form[f"interval_{name}"])
            for name in runtime.registry.adapters
            if form.get(f"interval_{name}")
        }
        prefs = Preferences(
            polling_minutes=form.get("polling_minutes", 60),
            retailer_intervals=intervals,
            enabled_retailers=selection(form, "retailers", runtime.registry.adapters),
            user_agent=str(form.get("user_agent", ""))[:500],
            request_timeout=form.get("request_timeout", 20),
            proxy=""
            if form.get("clear_proxy")
            else str(form.get("proxy", "")).strip() or previous.proxy,
            playwright_enabled=form.get("playwright_enabled") == "on",
            ntfy_url=str(form.get("ntfy_url", "https://ntfy.sh")).strip(),
            ntfy_topic=str(form.get("ntfy_topic", "")).strip()[:200],
            ntfy_token=""
            if form.get("clear_token")
            else str(form.get("ntfy_token", "")).strip() or previous.ntfy_token,
        )
        for url in (prefs.proxy, prefs.ntfy_url):
            if url and (
                urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).hostname
            ):
                raise ValueError("Invalid server/proxy URL")
        if "\r" in prefs.user_agent or "\n" in prefs.user_agent or not prefs.user_agent:
            raise ValueError("Invalid user agent")
    except (ValidationError, ValueError):
        raise HTTPException(
            422,
            "Invalid settings. Check intervals (5–10080 min), timeout (5–90 s) and server URLs.",
        ) from None
    runtime.settings.save(prefs)
    return RedirectResponse("/settings?saved=true", 303)


@router.post("/settings/rules/{host}/forget", dependencies=[Depends(protected)])
async def forget_rule(request: Request, host: str):
    request.app.state.runtime.rules.delete(host)
    return RedirectResponse("/settings", 303)


@router.get("/alerts")
async def alerts_page(request: Request, page: int = 1):
    page = max(1, page)
    with request.app.state.runtime.db.session() as session:
        alerts = session.scalars(
            select(Alert).order_by(Alert.id.desc()).offset((page - 1) * 50).limit(51)
        ).all()
        ids = {a.listing_id for a in alerts if a.listing_id}
        listing_urls = dict(
            session.execute(select(Listing.id, Listing.url).where(Listing.id.in_(ids))).all()
        )
    return render(
        request,
        "alerts.html",
        alerts=alerts[:50],
        page=page,
        has_more=len(alerts) > 50,
        listing_urls=listing_urls,
    )


@router.post("/alerts/{alert_id}/read", dependencies=[Depends(protected)])
async def read_alert(request: Request, alert_id: int):
    with request.app.state.runtime.db.session() as session:
        alert = session.get(Alert, alert_id)
        if alert is None:
            raise HTTPException(404, "Alert not found")
        alert.acknowledged = True
    return RedirectResponse("/alerts", 303)


@router.post("/alerts/{alert_id}/retry", dependencies=[Depends(protected)])
async def retry_notification(request: Request, alert_id: int):
    from app.schemas.domain import now

    runtime = request.app.state.runtime
    with runtime.db.session() as session:
        alert = session.get(Alert, alert_id)
        if alert is None:
            raise HTTPException(404, "Alert not found")
        alert.notification_state, alert.notification_attempts, alert.notification_next_at = (
            "pending",
            0,
            now(),
        )
    runtime.spawn(runtime.notifications.deliver())
    return RedirectResponse("/alerts", 303)
