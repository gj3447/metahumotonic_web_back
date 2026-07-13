"""ooptdd positive-TDD gate over the feedback pipeline — the 측정 layer in anger.

The point: `POST /api/feedback` returns `200 {ok, id}` whether the record landed
in Mongo or silently fell back to in-memory. A return-value test can't tell the
difference. These tests read the *emitted trace* back and positively assert the
durable write — and prove the gate goes RED on a silent loss the endpoint hides.

# KG: project_metahumotonic_web_integrate_core_dev_tech_2026_07_13, project_lakatotree_oo_ptdd_2026_06_14
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import trace
from app.store import FeedbackStore

pytest.importorskip("ooptdd", reason="vendored ooptdd core required for the gate")
from ooptdd import evaluate, load_gate  # noqa: E402

_GATE = str(Path(__file__).resolve().parent.parent / "gates" / "feedback_durability.yaml")


class _AcceptingCollection:
    """A Mongo collection stand-in whose insert_one SUCCEEDS (durable write)."""

    def __init__(self):
        self.docs = []

    async def insert_one(self, doc):
        self.docs.append(doc)


class _DroppingCollection:
    """Mongo is 'up' enough to be selected but every insert FAILS — the exact
    silent-loss shape: save() logs a warning, falls back to memory, returns an id."""

    async def insert_one(self, doc):
        raise RuntimeError("simulated mongo drop (401 / disk full / network)")


async def _save_feedback(collection) -> str:
    store = FeedbackStore()
    store._collection = collection  # pretend Mongo is connected
    if collection is not None:
        # bypass the settings/breaker gate in _get_collection for this unit test
        async def _get():
            return collection

        store._get_collection = _get  # type: ignore
    return await store.save({"type": "bug", "subject": "s", "body": "b"})


async def test_gate_green_when_durably_stored():
    trace.reset()
    record_id = await _save_feedback(_AcceptingCollection())
    assert record_id  # self-report: ok

    result = evaluate(trace.backend(), load_gate(_GATE, cid=record_id), cid=record_id)
    assert result["ok"], f"expected GREEN durable gate, got: {result}"


async def test_gate_red_on_silent_loss_that_endpoint_hides():
    trace.reset()
    # Mongo drops the write; save() STILL returns an id (the lie).
    record_id = await _save_feedback(_DroppingCollection())
    assert record_id  # self-report is a cheerful green — the bug

    result = evaluate(trace.backend(), load_gate(_GATE, cid=record_id), cid=record_id)
    # feedback_received arrived, but feedback_durably_stored never did → RED.
    assert not result["ok"], f"gate should be RED on silent loss, got: {result}"


async def test_pipeline_gate_holds_through_the_endpoint(client):
    """End-to-end through the ASGI app. In CI/offline there is no Mongo, so the
    write goes to memory → the durable gate is honestly RED (not a false green):
    ooptdd reports the deployment can't prove durability, which is the truth."""
    trace.reset()
    r = await client.post(
        "/api/feedback", json={"type": "feature", "subject": "s", "body": "b"}
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    cid = r.json()["id"]

    result = evaluate(trace.backend(), load_gate(_GATE, cid=cid), cid=cid)
    # received fired; durable did not (no Mongo in test) → the gate tells the truth.
    assert result["ok"] is False
