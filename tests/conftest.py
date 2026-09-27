import os
from uuid import uuid4
import pytest
from flask_migrate import upgrade
from sqlalchemy import create_engine, text
from glorax import create_app
from glorax.extensions import db
from glorax.facts import publish_collection
from glorax.models import utcnow


@pytest.fixture
def app():
    url=os.getenv('TEST_DATABASE_URL')
    if not url or not url.startswith('postgresql'):
        pytest.fail('Задайте TEST_DATABASE_URL отдельной PostgreSQL БД. Проверки SQLite не заменяют PostgreSQL.')
    engine=create_engine(url)
    schema='test_'+uuid4().hex
    with engine.begin() as conn: conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    app=create_app({'TESTING':True,'WTF_CSRF_ENABLED':False,'SQLALCHEMY_DATABASE_URI':url,'SQLALCHEMY_ENGINE_OPTIONS':{'connect_args':{'options':f'-csearch_path={schema}'},'pool_size':5,'max_overflow':3}})
    with app.app_context(): upgrade(directory='migrations')
    yield app
    with app.app_context(): db.session.remove();db.engine.dispose()
    with engine.begin() as conn: conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    engine.dispose()


@pytest.fixture
def client(app): return app.test_client()


def collection():
    now=utcnow().isoformat()
    return {'complete':True,'started_at':now,'finished_at':now,'coverage':{'test_fixture':True},'errors':[], 'projects':[
        {'key':f'test-project-{i}','canonical_url':f'https://example.org/project/{i}','name':f'Тестовый проект {i}','city':f'Город {i}','region':None,'status':'Тестовые данные',
         'facts':[{'category':'location','key':'city','value':f'Город {i}','value_type':'string','scope':{'level':'project'},'source_url':f'https://example.org/project/{i}','evidence':f'Город {i}','method':'test_fixture','verification_status':'verified','is_exclusive':True},
                  {'category':'buildings','key':'building_count','value':i+2,'value_type':'integer','scope':{'level':'project'},'source_url':f'https://example.org/project/{i}','evidence':str(i+2),'method':'test_fixture','verification_status':'verified','is_exclusive':True}],
         'sources':[],'coverage':{}} for i in range(4)]}


@pytest.fixture
def seeded(app):
    from glorax.models import Project
    with app.app_context():
        snapshot=publish_collection(collection());db.session.commit()
        yield {'dataset_id':snapshot.id,'project_ids':[p.id for p in Project.query.order_by(Project.key).all()]}
