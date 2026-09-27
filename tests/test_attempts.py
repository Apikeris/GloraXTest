from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import copy
from glorax.extensions import db
from glorax.models import Attempt,AttemptItem,Question,QuestionRevision,Project,Participant,utcnow,aware
from glorax.attempts import sweep_expired, available_question_counts


def start(client,project,name='Тестовый Сотрудник',code=''):
    response=client.post('/start_test',data={'project_id':project,'full_name':name,'employee_code':code})
    assert response.status_code==302
    return response.location.rsplit('/',1)[-1]


def current(client,aid): return client.get(f'/api/attempts/{aid}/current').get_json()


def test_current_only_stable_order_deadline_and_atomic_answers(app,client,seeded):
    aid=start(client,seeded['project_ids'][0]);q=current(client,aid)
    again=current(client,aid)
    assert q['options']==again['options'] and q['deadline']==again['deadline']
    assert q['id']==again['id']
    assert not {'correct_option_id','explanation','target_fact_revision_id'} & q.keys()
    assert all(set(o)=={'id','text'} for o in q['options'])
    assert len(q['options'])==4
    with app.app_context():
        item=db.session.get(AttemptItem,q['id']);correct=item.snapshot['correct_option_id'];before=copy.deepcopy(item.snapshot)
        assert int((aware(item.deadline)-aware(item.opened_at)).total_seconds())==20
    url=f'/api/attempts/{aid}/answer';body={'question_id':q['id'],'option_id':correct}
    assert client.post(url,json=body).get_json()['accepted']
    assert client.post(url,json=body).get_json()['duplicate']
    with app.app_context():
        assert db.session.get(Attempt,aid).score==1
        item=db.session.get(AttemptItem,q['id']);assert item.snapshot==before
        rev=db.session.get(QuestionRevision,item.question_revision_id);question=db.session.get(Question,rev.question_id)
        question.status='archived';db.session.get(Project,question.project_id).enabled=False;db.session.commit()
    q2=current(client,aid);assert q2['number']==2
    assert q2['id']!=q['id']
    assert client.post(url,json={'question_id':q2['id'],'option_id':q2['options'][0]['id']}).status_code==200
    done=current(client,aid);assert done['status']=='completed'
    result=client.get(done['result_url']);assert result.status_code==200
    assert before['explanation'].encode() not in result.data


def test_timeout_uses_server_time_and_background_skips_unopened(app,client,seeded,monkeypatch):
    now=utcnow();monkeypatch.setattr('glorax.attempts.server_now',lambda:now)
    aid=start(client,seeded['project_ids'][0]);q=current(client,aid)
    now+=timedelta(seconds=21)
    response=client.post(f'/api/attempts/{aid}/answer',json={'question_id':q['id'],'option_id':q['options'][0]['id']})
    assert response.status_code==409 and response.get_json()['expired']
    with app.app_context():
        assert db.session.get(AttemptItem,q['id']).outcome=='timeout'
        second=AttemptItem.query.filter_by(attempt_id=aid,position=2).one();assert second.opened_at is None
    q2=current(client,aid);assert q2['number']==2
    with app.app_context():
        assert aware(db.session.get(AttemptItem,q2['id']).deadline)==now+timedelta(seconds=20)
    now+=timedelta(seconds=21)
    with app.app_context():
        sweep_expired();assert db.session.get(Attempt,aid).status=='completed'


def test_inactivity_interrupted_full_denominator(app,client,seeded,monkeypatch):
    now=utcnow();aid=start(client,seeded['project_ids'][0]);current(client,aid)
    monkeypatch.setattr('glorax.attempts.server_now',lambda:now+timedelta(hours=2))
    with app.app_context():
        sweep_expired();a=db.session.get(Attempt,aid)
        assert a.status=='interrupted' and a.total==2
        assert [i.outcome for i in AttemptItem.query.filter_by(attempt_id=aid).order_by(AttemptItem.position)]==['timeout','not_reached']


def test_concurrent_posts_score_once(app,client,seeded):
    aid=start(client,seeded['project_ids'][0]);q=current(client,aid)
    cookie=client.get_cookie('session').value
    with app.app_context(): correct=db.session.get(AttemptItem,q['id']).snapshot['correct_option_id']
    def submit(_):
        other=app.test_client();other.set_cookie('session',cookie)
        return other.post(f'/api/attempts/{aid}/answer',json={'question_id':q['id'],'option_id':correct}).status_code
    with ThreadPoolExecutor(max_workers=6) as pool: assert list(pool.map(submit,range(6)))==[200]*6
    with app.app_context():
        db.session.expire_all();assert db.session.get(Attempt,aid).score==1
        assert AttemptItem.query.filter_by(attempt_id=aid,outcome='correct').count()==1


def test_ownership_future_question_invalid_option_and_names(app,client,seeded):
    aid=start(client,seeded['project_ids'][0]);q=current(client,aid)
    other=app.test_client()
    assert other.get(f'/api/attempts/{aid}/current').status_code==404
    assert other.get(f'/result/{aid}').status_code==404
    assert other.post(f'/api/attempts/{aid}/answer',json={'question_id':q['id'],'option_id':q['options'][0]['id']}).status_code==404
    with app.app_context(): future=AttemptItem.query.filter_by(attempt_id=aid,position=2).one();future_id=future.id
    assert client.post(f'/api/attempts/{aid}/answer',json={'question_id':future_id,'option_id':'x'}).status_code==409
    assert client.post(f'/api/attempts/{aid}/answer',json={'question_id':q['id'],'option_id':'x'}).status_code==400
    second=start(client,seeded['project_ids'][0]);third=start(client,seeded['project_ids'][0],code='EMP-17');fourth=start(client,seeded['project_ids'][0],code='EMP-17')
    with app.app_context():
        assert db.session.get(Attempt,aid).participant_id!=db.session.get(Attempt,second).participant_id
        assert db.session.get(Attempt,third).participant_id==db.session.get(Attempt,fourth).participant_id


def test_database_prevents_snapshot_and_deadline_mutation(app,client,seeded):
    import pytest
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    aid=start(client,seeded['project_ids'][0]);q=current(client,aid)
    with app.app_context():
        for sql in ["UPDATE attempt_item SET snapshot='{}' WHERE id=:id", "UPDATE attempt_item SET deadline=deadline+interval '10 second' WHERE id=:id", 'DELETE FROM attempt_item WHERE id=:id']:
            with pytest.raises(DBAPIError):db.session.execute(text(sql),{'id':q['id']});db.session.commit()
            db.session.rollback()


def test_topic_and_count_limit_no_duplicates(app,client,seeded):
    from glorax.attempts import select_questions
    with app.app_context():
        p=db.session.get(Project,seeded['project_ids'][0]);p.topic_distribution={'location':5};p.question_limit=9;db.session.commit()
        assert len(select_questions(p))==1
    response=client.post('/start_test',data={'project_id':seeded['project_ids'][0],'full_name':'Проверка','expected_count':'9'})
    assert response.location=='/'
    aid=start(client,seeded['project_ids'][0])
    with app.app_context():assert db.session.get(Attempt,aid).total==1


def test_catalogue_counts_use_published_facts_and_apply_settings(app,seeded):
    with app.app_context():
        project=db.session.get(Project,seeded['project_ids'][0])
        assert available_question_counts([project]) == {project.id: 2}
        project.topic_distribution={'location':5}
        project.question_limit=1
        db.session.commit()
        assert available_question_counts([project]) == {project.id: 1}
