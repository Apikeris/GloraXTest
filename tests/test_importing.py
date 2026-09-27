import copy
import pytest
from glorax.extensions import db
from glorax.models import Project,Question,QuestionRevision
from glorax.importing import preview_import,commit_import,parse_payload,export_prompt


def payload_from_generated():
    q=Question.query.first();r=db.session.get(QuestionRevision,q.current_revision_id);p=db.session.get(Project,q.project_id)
    return {'schema_version':'1.0','dataset_version':r.dataset_id,'questions':[{'external_id':'test-import-1','project_key':p.key,'type':'single_choice','category':r.category,'text':r.text,'target_fact_revision_id':r.target_fact_revision_id,'options':[{'id':o['id'],'fact_revision_id':o['fact_revision_id']} for o in r.options],'correct_option_id':r.correct_option_id,'explanation':'Проверяемая учебная формулировка','difficulty':'basic','tags':['тест']} ]}


def test_import_draft_repeat_conflict_explicit_update(app,seeded):
    with app.app_context():
        data=payload_from_generated();preview=preview_import(data);assert preview['valid']
        assert commit_import(data,['test-import-1'])['created']==1
        q=Question.query.filter_by(external_id='test-import-1').one();old=q.current_revision_id
        assert q.status=='draft' and q.origin=='ai_import'
        assert commit_import(data,['test-import-1'])['unchanged']==1
        data['questions'][0]['text']+=' Проверка.'
        assert preview_import(data)['questions'][0]['action']=='conflict'
        with pytest.raises(ValueError,match='Конфликт'):commit_import(data,['test-import-1'])
        assert commit_import(data,['test-import-1'],allow_updates=True)['updated']==1
        assert q.current_revision_id!=old and db.session.get(QuestionRevision,old)


@pytest.mark.parametrize('change',['unknown','key','duplicate','dataset','project'])
def test_bad_import_specific_errors(app,seeded,change):
    with app.app_context():
        data=payload_from_generated();q=data['questions'][0]
        if change=='unknown': q['options'][1]['fact_revision_id']='unknown'
        if change=='key': q['correct_option_id']='missing'
        if change=='duplicate': q['options'][1]=copy.deepcopy(q['options'][0])
        if change=='dataset':data['dataset_version']='unknown'
        if change=='project':q['project_key']='unknown'
        preview=preview_import(data);assert not preview['valid']
        errors=preview['errors']+[e for row in preview['questions'] for e in row['errors']]
        assert errors and all(e['path'] and e['message'] for e in errors)


def test_invalid_json_and_prompt_no_personal_data(app,seeded):
    for text in ['{"schema_version":1,"schema_version":2}','{"x":NaN}','a'*(1024*1024+1)]:
        with pytest.raises(ValueError):parse_payload(text)
    with app.app_context():
        prompt=export_prompt(seeded['project_ids'][0],10,[],'basic')
        assert seeded['dataset_id'] in prompt and 'JSON Schema' in prompt
        assert 'employee_code' not in prompt and 'full_name' not in prompt


def test_selected_batch_atomic_on_invalid_record(app,seeded):
    with app.app_context():
        data=payload_from_generated();bad=copy.deepcopy(data['questions'][0]);bad['external_id']='bad-second';bad['options'][0]['fact_revision_id']='unknown';data['questions'].append(bad)
        before=Question.query.count()
        with pytest.raises(ValueError):commit_import(data,['test-import-1','bad-second'])
        assert Question.query.count()==before
        assert commit_import(data,['test-import-1'])['created']==1
