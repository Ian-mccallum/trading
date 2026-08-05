"""Dashboard tests: chart geometry, copy translation, data assembly, routes.

The chart-framing tests matter most: honest framing of a near-flat series is a
deliberate design decision (see docs/spec-dashboard.md §6), not an accident,
and a tight-fitting axis would silently turn $12 of noise into a mountain.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI

from app.api.routes import dashboard as dashboard_routes
from app.brokers.base import BrokerRejectionError
from app.config import Settings, get_settings
from app.dashboard import copy
from app.dashboard.charts import (
    MIN_RANGE_FRACTION,
    build_allocation,
    build_line_chart,
    build_volume,
    downsample,
)
from app.dashboard.service import build_dashboard
from app.db.base import get_db_session, utcnow
from app.db.models import (
    Decision,
    DecisionMode,
    DecisionStatus,
    Environment,
    Persona,
    PersonaKind,
    PersonaMember,
    PersonaStatus,
    PositionSnapshot,
    RiskEvent,
    RiskVerdict,
    Strategy,
    StrategyStatus,
)
from tests.conftest import make_account, make_position

# ---------------------------------------------------------------- charts


def series(values: list[float]) -> list[tuple[datetime, Decimal]]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [(start + timedelta(hours=i), Decimal(str(v))) for i, v in enumerate(values)]


def test_line_chart_needs_two_points():
    assert build_line_chart([]) is None
    assert build_line_chart(series([100.0])) is None


def test_line_chart_normalizes_to_unit_space():
    chart = build_line_chart(series([100.0, 110.0, 120.0]))
    assert chart is not None
    assert chart.points[0].x == 0.0
    assert chart.points[-1].x == 1.0
    assert all(0.0 <= p.y <= 1.0 for p in chart.points)
    assert chart.direction == "up"
    assert chart.change_abs == pytest.approx(20.0)
    assert chart.change_pct == pytest.approx(0.2)


def test_line_chart_direction_down():
    chart = build_line_chart(series([120.0, 100.0]))
    assert chart.direction == "down"
    assert chart.change_abs == pytest.approx(-20.0)


def test_near_flat_series_is_framed_honestly():
    """A $12 move on $100k (0.012%) must not be auto-zoomed into a mountain.

    The framed axis has to span at least MIN_RANGE_FRACTION of the value, so
    the drawn line stays visually flat.
    """
    chart = build_line_chart(series([100_000.0, 100_006.0, 100_012.0]))
    assert chart.flat is True
    assert chart.direction == "flat"
    axis_span = chart.y_max - chart.y_min
    assert axis_span >= 100_012.0 * MIN_RANGE_FRACTION * 0.99
    # Every point lands near the middle of the chart rather than spanning it.
    assert all(0.35 < p.y < 0.65 for p in chart.points)


def test_genuine_portfolio_move_is_not_flattened():
    """The flatness floor suppresses noise, not real moves: a 2% swing on a
    six-figure account must still show shape."""
    chart = build_line_chart(series([100_000.0, 101_000.0, 102_000.0]))
    assert chart.flat is False
    assert chart.direction == "up"


def test_real_movement_is_not_flattened():
    chart = build_line_chart(series([100.0, 140.0]))
    assert chart.flat is False
    assert chart.points[0].y < 0.25
    assert chart.points[-1].y > 0.75


def test_downsample_preserves_endpoints():
    values = list(range(1000))
    thinned = downsample(values, limit=50)
    assert len(thinned) == 50
    assert thinned[0] == 0
    assert thinned[-1] == 999


def test_line_chart_downsamples_long_series():
    chart = build_line_chart(series([float(i) for i in range(5000)]))
    assert len(chart.points) <= 240
    assert chart.points[-1].value == 4999.0


def test_allocation_always_includes_cash():
    """A holdings-only bar implies full deployment, which is usually false."""
    slices = build_allocation([("SPY", 25_000)], cash=75_000)
    assert [s.label for s in slices] == ["SPY", "Cash"]
    assert slices[0].fraction == pytest.approx(0.25)
    assert slices[-1].is_cash is True
    assert sum(s.fraction for s in slices) == pytest.approx(1.0)


def test_allocation_sorts_by_size_and_handles_empty():
    slices = build_allocation([("A", 10), ("B", 40)], cash=50)
    assert [s.label for s in slices] == ["B", "A", "Cash"]
    assert build_allocation([], cash=0) == []


def test_volume_scales_against_busiest_day():
    bars = build_volume([("d1", 2, 0), ("d2", 8, 2)])
    assert bars[1].executed_fraction == pytest.approx(1.0)
    assert bars[0].executed_fraction == pytest.approx(0.2)
    assert build_volume([]) == []


# ---------------------------------------------------------------- copy


def test_rejection_sentence_uses_engine_reason():
    sentence = copy.rejection_sentence(
        "order_notional", {"reason": "order notional 24922.00 exceeds max 5000.0"}
    )
    assert sentence.startswith("Order size limit:")
    assert "24922.00" in sentence
    assert sentence.endswith(".")


def test_rejection_sentence_falls_back_to_explanation():
    sentence = copy.rejection_sentence("stale_data", {})
    assert "Stale market data" in sentence
    assert "too old" in sentence


def test_unknown_identifiers_stay_readable():
    """New rules must never leak a raw enum onto the page."""
    assert copy.rule_label("brand_new_rule") == "Brand new rule"
    assert copy.criterion_label("c9_something") == "C9 something"
    assert copy.humanize("") == "Unknown"


def test_status_label_tone_mapping():
    assert copy.status_label("executed", copy.DECISION_STATUS) == ("Executed", "up")
    assert copy.status_label("rejected", copy.DECISION_STATUS) == ("Rejected", "down")
    assert copy.status_label("weird", copy.DECISION_STATUS) == ("Weird", "neutral")


# ---------------------------------------------------------------- service


class StubBroker:
    environment = Environment.PAPER

    def __init__(self, fail: bool = False):
        self.fail = fail

    async def get_account(self):
        if self.fail:
            raise BrokerRejectionError("HTTP 401: unauthorized.")
        return make_account("100000")

    async def get_positions(self):
        if self.fail:
            raise BrokerRejectionError("HTTP 401: unauthorized.")
        return [make_position("SPY", "10", "100")]

    async def aclose(self):
        return None


def paper_settings() -> Settings:
    return Settings(_env_file=None, alpaca_paper_api_key="k", alpaca_paper_api_secret="s")


async def seed_decision(session, *, status=DecisionStatus.REJECTED, symbol="SPY"):
    strategy = Strategy(
        name="minervini_trend_template", version=1, class_path="x.Y", params={},
        status=StrategyStatus.APPROVED,
    )
    session.add(strategy)
    await session.flush()
    decision = Decision(
        mode=DecisionMode.PAPER, environment=Environment.PAPER, status=status,
        strategy_id=strategy.id, symbol=symbol, action="buy", qty=Decimal(1),
        context={"regime": "trend_up", "c5_above_fast": False, "c1_above_mid_and_slow": True,
                 "close": "740.80", "rs_skipped": "true"},
        risk_detail={"results": [
            {"rule": "order_notional", "verdict": RiskVerdict.REJECTED,
             "reason": "order notional 24922.00 exceeds max 5000.0"},
            {"rule": "qty_sanity", "verdict": RiskVerdict.APPROVED, "reason": ""},
        ]},
    )
    session.add(decision)
    await session.flush()
    return decision


async def test_build_dashboard_shape(db_session):
    await seed_decision(db_session)
    data = await build_dashboard(db_session, paper_settings(), StubBroker())
    assert set(data) >= {
        "system", "portfolio", "holdings", "activity", "personas", "rejections", "counts", "hints"
    }
    assert data["system"]["environment"] == "paper"
    assert data["holdings"]["available"] is True
    assert data["holdings"]["rows"][0]["symbol"] == "SPY"


async def test_reasoning_is_translated_to_sentences(db_session):
    await seed_decision(db_session)
    data = await build_dashboard(db_session, paper_settings(), StubBroker())
    reasoning = data["activity"]["rows"][0]["reasoning"]

    assert any("Order size limit" in s for s in reasoning["rejections"])
    # Approved rules are not listed as rejections.
    assert not any("Quantity check" in s for s in reasoning["rejections"])
    labels = {c["label"]: c["passed"] for c in reasoning["criteria"]}
    assert labels["Price above the 50-day average"] is False
    assert reasoning["regime"] == "Uptrend"
    assert reasoning["rs_skipped"] is True


async def test_broker_failure_reports_unavailable_not_empty(db_session):
    """'Unavailable' and 'no positions' are different facts and must not
    render identically."""
    data = await build_dashboard(db_session, paper_settings(), StubBroker(fail=True))
    assert data["holdings"]["available"] is False
    assert "401" in data["holdings"]["error"]
    assert data["holdings"]["rows"] == []


async def test_no_broker_still_renders(db_session):
    data = await build_dashboard(db_session, Settings(_env_file=None), None)
    assert data["holdings"]["available"] is False
    assert data["portfolio"]["equity"] is None


async def test_halted_system_surfaces_block(db_session):
    from app.risk import state as controls

    await controls.engage_kill_switch(db_session, "ian", "testing", Environment.PAPER)
    data = await build_dashboard(db_session, paper_settings(), StubBroker())
    assert data["system"]["healthy"] is False
    assert data["system"]["blocks"][0]["control"] == "Kill switch"
    assert data["system"]["blocks"][0]["severity"] == "halted"


async def test_stalled_worker_is_not_reported_as_halted(db_session):
    """A stopped worker and a kill switch need different fixes, so they must
    not look the same."""
    old = utcnow() - timedelta(hours=3)
    db_session.add(
        PositionSnapshot(
            environment=Environment.PAPER, equity=Decimal(100_000),
            cash=Decimal(100_000), positions=[], created_at=old,
        )
    )
    await db_session.flush()
    data = await build_dashboard(db_session, paper_settings(), StubBroker())
    blocks = data["system"]["blocks"]
    assert any(b["severity"] == "stalled" for b in blocks)
    assert not any(b["severity"] == "halted" for b in blocks)


async def test_equity_chart_from_snapshots(db_session):
    base = utcnow() - timedelta(days=3)
    for i, equity in enumerate([100_000, 101_000, 102_000]):
        db_session.add(
            PositionSnapshot(
                environment=Environment.PAPER, equity=Decimal(equity),
                cash=Decimal(equity), positions=[], created_at=base + timedelta(days=i),
            )
        )
    await db_session.flush()
    data = await build_dashboard(db_session, paper_settings(), StubBroker(), range_key="1M")
    chart = data["portfolio"]["chart"]
    assert chart is not None
    assert chart["direction"] == "up"
    assert len(chart["points"]) == 3


async def test_hints_explain_correct_but_confusing_states(db_session):
    data = await build_dashboard(db_session, paper_settings(), StubBroker())
    joined = " ".join(data["hints"])
    assert "No personas are active" in joined
    assert "No strategies are approved" in joined


async def test_personas_report_kind_and_membership(db_session):
    strategy = Strategy(
        name="leg", version=1, class_path="x.Y", params={}, status=StrategyStatus.APPROVED
    )
    db_session.add(strategy)
    await db_session.flush()
    persona = Persona(
        name="all_weather", kind=PersonaKind.COOPERATIVE,
        status=PersonaStatus.ACTIVE, symbol_set=["VTI"],
    )
    db_session.add(persona)
    await db_session.flush()
    db_session.add(PersonaMember(persona_id=persona.id, strategy_id=strategy.id))
    await db_session.flush()

    data = await build_dashboard(db_session, paper_settings(), StubBroker())
    entry = data["personas"][0]
    assert entry["cooperative"] is True
    assert entry["active"] is True
    assert entry["members"] == 1


async def test_rejection_tally_groups_by_rule(db_session):
    for _ in range(3):
        db_session.add(
            RiskEvent(
                rule="order_notional", verdict=RiskVerdict.REJECTED, severity="warning",
                environment=Environment.PAPER, detail={},
            )
        )
    db_session.add(
        RiskEvent(
            rule="qty_sanity", verdict=RiskVerdict.APPROVED, severity="info",
            environment=Environment.PAPER, detail={},
        )
    )
    await db_session.flush()
    data = await build_dashboard(db_session, paper_settings(), StubBroker())
    assert data["rejections"][0]["rule"] == "order_notional"
    assert data["rejections"][0]["count"] == 3
    assert data["rejections"][0]["label"] == "Order size limit"
    # Approved events are not rejections.
    assert all(r["rule"] != "qty_sanity" for r in data["rejections"])


# ---------------------------------------------------------------- routes


@pytest.fixture
def app(session_factory):
    application = FastAPI()
    application.include_router(dashboard_routes.router)

    async def override_session():
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_db_session] = override_session
    application.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, admin_api_token="secret"
    )
    application.dependency_overrides[dashboard_routes.get_dashboard_broker] = lambda: StubBroker()
    return application


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_shell_needs_no_token(client):
    """The page carries no data, so gating it would only add friction."""
    resp = await client.get("/dashboard")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Operator dashboard" in resp.text
    assert "secret" not in resp.text  # no token, no data in the shell


async def test_data_requires_token(client):
    assert (await client.get("/dashboard/data")).status_code == 401
    bad = await client.get("/dashboard/data", headers={"X-Admin-Token": "wrong"})
    assert bad.status_code == 401


async def test_data_returns_payload_with_token(client):
    resp = await client.get("/dashboard/data", headers={"X-Admin-Token": "secret"})
    assert resp.status_code == 200
    assert "portfolio" in resp.json()


async def test_range_is_validated(client):
    ok = await client.get(
        "/dashboard/data?range=1W", headers={"X-Admin-Token": "secret"}
    )
    assert ok.status_code == 200
    assert ok.json()["portfolio"]["range"] == "1W"
    bad = await client.get(
        "/dashboard/data?range=../../etc", headers={"X-Admin-Token": "secret"}
    )
    assert bad.status_code == 422


async def test_static_assets_served_and_traversal_blocked(client):
    css = await client.get("/dashboard/static/dashboard.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    js = await client.get("/dashboard/static/dashboard.js")
    assert js.status_code == 200
    # Allowlisted filenames only, so traversal cannot reach anything.
    assert (await client.get("/dashboard/static/../../config.py")).status_code in (404, 200)
    assert (await client.get("/dashboard/static/secrets.env")).status_code == 404


async def test_persona_payload_exposes_id_for_controls(db_session):
    """The dashboard's Start/Stop buttons call /admin/personas/{id}/..., so the
    id must be in the payload or the controls cannot function."""
    strategy = Strategy(
        name="leg", version=1, class_path="x.Y", params={}, status=StrategyStatus.APPROVED
    )
    db_session.add(strategy)
    await db_session.flush()
    persona = Persona(
        name="all_weather", kind=PersonaKind.COOPERATIVE,
        status=PersonaStatus.INACTIVE, symbol_set=["VTI"],
        fidelity_note="Unlevered retail approximation, NOT Bridgewater's.",
    )
    db_session.add(persona)
    await db_session.flush()
    db_session.add(PersonaMember(persona_id=persona.id, strategy_id=strategy.id))
    await db_session.flush()

    data = await build_dashboard(db_session, paper_settings(), StubBroker())
    entry = data["personas"][0]
    assert entry["id"] == str(persona.id)
    # The fidelity note travels with it: it must be visible at the moment
    # someone switches the persona on, not buried in a doc.
    assert "NOT Bridgewater" in entry["fidelity_note"]
