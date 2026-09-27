from datetime import timedelta
from glorax.extensions import db
from glorax.models import Fact,FactRevision,Question,QuestionRevision,utcnow
from glorax.questions import canonical_display,generate_questions,eligible_questions,validate_revision,validate_fact_options


def test_equivalence():
    assert canonical_display('10 млн ₽')==canonical_display('10 000 000 рублей')
    assert canonical_display('от 8 до 20 этажей')==canonical_display('8–20 этажа')
    assert canonical_display('Дом сдан')==canonical_display('Завершён')
    assert canonical_display('СПБ')==canonical_display('Санкт-Петербург')
    assert canonical_display('  29 м²')==canonical_display('29 кв. м')


def test_generator_idempotent_provenance_no_unknown_features(app,seeded):
    with app.app_context():
        count=Question.query.count();assert count==8
        report=generate_questions(seeded['dataset_id']);db.session.commit()
        assert Question.query.count()==count and report['created']==0
        for q in Question.query.all():
            r=db.session.get(QuestionRevision,q.current_revision_id)
            assert not validate_revision(r)
            assert len(r.options)==4
            assert len({o['text'] for o in r.options})==4
            target=db.session.get(FactRevision,r.target_fact_revision_id)
            for o in r.options:
                f=db.session.get(Fact,db.session.get(FactRevision,o['fact_revision_id']).fact_id)
                assert f.project_id==q.project_id if o['id']==r.correct_option_id else f.project_id!=q.project_id


def test_ambiguous_stale_equivalent_candidates_rejected(app,seeded):
    with app.app_context():
        q=Question.query.first();r=db.session.get(QuestionRevision,q.current_revision_id);t=db.session.get(FactRevision,r.target_fact_revision_id)
        refs=[db.session.get(FactRevision,o['fact_revision_id']) for o in r.options]
        with db.session.no_autoflush:
            t.is_exclusive=False
            assert any('единственное' in e for e in validate_fact_options(t,refs,r.dataset_id))
            t.is_exclusive=True;t.valid_until=utcnow()-timedelta(seconds=1)
            assert any('актуальности' in e for e in validate_revision(r))
            assert q.id not in {q.id for q in eligible_questions(q.project_id)}
        db.session.rollback()


def test_blank_source_value_is_skipped_without_breaking_snapshot(app,seeded):
    from conftest import collection
    from glorax.facts import publish_collection
    with app.app_context():
        data=collection();data['projects'][0]['facts'][0]['value']=''
        snapshot=publish_collection(data);db.session.commit()
        assert any('Значение факта отсутствует' in item['reasons'] for item in snapshot.report['generation']['skipped'])
        assert Question.query.filter_by(status='published').count()<8


def test_price_requires_full_cost_type_terms_and_sample(app,seeded):
    from glorax.questions import price_errors
    from glorax.models import Fact,FactRevision
    from glorax.facts import digest
    with app.app_context():
        fact=Fact(project_id=seeded['project_ids'][0],category='prices',key='max_price',scope={},scope_key=digest({}));db.session.add(fact);db.session.flush()
        r=FactRevision(fact_id=fact.id,dataset_id=seeded['dataset_id'],value='10000000',value_type='decimal',unit='RUB',method='test',verification_status='verified',conditions={},valid_until=utcnow()+timedelta(days=1))
        assert len(price_errors(r))>=4
        r.conditions={'currency':'RUB','price_kind':'total','property_type':'flat','payment_terms':'full_payment','sample_complete':False,'offer_count':10,'collection_started_at':'2026-01-01T00:00Z','collection_finished_at':'2026-01-01T00:01Z'}
        assert any('Полнота' in e for e in price_errors(r))
        r.conditions={**r.conditions,'sample_complete':True}
        assert not price_errors(r)
        db.session.rollback()


def test_template_upgrade_versions_without_duplicate_questions(app,seeded,monkeypatch):
    with app.app_context():
        before=Question.query.count();old_revisions={q.current_revision_id for q in Question.query.all()}
        monkeypatch.setattr('glorax.questions.TEMPLATE_VERSION','test-new-template')
        report=generate_questions(seeded['dataset_id']);db.session.commit()
        assert Question.query.count()==before and report['updated']==before
        assert old_revisions.isdisjoint({q.current_revision_id for q in Question.query.all()})
        assert all(db.session.get(QuestionRevision,rid) for rid in old_revisions)
