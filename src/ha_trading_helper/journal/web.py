"""The journal's web pages, opened from Home Assistant's sidebar through ingress.

Ingress serves the app under ``/api/hassio_ingress/<token>/`` and says so in the
``X-Ingress-Path`` header; every link and redirect is built on it. Home Assistant has
already authenticated the user when a request gets here.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ha_trading_helper import __version__
from ha_trading_helper.journal import charts, report
from ha_trading_helper.journal.equity import PERIODS
from ha_trading_helper.journal.ledger import KIND_LABELS
from ha_trading_helper.journal.store import Store

TEMPLATES = Path(__file__).parent / "templates"

SyncNow = Callable[[], Awaitable[Any]]


def _num(value: float, decimals: int = 2) -> str:
    text = f"{value:,.{decimals}f}"
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def money(value: float | None, decimals: int = 2) -> str:
    if value is None or math.isnan(value):
        return "—"
    return _num(value, decimals)


def signed(value: float | None, decimals: int = 2) -> str:
    if value is None:
        return "—"
    if abs(value) < 0.5 * 10**-decimals:
        return money(0.0, decimals)
    text = money(value, decimals)
    return f"+{text}" if value > 0 else text


def pct(value: float | None, decimals: int = 2) -> str:
    if value is None:
        return "—"
    text = _num(value * 100, decimals)
    return f"+{text} %" if value > 0 else f"{text} %"


def qty(value: float | None) -> str:
    if value is None:
        return "—"
    if value == 0:
        return "0"
    digits = max(0, min(10, 6 - math.floor(math.log10(abs(value)))))
    text = _num(value, digits)
    return text.rstrip("0").rstrip(",") if "," in text else text


def price(value: float | None) -> str:
    return qty(value)


def tone(value: float | None) -> str:
    if value is None or abs(value) < 5e-3:
        return ""
    return "pos" if value > 0 else "neg"


def duration(ms: float) -> str:
    hours = ms / 3_600_000
    if hours < 1:
        return f"{max(1, round(hours * 60))} min"
    if hours < 48:
        return f"{hours:.1f} h".replace(".", ",")
    return f"{hours / 24:.1f} d".replace(".", ",")


def create_app(
    store: Store,
    *,
    timezone: str = "Europe/Madrid",
    sync_now: SyncNow | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FastAPI:
    app = FastAPI(title="Bitácora OKX", docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES))
    tz = ZoneInfo(timezone)

    def when(ts_ms: int | None, fmt: str = "%d/%m/%Y %H:%M") -> str:
        if not ts_ms:
            return "—"
        return datetime.fromtimestamp(ts_ms / 1000, tz=tz).strftime(fmt)

    templates.env.filters.update(
        money=money,
        signed=signed,
        pct=pct,
        qty=qty,
        price=price,
        tone=tone,
        when=when,
        duration=duration,
    )

    def render(request: Request, name: str, page: str, **ctx: Any) -> HTMLResponse:
        base = request.headers.get("X-Ingress-Path", "").rstrip("/")
        last = store.get_json("last_sync", {})
        here = request.url.path.lstrip("/")
        if request.url.query:
            here += f"?{request.url.query}"
        return templates.TemplateResponse(
            request,
            name,
            {
                "base": base,
                "here": here,
                "page": page,
                "last_sync": last,
                "version": __version__,
                "kinds": KIND_LABELS,
                **ctx,
            },
        )

    def load() -> report.Data:
        return report.Data.load(store, clock())

    @app.get("/", response_class=HTMLResponse)
    def today(request: Request) -> HTMLResponse:
        d = load()
        ov = report.overview(d)
        days = [v.day for v in ov.last30]
        return render(
            request,
            "today.html",
            "hoy",
            ov=ov,
            data=d,
            chart=charts.line_chart(
                days,
                [("Valor", [v.equity for v in ov.last30])],
                "Valor de la cuenta, últimos 30 días",
            ),
        )

    @app.get("/posiciones", response_class=HTMLResponse)
    def positions(request: Request) -> HTMLResponse:
        d = load()
        return render(request, "positions.html", "posiciones", pos=report.positions(d))

    @app.get("/rendimiento", response_class=HTMLResponse)
    def performance(
        request: Request,
        periodo: str = "30d",
        desde: Annotated[str | None, Query()] = None,
        hasta: Annotated[str | None, Query()] = None,
    ) -> HTMLResponse:
        d = load()
        view = report.period_view(
            d, periodo if periodo in PERIODS else "30d", _date(desde), _date(hasta)
        )
        chart_equity = chart_pnl = ""
        if view.perf:
            days = [x.day for x in view.perf.days]
            invested, running = [], view.perf.start_equity
            for x in view.perf.days:
                running += x.flow
                invested.append(running)
            chart_equity = charts.line_chart(
                days,
                [("Valor", [x.equity for x in view.perf.days]), ("Dinero aportado", invested)],
                "Valor frente a dinero aportado",
            )
            chart_pnl = charts.bar_chart(
                days, [x.pnl for x in view.perf.days], "Resultado de cada día"
            )
        return render(
            request,
            "performance.html",
            "rendimiento",
            view=view,
            periods=PERIODS,
            chart_equity=chart_equity,
            chart_pnl=chart_pnl,
        )

    @app.get("/activos", response_class=HTMLResponse)
    def assets(request: Request) -> HTMLResponse:
        rows = report.assets(load())
        return render(
            request,
            "assets.html",
            "activos",
            rows=rows,
            total=sum(r.total for r in rows),
            fees=sum(r.fees for r in rows),
        )

    @app.get("/bitacora", response_class=HTMLResponse)
    def journal(
        request: Request,
        activo: str = "",
        tipo: str = "",
        restos: bool = False,
        desde: Annotated[str | None, Query()] = None,
        hasta: Annotated[str | None, Query()] = None,
    ) -> HTMLResponse:
        d = load()
        rows = report.journal(
            d, ccy=activo.upper(), kind=tipo, start=_date(desde), end=_date(hasta), dust=restos
        )
        ccys = sorted({r.ccy for r in report.journal(d, dust=True)})
        return render(
            request,
            "journal.html",
            "bitacora",
            rows=rows,
            ccys=ccys,
            f={
                "activo": activo.upper(),
                "tipo": tipo,
                "desde": desde or "",
                "hasta": hasta or "",
                "restos": restos,
            },
        )

    @app.post("/nota")
    def save_note(
        request: Request,
        ref: Annotated[str, Form()],
        text: Annotated[str, Form()] = "",
        tags: Annotated[str, Form()] = "",
        back: Annotated[str, Form()] = "bitacora",
    ) -> RedirectResponse:
        store.save_note(ref[:80], text[:5000], tags[:255], int(clock().timestamp() * 1000))
        return _back(request, back)

    @app.post("/sincronizar")
    async def sync(request: Request, back: Annotated[str, Form()] = "") -> RedirectResponse:
        if sync_now is not None:
            await sync_now()
        return _back(request, back)

    @app.get("/salud")
    def health() -> Response:
        return Response("ok", media_type="text/plain")

    return app


def _date(text: str | None) -> date | None:
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        return None


def _back(request: Request, back: str) -> RedirectResponse:
    base = request.headers.get("X-Ingress-Path", "").rstrip("/")
    # Only paths inside the app: never redirect elsewhere.
    target = back.lstrip("/") if back and "//" not in back and ":" not in back else ""
    return RedirectResponse(f"{base}/{target}", status_code=303)
