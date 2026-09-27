import os
import subprocess
import sys
import time
from datetime import timedelta

from sqlalchemy.exc import OperationalError

from scripts.migrate import prepare_database
from scripts import gunicorn_conf, start_render
from glorax.extensions import db
from glorax.jobs import enqueue_refresh, safe_job_error, worker_diagnostics, worker_heartbeat
from glorax.models import Job, Setting, utcnow


def test_startup_migrates_and_enqueues_once_without_resetting_jobs(app):
    prepare_database(app, enqueue_initial=True)
    prepare_database(app, enqueue_initial=True)
    with app.app_context():
        job=Job.query.one()
        assert job.state=='queued'
        job.state='failed';job.active_key=None;job.attempts=3
        db.session.commit()
    prepare_database(app, enqueue_initial=True)
    with app.app_context():
        assert Job.query.count()==1
        assert Job.query.one().state=='failed'


def test_existing_dataset_does_not_schedule_collection_on_start(app,seeded):
    prepare_database(app, enqueue_initial=True)
    with app.app_context():
        assert Job.query.count()==0


def test_queue_health_and_safe_database_error(app):
    with app.app_context():
        assert not worker_diagnostics()['online']
        worker_heartbeat()
        assert worker_diagnostics()['online']
        row=db.session.get(Setting,'worker_queue_heartbeat')
        row.value=(utcnow()-timedelta(minutes=2)).isoformat();db.session.commit()
        job=enqueue_refresh()
        assert 'start_render.py' in worker_diagnostics(job)['message']
    class DriverError(Exception):
        sqlstate='53300'
    exc=OperationalError('SQL with secret',{'password':'do-not-show'},DriverError('secret driver text'))
    message=safe_job_error(exc)
    assert '53300' in message and 'лимит' in message
    assert 'secret' not in message and 'do-not-show' not in message


def command(code):
    return [sys.executable,'-c',code]


def test_migration_failure_prevents_gunicorn_exec():
    executed=[]
    result=start_render.prepare_and_exec(os.environ.copy(),command('raise SystemExit(2)'),command('raise AssertionError'),exec_fn=lambda *args: executed.append(args))
    assert result==2 and not executed


def test_migration_completes_before_gunicorn_exec(tmp_path):
    marker=tmp_path/'migrated'
    executed=[]
    prepare=command(f'from pathlib import Path; Path({str(marker)!r}).write_text("done")')
    web=command('pass')
    def fake_exec(program,args,env):
        assert marker.read_text()=='done';executed.append((program,args,env))
        raise RuntimeError('exec called')
    try: start_render.prepare_and_exec(os.environ.copy(),prepare,web,exec_fn=fake_exec)
    except RuntimeError as exc: assert str(exc)=='exec called'
    assert executed and executed[0][1]==web


def test_worker_failure_restarts_without_stopping_gunicorn(tmp_path):
    starts=tmp_path/'starts'
    command_line=command(f'''import time
from pathlib import Path
p=Path({str(starts)!r}); n=int(p.read_text()) if p.exists() else 0; p.write_text(str(n+1))
if n==0: raise SystemExit(3)
time.sleep(60)''')
    supervisor=gunicorn_conf.WorkerSupervisor(command_line)
    supervisor.start()
    deadline=time.monotonic()+5
    while time.monotonic()<deadline and (not starts.exists() or starts.read_text()!='2'): time.sleep(0.02)
    assert starts.read_text()=='2'
    supervisor.stop(grace_seconds=1)
    assert supervisor.child.poll() is not None
