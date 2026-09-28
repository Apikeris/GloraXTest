import pytest
from werkzeug.security import generate_password_hash
from glorax.extensions import db
from glorax.models import Admin,Project,Fact,Question,Job


def login_session(app,client):
    with app.app_context():
        admin=Admin(username='admin-test',password_hash=generate_password_hash('test-password-long'));db.session.add(admin);db.session.commit();aid=admin.id
    with client.session_transaction() as s:s['admin_id']=aid
    return aid


def test_admin_protected_and_all_pages_render(app,client,seeded):
    for path in ['/admin/','/admin/attempts','/admin/projects','/admin/dataset.json','/admin/import','/admin/settings']:
        assert client.get(path).status_code in (302,401)
    login_session(app,client)
    for path in ['/admin/','/admin/attempts','/admin/participants','/admin/analytics','/admin/projects','/admin/questions','/admin/questions/new','/admin/settings','/admin/jobs','/admin/audit','/admin/ai-prompt','/admin/import','/admin/dataset.json','/admin/attempts.csv']:
        response=client.get(path);assert response.status_code==200,path
    with app.app_context():
        p=Project.query.first();f=Fact.query.first();q=Question.query.first()
        paths=[f'/admin/projects/{p.id}',f'/admin/projects/{p.id}/facts/new',f'/admin/facts/{f.id}/edit',f'/admin/questions/{q.id}']
    for path in paths: assert client.get(path).status_code==200,path
    assert client.post('/admin/jobs/refresh').status_code==302
    with app.app_context(): jid=Job.query.one().id
    assert client.get('/admin/jobs/'+jid).status_code==200
    assert client.get('/admin/jobs/'+jid+'.json').status_code==200


def test_csrf_login_rate_limit_and_escaping(app,client,seeded):
    app.config['WTF_CSRF_ENABLED']=True
    assert client.post('/start_test',data={'full_name':'Name'}).status_code==400
    app.config['WTF_CSRF_ENABLED']=False
    for _ in range(5):assert client.post('/admin/login',data={'username':'unknown','password':'bad'}).status_code==200
    assert client.post('/admin/login',data={'username':'unknown','password':'bad'}).status_code==429
    with app.app_context():
        p=Project.query.first();p.name='<script>alert(1)</script>';db.session.commit()
    page=client.get('/').data
    assert b'<script>alert(1)</script>' not in page and b'&lt;script&gt;' in page


def test_fact_edit_and_prompt_import_render(app,client,seeded):
    login_session(app,client)
    response=client.post('/admin/ai-prompt',data={'project_id':seeded['project_ids'][0],'count':'5','difficulty':'basic'})
    assert response.status_code==200 and 'Скопировать промпт'.encode() in response.data
    response=client.post('/admin/import',data={'action':'preview','payload':'{"schema_version":"wrong"}'})
    assert response.status_code==200 and b'JSON Schema' in response.data


def test_admin_cannot_set_more_than_twenty_questions(app, client, seeded):
    from glorax.facts import get_setting
    login_session(app, client)
    response = client.post('/admin/settings', data={'question_limit': '80', 'price_valid_days': '7',
        'fact_valid_days': '180', 'inactivity_minutes': '60'})
    assert response.status_code == 200
    assert '1–20'.encode() in response.data
    with app.app_context():
        assert get_setting('question_limit') is None
    response = client.post(f"/admin/projects/{seeded['project_ids'][0]}",
                           data={'question_limit': '80', 'enabled': 'on', 'topic_distribution': '{}'})
    assert response.status_code == 200
    with app.app_context():
        assert db.session.get(Project, seeded['project_ids'][0]).question_limit is None
