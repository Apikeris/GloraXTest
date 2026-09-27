"""Gunicorn master hooks for the durable queue on one Render Free service."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading


ROOT = Path(__file__).resolve().parent.parent


class WorkerSupervisor:
    def __init__(self, command=None, popen=subprocess.Popen):
        self.command = command or [sys.executable, '-m', 'glorax.worker', '--compact']
        self.popen = popen
        self.stop_event = threading.Event()
        self.child = None
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._run, name='glorax-worker-supervisor', daemon=True)
        self.thread.start()

    def _run(self):
        delay = 1
        while not self.stop_event.is_set():
            self.child = self.popen(self.command, cwd=ROOT, env=os.environ.copy(), start_new_session=True)
            print('Render: worker started after web socket became ready', flush=True)
            while not self.stop_event.wait(0.5) and self.child.poll() is None:
                pass
            if self.stop_event.is_set():
                break
            code = self.child.wait()
            print(f'Render: worker exited ({code}); restarting in {delay}s', flush=True)
            if self.stop_event.wait(delay):
                break
            delay = min(delay * 2, 30)

    def stop(self, grace_seconds=10):
        self.stop_event.set()
        child = self.child
        if child and child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
        if self.thread:
            self.thread.join(timeout=grace_seconds)


supervisor = WorkerSupervisor()


def when_ready(server):
    """Run in the Gunicorn master after its listening socket is ready."""
    supervisor.start()


def on_exit(server):
    supervisor.stop()
