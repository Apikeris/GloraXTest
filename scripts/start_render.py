"""Render Free entry point: migrate once, then replace this process with Gunicorn.

Gunicorn must be the service's primary process so Render can reliably discover the
HTTP socket. The queue process starts from a Gunicorn master hook after the socket
is ready.
"""
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent


def prepare_and_exec(env, prepare_command, web_command, run=subprocess.run, exec_fn=os.execvpe):
    print('Render: applying migrations before web start', flush=True)
    result = run(prepare_command, cwd=ROOT, env=env, check=False)
    if result.returncode != 0:
        print('Render: database preparation failed; web was not started', flush=True)
        return result.returncode or 1
    print('Render: migrations complete; replacing launcher with Gunicorn', flush=True)
    exec_fn(web_command[0], web_command, env)
    raise RuntimeError('execvpe unexpectedly returned')


def main():
    from dotenv import load_dotenv

    load_dotenv(ROOT / '.env')  # Render environment values take precedence.
    env = os.environ.copy()
    env.setdefault('DB_POOL_SIZE', '2')
    env.setdefault('DB_MAX_OVERFLOW', '0')
    env.setdefault('PYTHONUNBUFFERED', '1')
    if env.get('RENDER') == 'true':
        env.setdefault('TRUST_PROXY', '1')
    port = int(env.get('PORT', '10000'))
    if not 1 <= port <= 65535:
        raise ValueError('PORT must be between 1 and 65535')
    web_command = [
        sys.executable, '-m', 'gunicorn', 'app:app',
        '--config', str(ROOT / 'scripts' / 'gunicorn_conf.py'),
        '--bind', f'0.0.0.0:{port}', '--worker-class', 'gthread', '--workers', '1', '--threads', '4',
        '--timeout', '60', '--graceful-timeout', '10',
        '--access-logfile', '-', '--error-logfile', '-',
    ]
    prepare_command = [sys.executable, str(ROOT / 'scripts/migrate.py'), '--enqueue-initial-refresh']
    return prepare_and_exec(env, prepare_command, web_command)


if __name__ == '__main__':
    raise SystemExit(main())
