import os
import subprocess
import sys
from datetime import timedelta

from sqlalchemy.exc import OperationalError

from scripts.migrate import prepare_database
from scripts import start_render
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


def capture_children(monkeypatch):
    real_popen=subprocess.Popen
    children=[]
    def popen(*args,**kwargs):
        child=real_popen(*args,**kwargs);children.append(child);return child
    monkeypatch.setattr(start_render.subprocess,'Popen',popen)
    return children


def command(code):
    return [sys.executable,'-c',code]


def test_migration_failure_prevents_web_and_worker(monkeypatch):
    children=capture_children(monkeypatch)
    assert start_render.serve([('web',command('raise AssertionError'))],os.environ.copy(),command('raise SystemExit(2)'),poll_seconds=0.01)==1
    assert len(children)==1 and children[0].returncode==2


def test_migration_before_children_and_worker_failure_restarts_only_worker(monkeypatch,tmp_path):
    marker=tmp_path/'migrated'
    worker_starts=tmp_path/'worker-starts'
    children=capture_children(monkeypatch)
    prepare=command(f'from pathlib import Path; Path({str(marker)!r}).write_text("done")')
    web=command(f'''import os,time
from pathlib import Path
assert Path({str(marker)!r}).exists()
deadline=time.monotonic()+10
while time.monotonic()<deadline:
    path=Path({str(worker_starts)!r})
    if path.exists() and path.read_text() == "2":
        os.kill(os.getppid(), __import__('signal').SIGTERM)
        break
    time.sleep(0.01)
else:
    raise SystemExit("worker was not restarted")
time.sleep(60)''')
    worker=command(f'''import sys,time
from pathlib import Path
path=Path({str(worker_starts)!r})
starts=int(path.read_text()) if path.exists() else 0
path.write_text(str(starts+1))
if starts == 0: raise SystemExit(3)
time.sleep(60)''')
    assert start_render.serve([('web',web),('worker',worker)],os.environ.copy(),prepare,poll_seconds=0.01)==0
    assert len(children)==4
    assert children[1].returncode is not None
    assert children[2].returncode==3
    assert children[3].returncode is not None


def test_render_stop_signal_stops_both_services(monkeypatch):
    children=capture_children(monkeypatch)
    web=command('import time; time.sleep(60)')
    worker=command('import os,signal,time; time.sleep(0.1); os.kill(os.getppid(),signal.SIGTERM); time.sleep(60)')
    assert start_render.serve([('web',web),('worker',worker)],os.environ.copy(),command('pass'),poll_seconds=0.01)==0
    assert len(children)==3 and all(p.poll() is not None for p in children)
