import threading

from .base import AnalysisContext
from . import store


class AnalysisCancelled(Exception):
    pass


class JobRunner:
    def __init__(
        self,
        state_path,
        resolve_capture,
        connect_capture,
        input_revision,
        module_lookup,
        poll_seconds=1.0,
    ):
        self.state_path = state_path
        self.resolve_capture = resolve_capture
        self.connect_capture = connect_capture
        self.input_revision = input_revision
        self.module_lookup = module_lookup
        self.poll_seconds = poll_seconds
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="wdvisual-analysis", daemon=True
            )
            self._thread.start()

    def wake(self):
        self.start()
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2)

    def run_one(self):
        job = store.claim_next_job(self.state_path())
        if job is None:
            return False
        try:
            module = self.module_lookup(job["module_name"], job["module_version"])
            if module is None:
                raise ValueError(
                    f"Analysis module {job['module_name']} {job['module_version']} is unavailable"
                )
            capture_path, _metadata = self.resolve_capture(job["scope_key"])
            revision = self.input_revision(job["scope_key"], capture_path)
            if revision != job["input_revision"]:
                raise ValueError("The capture changed after this job was queued; queue a new analysis")
            context = AnalysisContext(
                scope=job["input_scope"],
                input_revision=job["input_revision"],
                capture_path=capture_path,
                parameters=job["parameters"],
            )

            def progress(value):
                if store.update_progress(self.state_path(), job["id"], value):
                    raise AnalysisCancelled()

            with self.connect_capture(capture_path) as connection:
                result = module.run(connection, context, progress)
            store.complete_job(self.state_path(), job, result)
        except AnalysisCancelled:
            store.fail_job(self.state_path(), job["id"], "Cancelled")
        except Exception as error:
            store.fail_job(self.state_path(), job["id"], error)
        return True

    def _loop(self):
        while not self._stop.is_set():
            worked = self.run_one()
            if not worked:
                self._wake.wait(self.poll_seconds)
                self._wake.clear()
