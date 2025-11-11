# ================================================================
# 🧩 Shared State Module
# ================================================================

import re
import os
import json
import threading
import logging
import time
import random
from datetime import datetime
from collections import defaultdict

user_busy = {}
_busy_records = {}
_busy_lock = threading.Lock()

def parse_proxy_line(line: str):
    """Parses proxies in common formats and returns a dict or None if invalid."""
    if not line:
        return None

    line = line.strip().replace(" ", "")

    # Try multiple proxy patterns
    patterns = [
        # host:port:user:pass
        r"^([\w\.-]+):(\d{2,6}):([^:@]+):(.+)$",
        # user:pass@host:port
        r"^([^:@]+):([^:@]+)@([\w\.-]+):(\d{2,6})$",
        # user:pass:host:port
        r"^([^:@]+):([^:@]+):([\w\.-]+):(\d{2,6})$",
        # host:port@user:pass
        r"^([\w\.-]+):(\d{2,6})@([^:@]+):([^:@]+)$",
        # host:port (no auth)
        r"^([\w\.-]+):(\d{2,6})$",
    ]

    for p in patterns:
        m = re.match(p, line)
        if m:
            g = m.groups()
            if len(g) == 2:
                host, port = g
                return {"host": host, "port": int(port)}
            elif len(g) == 4:
                # Try to figure out which pattern matched
                if "@" in line or line.count(":") > 2:
                    # If it's host:port:user:pass
                    if g[0].replace(".", "").isalpha() or g[0].count(".") >= 1:
                        return {"host": g[0], "port": int(g[1]), "user": g[2], "pass": g[3]}
                    # Else maybe user:pass@host:port
                    elif g[2].replace(".", "").isalpha() or g[2].count(".") >= 1:
                        return {"host": g[2], "port": int(g[3]), "user": g[0], "pass": g[1]}
    return None

# ============================================================
# 🧾 Shared Function — Save Live CC JSON (per user & worker)
# ============================================================

_livecc_folder_lock = threading.Lock()


def set_user_busy(chat_id: str, label: str):
    with _busy_lock:
        user_busy[str(chat_id)] = label or True
        _busy_records[str(chat_id)] = {"label": label, "started": time.time()}


def clear_user_busy(chat_id: str):
    with _busy_lock:
        user_busy.pop(str(chat_id), None)
        _busy_records.pop(str(chat_id), None)


def is_user_busy(chat_id: str) -> bool:
    with _busy_lock:
        return str(chat_id) in user_busy


def busy_snapshot():
    with _busy_lock:
        return {
            chat_id: record.copy()
            for chat_id, record in _busy_records.items()
        }

def save_live_cc_to_json(user_id: str, worker_id: int, live_data: dict):
    """
    Thread-safe shared function.
    Each worker writes to its own live file:
        live-cc/<user_id>/Live_cc_<user_id>_<worker_id>.json
    """
    folder = os.path.join("live-cc", str(user_id))

    # Ensure per-user folder exists safely
    with _livecc_folder_lock:
        try:
            os.makedirs(folder, exist_ok=True)
        except Exception as e:
            logging.warning(f"[LIVE JSON] Failed to create folder {folder}: {e}")
            return

    file_path = os.path.join(folder, f"Live_cc_{user_id}_{worker_id}.json")

    # Add timestamp
    live_data["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        # Each worker writes to its own file (no shared writes)
        if os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                existing = []
        else:
            existing = []

        existing.append(live_data)

        # Write atomically with .tmp → replace
        tmp_path = f"{file_path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, file_path)

        logging.info(f"[LIVE JSON] Worker {worker_id} → {file_path}")
    except Exception as e:
        logging.error(f"[LIVE JSON ERROR] User {user_id}, Worker {worker_id}: {e}")
# ================================================================
# 🔁 Shared Function — Retry logic for site checks (Manual + Mass)
# ================================================================
def try_process_with_retries(card_data, chat_id, user_proxy=None, worker_id=None, max_tries=None, stop_checker=None):
    from site_auth_manager import remove_user_site, _load_state, process_card_for_user_sites, get_next_user_site

    def should_stop() -> bool:
        try:
            return bool(stop_checker and stop_checker())
        except Exception:
            return False

    # 🧩 Load once at start, cache sites in memory
    try:
        state = _load_state(chat_id)
        user_sites = list(state.get(str(chat_id), {}).get("sites", {}).keys())
    except Exception:
        user_sites = []

    if should_stop():
        return None, {"status": "STOPPED", "reason": "User requested stop"}

    if not user_sites:
        return None, {"status": "DECLINED", "reason": "No sites configured", "site_dead": True}

    # Determine randomized rotation order for this check
    try:
        primary_site = get_next_user_site(chat_id)
    except Exception:
        primary_site = None

    if primary_site and primary_site in user_sites:
        remaining_sites = [site for site in user_sites if site != primary_site]
        random.shuffle(remaining_sites)
        sites_queue = [primary_site, *remaining_sites]
    else:
        sites_queue = random.sample(user_sites, k=len(user_sites))
    site_retry_counts = defaultdict(int)
    confirmed_dead_sites = []
    last_site_used = None
    result = None
    last_failure_reason = None

    base_attempts = max(len(sites_queue), 1)
    max_attempts = max(max_tries or base_attempts, base_attempts) * 2
    attempts = 0

    def _is_potential_dead(reason_text: str) -> bool:
        reason_lower = (reason_text or "").lower()
        keyword_match = any(
            key in reason_lower
            for key in (
                "site response failed",
                "site no response",
                "stripe token error",
            )
        )
        return keyword_match

    while sites_queue and attempts < max_attempts:
        if should_stop():
            return None, {"status": "STOPPED", "reason": "User requested stop"}

        current_site = sites_queue[0]
        site_retry_counts[current_site] += 1
        attempts += 1

        print(f"[TRY] Attempt {attempts}/{max_attempts} using site: {current_site} (retry #{site_retry_counts[current_site]})")

        site_url, result = process_card_for_user_sites(
            card_data,
            chat_id,
            proxy=user_proxy,
            worker_id=worker_id,
            preferred_site=current_site,
            stop_checker=stop_checker,
        )

        if not isinstance(result, dict):
            result = {"status": "DECLINED", "reason": str(result or "Invalid result")}

        if result.get("status") == "STOPPED":
            return site_url, result

        reason_text = result.get("reason") or result.get("message") or ""
        last_failure_reason = reason_text
        # Check both the site_dead flag and reason text for dead site detection
        potential_dead = result.get("site_dead", False) or _is_potential_dead(reason_text)

        if potential_dead:
            if site_retry_counts[current_site] < 2:
                print(f"[RETRY] {current_site} flagged as dead. Retrying once more to confirm.")
                continue

            print(f"[CONFIRM] Removing dead site immediately: {current_site}")
            confirmed_dead_sites.append(current_site)
            # 🧹 Remove dead site immediately so it won't be used by other cards
            try:
                removed = remove_user_site(chat_id, current_site, worker_id=worker_id)
                if removed:
                    print(f"[AUTO] Immediately removed dead site: {current_site}")
            except Exception as e:
                print(f"[AUTO] Error removing site {current_site} immediately: {e}")
            sites_queue.pop(0)
            continue

        # ✅ Site responded (even if declined)
        last_site_used = site_url or current_site
        break

    # 🧹 Safety net: Clean up any dead sites that weren't removed immediately
    # (Most sites should already be removed above, but this ensures nothing is missed)
    for dead_site in confirmed_dead_sites:
        try:
            removed = remove_user_site(chat_id, dead_site, worker_id=worker_id)
            if removed:
                print(f"[AUTO] Safety cleanup: Removed dead site: {dead_site}")
        except Exception as e:
            print(f"[AUTO] Error in safety cleanup for site {dead_site}: {e}")

    # 🧠 No site produced a valid response
    if last_site_used is None:
        print("[FAIL] All sites failed or were removed during retries.")
        fallback_reason = "All your sites have died during checking. Please add new ones."
        return None, {
            "status": "DECLINED",
            "reason": fallback_reason,
            "site_dead": True,
            "dead_sites_removed": confirmed_dead_sites,
            "last_failure_reason": last_failure_reason,
        }

    if should_stop():
        return last_site_used, {"status": "STOPPED", "reason": "User requested stop"}

    # ✅ Annotate result with removal metadata for callers
    if isinstance(result, dict):
        result.setdefault("dead_sites_removed", confirmed_dead_sites)

    return last_site_used, result
