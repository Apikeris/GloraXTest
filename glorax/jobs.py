"""PostgreSQL queue: durable state, unique active job, leases and fencing."""
from datetime import timedelta
from sqlalchemy.exc import IntegrityError
from flask import current_app
from .extensions import db
from .models import Job, utcnow, uid


def enqueue_refresh():
    existing=db.session.execute(db.select(Job).where(Job.active_key=='refresh')).scalar_one_or_none()
    if existing: return existing
    job=Job(kind='refresh',active_key='refresh',state='queued')
    db.session.add(job)
    try: db.session.commit()
    except IntegrityError:
        db.session.rollback();job=db.session.execute(db.select(Job).where(Job.active_key=='refresh')).scalar_one()
    return job


def recover_jobs():
    cutoff=utcnow()-timedelta(seconds=current_app.config.get('WORKER_LEASE_SECONDS',600))
    jobs=db.session.execute(db.select(Job).where(Job.state=='running',Job.heartbeat_at<cutoff).with_for_update(skip_locked=True)).scalars().all()
    for job in jobs:
        job.lease_token=None
        if job.attempts>=current_app.config.get('WORKER_MAX_ATTEMPTS',3):
            job.state='failed';job.active_key=None;job.finished_at=utcnow();job.error='Worker потерял соединение; исчерпаны повторы.'
        else:
            job.state='queued';job.stage='Восстановление после остановки';job.available_at=utcnow()
    db.session.commit();return len(jobs)


def claim_job():
    job=db.session.execute(db.select(Job).where(Job.state=='queued',Job.available_at<=utcnow()).order_by(Job.created_at).with_for_update(skip_locked=True).limit(1)).scalar_one_or_none()
    if not job: db.session.rollback();return None
    job.state='running';job.started_at=utcnow();job.heartbeat_at=utcnow();job.lease_token=uid();job.attempts+=1;job.stage='Обнаружение проектов'
    result=(job.id,job.lease_token);db.session.commit();return result


def run_job(job_id,token,collector=None):
    from .parser import scrape
    from .facts import publish_collection
    collector=collector or scrape
    def guard():
        # Row lock is held during publication; an expired worker cannot publish.
        job=db.session.execute(db.select(Job).where(Job.id==job_id).execution_options(populate_existing=True).with_for_update()).scalar_one()
        if job.state!='running' or job.lease_token!=token: raise RuntimeError('Задание уже передано другому worker.')
        return job
    def progress(stage,done=0,total=0,detail=''):
        job=guard();job.stage={"discovery":"Обнаружение проектов","collection":"Сбор","normalization":"Нормализация"}.get(str(stage),str(stage))[:80];job.progress=int(done);job.total=int(total);job.detail=str(detail)[:2000];job.heartbeat_at=utcnow();db.session.commit()
    try:
        collection=collector(progress=progress)
        if not collection.get('complete'):
            job=guard();job.report={'coverage':collection.get('coverage',{}),'errors':collection.get('errors',[])};db.session.commit()
        progress('Проверка и публикация',0,1)
        job=guard()
        snapshot=publish_collection(collection,job_guard=guard)
        job=guard();job.state='succeeded';job.active_key=None;job.finished_at=utcnow();job.stage='Готово';job.progress=job.total=1;job.report={'dataset_id':snapshot.id,**snapshot.report};job.error=None
        db.session.commit();return True
    except Exception as exc:
        db.session.rollback()
        job=db.session.execute(db.select(Job).where(Job.id==job_id).with_for_update()).scalar_one_or_none()
        if job and job.lease_token==token and job.state=='running':
            # Do not expose exception messages from drivers (may contain credentials).
            from .parser import SourceError
            safe=str(exc)[:2000] if isinstance(exc,(ValueError,SourceError)) else 'Сбой обработки. Тип: '+type(exc).__name__
            job.error=safe;job.report={**job.report,'last_error':safe}
            if job.attempts<current_app.config.get('WORKER_MAX_ATTEMPTS',3):
                job.state='queued';job.stage='Ожидание повторной попытки';job.available_at=utcnow()+timedelta(seconds=30*job.attempts);job.lease_token=None
            else: job.state='failed';job.active_key=None;job.finished_at=utcnow();job.stage='Ошибка'
            db.session.commit()
        else: db.session.rollback()
        return False
