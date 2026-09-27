import hashlib
import hmac
import random
import secrets
import time
from datetime import timedelta
from flask import Blueprint, Response, abort, current_app, jsonify, redirect, render_template, request, session, url_for, flash
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.dialects.postgresql import insert
from .extensions import db
from .models import Project, Participant, Attempt, AttemptItem, QuestionRevision, uid, utcnow, aware
from .facts import get_setting, latest_dataset

bp=Blueprint('public',__name__)
rng=random.SystemRandom()


def server_now():
    if db.engine.dialect.name=='postgresql':
        return db.session.execute(db.select(db.func.clock_timestamp())).scalar_one()
    return utcnow()


def session_hash():
    if 'participant_session' not in session: session['participant_session']=secrets.token_urlsafe(32)
    return hashlib.sha256(session['participant_session'].encode()).hexdigest()


def _apply_question_settings(project, questions, shuffle=True):
    questions = list(questions)
    if shuffle: rng.shuffle(questions)
    distribution = project.topic_distribution or {}
    if distribution:
        buckets = {key: [] for key in distribution}
        for question in questions:
            revision = db.session.get(QuestionRevision, question.current_revision_id)
            if revision and revision.category in buckets:
                buckets[revision.category].append(question)
        questions = [question for key, items in buckets.items() for question in items[:max(0, int(distribution[key]))]]
        if shuffle: rng.shuffle(questions)
    limit = project.question_limit or get_setting('question_limit', None)
    return questions[:int(limit)] if limit else questions


def select_questions(project, shuffle=True):
    from .questions import eligible_questions
    questions = eligible_questions(project) if project.enabled else []
    return _apply_question_settings(project, questions, shuffle)


def available_question_counts(projects):
    """Calculate all public catalogue counts without one validation query set per card."""
    from .questions import eligible_questions_for_projects
    eligible = eligible_questions_for_projects(projects)
    return {project.id: len(_apply_question_settings(project, eligible.get(project.id, []), shuffle=False)) for project in projects}


def owned_attempt(attempt_id):
    attempt=db.session.execute(db.select(Attempt).where(Attempt.id==attempt_id).with_for_update()).scalar_one_or_none()
    if not attempt or not hmac.compare_digest(attempt.session_hash,session_hash()): abort(404)
    return attempt


def items_for(attempt):
    return db.session.execute(db.select(AttemptItem).where(AttemptItem.attempt_id==attempt.id).order_by(AttemptItem.position)).scalars().all()


def expire_attempt(attempt,now=None):
    now=now or server_now()
    if attempt.status!='in_progress': return
    items=items_for(attempt)
    for item in items:
        if item.outcome is None and item.deadline and aware(item.deadline)<=now:
            item.outcome='timeout';item.accepted_at=aware(item.deadline);item.response_ms=20000
    if all(i.outcome is not None for i in items):
        attempt.status='completed';attempt.finished_at=now
    elif aware(attempt.last_activity_at)+timedelta(minutes=int(get_setting('inactivity_minutes',60)))<=now:
        for item in items:
            if item.outcome is None:
                item.outcome='not_reached';item.accepted_at=now
        attempt.status='interrupted';attempt.finished_at=now


def sweep_expired():
    now=server_now()
    attempts=db.session.execute(db.select(Attempt).where(Attempt.status=='in_progress').with_for_update(skip_locked=True)).scalars().all()
    for attempt in attempts: expire_attempt(attempt,now)
    db.session.commit()
    return len(attempts)


def result_data(attempt):
    items=items_for(attempt)
    return {'score':attempt.score,'total':attempt.total,'percent':round(attempt.score*100/attempt.total,1) if attempt.total else 0,
        'errors':sum(i.outcome=='wrong' for i in items),'timeouts':sum(i.outcome=='timeout' for i in items),'not_reached':sum(i.outcome=='not_reached' for i in items),'status':attempt.status}


@bp.get('/')
def index():
    # Render's port detector issues HEAD /. Do not make availability depend on
    # PostgreSQL (the normal GET below performs several catalogue queries).
    if request.method == 'HEAD':
        return Response(status=200)
    stage_started = time.monotonic()
    try:
        current_app.logger.warning('Catalogue request started')
        projects = list(db.session.execute(db.select(Project).order_by(Project.name)).scalars())
        current_app.logger.warning('Catalogue stage=projects count=%d seconds=%.3f', len(projects), time.monotonic() - stage_started)
        stage_started = time.monotonic()
        counts = available_question_counts(projects)
        current_app.logger.warning('Catalogue stage=question_counts count=%d seconds=%.3f', sum(counts.values()), time.monotonic() - stage_started)
        stage_started = time.monotonic()
        cards=[]
        for project in projects:
            count = counts[project.id]
            cards.append({'project':project,'count':count,'reason': 'Проект отключён администратором.' if not project.enabled else 'Нет актуальных опубликованных вопросов: данные ожидают проверки или недостаточно однозначных вариантов.' if not count else None})
        dataset = latest_dataset()
        current_app.logger.warning('Catalogue stage=dataset seconds=%.3f', time.monotonic() - stage_started)
        return render_template('index.html',cards=cards,dataset=dataset)
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error('Catalogue database query failed: %s; pool=%s', type(exc).__name__, db.engine.pool.status())
        return render_template('error.html', message='Не удалось подключиться к базе проектов. Попробуйте обновить страницу через минуту.'), 503


@bp.post('/start_test')
def start_test():
    name=' '.join(request.form.get('full_name',request.form.get('username','')).split())
    code=request.form.get('employee_code','').strip()
    if not name or len(name)>200 or len(code)>100: abort(400)
    if db.engine.dialect.name=='postgresql': db.session.execute(db.text('SELECT pg_advisory_xact_lock_shared(73192041)'))
    project=db.session.get(Project,request.form.get('project_id'))
    if not project: abort(400)
    questions=select_questions(project)
    expected=request.form.get('expected_count')
    if expected and (not expected.isdigit() or int(expected)!=len(questions)):
        flash('Число доступных вопросов изменилось. Проверьте обновлённый каталог.','error');return redirect('/')
    if not questions:
        flash('Тест пока недоступен: нет пригодных опубликованных вопросов.','error');return redirect('/')
    code_hash=hmac.new(current_app.secret_key.encode(),('employee:'+code).encode(),hashlib.sha256).hexdigest() if code else None
    participant=None
    if code_hash and db.engine.dialect.name=='postgresql':
        db.session.execute(insert(Participant).values(id=uid(),employee_code_hash=code_hash,name=name,created_at=utcnow()).on_conflict_do_nothing(index_elements=['employee_code_hash']))
    if code_hash: participant=db.session.execute(db.select(Participant).where(Participant.employee_code_hash==code_hash)).scalar_one_or_none()
    if not participant:
        participant=Participant(name=name,employee_code_hash=code_hash);db.session.add(participant);db.session.flush()
    dataset=latest_dataset()
    attempt=Attempt(participant_id=participant.id,full_name=name,project_id=project.id,project_name=project.name,session_hash=session_hash(),dataset_id=dataset.id if dataset else None,total=len(questions),settings={'seconds_per_question':20,'question_limit':project.question_limit or get_setting('question_limit'),'topic_distribution':project.topic_distribution,'show_review':bool(get_setting('show_review',False))})
    db.session.add(attempt);db.session.flush()
    for position,q in enumerate(questions,1):
        revision=db.session.get(QuestionRevision,q.current_revision_id)
        options=[];correct=None
        for option in revision.options:
            opaque=uid()
            options.append({'id':opaque,'text':option['text'],'fact_revision_id':option['fact_revision_id']})
            if option['id']==revision.correct_option_id: correct=opaque
        rng.shuffle(options)
        snapshot={'text':revision.text,'category':revision.category,'options':options,'correct_option_id':correct,'explanation':revision.explanation,'dataset_id':revision.dataset_id,'target_fact_revision_id':revision.target_fact_revision_id,'question_id':q.id,'origin':q.origin}
        db.session.add(AttemptItem(attempt_id=attempt.id,position=position,question_revision_id=revision.id,snapshot=snapshot))
    db.session.commit()
    return redirect(url_for('public.test_page',attempt_id=attempt.id))


@bp.get('/test/<attempt_id>')
def test_page(attempt_id):
    attempt=owned_attempt(attempt_id);expire_attempt(attempt);db.session.commit()
    if attempt.status!='in_progress': return redirect(url_for('public.result',attempt_id=attempt.id))
    return render_template('test.html',attempt=attempt)


@bp.get('/api/attempts/<attempt_id>/current')
def current_question(attempt_id):
    attempt=owned_attempt(attempt_id);now=server_now();expire_attempt(attempt,now)
    if attempt.status!='in_progress':
        db.session.commit();return jsonify(status=attempt.status,result_url=url_for('public.result',attempt_id=attempt.id))
    item=next(i for i in items_for(attempt) if i.outcome is None)
    if item.opened_at is None: item.opened_at=now;item.deadline=now+timedelta(seconds=20)
    attempt.last_activity_at=now
    data={'status':'in_progress','id':item.id,'text':item.snapshot['text'],'category':item.snapshot['category'],'options':[{'id':o['id'],'text':o['text']} for o in item.snapshot['options']],'number':item.position,'total':attempt.total,'deadline':aware(item.deadline).isoformat(),'server_now':now.isoformat()}
    db.session.commit();return jsonify(data)


@bp.post('/api/attempts/<attempt_id>/answer')
def answer(attempt_id):
    data=request.get_json(silent=True) or {}
    attempt=owned_attempt(attempt_id)
    now=server_now();expire_attempt(attempt,now)
    item=db.session.get(AttemptItem,data.get('question_id'))
    if not item or item.attempt_id!=attempt.id: db.session.commit();abort(400)
    if item.outcome is not None:
        db.session.commit()
        if item.outcome in ('correct','wrong'): return jsonify(accepted=True,duplicate=True)
        return jsonify(accepted=False,error='Время ответа истекло.',expired=True),409
    if attempt.status!='in_progress' or not item.opened_at:
        db.session.commit();abort(409)
    if any(i.position<item.position and i.outcome is None for i in items_for(attempt)):
        db.session.commit();abort(409)
    option_id=data.get('option_id')
    if option_id not in {o['id'] for o in item.snapshot['options']}: db.session.commit();abort(400)
    item.selected_option_id=option_id;item.accepted_at=now
    item.response_ms=max(0,int((now-aware(item.opened_at)).total_seconds()*1000))
    item.outcome='correct' if option_id==item.snapshot['correct_option_id'] else 'wrong'
    if item.outcome=='correct': attempt.score+=1
    attempt.last_activity_at=now
    if all(i.outcome is not None for i in items_for(attempt)):
        attempt.status='completed';attempt.finished_at=now
    db.session.commit();return jsonify(accepted=True)


@bp.get('/result/<attempt_id>')
def result(attempt_id):
    attempt=owned_attempt(attempt_id);expire_attempt(attempt);db.session.commit()
    if attempt.status=='in_progress': return redirect(url_for('public.test_page',attempt_id=attempt.id))
    return render_template('result.html',attempt=attempt,result=result_data(attempt),items=items_for(attempt) if attempt.settings.get('show_review') else None)
