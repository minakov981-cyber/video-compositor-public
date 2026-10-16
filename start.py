"""Container entrypoint: the web server (gunicorn) and the render worker side by side.

Forwards SIGTERM to both so a deploy shuts them down cleanly, restarts the worker if it crashes,
and exits when gunicorn exits so Railway can restart the container.
"""
import os
import signal
import subprocess
import sys
import time

# --workers 3: three requests (uploads included) at once. Renders don't block them, they run in worker.py.
GUNICORN = ["gunicorn", "app:app", "--bind", "0.0.0.0:8080", "--workers", "3", "--timeout", "120"]
WORKER   = [sys.executable, "worker.py"]


def main():
    web      = subprocess.Popen(GUNICORN)
    worker   = subprocess.Popen(WORKER)
    stopping = False

    def stop(signum, _frame):
        nonlocal stopping
        stopping = True
        for p in (worker, web):
            if p.returncode is None:
                try:
                    p.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    pass

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    web_code = None
    while True:
        try:
            pid, status = os.waitpid(-1, 0)  # also reaps orphaned FFmpeg processes (we are PID 1)
        except ChildProcessError:
            break
        code = os.waitstatus_to_exitcode(status)
        if pid == web.pid:
            web.returncode = web_code = code
            if not stopping:
                print(f"[start] gunicorn exited ({code}), stopping", flush=True)
                stop(signal.SIGTERM, None)
        elif pid == worker.pid:
            worker.returncode = code
            if not stopping and web_code is None:
                print(f"[start] worker exited ({code}), restarting in 3 s", flush=True)
                time.sleep(3)
                worker = subprocess.Popen(WORKER)
        if web.returncode is not None and worker.returncode is not None:
            break
    sys.exit(0 if stopping else (web_code or 1))


if __name__ == "__main__":
    main()
