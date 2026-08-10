"""The pipeline runner: lifecycle, the 2FA hand-off, cancellation, HTTP layer.

A fake pipeline stands in for the browser automation — these tests are about
the machinery every pipeline shares (claiming, logging, parking on input,
finishing states), which is exactly the part that must not depend on B3's
website being reachable or stable.
"""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import current_portfolio
from app.db.models import Notification, PipelineRun
from app.db.session import get_db
from app.main import app
from app.pipelines import base
from app.pipelines.base import PipelineError
from app.pipelines import runner
from app.services.portfolio_registry import get_default_portfolio


class FakePipeline(base.Pipeline):
    spec = base.PipelineSpec(
        key="fake",
        name="Fake",
        description="test double",
        credentials=(),
        schedule="nunca",
    )

    def __init__(self, body) -> None:
        self._body = body

    def run(self, ctx) -> dict:
        return self._body(ctx)


@pytest.fixture
def fake(request):
    """Register a FakePipeline whose body the test supplies later."""

    holder: dict = {}
    pipeline = FakePipeline(lambda ctx: holder["body"](ctx))
    base.register(pipeline)
    yield holder
    base._REGISTRY.pop("fake", None)


@pytest.fixture
def client(engine, db: Session):
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    portfolio = get_default_portfolio(db)

    def override_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[current_portfolio] = lambda: portfolio
    with TestClient(app, raise_server_exceptions=True) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _fast_waits(monkeypatch):
    """The 2-second poll tick is right for humans and wrong for a test suite."""
    monkeypatch.setattr(runner, "_WAIT_TICK", 0.05)


def wait_for(predicate, timeout: float = 10.0):
    """Poll until ``predicate()`` returns something truthy; return it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not reached in time")


def run_row(db: Session, run_id: int) -> PipelineRun:
    db.expire_all()
    return db.get(PipelineRun, run_id)


def finished(db: Session, run_id: int):
    def check():
        row = run_row(db, run_id)
        return row if row.status not in runner.ACTIVE_STATUSES else None

    return wait_for(check)


# ------------------------------------------------------------------ lifecycle


def test_success_records_result_log_and_notification(db, portfolio, fake):
    def body(ctx):
        ctx.log("primeiro passo")
        ctx.log("segundo passo")
        return {"rows_imported": 3, "rows_duplicate": 1}

    fake["body"] = body
    run_id = runner.start_run("fake")
    row = finished(db, run_id)

    assert row.status == "success"
    assert row.result["rows_imported"] == 3
    assert [entry["message"] for entry in row.log] == ["primeiro passo", "segundo passo"]
    assert row.finished_at is not None
    note = db.scalar(select(Notification).where(Notification.kind == "pipeline"))
    assert note is not None and note.level == "success"
    assert "3 movimentações novas" in note.body


def test_options_reach_the_pipeline(db, portfolio, fake):
    fake["body"] = lambda ctx: {"full": ctx.options.get("full_history")}
    run_id = runner.start_run("fake", options={"full_history": True})
    row = finished(db, run_id)

    assert row.status == "success"
    assert row.result["full"] is True
    assert row.options == {"full_history": True}


def test_pipeline_error_message_reaches_the_row(db, portfolio, fake):
    def body(ctx):
        raise PipelineError("a B3 recusou o acesso")

    fake["body"] = body
    run_id = runner.start_run("fake")
    row = finished(db, run_id)

    assert row.status == "failed"
    assert row.error == "a B3 recusou o acesso"


def test_unexpected_exception_becomes_generic_error(db, portfolio, fake):
    def body(ctx):
        raise RuntimeError("selector exploded")

    fake["body"] = body
    run_id = runner.start_run("fake")
    row = finished(db, run_id)

    assert row.status == "failed"
    assert row.error == runner.GENERIC_ERROR  # the internals never reach the screen


def test_second_start_is_rejected_while_running(db, portfolio, fake):
    release = threading.Event()

    def body(ctx):
        release.wait(timeout=10)
        return {}

    fake["body"] = body
    run_id = runner.start_run("fake")
    try:
        with pytest.raises(runner.PipelineBusy):
            runner.start_run("fake")
    finally:
        release.set()
    finished(db, run_id)


# ------------------------------------------------------------ the 2FA hand-off


def test_request_input_roundtrip(db, portfolio, fake):
    def body(ctx):
        code = ctx.request_input("Digite o código.")
        return {"code": code}

    fake["body"] = body
    run_id = runner.start_run("fake")

    waiting = wait_for(
        lambda: run_row(db, run_id).status == "waiting_input" and run_row(db, run_id)
    )
    assert waiting.input_request["prompt"] == "Digite o código."
    # The parked run announces itself in the bell, for whoever can answer.
    assert db.scalar(select(Notification).where(Notification.level == "warning")) is not None

    waiting.input_response = {"value": " 123456 "}
    db.commit()

    row = finished(db, run_id)
    assert row.status == "success"
    assert row.result["code"] == "123456"  # request_input trims — codes get copy-pasted with spaces
    assert row.input_request is None and row.input_response is None


def test_input_timeout_fails_with_instructions(db, portfolio, fake, monkeypatch):
    monkeypatch.setattr(runner, "INPUT_TIMEOUT", runner.timedelta(seconds=0.2))

    def body(ctx):
        return {"code": ctx.request_input("Digite o código.")}

    fake["body"] = body
    run_id = runner.start_run("fake")
    row = finished(db, run_id)

    assert row.status == "failed"
    assert "manualmente" in row.error


def test_cancel_while_waiting_for_input(db, portfolio, fake):
    def body(ctx):
        return {"code": ctx.request_input("Digite o código.")}

    fake["body"] = body
    run_id = runner.start_run("fake")
    waiting = wait_for(lambda: run_row(db, run_id).status == "waiting_input" and run_row(db, run_id))
    waiting.cancel_requested = True
    db.commit()

    row = finished(db, run_id)
    assert row.status == "cancelled"


# ------------------------------------------------------------------ staleness


def test_stale_run_is_failed_and_unblocks_the_claim(db, portfolio, fake):
    stale = PipelineRun(
        pipeline="fake",
        status="running",
        heartbeat_at=runner._now() - runner.STALE_AFTER * 2,
        log=[],
    )
    db.add(stale)
    db.commit()

    fake["body"] = lambda ctx: {}
    run_id = runner.start_run("fake")  # would raise PipelineBusy if stale rows blocked
    finished(db, run_id)

    db.expire_all()
    assert db.get(PipelineRun, stale.id).status == "failed"
    assert "interrompida" in db.get(PipelineRun, stale.id).error


# ----------------------------------------------------------------- HTTP layer


def test_list_pipelines_shows_b3_unconfigured(client):
    payload = client.get("/api/pipelines").json()
    b3 = next(item for item in payload["pipelines"] if item["key"] == "b3")
    assert b3["configured"] is False
    assert {cred["key"] for cred in b3["credentials"]} == {"b3_cpf", "b3_password"}
    # Write-only: the payload says whether, never what.
    assert all(set(cred) == {"key", "label", "configured"} for cred in b3["credentials"])


def test_trigger_unconfigured_pipeline_is_a_400(client):
    response = client.post("/api/pipelines/b3/run")
    assert response.status_code == 400
    assert "credenciais" in response.json()["detail"]


def test_trigger_unknown_pipeline_is_a_404(client):
    assert client.post("/api/pipelines/nope/run").status_code == 404


def test_answer_endpoint_feeds_the_parked_run(client, db, portfolio, fake):
    def body(ctx):
        return {"code": ctx.request_input("Digite o código.")}

    fake["body"] = body
    run_id = client.post("/api/pipelines/fake/run").json()["run_id"]
    wait_for(lambda: run_row(db, run_id).status == "waiting_input")

    # Answering a run that is not waiting is a conflict, not a silent no-op.
    response = client.post(f"/api/pipelines/runs/{run_id}/input", json={"value": "654321"})
    assert response.status_code == 200

    row = finished(db, run_id)
    assert row.result["code"] == "654321"

    late = client.post(f"/api/pipelines/runs/{run_id}/input", json={"value": "x"})
    assert late.status_code == 409


def test_cancel_endpoint(client, db, portfolio, fake):
    release = threading.Event()

    def body(ctx):
        for _ in range(200):
            if release.wait(timeout=0.05):
                break
            ctx.check_cancel()
        return {}

    fake["body"] = body
    run_id = client.post("/api/pipelines/fake/run").json()["run_id"]
    try:
        assert client.post(f"/api/pipelines/runs/{run_id}/cancel").json()["cancelling"] is True
        row = finished(db, run_id)
        assert row.status == "cancelled"
    finally:
        release.set()

    assert client.post(f"/api/pipelines/runs/{run_id}/cancel").status_code == 409


def test_runs_history_endpoint(client, db, portfolio, fake):
    fake["body"] = lambda ctx: {"rows_imported": 0, "rows_duplicate": 5}
    run_id = client.post("/api/pipelines/fake/run").json()["run_id"]
    finished(db, run_id)

    runs = client.get("/api/pipelines/runs", params={"pipeline": "fake"}).json()["runs"]
    assert runs[0]["id"] == run_id
    assert runs[0]["status"] == "success"
    assert runs[0]["active"] is False
