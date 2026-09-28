"""Render worker supervisor: queue and timeout maintenance in separate processes."""

import argparse
import multiprocessing
import signal
import threading
import time

from . import create_app
from .attempts import sweep_expired
from .extensions import db
from .jobs import claim_job, recover_jobs, run_job, safe_job_error, worker_heartbeat


def maintenance(stop, interval_seconds=5):
    app = create_app()
    with app.app_context():
        while not stop.is_set():
            try:
                sweep_expired()
            except Exception as exc:
                db.session.rollback()
                app.logger.error("Timeout maintenance failed: %s", type(exc).__name__)
            finally:
                db.session.remove()
            stop.wait(interval_seconds)


def queue(stop, once=False, idle_poll_seconds=5, recovery_interval_seconds=30):
    app = create_app()
    with app.app_context():
        if db.engine.dialect.name != "postgresql":
            raise RuntimeError("Worker требует PostgreSQL.")
        heartbeat_at = recovery_at = 0
        while not stop.is_set():
            try:
                now = time.monotonic()
                if now - heartbeat_at >= 15:
                    worker_heartbeat()
                    heartbeat_at = now
                if now - recovery_at >= recovery_interval_seconds:
                    recover_jobs()
                    recovery_at = now
                job = claim_job()
                if job:
                    succeeded = run_job(*job)
                    if once and not succeeded:
                        raise RuntimeError(
                            "Задание завершилось ошибкой; проверьте сохранённый отчёт"
                        )
                if once:
                    sweep_expired()
                    break
                if not job:
                    stop.wait(idle_poll_seconds)
            except Exception as exc:
                db.session.rollback()
                app.logger.error("Worker iteration failed: %s", safe_job_error(exc))
                if once:
                    raise
                stop.wait(5)
            finally:
                db.session.remove()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Queue and timer threads in a dedicated worker process (Render Free)",
    )
    args = parser.parse_args()
    if args.once:
        queue(threading.Event(), True)
        return
    if args.compact:
        # These threads live only in the separately supervised worker executable,
        # never in Gunicorn. PostgreSQL, not a Python thread, owns job state.
        stop = threading.Event()
        children = [
            threading.Thread(target=queue, args=(stop,), name="glorax-queue", daemon=True),
            threading.Thread(target=maintenance, args=(stop,), name="glorax-timeouts", daemon=True),
        ]
    else:
        ctx = multiprocessing.get_context("spawn")
        stop = ctx.Event()
        children = [
            ctx.Process(target=queue, args=(stop,), name="glorax-queue"),
            ctx.Process(target=maintenance, args=(stop,), name="glorax-timeouts"),
        ]

    def terminate(*_):
        stop.set()

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    for child in children:
        child.start()
    failed = False
    try:
        while not stop.wait(1):
            if any(not child.is_alive() for child in children):
                failed = True
                stop.set()
    finally:
        for child in children:
            child.join(timeout=5)
        if not args.compact:
            for child in children:
                if child.is_alive():
                    child.terminate()
            for child in children:
                child.join(timeout=2)
    if failed:
        raise SystemExit(1)  # Render restarts supervisor; leases recover queued work.


if __name__ == "__main__":
    main()
