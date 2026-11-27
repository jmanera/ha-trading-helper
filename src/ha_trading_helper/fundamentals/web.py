"""The fundamentals pages, opened from Home Assistant's sidebar through ingress.

Links and redirects are built on the ``X-Ingress-Path`` header, like the journal's.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ha_trading_helper import __version__
from ha_trading_helper.fundamentals import fmt, sentiment
from ha_trading_helper.fundamentals.market import Pair
from ha_trading_helper.fundamentals.scoring import CATEGORY_LABELS
from ha_trading_helper.fundamentals.service import (
    COINGECKO_CALLS_PER_COIN,
    COINGECKO_MONTHLY,
)

if TYPE_CHECKING:
    from ha_trading_helper.fundamentals.service import FundamentalsService

TEMPLATES = Path(__file__).parent / "templates"
SYMBOL = re.compile(r"^[A-Za-z0-9]{1,20}$")

MISSING = -1e9  # pairs without the value go last when sorting from high to low

# Each key sorts on the pair and the fundamental scores already computed.
SORTS: dict[str, Callable[[Pair, dict[str, float]], Any]] = {
    "cambio": lambda p, _: p.change_4h if p.change_4h is not None else MISSING,
    "cambio24": lambda p, _: p.change_24h if p.change_24h is not None else MISSING,
    "volumen": lambda p, _: p.volume_4h or 0.0,
    "volumen24": lambda p, _: p.volume_24h,
    "puntos": lambda p, scores: scores.get(p.base, MISSING),
    "par": lambda p, _: p.inst_id,
}
DEFAULT_SORT = "cambio"


def tone(value: float | None) -> str:
    if value is None or abs(value) < 5e-4:
        return ""
    return "pos" if value > 0 else "neg"


def score_tone(value: float | None) -> str:
    if value is None:
        return ""
    return "pos" if value >= 65 else "neg" if value < 40 else "mid"


def risk_tone(level: str | None) -> str:
    return {"bajo": "pos", "medio": "mid", "alto": "neg"}.get(level or "", "")


def outlook_tone(outlook: str) -> str:
    return {"alcista": "pos", "bajista": "neg"}.get(outlook, "mid")


def sort_pairs(
    pairs: list[Pair],
    key: str,
    descending: bool,
    query: str = "",
    scores: dict[str, float] | None = None,
) -> list[Pair]:
    q = query.strip().upper()
    rows = [p for p in pairs if q in p.inst_id] if q else list(pairs)
    sort = SORTS.get(key, SORTS[DEFAULT_SORT])
    known = scores or {}
    return sorted(rows, key=lambda p: sort(p, known), reverse=descending)


def create_app(service: FundamentalsService, *, timezone: str = "Europe/Madrid") -> FastAPI:
    app = FastAPI(title="Análisis fundamental", docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES))
    tz = ZoneInfo(timezone)

    def when(ts: float | None, pattern: str = "%d/%m/%Y %H:%M") -> str:
        return datetime.fromtimestamp(ts, tz=tz).strftime(pattern) if ts else "—"

    templates.env.filters.update(
        big=fmt.big,
        pct=fmt.pct,
        price=fmt.price,
        num=fmt.num,
        tone=tone,
        score_tone=score_tone,
        risk_tone=risk_tone,
        outlook_tone=outlook_tone,
        fng_label=sentiment.label,
        when=when,
    )

    def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        base = request.headers.get("X-Ingress-Path", "").rstrip("/")
        return templates.TemplateResponse(
            request,
            name,
            {
                "base": base,
                "version": __version__,
                "updated": service.updated,
                "error": service.error,
                "page": "pares",
                **ctx,
            },
        )

    @app.get("/", response_class=HTMLResponse)
    def pairs(
        request: Request, orden: str = DEFAULT_SORT, dir: str = "desc", q: str = ""
    ) -> HTMLResponse:
        key = orden if orden in SORTS else DEFAULT_SORT
        descending = dir != "asc"
        analyses = {p.base: a for p in service.pairs if (a := service.cached(p.base))}
        scores = {k: s for k, a in analyses.items() if (s := a.scores.overall) is not None}
        rows = sort_pairs(service.pairs, key, descending, q, scores)
        pending = len(service.pairs) - len(analyses)
        calls = pending * COINGECKO_CALLS_PER_COIN
        per_minute = 25 if service.settings.coingecko_api_key else 5
        return render(
            request,
            "pairs.html",
            rows=rows,
            total=len(service.pairs),
            analyses=analyses,
            orden=key,
            descending=descending,
            q=q,
            bulk=service.bulk,
            pending=pending,
            estimate={
                "calls": calls,
                "minutes": calls / per_minute,
                "used": service.coingecko_calls,
                "monthly": COINGECKO_MONTHLY,
                "key": bool(service.settings.coingecko_api_key),
                "github": bool(service.settings.github_token),
            },
        )

    @app.get("/mercado", response_class=HTMLResponse)
    async def market(request: Request) -> HTMLResponse:
        g = await service.refresh_global()
        history = service.history
        rows = sentiment.readings(history)
        return render(
            request,
            "market.html",
            page="mercado",
            g=g,
            global_error=service.global_error,
            gauge=sentiment.gauge(g.fng, g.sentiment) if g else "",
            chart=sentiment.history_chart(rows),
            summary=sentiment.summarize(history, service.today()),
            mood=sentiment.mood(g.sentiment) if g else "",
        )

    @app.post("/mercado/actualizar")
    async def refresh_market(request: Request) -> RedirectResponse:
        await service.refresh_global(max_age=60)
        return _redirect(request, "mercado")

    @app.get("/moneda/{symbol}", response_class=HTMLResponse)
    async def coin(request: Request, symbol: str) -> HTMLResponse:
        if not SYMBOL.match(symbol) or service.pair(symbol) is None:
            return render(request, "missing.html", symbol=symbol.upper()[:20])
        analysis = await service.analysis(symbol)
        return render(
            request,
            "coin.html",
            a=analysis,
            s=analysis.scores if analysis else None,
            pair=service.pair(symbol),
            labels=CATEGORY_LABELS,
        )

    @app.post("/moneda/{symbol}/actualizar")
    async def refresh_coin(request: Request, symbol: str) -> RedirectResponse:
        if SYMBOL.match(symbol) and service.pair(symbol) is not None:
            await service.analysis(symbol, refresh=True)
            return _redirect(request, f"moneda/{symbol.upper()}")
        return _redirect(request, "")

    @app.post("/analizar-todo")
    async def analyze_all(request: Request) -> RedirectResponse:
        service.start_bulk()
        return _redirect(request, "")

    @app.post("/analizar-todo/detener")
    def stop_all(request: Request) -> RedirectResponse:
        service.cancel_bulk()
        return _redirect(request, "")

    @app.post("/actualizar")
    async def refresh_pairs(request: Request) -> RedirectResponse:
        await service.refresh()
        return _redirect(request, "")

    @app.get("/salud")
    def health() -> Response:
        return Response("ok", media_type="text/plain")

    return app


def _redirect(request: Request, path: str) -> RedirectResponse:
    base = request.headers.get("X-Ingress-Path", "").rstrip("/")
    return RedirectResponse(f"{base}/{path}", status_code=303)
