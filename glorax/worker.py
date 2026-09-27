"""Render worker supervisor: queue and timeout maintenance in separate processes."""
import argparse
import multiprocessing
import signal
import time
from . import create_app
from .extensions import db
from .jobs import recover_jobs,claim_job,run_job
from .attempts import sweep_expired


def maintenance(stop):
    app=create_app()
    with app.app_context():
        while not stop.is_set():
            try: sweep_expired()
            except Exception as exc:
                db.session.rollback();app.logger.error('Timeout maintenance failed: %s',type(exc).__name__)
            finally: db.session.remove()
            stop.wait(1)


def queue(stop,once=False):
    app=create_app()
    with app.app_context():
        if db.engine.dialect.name!='postgresql': raise RuntimeError('Worker требует PostgreSQL.')
        while not stop.is_set():
            try:
                recover_jobs();job=claim_job()
                if job:
                    succeeded=run_job(*job)
                    if once and not succeeded: raise RuntimeError("Задание завершилось ошибкой; проверьте сохранённый отчёт")
                if once: sweep_expired();break
                if not job: stop.wait(1)
            except Exception as exc:
                db.session.rollback();app.logger.error('Worker iteration failed: %s',type(exc).__name__)
                if once: raise
                stop.wait(5)
            finally: db.session.remove()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--once',action='store_true');args=parser.parse_args()
    ctx=multiprocessing.get_context('spawn');stop=ctx.Event()
    if args.once: queue(stop,True);return
    children=[ctx.Process(target=queue,args=(stop,),name='glorax-queue'),ctx.Process(target=maintenance,args=(stop,),name='glorax-timeouts')]
    def terminate(*_): stop.set()
    signal.signal(signal.SIGTERM,terminate);signal.signal(signal.SIGINT,terminate)
    for child in children: child.start()
    failed=False
    try:
        while not stop.wait(1):
            if any(not child.is_alive() for child in children):
                failed=True;stop.set()
    finally:
        for child in children: child.join(timeout=5)
        for child in children:
            if child.is_alive(): child.terminate()
        for child in children: child.join(timeout=2)
    if failed: raise SystemExit(1)  # Render restarts supervisor; leases recover queued work.

if __name__=='__main__': main()
