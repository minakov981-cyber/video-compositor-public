"""Render worker: runs queued renders and tools from Postgres, plus the file cleanup loop.

start.py runs it next to gunicorn. Each job runs in its own process (worker.py --job <id>), so the
worker can stop a job that hangs or uses too much memory without going down itself.
On SIGTERM (a deploy) the jobs in progress go back to the queue and the next container renders
them again; if the whole worker dies without warning, its jobs go back once their heartbeat is stale.
"""
import os
import signal
import subprocess
import sys
import threading
import time

import sentry_sdk

import app as web
from db import (claim_render_job, fail_render_job, get_render_job, heartbeat_render_jobs,
                requeue_render_jobs, requeue_stale_render_jobs)

POLL_SECONDS      = 1.5
HEARTBEAT_SECONDS = 10
STALE_SECONDS     = 45   # no heartbeat for this long = the worker running it is gone
STALE_CHECK_EVERY = 15
MAX_ATTEMPTS      = 3    # a job whose process keeps crashing (e.g. out of memory) fails after this
try:
    # Longest a single render or tool may run. Videos are at most 5 minutes, so a normal render is far below this.
    JOB_TIMEOUT_SECONDS = int(os.environ.get("JOB_TIMEOUT_SECONDS", "1200"))
except ValueError:
    JOB_TIMEOUT_SECONDS = 1200

CRASHED_MSG = "This render stopped unexpectedly several times. Please try again with fewer or shorter clips."
TIMEOUT_MSG = (f"Rendering took longer than {max(1, JOB_TIMEOUT_SECONDS // 60)} minutes and was stopped. "
               "Please try again with fewer or shorter clips.")

_running  = {}           # job_id -> (Popen, started monotonic, job row)
_stopping = threading.Event()


def _fail(job, message):
    """Fail a job the worker gave up on and give back the free-plan render it used."""
    if fail_render_job(job["job_id"], message):
        web.refund_job(job)
        print(f"[worker] job {job['job_id']} failed: {message}", flush=True)


def _kill(proc):
    """Stop a job process together with its FFmpeg children (it leads its own process group)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait()


def _start(job):
    if job["attempts"] > MAX_ATTEMPTS:
        _fail(job, CRASHED_MSG)
        return
    proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--job", job["job_id"]],
                            env={**os.environ, "CLIPSNAP_JOB_PROCESS": "1"}, start_new_session=True)
    _running[job["job_id"]] = (proc, time.monotonic(), job)


def _check_running():
    for job_id, (proc, started, job) in list(_running.items()):
        code = proc.poll()
        if code is None:
            if time.monotonic() - started > JOB_TIMEOUT_SECONDS:
                _kill(proc)
                _running.pop(job_id, None)
                _fail(job, TIMEOUT_MSG)
            continue
        _running.pop(job_id, None)
        if code != 0:
            # The job process died before recording a result (killed for memory, crash): run it again
            print(f"[worker] job {job_id} process exited with {code}", flush=True)
            row = get_render_job(job_id)
            if row and row["status"] == "processing":
                if job["attempts"] >= MAX_ATTEMPTS:
                    _fail(job, CRASHED_MSG)
                else:
                    requeue_render_jobs([job_id], count_attempt=True)


def _heartbeat_loop():
    while not _stopping.wait(HEARTBEAT_SECONDS):
        try:
            heartbeat_render_jobs(list(_running))
        except Exception as exc:
            print(f"[worker] heartbeat failed: {exc}", flush=True)


def serve():
    """Claim and run jobs until stopped. Runs in the main thread of worker.py (or a thread in local dev)."""
    threading.Thread(target=web._cleanup_loop, daemon=True).start()
    threading.Thread(target=_heartbeat_loop, daemon=True).start()
    print(f"[worker] started, {web.MAX_CONCURRENT_RENDERS} job(s) at a time, "
          f"timeout {JOB_TIMEOUT_SECONDS}s", flush=True)
    last_stale_check = 0.0
    while not _stopping.is_set():
        try:
            _check_running()
            if time.monotonic() - last_stale_check > STALE_CHECK_EVERY:
                last_stale_check = time.monotonic()
                n = requeue_stale_render_jobs(STALE_SECONDS)
                if n:
                    print(f"[worker] re-queued {n} interrupted job(s)", flush=True)
            if len(_running) < web.MAX_CONCURRENT_RENDERS:
                job = claim_render_job()
                if job:
                    _start(job)
                    continue  # there may be more waiting
        except Exception as exc:
            sentry_sdk.capture_exception(exc)
            print(f"[worker] loop error: {exc}", flush=True)
            time.sleep(5)
        time.sleep(POLL_SECONDS)


def _stop(signum, _frame):
    _stopping.set()
    running = list(_running.values())
    for proc, _, _ in running:
        _kill(proc)
    ids = [job["job_id"] for _, _, job in running]
    try:
        requeue_render_jobs(ids)
    except Exception as exc:
        print(f"[worker] could not re-queue {ids}: {exc}", flush=True)
    print(f"[worker] stopping, {len(ids)} job(s) back in the queue", flush=True)
    os._exit(0)


def _exit_with_parent():
    """If the worker that started this job process dies, stop too: its replacement re-runs the job."""
    parent = os.getppid()
    while True:
        time.sleep(2)
        if os.getppid() != parent:
            os.killpg(0, signal.SIGKILL)


def run_one(job_id):
    """Body of a job process: run the job and record its result (run_job records failures too)."""
    threading.Thread(target=_exit_with_parent, daemon=True).start()
    job = get_render_job(job_id)
    if not job or job["status"] != "processing":
        return
    try:
        web.run_job(job)
    except Exception as exc:
        sentry_sdk.capture_exception(exc)
        if fail_render_job(job_id, "Rendering failed. Please try again."):
            web.refund_job(job)
    sentry_sdk.flush(timeout=5)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--job":
        run_one(sys.argv[2])
    else:
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        serve()
