"""One Render Free service: migrate once, then supervise web and queue processes.

No shell, background work inside Gunicorn, Redis, or persistent local files are needed.
Render may suspend all processes; PostgreSQL keeps jobs and fenced leases.
"""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent


def stop_children(children, grace_seconds=15):
    # Each child owns a process group, including Gunicorn/worker descendants.
    for child in children:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + grace_seconds
    while any(child.poll() is None for child in children) and time.monotonic() < deadline:
        time.sleep(0.1)
    for child in children:
        # A supervisor may exit before a blocked descendant; kill its group too.
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()


def serve(commands, env, prepare_command, poll_seconds=0.5):
    running = {}
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        print('Render: applying migrations before web and worker start', flush=True)
        migration = subprocess.Popen(prepare_command, cwd=ROOT, env=env, start_new_session=True)
        running['migration'] = migration
        while migration.poll() is None and not stopping:
            time.sleep(poll_seconds)
        if stopping:
            return 0
        if migration.returncode != 0:
            print('Render: database preparation failed; web and worker were not started', flush=True)
            return 1
        migration.wait()
        running.clear()
        for name, command in commands:
            if stopping:
                return 0
            child = subprocess.Popen(command, cwd=ROOT, env=env, start_new_session=True)
            running[name] = child
            print(f'Render: {name} started', flush=True)
        worker_restart_delay = 1
        while not stopping:
            for name, command in commands:
                child = running[name]
                if child.poll() is not None:
                    code = child.returncode
                    child.wait()  # Reap the completed process before replacing it.
                    if name == 'web':
                        print(f'Render: web exited ({code}); stopping service for restart', flush=True)
                        return 1
                    print(f'Render: {name} exited ({code}); web remains available; restarting worker in {worker_restart_delay}s', flush=True)
                    del running[name]
                    deadline = time.monotonic() + worker_restart_delay
                    while not stopping and time.monotonic() < deadline:
                        time.sleep(min(poll_seconds, max(0, deadline-time.monotonic())))
                    if stopping:
                        return 0
                    running[name] = subprocess.Popen(command, cwd=ROOT, env=env, start_new_session=True)
                    print(f'Render: {name} restarted', flush=True)
                    worker_restart_delay = min(worker_restart_delay * 2, 30)
            time.sleep(poll_seconds)
        return 0
    finally:
        stop_children(list(running.values()))
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main():
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')  # Render environment values take precedence.
    env = os.environ.copy()
    env.setdefault('DB_POOL_SIZE', '2')  # Migration lock + Alembic need two connections.
    env.setdefault('DB_MAX_OVERFLOW', '0')
    env.setdefault('PYTHONUNBUFFERED', '1')
    if env.get('RENDER') == 'true':
        env.setdefault('TRUST_PROXY', '1')
    port = int(env.get('PORT', '10000'))
    if not 1 <= port <= 65535:
        raise ValueError('PORT must be between 1 and 65535')
    commands = [
        ('web', [sys.executable, '-m', 'gunicorn', 'app:app', '--bind', f'0.0.0.0:{port}',
                 '--workers', '1', '--threads', '2', '--timeout', '60', '--graceful-timeout', '10',
                 '--access-logfile', '-', '--error-logfile', '-']),
        ('worker', [sys.executable, '-m', 'glorax.worker', '--compact']),
    ]
    return serve(commands, env, [sys.executable, str(ROOT / 'scripts/migrate.py'), '--enqueue-initial-refresh'])


if __name__ == '__main__':
    raise SystemExit(main())
