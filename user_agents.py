import random
import threading

try:
    from fake_useragent import UserAgent  # type: ignore
except Exception:  # pragma: no cover
    UserAgent = None  # type: ignore

_DEFAULT_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Mobile Safari/537.36",
]

_ua_instance = None
_lock = threading.Lock()


def get_random_user_agent() -> str:
    """
    Return a random User-Agent string.
    Uses fake_useragent when available, otherwise falls back to a curated list.
    """
    global _ua_instance

    if UserAgent is not None:
        try:
            with _lock:
                if _ua_instance is None:
                    _ua_instance = UserAgent()
            return _ua_instance.random
        except Exception:
            pass

    return random.choice(_DEFAULT_AGENTS)
