from types import SimpleNamespace

from glorax.catalogue import available_question_counts
from glorax.extensions import db
from glorax.facts import publish_collection
from glorax.models import Fact, FactRevision, Project, Question, QuestionRevision, uid
from glorax.question_policy import area_policy_clause, area_policy_error
from glorax.questions import eligible_questions, revision_data
from tests.conftest import collection


def test_area_policy_handles_units_and_scoped_maxima():
    for key, category, unit, rooms, allowed in [
        ("min_area", "layouts", "м²", "0", False),
        ("max_area", "layouts", "м²", None, False),
        ("max_area", "layouts", "м²", "1", False),
        ("max_area", "layouts", "м²", "0", True),
        ("studio_max_area", "layouts", "м²", "0", True),
        ("parameter_area", "layouts", "кв. м", None, False),
        ("ceiling_height", "layouts", "м", None, True),
        ("land_area", "overview", "га", None, True),
        ("apartment_formats", "layouts", None, None, True),
    ]:
        fact = SimpleNamespace(key=key, category=category, scope={"rooms": rooms})
        assert (area_policy_error(fact, SimpleNamespace(unit=unit)) is None) == allowed


def test_old_published_area_questions_excluded_from_counts_and_new_tests(app):
    payload = collection()
    for i, project in enumerate(payload["projects"]):
        for key, rooms in [("min_area", "0"), ("max_area", "0")]:
            project["facts"].append(
                {
                    "category": "layouts",
                    "key": key,
                    "value": str(30 + i),
                    "value_type": "decimal",
                    "unit": "м²",
                    "scope": {"level": "property_type", "property_type": "flat", "rooms": rooms},
                    "source_url": project["canonical_url"],
                    "evidence": "Подтверждённая площадь",
                    "method": "test_fixture",
                    "verification_status": "verified",
                    "is_exclusive": True,
                }
            )
    with app.app_context():
        dataset = publish_collection(payload)
        project = Project.query.order_by(Project.key).first()
        # Simulate a question that was published before the new policy.
        targets = list(
            db.session.scalars(
                db.select(FactRevision)
                .join(Fact, Fact.id == FactRevision.fact_id)
                .where(Fact.key == "min_area")
                .order_by(Fact.project_id)
            )
        )
        target = next(
            r for r in targets if db.session.get(Fact, r.fact_id).project_id == project.id
        )
        q = Question(
            id=uid(),
            project_id=project.id,
            origin="generated",
            status="published",
            generation_key="old-area",
        )
        db.session.add(q)
        db.session.flush()
        rev = QuestionRevision(
            id=uid(),
            question_id=q.id,
            fingerprint="old-area",
            **revision_data(target, [r for r in targets if r.id != target.id], project, dataset.id),
        )
        db.session.add(rev)
        db.session.flush()
        q.current_revision_id = rev.id
        db.session.commit()
        selected = eligible_questions(project)
        assert q.id not in {question.id for question in selected}
        assert available_question_counts([project])[project.id] == len(selected)
        assert any(
            db.session.get(
                Fact,
                db.session.get(
                    FactRevision,
                    db.session.get(
                        QuestionRevision, question.current_revision_id
                    ).target_fact_revision_id,
                ).fact_id,
            ).key
            == "max_area"
            for question in selected
        )
        sql_allowed = set(
            db.session.scalars(
                db.select(FactRevision.id)
                .join(Fact, Fact.id == FactRevision.fact_id)
                .where(area_policy_clause(Fact, FactRevision))
            )
        )
        for revision in FactRevision.query.all():
            assert (revision.id in sql_allowed) == (
                area_policy_error(db.session.get(Fact, revision.fact_id), revision) is None
            )
