"""Database round-trip budgets and failure paths that motivated the refactor."""

import csv
import io
from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import event

from glorax.attempts import sweep_expired
from glorax.extensions import db
from glorax.facts import export_dataset, get_setting, set_setting
from glorax.importing import export_prompt
from glorax.models import AttemptItem, Setting, utcnow
from glorax.questions import generate_questions
from tests.test_admin import login_session
from tests.test_attempts import current, start


@contextmanager
def select_queries(app):
    with app.app_context():
        engine = db.engine
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


def test_settings_default_and_rollback_are_transaction_local(app):
    with app.app_context():
        assert get_setting("missing") is None
        assert get_setting("missing", 17) == 17
        set_setting("price_valid_days", 7)
        db.session.commit()
        set_setting("price_valid_days", 30)
        assert get_setting("price_valid_days") == 30
        db.session.rollback()
        assert get_setting("price_valid_days") == 7
        set_setting("nullable", None)
        db.session.commit()
        assert get_setting("nullable", 20) is None


def test_exports_and_regeneration_keep_query_count_bounded(app, seeded):
    with app.app_context():
        db.session.remove()
        with select_queries(app) as queries:
            data = export_dataset()
        assert len(data["facts"]) == 8
        assert len(queries) <= 3
        db.session.remove()
        with select_queries(app) as queries:
            prompt = export_prompt(seeded["project_ids"][0])
        assert "JSON Schema" in prompt and seeded["dataset_id"] in prompt
        assert len(queries) <= 8
        db.session.remove()
        with select_queries(app) as queries:
            report = generate_questions(seeded["dataset_id"])
        assert report["unchanged"] == 8 and report["created"] == 0
        assert len(queries) <= 16
        db.session.rollback()


def test_csv_aggregates_counts_and_literal_participant_search(app, client, seeded):
    ids = [start(client, seeded["project_ids"][0], name=r"Имя\%_") for _ in range(6)]
    q = current(client, ids[0])
    response = client.post(
        f"/api/attempts/{ids[0]}/answer",
        json={"question_id": q["id"], "option_id": q["options"][0]["id"]},
    )
    assert response.status_code == 200
    login_session(app, client)
    with select_queries(app) as queries:
        response = client.get("/admin/attempts.csv")
    rows = list(csv.reader(io.StringIO(response.data.decode("utf-8-sig"))))
    assert response.status_code == 200 and len(rows) == 7
    assert len(queries) <= 4
    assert client.get("/admin/participants", query_string={"name": "\\"}).status_code == 200
    assert client.get("/admin/questions", query_string={"q": "\\"}).status_code == 200
    assert client.get("/admin/analytics").status_code == 200


def test_sweeper_ignores_live_attempts_and_batches_expired_items(app, client, seeded, monkeypatch):
    now = utcnow()
    monkeypatch.setattr("glorax.attempts.server_now", lambda: now)
    expired = [start(client, seeded["project_ids"][0]) for _ in range(5)]
    for aid in expired:
        current(client, aid)
    now += timedelta(seconds=21)
    live = start(client, seeded["project_ids"][0])
    live_q = current(client, live)
    with app.app_context(), select_queries(app) as queries:
        assert sweep_expired() == 5
        assert len(queries) <= 4
        assert db.session.get(AttemptItem, live_q["id"]).outcome is None
        assert AttemptItem.query.filter_by(outcome="timeout").count() == 5
        assert sweep_expired() == 0


@pytest.mark.parametrize(
    "body",
    [[], ["question"], "text", 123, {"question_id": []}, {"question_id": {}, "option_id": "a"}],
)
def test_malformed_answer_json_is_a_client_error(app, client, seeded, body):
    aid = start(client, seeded["project_ids"][0])
    assert client.post(f"/api/attempts/{aid}/answer", json=body).status_code == 400


def test_bad_heartbeat_and_source_url_do_not_crash_admin(app):
    from glorax.admin.common import safe_url
    from glorax.jobs import worker_diagnostics

    with app.app_context():
        db.session.add(Setting(key="worker_queue_heartbeat", value="invalid-date"))
        db.session.commit()
        assert worker_diagnostics()["online"] is False
        assert safe_url("https://[") is None


def test_owned_http_session_closes_on_collection_failure(monkeypatch):
    from glorax import parser

    closed = []

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            closed.append(True)

    monkeypatch.setattr(parser, "HTTPClient", Client)

    def fail(client):
        raise parser.SourceError("source unavailable")

    monkeypatch.setattr(parser, "collect_catalog", fail)
    with pytest.raises(parser.SourceError):
        parser.scrape()
    assert closed == [True]


def test_route_contract_is_unchanged(app):
    import json
    from pathlib import Path

    expected = json.loads((Path(__file__).parent / "fixtures/routes.json").read_text())
    actual = sorted(
        [str(rule), rule.endpoint, sorted(rule.methods)] for rule in app.url_map.iter_rules()
    )
    assert actual == expected


def test_hundred_import_previews_use_one_batch(app, seeded):
    import copy

    from glorax.importing import preview_import
    from tests.test_importing import payload_from_generated

    with app.app_context():
        payload = payload_from_generated()
        item = payload["questions"][0]
        payload["questions"] = [
            {**copy.deepcopy(item), "external_id": f"preview-{i}"} for i in range(100)
        ]
        db.session.remove()
        with select_queries(app) as queries:
            preview = preview_import(payload)
        assert preview["valid"] and len(preview["questions"]) == 100
        assert len(queries) <= 8


def test_question_and_answer_read_items_once(app, client, seeded):
    aid = start(client, seeded["project_ids"][0])
    with select_queries(app) as queries:
        q = current(client, aid)
    assert sum("FROM attempt_item" in statement for statement in queries) == 1
    with select_queries(app) as queries:
        response = client.post(
            f"/api/attempts/{aid}/answer",
            json={"question_id": q["id"], "option_id": q["options"][0]["id"]},
        )
    assert response.status_code == 200 and response.json["accepted"]
    assert sum("FROM attempt_item" in statement for statement in queries) == 1
