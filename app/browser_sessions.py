"""One owning thread per active account; idle browser sessions never occupy the booking pool."""
import queue
import threading
import time
from contextlib import contextmanager

IDLE_SECONDS = 600
_actors = {}
_guard = threading.Lock()
_slots = threading.BoundedSemaphore(4)
_local = threading.local()


class AccountSession:
    def __init__(self, account_id):
        self.account_id = account_id
        self.jobs = queue.Queue()
        self.context = None
        self.pw = None
        self.runtime_owner = None
        self.stopping = threading.Event()
        self.thread = None

    @contextmanager
    def runtime(self):
        if self.pw is None:
            from playwright.sync_api import sync_playwright
            self.runtime_owner = sync_playwright()
            self.pw = self.runtime_owner.start()
        yield self.pw

    def open(self, pw, profile):
        from .browser_reserve import launch_context
        if self.context:
            try:
                if self.context.pages and not self.context.pages[0].is_closed():
                    idle_gate = getattr(self, 'idle_gate', None)
                    if idle_gate:
                        self.context.unroute('**/*', idle_gate.route)
                        self.idle_gate = None
                    return self.context
            except Exception:
                pass
            self.close_context()
        self.context = launch_context(pw, profile)
        return self.context

    def close_context(self):
        if self.context:
            try:
                self.context.close()
            except Exception:
                pass
            self.context = None

    def run(self):
        _local.session = self
        last_job = time.monotonic()
        try:
            while not self.stopping.is_set():
                try:
                    fn, args, kwargs = self.jobs.get(timeout=0.2)
                except queue.Empty:
                    if self.context:
                        try:
                            self.context.pages[0].wait_for_timeout(50)
                        except Exception:
                            self.close_context()
                    with _guard:
                        if self.jobs.empty() and time.monotonic() - last_job >= IDLE_SECONDS:
                            # Release the profile before another submission can
                            # create its replacement owner at this boundary.
                            self.close_context()
                            _actors.pop(self.account_id, None)
                            break
                    continue
                with _slots:
                    try:
                        fn(*args, **kwargs)
                    except Exception:
                        # Durable jobs are recovered as interrupted on restart.
                        import logging
                        logging.getLogger(__name__).error('account %s worker failed', self.account_id)
                last_job = time.monotonic()
        finally:
            self.close_context()
            try:
                # sync_playwright().start() returns the Playwright object;
                # stop belongs to that object, not to its context manager.
                if self.pw:
                    self.pw.stop()
            except Exception:
                pass
            finally:
                self.pw = None
                self.runtime_owner = None
                with _guard:
                    if _actors.get(self.account_id) is self:
                        _actors.pop(self.account_id, None)
                _local.session = None


def current_session():
    return getattr(_local, 'session', None)


def submit(account_id, fn, *args, **kwargs):
    with _guard:
        actor = _actors.get(account_id)
        if actor is None:
            actor = AccountSession(account_id)
            _actors[account_id] = actor
            actor.jobs.put((fn, args, kwargs))
            actor.thread = threading.Thread(target=actor.run, name=f'account-browser-{account_id}', daemon=True)
            actor.thread.start()
        else:
            actor.jobs.put((fn, args, kwargs))


def close_account(account_id):
    with _guard:
        actor = _actors.get(account_id)
        if actor:
            # Drain queued jobs through their fresh account-enabled checks.
            # This also permits re-enabling an account without queuing work
            # onto a thread that has already been told to exit.
            actor.jobs.put((actor.close_context, (), {}))


def owns_account(account_id):
    with _guard:
        actor = _actors.get(account_id)
        return bool(actor and actor.thread and actor.thread.is_alive() and not actor.stopping.is_set())


def shutdown():
    with _guard:
        for actor in _actors.values():
            actor.stopping.set()
