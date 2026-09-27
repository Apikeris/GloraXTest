"""Evidence normalization, snapshots and explicit editorial overrides."""
import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse
from flask import current_app
from .extensions import db
from .models import Dataset, DatasetFact, Fact, FactRevision, Project, Source, Setting, Question, QuestionRevision, utcnow, aware, uid


def get_setting(key,default=None):
    cache=db.session.info.setdefault('glorax_settings_cache',{})
    if key not in cache:
        row=db.session.get(Setting,key)
        cache[key]=row.value if row else default
    return cache[key]


def set_setting(key,value):
    db.session.info.setdefault('glorax_settings_cache',{})[key]=value
    row=db.session.get(Setting,key)
    if row: row.value=value
    else: db.session.add(Setting(key=key,value=value))


def latest_dataset():
    return db.session.execute(db.select(Dataset).where(Dataset.status=='published').order_by(Dataset.published_at.desc())).scalars().first()


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()


def parse_date(value):
    return aware(datetime.fromisoformat(value.replace('Z','+00:00'))) if isinstance(value,str) else aware(value) if value else utcnow()


def revision_members(dataset_id):
    return db.session.execute(db.select(FactRevision).join(DatasetFact,DatasetFact.revision_id==FactRevision.id).where(DatasetFact.dataset_id==dataset_id)).scalars().all()


def _new_snapshot(report):
    previous=latest_dataset()
    snapshot=Dataset(status='staging',report=report)
    db.session.add(snapshot);db.session.flush()
    members={r.fact_id:r.id for r in revision_members(previous.id)} if previous else {}
    return snapshot,members


def _revision(fact,snapshot,payload,source=None,author=None):
    value=payload.get('value')
    value_type=payload.get('value_type','string')
    numeric=None
    if value is not None and value_type in ('decimal','integer','number','money'):
        numeric=Decimal(str(value))
        if not numeric.is_finite(): raise ValueError('Число должно быть конечным.')
        if value_type=='integer' and numeric != numeric.to_integral_value(): raise ValueError('Требуется целое число без округления.')
        value=str(numeric) if value_type!='integer' else int(numeric)
    verified=payload.get('verification_status','needs_review')
    if verified=='verified' and (not payload.get('source_url') or not payload.get('evidence') or value is None):
        raise ValueError('Подтверждённый факт требует значения, источника и цитаты.')
    created=parse_date(payload.get('fetched_at'))
    configured=get_setting('price_valid_days',7) if fact.category=='prices' else get_setting('fact_valid_days',180)
    days=int(configured) if fact.category=='prices' else min(int(payload['valid_days']),int(configured)) if payload.get('valid_days') else int(configured)
    row=FactRevision(id=uid(),fact_id=fact.id,dataset_id=snapshot.id,value=value,numeric_value=numeric,value_type=value_type,
        unit=payload.get('unit'),source_id=source.id if source else None,source_url=payload.get('source_url'),evidence=payload.get('evidence'),
        method=payload.get('method','http'),verification_status=verified,is_exclusive=bool(payload.get('is_exclusive',False)),
        missing_reason=payload.get('missing_reason') or ('Источник не содержит значение' if value is None else None),conditions=payload.get('conditions') or {},
        created_at=created,valid_until=created+timedelta(days=int(days)),author_id=author)
    db.session.add(row);return row


def _invalidate_questions(changed_ids):
    for q in db.session.execute(db.select(Question).where(Question.status!='archived')).scalars():
        r=db.session.get(QuestionRevision,q.current_revision_id) if q.current_revision_id else None
        if r and any(o.get('fact_revision_id') in changed_ids for o in r.options):
            q.status='needs_review';q.review_reason='Источники изменились; требуется новая версия и проверка.'


def publish_collection(collection,job_guard=None):
    """Caller owns transaction; a failed traversal cannot replace published data."""
    if not collection.get('complete') or not collection.get('projects'):
        raise ValueError('Обход каталога неполон: рабочий снимок сохранён.')
    # PostgreSQL advisory lock serializes collectors and editors without external services.
    if db.engine.dialect.name=='postgresql': db.session.execute(db.text('SELECT pg_advisory_xact_lock(73192041)'))
    if job_guard: job_guard()
    snapshot,members=_new_snapshot({'coverage':collection.get('coverage',{}),'errors':collection.get('errors',[]),'started_at':collection.get('started_at'),'finished_at':collection.get('finished_at')})
    changes=[];new=[];conflicts=[];replaced=set();fact_heads={}
    projects_by_key={p.key:p for p in db.session.execute(db.select(Project)).scalars()}
    facts_by_identity={}
    current_revision_ids=set()
    for fact in db.session.execute(db.select(Fact)).scalars():
        facts_by_identity[(fact.project_id,fact.key,fact.scope_key)]=fact
        if fact.current_revision_id: current_revision_ids.add(fact.current_revision_id)
    current_revisions={r.id:r for r in db.session.execute(db.select(FactRevision).where(FactRevision.id.in_(current_revision_ids))).scalars()} if current_revision_ids else {}
    for data in collection['projects']:
        project=projects_by_key.get(data['key'])
        if not project:
            project=Project(id=uid(),key=data['key'],name=data['name']);db.session.add(project);db.session.flush();projects_by_key[project.key]=project;new.append(project.name)
        elif any(getattr(project,k)!=data.get(k) for k in ('name','city','region','status')): changes.append(project.name)
        for field in ('name','canonical_url','city','region','status'):
            if data.get(field) is not None: setattr(project,field,data[field])
        project.coverage=data.get('coverage',{});project.last_seen_at=parse_date(collection.get('finished_at'))
        sources={}
        for raw in data.get('sources',[]):
            content=raw.get('content','')
            if not isinstance(content,str): content=json.dumps(content,ensure_ascii=False)
            source=Source(id=uid(),dataset_id=snapshot.id,project_id=project.id,url=raw['url'],content=content,content_type=raw.get('content_type','text/html'),checksum=hashlib.sha256(content.encode()).hexdigest(),fetched_at=parse_date(raw.get('fetched_at')))
            db.session.add(source);sources[source.url]=source
        # Revisions refer to source IDs immediately. Persist this project's
        # small source batch before queueing its many fact revisions.
        if sources: db.session.flush()
        for candidate in data.get('facts',[]):
            scope=candidate.get('scope') or {};scope_key=digest(scope)
            identity=(project.id,candidate['key'],scope_key)
            fact=facts_by_identity.get(identity)
            if not fact:
                fact=Fact(id=uid(),project_id=project.id,key=candidate['key'],category=candidate['category'],scope=scope,scope_key=scope_key)
                db.session.add(fact);facts_by_identity[identity]=fact
            previous_id=fact_heads.get(fact.id,(None,fact.current_revision_id))[1]
            old=current_revisions.get(previous_id) if previous_id else None
            source=sources.get(candidate.get('source_url'))
            candidate={**candidate,'fetched_at':source.fetched_at if source else collection.get('finished_at')}
            if fact.manual_override:
                if old and old.value!=candidate.get('value'):
                    candidate['verification_status']='needs_review'
                    r=_revision(fact,snapshot,candidate,sources.get(candidate.get('source_url')))
                    conflicts.append({'fact_id':fact.id,'revision_id':r.id,'reason':'Конфликт с ручной версией; ручная версия сохранена.'})
                continue
            r=_revision(fact,snapshot,candidate,sources.get(candidate.get('source_url')))
            if old and old.verification_status=='verified' and r.verification_status!='verified':
                if r.value is not None:
                    fact.review_pending=True;replaced.add(old.id)
                conflicts.append({'fact_id':fact.id,'revision_id':r.id,'reason':'Новый кандидат не подтверждён; сохранён старый факт с исходной датой.'})
                continue
            if old: replaced.add(old.id)
            fact_heads[fact.id]=(fact,r.id);fact.review_pending=False;members[fact.id]=r.id;current_revisions[r.id]=r
    # First persist fact/revision/source rows in batches, with the old fact heads
    # still pointing at published versions. The head FK is intentionally immediate.
    db.session.flush()
    for fact,rid in fact_heads.values(): fact.current_revision_id=rid
    _invalidate_questions(replaced)
    for revision_id in members.values(): db.session.add(DatasetFact(dataset_id=snapshot.id,revision_id=revision_id))
    db.session.flush()
    snapshot.status='published';snapshot.published_at=utcnow()
    from .questions import generate_questions
    generation=generate_questions(snapshot.id)
    snapshot.report={**snapshot.report,'new_projects':new,'changed_projects':changes,'conflicts':conflicts,'generation':generation}
    if job_guard: job_guard()
    return snapshot


def save_manual_fact(project_id,payload,author_id):
    if not author_id: raise ValueError('Не указан автор.')
    if not db.session.get(Project,project_id): raise ValueError('Проект не найден.')
    if urlparse(payload.get('source_url','')).scheme not in ('http','https'): raise ValueError('Источник должен быть HTTP(S) ссылкой.')
    if payload.get('verification_status') not in ('verified','needs_review'): raise ValueError('Недопустимый статус проверки.')
    if not payload.get('key') or not payload.get('category') or not payload.get('evidence'): raise ValueError('Укажите ключ, категорию и обоснование.')
    if db.engine.dialect.name=='postgresql': db.session.execute(db.text('SELECT pg_advisory_xact_lock(73192041)'))
    snapshot,members=_new_snapshot({'manual_edit':True,'author_id':author_id})
    scope=payload.get('scope') or {}
    fact=db.session.get(Fact,payload.get('fact_id')) if payload.get('fact_id') else db.session.execute(db.select(Fact).where(Fact.project_id==project_id,Fact.key==payload['key'],Fact.scope_key==digest(scope))).scalar_one_or_none()
    if fact and fact.project_id!=project_id: raise ValueError('Факт другого проекта.')
    if not fact:
        fact=Fact(project_id=project_id,key=payload['key'],category=payload['category'],scope=scope,scope_key=digest(scope));db.session.add(fact);db.session.flush()
    payload={**payload,'method':payload.get('method') or 'manual'}
    r=_revision(fact,snapshot,payload,author=author_id)
    db.session.flush()
    if fact.current_revision_id: _invalidate_questions({fact.current_revision_id})
    fact.current_revision_id=r.id;fact.manual_override=True;fact.review_pending=False;members[fact.id]=r.id
    for rid in members.values(): db.session.add(DatasetFact(dataset_id=snapshot.id,revision_id=rid))
    snapshot.status='published';snapshot.published_at=utcnow();db.session.flush()
    return r


def export_dataset():
    snapshot=latest_dataset()
    if not snapshot: return {'dataset_version':None,'projects':[],'facts':[]}
    facts=[]
    for r in revision_members(snapshot.id):
        f=db.session.get(Fact,r.fact_id)
        facts.append({'id':f.id,'revision_id':r.id,'project_id':f.project_id,'category':f.category,'key':f.key,'scope':f.scope,'value':r.value,'value_type':r.value_type,'unit':r.unit,'source_url':r.source_url,'evidence':r.evidence,'method':r.method,'verification_status':r.verification_status,'created_at':r.created_at.isoformat(),'valid_until':r.valid_until.isoformat() if r.valid_until else None,'conditions':r.conditions,'missing_reason':r.missing_reason})
    return {'dataset_version':snapshot.id,'published_at':snapshot.published_at.isoformat(),'coverage':snapshot.report,'projects':[{'id':p.id,'key':p.key,'name':p.name,'city':p.city,'region':p.region,'status':p.status,'canonical_url':p.canonical_url,'coverage':p.coverage} for p in db.session.execute(db.select(Project)).scalars()],'facts':facts}
