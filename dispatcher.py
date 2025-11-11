import logging
import queue
import threading
import time
import itertools
import random
import re
from typing import Optional

from telebot.apihelper import ApiTelegramException


class MessageDispatcher:
    """
    Centralized Telegram sender with rate limiting and automatic retry/backoff.
    Enqueue bot method calls (send_message, send_document, etc.) and the worker
    serializes execution to avoid flood limits.
    """

    def __init__(self, bot, rate_per_second: int = 25, max_retries: int = 3):
        self.bot = bot
        self.max_retries = max_retries
        self._min_interval = 1.0 / max(rate_per_second, 1)
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._counter = itertools.count()
        self._stop_event = threading.Event()
        self._last_sent = 0.0
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def enqueue(self, method: str, *args, delay: float = 0.0, retry_attempt: int = 0, **kwargs):
        run_at = time.time() + max(delay, 0.0)
        self._queue.put((run_at, next(self._counter), method, args, kwargs, retry_attempt))

    def shutdown(self, timeout: Optional[float] = None):
        self._stop_event.set()
        self.enqueue("__shutdown__")
        self._worker.join(timeout=timeout)

    def wait_until_idle(self, timeout: Optional[float] = None) -> bool:
        """
        Block until all queued tasks are processed or timeout occurs.
        Returns True when the queue drained, False if timeout expired first.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            with self._queue.all_tasks_done:
                while self._queue.unfinished_tasks:
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        return False
                    self._queue.all_tasks_done.wait(timeout=remaining)
            return True
        except AttributeError:
            # Fallback for alternative Queue implementations lacking all_tasks_done
            start = time.monotonic()
            while self._queue.unfinished_tasks:
                if timeout is not None and (time.monotonic() - start) >= timeout:
                    return False
                time.sleep(0.05)
            return True

    # ------------------------------------------------------------------ #
    # Internal worker
    # ------------------------------------------------------------------ #
    def _run(self):
        while not self._stop_event.is_set():
            try:
                run_at, _, method, args, kwargs, attempt = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if method == "__shutdown__":
                self._queue.task_done()
                break

            now = time.time()
            if run_at > now:
                time.sleep(run_at - now)

            wait = self._min_interval - (time.time() - self._last_sent)
            if wait > 0:
                time.sleep(wait)

            try:
                getattr(self.bot, method)(*args, **kwargs)
                self._last_sent = time.time()
            except ApiTelegramException as e:
                self._handle_api_error(e, method, args, kwargs, attempt)
            except Exception as exc:  # pragma: no cover - defensive logging
                logging.error(f"[Dispatcher] {method} failed: {exc}", exc_info=True)
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _handle_api_error(self, error, method, args, kwargs, attempt):
        message = str(error)
        if attempt >= self.max_retries:
            logging.error(f"[Dispatcher] {method} giving up after {attempt} retries: {message}")
            return

        if any(token in message.lower() for token in ["too many requests", "flood control", "retry after"]):
            wait = self._parse_retry_delay(message)
            logging.warning(f"[Dispatcher] Rate limited. Retrying {method} in {wait:.2f}s (attempt {attempt+1})")
            run_at = time.time() + wait
            self._queue.put((run_at, next(self._counter), method, args, kwargs, attempt + 1))
            return

        # Non-rate-limit error
        logging.error(f"[Dispatcher] {method} error: {message}", exc_info=True)

    @staticmethod
    def _parse_retry_delay(message: str) -> float:
        match = re.search(r"(?:retry (?:after|in)\s*)(\d+)", message, re.IGNORECASE)
        if match:
            base = int(match.group(1))
        else:
            base = 5
        return base + random.uniform(0.3, 1.2)
