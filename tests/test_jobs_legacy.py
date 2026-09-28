import hashlib
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import collection
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from glorax.extensions import db
from glorax.facts import latest_dataset, publish_collection, save_manual_fact
from glorax.jobs import claim_job, enqueue_refresh, recover_jobs, run_job
from glorax.legacy import import_legacy
from glorax.models import (
    Admin,
    Attempt,
    AttemptItem,
    Fact,
    FactRevision,
    Job,
    Project,
    Question,
    utcnow,
)


def test_queue_duplicate_recovery_fencing_failure_keeps_snapshot(app, seeded):
    with app.app_context():
        first = enqueue_refresh()
        assert enqueue_refresh().id == first.id
        job_id, token = claim_job()
        assert claim_job() is None
        job = db.session.get(Job, job_id)
        job.heartbeat_at = utcnow() - timedelta(hours=1)
        db.session.commit()
        assert recover_jobs() == 1
        new_id, new_token = claim_job()
        assert new_id == job_id and new_token != token
        assert not run_job(job_id, token, lambda progress: collection())
        assert latest_dataset().id == seeded["dataset_id"]
        bad = collection()
        bad["complete"] = False
        bad["errors"] = ["source failed"]
        assert not run_job(job_id, new_token, lambda progress: bad)
        assert latest_dataset().id == seeded["dataset_id"]
        job = db.session.get(Job, job_id)
        job.available_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
        claimed = claim_job()
        assert run_job(*claimed, collector=lambda progress: collection())
        assert db.session.get(Job, job_id).state == "succeeded"
        assert Question.query.count() == 8


def test_atomic_publication_rolls_back_partial_invalid_collection(app, seeded):
    with app.app_context():
        bad = collection()
        bad["projects"][0]["name"] = "Should rollback"
        bad["projects"][1]["facts"][0]["verification_status"] = "verified"
        bad["projects"][1]["facts"][0]["evidence"] = None
        with pytest.raises(ValueError):
            publish_collection(bad)
        db.session.rollback()
        assert latest_dataset().id == seeded["dataset_id"]
        assert Project.query.filter_by(key="test-project-0").one().name != "Should rollback"


def test_manual_override_preserved_and_history_immutable(app, seeded):
    with app.app_context():
        admin = Admin(username="tester", password_hash="not-used")
        db.session.add(admin)
        db.session.flush()
        fact = Fact.query.filter_by(project_id=seeded["project_ids"][0], key="city").one()
        old = fact.current_revision_id
        revision = save_manual_fact(
            fact.project_id,
            {
                "fact_id": fact.id,
                "category": "location",
                "key": "city",
                "scope": fact.scope,
                "value": "Исправленный город",
                "value_type": "string",
                "source_url": "https://example.org/correction",
                "evidence": "Проверенный исправленный город",
                "verification_status": "verified",
                "is_exclusive": True,
            },
            admin.id,
        )
        db.session.commit()
        rid = revision.id
        snap = publish_collection(collection())
        db.session.commit()
        assert fact.current_revision_id == rid
        assert snap.report["conflicts"]
        assert db.session.get(FactRevision, old)
        with pytest.raises(DBAPIError):
            db.session.execute(
                text("UPDATE fact_revision SET evidence=:v WHERE id=:id"),
                {"v": "mutated", "id": old},
            )
            db.session.commit()
        db.session.rollback()
        assert any(
            fk["name"] == "fk_fact_current_revision"
            for fk in inspect(db.engine).get_foreign_keys("fact")
        )


def test_legacy_backup_repeatable_and_no_invented_history(app, tmp_path):
    import sqlite3

    old = tmp_path / "old.db"
    conn = sqlite3.connect(old)
    conn.executescript("""CREATE TABLE project(id INTEGER PRIMARY KEY,name TEXT,city TEXT,min_price FLOAT,max_price FLOAT,features TEXT,pros_cons TEXT);
CREATE TABLE question(id INTEGER PRIMARY KEY,project_id INTEGER,text TEXT,correct_answer TEXT,distractors TEXT);
CREATE TABLE attempt(id INTEGER PRIMARY KEY,username TEXT,project_filter TEXT,start_time TEXT,score INTEGER);
CREATE TABLE answer_log(id INTEGER PRIMARY KEY,attempt_id INTEGER,question_id INTEGER,selected_answer TEXT,is_correct BOOLEAN);
INSERT INTO project VALUES(1,'Старый проект','Город',1.1,2.2,'Особенность','Оценка');
INSERT INTO question VALUES(1,1,'Современный текст','Ответ','["Б","В","Г"]');
INSERT INTO attempt VALUES(1,'Тестовое имя','1','2025-01-01 12:00:00',4);
INSERT INTO answer_log VALUES(1,1,999,'Сохранённый ответ',1);""")
    conn.commit()
    conn.close()
    before = hashlib.sha256(old.read_bytes()).hexdigest()
    with app.app_context():
        result = import_legacy(old, tmp_path / "backups")
        assert Path(result["backup"]).exists()
        assert import_legacy(old, tmp_path / "backups")["already_imported"]
        assert Attempt.query.count() == 1 and Project.query.count() == 1
        assert Attempt.query.one().score == 4
        item = AttemptItem.query.one()
        assert item.snapshot["text"] is None and item.snapshot["options"] == []
        assert item.snapshot["legacy_log"]["selected_answer"] == "Сохранённый ответ"
        assert Question.query.one().status == "needs_review"
        assert all(f.verification_status == "legacy_unverified" for f in FactRevision.query.all())
    assert hashlib.sha256(old.read_bytes()).hexdigest() == before


def test_migration_downgrade_and_upgrade_on_postgresql(app):
    from flask_migrate import downgrade, upgrade

    with app.app_context():
        downgrade(directory="migrations", revision="base")
        upgrade(directory="migrations")
        assert inspect(db.engine).has_table("attempt_item")
        assert any(
            fk["name"] == "fk_question_current_revision"
            for fk in inspect(db.engine).get_foreign_keys("question")
        )


def test_source_conflict_preserves_fact_but_pauses_questions(app, seeded):
    from glorax.questions import eligible_questions

    with app.app_context():
        fact = Fact.query.filter_by(project_id=seeded["project_ids"][0], key="city").one()
        old = fact.current_revision_id
        data = collection()
        data["projects"][0]["facts"][0].update(
            value="Спорный город", verification_status="needs_review"
        )
        publish_collection(data)
        db.session.commit()
        assert fact.current_revision_id == old and fact.review_pending
        assert len(eligible_questions(fact.project_id)) == 1
