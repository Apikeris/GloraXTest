"""Authenticated administrative workflows; evidence and history are never deleted."""
import csv
import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from flask import Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from .extensions import db
from .models import (Admin, Attempt, AttemptItem, AuditLog, Dataset, DatasetFact, Fact,
                     FactRevision, Job, Participant, Project, Question, QuestionRevision,
                     Source, aware, uid, utcnow)

bp = Blueprint('admin', __name__, url_prefix='/admin')
STATUSES = {'in_progress': 'Идёт', 'completed': 'Завершена', 'interrupted': 'Прервана'}
OUTCOMES = {'correct': 'Верно', 'wrong': 'Ошибка', 'timeout': 'Время истекло',
            'not_reached': 'Не дошёл до вопроса', 'legacy_unknown': 'Исторические данные неполны', None: 'Ожидает ответа'}
QUESTION_STATUSES = {'draft': 'Черновик', 'published': 'Опубликован', 'archived': 'Архив', 'needs_review': 'Нужна проверка'}
from .questions import CATEGORIES


@bp.before_request
def require_admin():
    if not session.get('admin_id') or not db.session.get(Admin, session['admin_id']):
        if request.path.endswith('.json') or request.is_json:
            return jsonify(error='Необходим вход администратора'), 401
        return redirect(url_for('auth.login', next=request.full_path))


def audit(action, entity_id=None, detail=None):
    db.session.add(AuditLog(admin_id=session['admin_id'], action=action,
                            entity_id=entity_id, detail=detail or {}))


@bp.app_template_filter('admin_date')
def admin_date(value):
    if not value:
        return '—'
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return value
    zone = ZoneInfo(current_app.config.get('DISPLAY_TIMEZONE', 'Europe/Moscow'))
    return aware(value).astimezone(zone).strftime('%d.%m.%Y %H:%M:%S')


def safe_url(value):
    return value if value and urlsplit(value).scheme in ('http', 'https') else None


@bp.context_processor
def admin_context():
    def page_url(page):
        args = request.args.to_dict()
        args['page'] = page
        return url_for(request.endpoint, **(request.view_args or {}), **args)
    return dict(attempt_statuses=STATUSES, outcomes=OUTCOMES,
                question_statuses=QUESTION_STATUSES, categories=CATEGORIES,
                page_url=page_url, safe_source_url=safe_url)


def integer(value, default=None, minimum=None, maximum=None):
    if value in (None, ''):
        return default
    try:
        result = int(value)
    except (ValueError, TypeError):
        raise ValueError('Введите целое число')
    if minimum is not None and result < minimum or maximum is not None and result > maximum:
        raise ValueError(f'Число должно быть в диапазоне {minimum}–{maximum}')
    return result


def project_list():
    return Project.query.order_by(Project.name).all()


def attempt_query():
    query = Attempt.query
    if request.args.get('name'):
        needle = request.args['name'].strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        query = query.filter(Attempt.full_name.ilike(f'%{needle}%', escape='\\'))
    if request.args.get('participant_id'):
        query = query.filter_by(participant_id=request.args['participant_id'])
    if request.args.get('project_id'):
        query = query.filter_by(project_id=request.args['project_id'])
    if request.args.get('status') in STATUSES:
        query = query.filter_by(status=request.args['status'])
    for key, end in [('date_from', False), ('date_to', True)]:
        if request.args.get(key):
            try:
                day = datetime.strptime(request.args[key], '%Y-%m-%d').replace(tzinfo=ZoneInfo(current_app.config.get('DISPLAY_TIMEZONE', 'Europe/Moscow')))
            except ValueError:
                abort(400, description='Некорректная дата фильтра')
            query = query.filter(Attempt.started_at < day + timedelta(days=1) if end else Attempt.started_at >= day)
    percentage = Attempt.score * 100.0 / func.nullif(Attempt.total, 0)
    try:
        if request.args.get('min_result'):
            query = query.filter(Attempt.legacy_data.is_(None)).filter(percentage >= integer(request.args['min_result'], minimum=0, maximum=100))
        if request.args.get('max_result'):
            query = query.filter(Attempt.legacy_data.is_(None)).filter(percentage <= integer(request.args['max_result'], minimum=0, maximum=100))
    except ValueError as exc:
        abort(400, description=str(exc))
    sorts = {'newest': Attempt.started_at.desc(), 'oldest': Attempt.started_at.asc(),
             'name': Attempt.full_name.asc(), 'score_desc': percentage.desc(), 'score_asc': percentage.asc()}
    return query.order_by(sorts.get(request.args.get('sort'), sorts['newest']), Attempt.id)


@bp.get('/')
def dashboard():
    from .facts import latest_dataset
    return render_template('admin.html', counts={
        'projects': Project.query.count(), 'participants': Participant.query.count(),
        'attempts': Attempt.query.count(), 'published': Question.query.filter_by(status='published').count(),
        'review': Question.query.filter(Question.status.in_(['draft', 'needs_review'])).count()},
        dataset=latest_dataset(), jobs=Job.query.order_by(Job.created_at.desc()).limit(5).all(),
        recent=Attempt.query.order_by(Attempt.started_at.desc()).limit(10).all())


@bp.get('/attempts')
def attempts():
    page = attempt_query().paginate(page=request.args.get('page', 1, type=int), per_page=30, error_out=False)
    return render_template('admin_attempts.html', page=page, projects=project_list())


def csv_cell(value):
    text = '' if value is None else str(value)
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else text


@bp.get('/attempts.csv')
def attempts_csv():
    out = io.StringIO(newline='')
    writer = csv.writer(out)
    writer.writerow(['ID попытки', 'ID участника', 'ФИО при старте', 'Проект', 'Начало (UTC)', 'Окончание (UTC)', 'Статус', 'Баллы', 'Назначено', 'Процент', 'Ошибки', 'Таймауты', 'Не дошёл', 'ID датасета'])
    for attempt in attempt_query().yield_per(100):
        counts = dict(db.session.query(AttemptItem.outcome, func.count()).filter_by(attempt_id=attempt.id).group_by(AttemptItem.outcome).all())
        writer.writerow([csv_cell(x) for x in [attempt.id, attempt.participant_id, attempt.full_name,
            attempt.project_name, aware(attempt.started_at).isoformat(), aware(attempt.finished_at).isoformat() if attempt.finished_at else '',
            STATUSES[attempt.status], attempt.score, '' if attempt.legacy_data else attempt.total, '' if attempt.legacy_data else round(attempt.score * 100 / attempt.total, 2) if attempt.total else 0,
            counts.get('wrong', 0), counts.get('timeout', 0), counts.get('not_reached', 0), attempt.dataset_id]])
    audit('attempts.export_csv', detail={'filters': request.args.to_dict()})
    db.session.commit()
    return Response('\ufeff' + out.getvalue(), content_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename=glorax-results.csv'})


@bp.get('/participants')
def participants():
    query = Participant.query
    if request.args.get('name'):
        query = query.filter(Participant.name.ilike('%' + request.args['name'].strip().replace('%', '\\%').replace('_', '\\_') + '%', escape='\\'))
    if request.args.get('participant_id'):
        query = query.filter_by(id=request.args['participant_id'])
    page = query.order_by(Participant.created_at.desc()).paginate(page=request.args.get('page', 1, type=int), per_page=30, error_out=False)
    counts = dict(db.session.query(Attempt.participant_id, func.count()).filter(Attempt.participant_id.in_([x.id for x in page.items])).group_by(Attempt.participant_id).all())
    return render_template('admin_participants.html', page=page, counts=counts)


@bp.get('/attempts/<attempt_id>')
def attempt_detail(attempt_id):
    attempt = db.get_or_404(Attempt, attempt_id)
    items = AttemptItem.query.filter_by(attempt_id=attempt.id).order_by(AttemptItem.position).all()
    return render_template('admin_attempt_detail.html', attempt=attempt, items=items)


@bp.get('/analytics')
def analytics():
    # Snapshot categories are read from immutable attempt items, not the current question bank.
    project_rows = db.session.query(Attempt.project_name, func.count(Attempt.id), func.sum(Attempt.score), func.sum(Attempt.total)).filter(Attempt.legacy_data.is_(None)).group_by(Attempt.project_name).order_by(Attempt.project_name).all()
    topics, errors = {}, {}
    for item in AttemptItem.query.yield_per(200):
        if item.outcome=='legacy_unknown': continue
        category = item.snapshot.get('category', 'unknown')
        row = topics.setdefault(category, {'total': 0, 'correct': 0, 'wrong': 0, 'timeout': 0, 'not_reached': 0})
        row['total'] += 1
        if item.outcome in row:
            row[item.outcome] += 1
        if item.outcome == 'wrong':
            text = item.snapshot.get('text', 'Формулировка не сохранена')
            errors[text] = errors.get(text, 0) + 1
    return render_template('admin_analytics.html', project_rows=project_rows, topics=topics,
                           errors=sorted(errors.items(), key=lambda pair: -pair[1])[:30])


@bp.get('/projects')
def projects():
    from .attempts import available_question_counts
    values = project_list()
    # Publication already performed deep validation. Use the grouped SQL
    # counter shared with the employee catalogue instead of revalidating each
    # question serially for every project on every admin page load.
    counts = available_question_counts(values)
    return render_template('admin_projects.html', projects=values, counts=counts)


@bp.route('/projects/<project_id>', methods=['GET', 'POST'])
def project_detail(project_id):
    project = db.get_or_404(Project, project_id)
    if request.method == 'POST':
        try:
            limit = integer(request.form.get('question_limit'), minimum=1, maximum=1000)
            distribution = json.loads(request.form.get('topic_distribution') or '{}')
            if not isinstance(distribution, dict) or any(not isinstance(k, str) or type(v) is not int or v < 0 for k, v in distribution.items()):
                raise ValueError('Распределение: JSON-объект «категория»: целое число вопросов ≥ 0')
            project.question_limit = limit
            project.topic_distribution = distribution
            project.enabled = request.form.get('enabled') == 'on'
            audit('project.settings', project.id, {'enabled': project.enabled, 'question_limit': limit, 'topic_distribution': distribution})
            db.session.commit()
            flash('Настройки проекта сохранены', 'success')
            return redirect(url_for('admin.project_detail', project_id=project.id))
        except (ValueError, json.JSONDecodeError) as exc:
            db.session.rollback()
            flash(str(exc), 'error')
    facts = db.session.query(Fact, FactRevision).outerjoin(FactRevision, Fact.current_revision_id == FactRevision.id).filter(Fact.project_id == project.id).order_by(Fact.category, Fact.key).all()
    sources = Source.query.filter_by(project_id=project.id).order_by(Source.fetched_at.desc()).limit(40).all()
    return render_template('admin_project_detail.html', project=project, facts=facts, sources=sources)


@bp.route('/projects/<project_id>/facts/new', methods=['GET', 'POST'])
@bp.route('/facts/<fact_id>/edit', methods=['GET', 'POST'])
def fact_edit(project_id=None, fact_id=None):
    fact = db.get_or_404(Fact, fact_id) if fact_id else None
    project = db.get_or_404(Project, fact.project_id if fact else project_id)
    revision = db.session.get(FactRevision, fact.current_revision_id) if fact else None
    errors = []
    if request.method == 'POST':
        from .facts import save_manual_fact
        try:
            value = json.loads(request.form.get('value', 'null'))
            scope = json.loads(request.form.get('scope') or '{}')
            conditions = json.loads(request.form.get('conditions') or '{}')
            if not isinstance(scope, dict) or not isinstance(conditions, dict):
                raise ValueError('Область применения и условия должны быть JSON-объектами')
            source_url = request.form.get('source_url', '').strip()
            if source_url and not safe_url(source_url):
                raise ValueError('Источник должен иметь адрес http:// или https://')
            evidence = request.form.get('evidence', '').strip()
            if len(evidence) < 5:
                raise ValueError('Приведите подтверждающий фрагмент или обоснование экспертной оценки')
            missing_reason = request.form.get('missing_reason', '').strip()
            if value is None and not missing_reason:
                raise ValueError('Укажите причину отсутствия значения')
            category = request.form.get('category', '').strip()
            key = request.form.get('key', '').strip()
            if not category or not key or len(key) > 150 or len(category) > 50:
                raise ValueError('Укажите категорию и ключ характеристики (не более 150 символов)')
            if fact and (key != fact.key or scope != fact.scope):
                raise ValueError('Ключ и область применения определяют идентичность факта. Для другого ключа создайте новый факт')
            expert = request.form.get('expert') == 'on'
            if not source_url and not expert and value is not None:
                raise ValueError('Для подтверждённого факта требуется URL источника')
            payload = dict(fact_id=fact.id if fact else None, category=category, key=key, value=value, scope=scope,
                unit=request.form.get('unit', '').strip() or None,
                value_type=request.form.get('value_type', 'string'), evidence=evidence,
                source_url=source_url or (request.url if expert else None), method='expert_assessment' if expert else 'manual',
                verification_status='verified' if request.form.get('verified') == 'on' and value is not None else 'needs_review',
                is_exclusive=request.form.get('is_exclusive') == 'on', missing_reason=missing_reason or None,
                conditions=conditions)
            if expert:
                payload['conditions'] = {**conditions, 'expert_assessment': True}
                payload['is_exclusive'] = request.form.get('is_exclusive') == 'on'
            new_revision = save_manual_fact(project.id, payload, session['admin_id'])
            audit('fact.create_revision', fact.id if fact else project.id, {'key': key, 'expert_assessment': expert})
            db.session.commit()
            flash('Создана новая версия факта. Ручное значение защищено от автоматической перезаписи', 'success')
            return redirect(url_for('admin.project_detail', project_id=project.id))
        except (ValueError, InvalidOperation, IntegrityError) as exc:
            db.session.rollback()
            errors = [str(exc) if not isinstance(exc, IntegrityError) else 'Конфликт версии: обновите страницу и повторите сохранение']
    history = FactRevision.query.filter_by(fact_id=fact.id).order_by(FactRevision.created_at.desc()).all() if fact else []
    return render_template('admin_fact_edit.html', project=project, fact=fact, revision=revision, history=history, errors=errors)


@bp.get('/sources/<source_id>')
def source_detail(source_id):
    source = db.get_or_404(Source, source_id)
    return render_template('admin_source.html', source=source)


@bp.get('/dataset.json')
def dataset_export():
    from .facts import export_dataset
    payload = export_dataset()
    audit('dataset.export')
    db.session.commit()
    return Response(json.dumps(payload, ensure_ascii=False, default=str, indent=2), content_type='application/json', headers={'Content-Disposition': 'attachment; filename=glorax-dataset.json'})


@bp.get('/questions')
def questions():
    query = db.session.query(Question, QuestionRevision, Project).join(Project, Question.project_id == Project.id).outerjoin(QuestionRevision, Question.current_revision_id == QuestionRevision.id)
    if request.args.get('project_id'):
        query = query.filter(Question.project_id == request.args['project_id'])
    if request.args.get('status') in QUESTION_STATUSES:
        query = query.filter(Question.status == request.args['status'])
    if request.args.get('origin') in ('generated', 'manual', 'ai_import'):
        query = query.filter(Question.origin == request.args['origin'])
    if request.args.get('category'):
        query = query.filter(QuestionRevision.category == request.args['category'])
    if request.args.get('q'):
        query = query.filter(QuestionRevision.text.ilike('%' + request.args['q'].strip().replace('%', '\\%').replace('_', '\\_') + '%', escape='\\'))
    page = query.order_by(Project.name, QuestionRevision.created_at.desc()).paginate(page=request.args.get('page', 1, type=int), per_page=30, error_out=False)
    return render_template('admin_questions.html', page=page, projects=project_list())


def editor_facts():
    from .facts import latest_dataset
    dataset = latest_dataset()
    rows = []
    if dataset:
        rows = db.session.query(FactRevision, Fact, Project).join(Fact, FactRevision.fact_id == Fact.id).join(Project, Fact.project_id == Project.id).join(DatasetFact, DatasetFact.revision_id == FactRevision.id).filter(DatasetFact.dataset_id == dataset.id, FactRevision.verification_status == 'verified').order_by(Project.name, Fact.category, Fact.key).all()
    return dataset, rows


@bp.route('/questions/new', methods=['GET', 'POST'])
@bp.route('/questions/<question_id>', methods=['GET', 'POST'])
def question_edit(question_id=None):
    from .questions import validate_revision, format_value
    question = db.get_or_404(Question, question_id) if question_id else None
    revision = db.session.get(QuestionRevision, question.current_revision_id) if question else None
    dataset, facts = editor_facts()
    errors = []
    if request.method == 'POST':
        if not dataset:
            errors = ['Нет опубликованного снимка данных. Сначала загрузите и проверьте факты']
        else:
            try:
                project_id = question.project_id if question else request.form.get('project_id')
                if not db.session.get(Project, project_id):
                    raise ValueError('Выберите существующий проект')
                text = request.form.get('text', '').strip()
                explanation = request.form.get('explanation', '').strip()
                if not 10 <= len(text) <= 500 or len(explanation) > 2000:
                    raise ValueError('Вопрос: 10–500 символов, объяснение: не более 2000')
                if question is None:
                    question = Question(id=uid(), project_id=project_id, origin='manual', status='draft')
                    db.session.add(question)
                    db.session.flush()
                options = []
                for index in range(4):
                    ref = request.form.get(f'option_{index}')
                    fact_revision = db.session.get(FactRevision, ref)
                    if not fact_revision:
                        raise ValueError(f'Вариант {index + 1}: версия факта не найдена')
                    options.append({'id': str(index), 'fact_revision_id': ref, 'text': format_value(fact_revision)})
                target = request.form.get('target_fact_revision_id')
                correct = request.form.get('correct_option_id', '0')
                new = QuestionRevision(id=uid(), question_id=question.id, dataset_id=dataset.id,
                    text=text, category=request.form.get('category', '').strip(), options=options,
                    correct_option_id=correct, target_fact_revision_id=target,
                    explanation=explanation, difficulty=request.form.get('difficulty', 'basic'),
                    tags=[t.strip() for t in request.form.get('tags', '').split(',') if t.strip()],
                    author_id=session['admin_id'], semantic_reviewed=request.form.get('semantic_reviewed') == 'on',
                    fingerprint=hashlib.sha256(json.dumps({'text': text, 'options': options, 'target': target, 'correct': correct, 'explanation': explanation}, ensure_ascii=False, sort_keys=True).encode()).hexdigest())
                errors = validate_revision(new, require_current=True, semantic_review=True)
                if errors:
                    db.session.rollback()
                else:
                    db.session.add(new)
                    db.session.flush()
                    if question.origin == 'generated': question.origin = 'manual'
                    question.current_revision_id = new.id
                    question.status = 'draft'
                    question.review_reason = None
                    audit('question.edit', question.id, {'revision_id': new.id})
                    db.session.commit()
                    flash('Новая версия сохранена как черновик. Проверьте предпросмотр перед публикацией', 'success')
                    return redirect(url_for('admin.question_edit', question_id=question.id))
            except (ValueError, IntegrityError) as exc:
                db.session.rollback()
                errors = [str(exc) if not isinstance(exc, IntegrityError) else 'Конфликт сохранения. Обновите страницу']
    return render_template('admin_question_edit.html', question=question, revision=revision,
                           projects=project_list(), dataset=dataset, facts=facts, errors=errors,
                           render_fact=format_value)


@bp.post('/questions/<question_id>/status')
def question_status(question_id):
    from .questions import validate_revision
    question = db.get_or_404(Question, question_id)
    status = request.form.get('status')
    if status not in QUESTION_STATUSES:
        abort(400)
    if status == 'published':
        revision = db.session.get(QuestionRevision, question.current_revision_id)
        if not revision:
            flash('У вопроса нет версии', 'error')
            return redirect(url_for('admin.question_edit', question_id=question.id))
        errors = validate_revision(revision, require_current=True, semantic_review=revision.semantic_reviewed)
        if question.origin != 'generated' and not revision.semantic_reviewed:
            errors.append('Подтвердите смысловую проверку в редакторе и сохраните новую версию')
        if errors:
            for error in errors:
                flash(str(error), 'error')
            return redirect(url_for('admin.question_edit', question_id=question.id))
    question.status = status
    audit('question.status', question.id, {'status': status})
    db.session.commit()
    flash('Статус вопроса изменён. Снимки прошлых попыток сохранены', 'success')
    return redirect(url_for('admin.question_edit', question_id=question.id))


@bp.route('/settings', methods=['GET', 'POST'])
def settings():
    from .facts import get_setting, set_setting
    defaults = {'show_review': False, 'price_valid_days': 7, 'fact_valid_days': 180, 'inactivity_minutes': 60, 'question_limit': None}
    if request.method == 'POST':
        try:
            values = {'show_review': request.form.get('show_review') == 'on'}
            for key in ('price_valid_days', 'fact_valid_days', 'inactivity_minutes'):
                values[key] = integer(request.form.get(key), minimum=1, maximum=3650)
                if values[key] is None:
                    raise ValueError('Заполните все сроки актуальности и бездействия')
            values['question_limit'] = integer(request.form.get('question_limit'), minimum=1, maximum=1000)
            for key, value in values.items():
                set_setting(key, value)
            audit('settings.update', detail=values)
            db.session.commit()
            flash('Настройки сохранены', 'success')
            return redirect(url_for('admin.settings'))
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'error')
    return render_template('admin_settings.html', values={k: get_setting(k, v) for k, v in defaults.items()})


@bp.get('/jobs')
def jobs():
    from .jobs import worker_diagnostics
    page = Job.query.order_by(Job.created_at.desc()).paginate(page=request.args.get('page', 1, type=int), per_page=20, error_out=False)
    return render_template('admin_jobs.html', page=page, worker=worker_diagnostics())


@bp.post('/jobs/refresh')
def refresh():
    from .jobs import enqueue_refresh
    job = enqueue_refresh()
    audit('job.enqueue', job.id)
    db.session.commit()
    flash('Задание добавлено в очередь. При повторном нажатии используется уже активное задание', 'success')
    return redirect(url_for('admin.job_detail', job_id=job.id))


@bp.post('/jobs/<job_id>/retry')
def retry_job(job_id):
    job = db.get_or_404(Job, job_id)
    if job.state not in ('failed', 'cancelled'):
        abort(400, description='Повтор доступен только для неудачного задания')
    from .jobs import enqueue_refresh
    new_job = enqueue_refresh()
    audit('job.retry', new_job.id, {'previous_job_id': job.id})
    db.session.commit()
    return redirect(url_for('admin.job_detail', job_id=new_job.id))


@bp.get('/jobs/<job_id>')
def job_detail(job_id):
    from .jobs import worker_diagnostics
    job=db.get_or_404(Job, job_id)
    return render_template('admin_job_detail.html', job=job, worker=worker_diagnostics(job))


@bp.get('/jobs/<job_id>.json')
def job_status(job_id):
    from .jobs import worker_diagnostics
    job = db.get_or_404(Job, job_id)
    return jsonify(id=job.id, state=job.state, stage=job.stage, progress=job.progress, total=job.total,
                   detail=job.detail, error=job.error, report=job.report, worker=worker_diagnostics(job),
                   heartbeat_at=job.heartbeat_at.isoformat() if job.heartbeat_at else None,
                   started_at=job.started_at.isoformat() if job.started_at else None,
                   finished_at=job.finished_at.isoformat() if job.finished_at else None)


@bp.get('/audit')
def audit_log():
    page = AuditLog.query.order_by(AuditLog.created_at.desc()).paginate(page=request.args.get('page', 1, type=int), per_page=50, error_out=False)
    admins = {a.id: a.username for a in Admin.query.all()}
    return render_template('admin_audit.html', page=page, admins=admins)


@bp.route('/ai-prompt', methods=['GET', 'POST'])
def prompt_export():
    from .importing import export_prompt
    prompt, errors = None, []
    if request.method == 'POST':
        try:
            project_id = request.form.get('project_id')
            db.get_or_404(Project, project_id)
            count = integer(request.form.get('count'), default=10, minimum=1, maximum=100)
            themes = [v.strip() for v in request.form.get('themes', '').split(',') if v.strip()]
            difficulty = request.form.get('difficulty', 'basic')
            if difficulty not in ('basic', 'intermediate', 'advanced'):
                raise ValueError('Недопустимая сложность')
            prompt = export_prompt(project_id, count, themes, difficulty)
            if not isinstance(prompt, str):
                prompt = json.dumps(prompt, ensure_ascii=False, indent=2, default=str)
            audit('ai.export_prompt', project_id, {'count': count, 'themes': themes, 'difficulty': difficulty})
            db.session.commit()
        except ValueError as exc:
            errors = [str(exc)]
    return render_template('admin_prompt.html', projects=project_list(), prompt=prompt, errors=errors)


def read_import_payload():
    uploaded = request.files.get('file')
    if uploaded and uploaded.filename:
        if not uploaded.filename.lower().endswith('.json'):
            raise ValueError('Разрешён файл .json')
        raw = uploaded.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError('Размер JSON не должен превышать 1 МиБ')
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            raise ValueError('Файл должен быть в кодировке UTF-8')
    else:
        text = request.form.get('payload', '')
        if len(text.encode()) > 1024 * 1024:
            raise ValueError('Размер JSON не должен превышать 1 МиБ')
    try:
        from .importing import parse_payload
        return parse_payload(text)
    except (ValueError, TypeError):
        raise ValueError('Некорректный JSON: проверьте кавычки, скобки и запятые')


@bp.route('/import', methods=['GET', 'POST'])
def import_questions():
    from .importing import preview_import, commit_import
    payload, preview, errors, report = None, None, [], None
    if request.method == 'POST':
        try:
            payload = read_import_payload()
            if request.form.get('action') == 'commit':
                selected = request.form.getlist('selected')
                if not selected:
                    raise ValueError('Выберите хотя бы один валидный вопрос')
                report = commit_import(payload, selected, allow_updates=request.form.get('allow_updates') == 'on', author_id=session['admin_id'])
                audit('questions.import', detail={'selected_external_ids': selected, 'allow_updates': request.form.get('allow_updates') == 'on'})
                db.session.commit()
                flash('Импорт завершён. Свободные формулировки сохранены черновиками для смысловой проверки', 'success')
            else:
                preview = preview_import(payload)
        except (ValueError, IntegrityError) as exc:
            db.session.rollback()
            errors = [str(exc) if not isinstance(exc, IntegrityError) else 'Конфликт импорта: данные были изменены. Повторите предпросмотр']
    return render_template('admin_import.html', payload=payload, preview=preview, errors=errors, report=report)
