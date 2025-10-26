# ============================================================
# 🧹 Logging Configuration
# ============================================================
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import logging
import os
import re
import time
import json
import random
import string
import threading
import asyncio
import subprocess
import html
import shutil
from urllib.parse import urlparse
from datetime import datetime
import glob
from site_auth_manager import ensure_user_site_exists
# Silence noisy urllib3 logs
logging.getLogger("urllib3").setLevel(logging.WARNING)
from site_auth_manager import replace_user_sites
from mass_check import merge_livecc_user_files

# ============================================================
# 🧩 Telegram Bot Imports
# ============================================================
import telebot
from telebot import types
# ============================================================
# ⚙️ Config & Global Settings
# ============================================================
from config import load_config
from site_auth_manager import ensure_user_site_exists
from runtime_config import set_default_site, get_default_site, RUNTIME_CONFIG
cfg = load_config()
BOT_TOKEN = cfg["BOT_TOKEN"]
CHANNEL_ID = cfg["CHANNEL_ID"]
ADMIN_ID = cfg["ADMIN_ID"]
MAX_WORKERS = cfg["MAX_WORKERS"]
BATCH_SIZE = cfg["BATCH_SIZE"]
DELAY_BETWEEN_BATCHES = cfg["DELAY_BETWEEN_BATCHES"]
from shared_state import save_live_cc_to_json
from proxy_manager import (
    add_user_proxy,
    replace_user_proxies,
    delete_user_proxies,
    list_user_proxies,
    get_user_proxy,
)


from mass_check import (
    handle_file as handle_mass_file,
    run_mass_check_thread,   # thread launcher we use on file upload
    get_stop_event,
    set_stop_event,
    clear_stop_event,
    is_stop_requested,
    stop_events,
)


from manual_check import register_manual_check
from proxy_manager import parse_proxy_line
from proxy_check import register_checkproxy
from site_auth_manager import (
    SiteAuthManager,
    _load_state,
    _save_state,
    process_card_for_user_sites,
)

# Global dictionary to hold temporary site input for each user
user_sites = {}
# ================================================================
# 🚦 USER BUSY TRACKER
# ================================================================
from shared_state import user_busy

def safe_load_state(chat_id):
    try:
        return _load_state(chat_id)
    except Exception as e:
        print(f"[WARN] Failed to load state for {chat_id}: {e}")
        return {}

# ============================================================
# 🧩 AUTO-DEFAULT SITE INITIALIZER (fixed: creates inside user folder)
# ============================================================

def ensure_user_default_site(chat_id):
    """
    Ensures that a per-user sites_<id>.json exists inside /sites/<chat_id>/,
    with the proper nested structure and default site from runtime_config.
    """
    try:
        from runtime_config import get_default_site
        default_site = get_default_site()

        user_dir = os.path.join("sites", str(chat_id))
        os.makedirs(user_dir, exist_ok=True)
        file_path = os.path.join(user_dir, f"sites_{chat_id}.json")

        if not os.path.exists(file_path):
            default_state = {
                str(chat_id): {
                    "sites": {
                        default_site: {
                            "accounts": [],
                            "cookies": None,
                            "payment_count": 0,
                            "mode": "rotate",
                        }
                    }
                }
            }

            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(default_state, f, indent=2)

            print(f"[AUTO-SITE] Created default site file inside folder for {chat_id}")
        else:
            print(f"[AUTO-SITE] Default site already exists in folder for {chat_id}")

    except Exception as e:
        print(f"[AUTO-SITE ERROR] {chat_id}: {e}")






from sitechk import check_command, get_base_url
from bininfo import round_robin_bin_lookup

# ============================================================
# 💳 Card Generator (Optional Utilities)
# ============================================================
from cardgen import (
    generate_luhn_cards_parallel,
    generate_luhn_cards_fixed_expiry,
    save_cards_to_file,
    get_random_expiry,
)


# ============================================================
# 🤖 INITIALIZE BOT
# ============================================================
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
# ================================================================
# 🧩 Global Safe Forward Patch — Logs channel errors silently
# ================================================================
import functools

_original_send_message = bot.send_message
_original_send_document = bot.send_document
_original_send_photo = bot.send_photo
_original_send_video = bot.send_video

def _safe_wrapper(func_name, orig_func):
    @functools.wraps(orig_func)
    def wrapped(*args, **kwargs):
        try:
            return orig_func(*args, **kwargs)
        except Exception as e:
            # Only log if forwarding to CHANNEL_ID
            if len(args) > 0 and str(args[0]) == str(CHANNEL_ID):
                logging.debug(f"[CHANNEL_FORWARD_ERROR:{func_name}] {e}")
            else:
                raise  # re-raise for normal user messages
    return wrapped

# Patch the bot methods globally
bot.send_message = _safe_wrapper("send_message", _original_send_message)
bot.send_document = _safe_wrapper("send_document", _original_send_document)
bot.send_photo = _safe_wrapper("send_photo", _original_send_photo)
bot.send_video = _safe_wrapper("send_video", _original_send_video)

# Register mass check system (STOP + Resume callbacks)
from mass_check import activechecks
from manual_check import user_locks, user_locks_lock
clean_waiting_users = set()
def is_user_busy(chat_id: str):
    """Return True if user currently has an active mass or manual check."""
    if user_busy.get(chat_id):  # 🔹 ADD THIS LINE
        return True

    # Mass check running?
    if chat_id in activechecks:
        return True

    # Manual check running?
    with user_locks_lock:
        if chat_id in user_locks and user_locks[chat_id].locked():
            return True
    return False



# ============================================================
# 🧩 Safe Telegram Sender (Flood Control & Retry-Aware)
# ============================================================
def safe_send(bot, method, *args, **kwargs):
    """
    Thread-safe Telegram sender with rate-limit protection and retry-after support.
    Usage:
        safe_send(bot, "send_message", chat_id, text, parse_mode="HTML")
    """

    def run():
        max_attempts = 3  # Prevent infinite loops
        delay_between = 0.4

        for attempt in range(1, max_attempts + 1):
            try:
                time.sleep(delay_between)  # small delay before sending
                getattr(bot, method)(*args, **kwargs)
                return  # ✅ success — exit
            except telebot.apihelper.ApiTelegramException as e:
                err_text = str(e)
                if "Too Many Requests" in err_text:
                    # Extract retry time safely
                    import re
                    match = re.search(r"retry after (\d+)", err_text)
                    wait = int(match.group(1)) if match else 5
                    logging.warning(f"[RATE-LIMIT] Waiting {wait}s before retry (attempt {attempt})…")
                    time.sleep(wait)
                    continue
                else:
                    logging.error(f"[safe_send TELEGRAM ERROR] {e}")
                    break  # Non-rate-limit error → stop retrying
            except Exception as e:
                logging.error(f"[safe_send GENERAL ERROR attempt {attempt}] {e}")
                time.sleep(1)
                continue

        logging.error(f"[safe_send] Failed after {max_attempts} attempts for {method}")

    threading.Thread(target=run, daemon=True).start()


from cardgen import (
    generate_luhn_cards_parallel,
    generate_luhn_cards_fixed_expiry,
    save_cards_to_file,
    get_random_expiry,
)

from bininfo import round_robin_bin_lookup
from sitechk import get_base_url
from proxy_manager import parse_proxy_line
from manual_check import register_manual_check
from mass_check import handle_file




# -------------------------------------------------
# Base Directory
# -------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# -------------------------------------------------
# 🔹 Safe Send Function
# -------------------------------------------------


# -------------------------------------------------
# Logging Configuration
# -------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)

# -------------------------------------------------
# File Constants
# -------------------------------------------------
SITE_STORAGE_FILE = "current_site.txt"
ALLOWED_USERS_FILE = "allowed_users.json"
MASTER_FILE = "master_live_ccs.json"
REDEEM_CODES_FILE = "redeem_codes.json"

# Proxy state
user_proxy_mode = set()

# -------------------------------------------------
# Initialize Bot
# -------------------------------------------------


# ================================================================
# 🔒 Command Control System — prevents overlapping commands
# ================================================================
user_active_command = {}
command_lock = threading.Lock()


def set_active_command(chat_id, command):
    """Register a new command for this user and cancel the previous one."""
    with command_lock:
        user_active_command[chat_id] = command


def clear_active_command(chat_id):
    """Clear the user's active command."""
    with command_lock:
        user_active_command.pop(chat_id, None)


def is_command_active(chat_id, command=None):
    """Check if a user currently has an active command."""
    with command_lock:
        if command:
            return user_active_command.get(chat_id) == command
        return chat_id in user_active_command


def reset_user_states(chat_id):
    """Clear all temp variables from /site or /proxy setup."""
    try:
        user_site_last_instruction.pop(chat_id, None)
    except Exception:
        pass
    try:
        user_proxy_temp.pop(chat_id, None)
    except Exception:
        pass
    try:
        user_proxy_messages.pop(chat_id, None)
    except Exception:
        pass

# -------------------------------------------------
# 🔹 Auto Delete Message Helper
# -------------------------------------------------
def _auto_delete_message_later(bot, chat_id, message_id, delay=5):
    """
    Deletes a Telegram message after a specified delay (in seconds).
    Used for temporary error or info messages.
    """

    def delete_later():
        try:
            time.sleep(delay)
            bot.delete_message(chat_id, message_id)
        except Exception:
            pass

    threading.Thread(target=delete_later, daemon=True).start()

# ✅ Proxy checker command
register_checkproxy(bot)

# -------------------------------------------------
# Helpers: JSON Persistence
# -------------------------------------------------

ALLOWED_FILE = "allowed_users.json"

# Safe loader for allowed_users
def load_allowed_users():
    if os.path.exists(ALLOWED_FILE):
        try:
            with open(ALLOWED_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                # force convert dict → list
                if isinstance(data, dict):
                    data = list(data.keys())
                elif not isinstance(data, list):
                    data = []
                logging.info(f"[LOAD] Loaded {len(data)} allowed users")
                return data
        except Exception as e:
            logging.error(f"[LOAD ERROR] Failed to load allowed users: {e}")
            return []
    else:
        logging.warning(f"[LOAD WARN] {ALLOWED_FILE} not found — starting with empty list")
        return []

# Load existing allowed users or create an empty list
if os.path.exists(ALLOWED_FILE):
    try:
        with open(ALLOWED_FILE, "r", encoding="utf-8") as f:
            allowed_users = json.load(f)
        if not isinstance(allowed_users, list):
            allowed_users = []
            logging.warning("[INIT WARN] allowed_users.json not a list — reset to empty list")
    except Exception as e:
        logging.error(f"[INIT ERROR] Could not read allowed_users.json: {e}")
        allowed_users = []
else:
    logging.info("[INIT] allowed_users.json not found — creating new empty list")
    allowed_users = []

def save_allowed_users(data):
    try:
        with open(ALLOWED_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        logging.info(f"[SAVE] Allowed users saved ({len(data)} total)")
    except Exception as e:
        logging.error(f"[SAVE ERROR] Failed to save allowed users: {e}")


# Initialize
allowed_users = load_allowed_users()
from config import ADMIN_ID
if str(ADMIN_ID) not in allowed_users:
    allowed_users.append(str(ADMIN_ID))
    save_allowed_users(allowed_users)
    print(f"[INFO] Admin {ADMIN_ID} auto-added to allowed users.")

def load_user_live_ccs(chat_id):
    path = f"live_ccs_{chat_id}.json"
    if not os.path.exists(path):
        logging.debug(f"No live_ccs file for {chat_id}, returning empty list")
        return []
    with open(path, "r") as f:
        data = json.load(f)
        logging.debug(f"Loaded {len(data)} live CCs for {chat_id}")
        return data


def save_user_live_ccs(chat_id, ccs):
    path = f"live_ccs_{chat_id}.json"
    with open(path, "w") as f:
        json.dump(ccs, f, indent=2)
    logging.debug(f"Saved {len(ccs)} live CCs for {chat_id}")


def load_master_live_ccs():
    if not os.path.exists(MASTER_FILE):
        with open(MASTER_FILE, "w") as f:
            json.dump([], f)
        logging.debug("Created empty master_live_ccs.json")
        return []
    with open(MASTER_FILE, "r") as f:
        data = json.load(f)
        logging.debug(f"Loaded {len(data)} master live CCs")
        return data


def save_master_live_ccs(ccs):
    with open(MASTER_FILE, "w") as f:
        json.dump(ccs, f, indent=2)
    logging.debug(f"Saved {len(ccs)} master live CCs")


def load_redeem_codes():
    if not os.path.exists(REDEEM_CODES_FILE):
        with open(REDEEM_CODES_FILE, "w") as f:
            json.dump([], f)
        logging.debug("Created empty redeem_codes.json")
        return []
    with open(REDEEM_CODES_FILE, "r") as f:
        codes = json.load(f)
        logging.debug(f"Loaded redeem codes: {codes}")
        return codes


def save_redeem_codes(codes):
    with open(REDEEM_CODES_FILE, "w") as f:
        json.dump(codes, f, indent=2)
    logging.debug(f"Saved redeem codes: {codes}")


# -------------------------------------------------
# Global State
# -------------------------------------------------
valid_redeem_codes = load_redeem_codes()

register_manual_check(bot, allowed_users)

# -------------------------------------------------
# Utility Functions
# -------------------------------------------------
def generate_redeem_code():
    return "-".join(
        "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
        for _ in range(3)
    )


site_last_instruction = {}
def save_current_site(sites):
    """Save current active sites, removing duplicates but preserving order."""
    unique_sites = []
    for s in sites:
        if s not in unique_sites:
            unique_sites.append(s)

    with open(SITE_STORAGE_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(unique_sites) + "\n")

    logging.debug(f"Saved {len(unique_sites)} sites to {SITE_STORAGE_FILE}")
    return unique_sites


def load_current_site():
    """Load the last used site list from file or return default."""
    try:
        with open(SITE_STORAGE_FILE, "r", encoding="utf-8") as f:
            sites = [line.strip() for line in f if line.strip()]
            return sites if sites else [DEFAULT_API_URL]
    except FileNotFoundError:
        return [DEFAULT_API_URL]


# ================================================================
# Start and Help Commands
# ================================================================
# --- Replace your existing handle_start block with this ---

# Map of command (button id) -> (short label, usage/help text)
COMMAND_HELP = {
    "gen": (
        "/gen",
        "/gen <BIN> <MM|YY>\nGenerate 10 sample test cards (example: /gen 478200 11|25)."
    ),
    "gens": (
        "/gens",
        "/gens <BIN> <MM|YY> <count>\nBulk generate cards (saves as .txt)."
    ),
    "chk": (
        "/chk or .chk",
        "/chk <card>\nCheck a single card. Format: number|mm|yyyy|cvc (example: /chk 4111111111111111|01|2026|123)."
    ),
    "check": (
        "/check",
        "/check <url> [card]\nAnalyze a site's payment gateway. Optionally provide a card."
    ),
    "mass": (
        "/mass",
        "/mass\nUpload a .txt file with cards (one per line) to run a mass check."
    ),
    "site": (
        "/site",
        "/site\nManage your saved sites (add/replace/reset modes)."
    ),
    "sitelist": (
        "/sitelist",
        "/sitelist\nShow your saved sites."
    ),
    "proxy": (
        "/proxy",
        "/proxy\nManage your proxies (add/replace/delete)."
    ),
    "checkproxy": (
        "/checkproxy",
        "/checkproxy <proxy>\nTest a single proxy (format ip:port or socks5://... )."
    ),
    "request": (
        "/request",
        "/request\nSend an access request to the admin."
    ),
    "clean": (
        "/clean",
        "/clean\n Make the file only cards."
    ),
}


@bot.message_handler(commands=["start"])
def handle_start(message):
    chat_id = str(message.chat.id)
    username = (message.from_user.username or message.from_user.first_name or "User")

    # ✅ Automatically ensure the user’s site JSON exists
    ensure_user_site_exists(chat_id)

    # ✅ Automatically create the user's live-cc folder and base JSON
    try:
        user_folder = os.path.join("live-cc", chat_id)
        os.makedirs(user_folder, exist_ok=True)

        # Create an initial Live_cc JSON file if not exists
        base_json = os.path.join(user_folder, f"Live_cc_{chat_id}_1.json")
        if not os.path.exists(base_json):
            with open(base_json, "w", encoding="utf-8") as f:
                f.write("[]")  # empty list
        else:
            # File exists but empty? ensure valid JSON
            with open(base_json, "r+", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    f.seek(0)
                    f.write("[]")
                    f.truncate()
    except Exception as e:
        import logging
        logging.warning(f"[START ERROR] Could not create live folder for {chat_id}: {e}")

    # 🔹 If user is not allowed, show a minimal keyboard with /request
    if chat_id not in allowed_users:
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton("Request access /request", callback_data="usage_request"))
        bot.send_message(
            chat_id,
            f"Hello <b>{username}</b> — you are not authorized yet.\n"
            "Press the button below to see how to request access.",
            parse_mode="HTML",
            reply_markup=kb,
        )
        return

    # 🔹 Authorized user: build main command keyboard
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("Generate (/gen)", callback_data="usage_gen"),
        types.InlineKeyboardButton("Bulk gen (/gens)", callback_data="usage_gens"),
        types.InlineKeyboardButton("Single check (/chk)", callback_data="usage_chk"),
        types.InlineKeyboardButton("Site check (/check)", callback_data="usage_check"),
        types.InlineKeyboardButton("Mass (/mass)", callback_data="usage_mass"),
        types.InlineKeyboardButton("Sites (/site)", callback_data="usage_site"),
        types.InlineKeyboardButton("Sitelist (/sitelist)", callback_data="usage_sitelist"),
        types.InlineKeyboardButton("Proxy (/proxy)", callback_data="usage_proxy"),
        types.InlineKeyboardButton("Check Proxy (/checkproxy)", callback_data="usage_checkproxy"),
        types.InlineKeyboardButton("Clean (/clean)", callback_data="usage_clean"),
    )

    bot.send_message(
        chat_id,
        f"Hello, <b>{username}</b>.\n"
        "Tap any command below to see its usage example.",
        parse_mode="HTML",
        reply_markup=kb,
    )


# -------------------------------------------------------------
# Callback handler for the buttons above (usage messages)
# -------------------------------------------------------------
@bot.callback_query_handler(func=lambda call: str(call.data).startswith("usage_"))
def handle_usage_button(call):
    try:
        data = call.data  # e.g. "usage_gen"
        parts = data.split("_", 1)
        if len(parts) != 2:
            bot.answer_callback_query(call.id, "Invalid request.")
            return
        cmd = parts[1]

        # Get help text (fallback if missing)
        if cmd in COMMAND_HELP:
            label, help_text = COMMAND_HELP[cmd]
        else:
            label, help_text = (cmd, "No usage info available for this command.")

        # Acknowledge the button press (small tooltip)
        bot.answer_callback_query(call.id)

        # Send usage/help as a reply to the user (or optionally edit message)
        import html
        bot.send_message(
            call.message.chat.id,
            f"<b>{label}</b>\n\n<code>{html.escape(help_text)}</code>",
            parse_mode="HTML"
        )


    except Exception as e:
        logging.error(f"[USAGE BUTTON ERROR] {e}")
        try:
            bot.answer_callback_query(call.id, "Error retrieving usage.")
        except Exception:
            pass



# ================================================================
# Admin Command / Help
# ================================================================
@bot.message_handler(commands=["help", "cmd", "cmds", "cmnds"])
def show_commands(message):
    chat_id = str(message.chat.id)

    if chat_id == str(ADMIN_ID):
        # 👑 Full admin command list
        msg = (
            "🤖 <b>Admin Commands</b>\n\n"
            "/chk <code>card|mm|yy|cvc</code> – Check single card\n"
            "/site – Manage sites (Admin)\n"
            "/sitelist – Show sites (Admin)\n"
            "/proxy – Manage proxies (Admin)\n"
            "/get all – Get all live CCs (from all users)\n"
            "/get all <code>USER_ID</code> – Get lives from specific user\n"
            "/get all bin <code>BIN</code> – Filter by BIN (all files)\n"
            "/get all bank <code>BANK</code> – Filter by bank (all files)\n"
            "/get all country <code>COUNTRY</code> – Filter by country (all files)\n"
            "/get_master_data – Admin only\n"
            "/code – Generate redeem code\n"
            "/redeem <code>CODE</code> – Redeem access code\n"
            "/request – Request access\n"
            "/send <code>MESSAGE</code> – Broadcast message\n"
            "/delete <code>USER_ID</code> – Remove user\n"
        )
        safe_send(bot, "send_message", chat_id, msg, parse_mode="HTML")
        logging.debug(f"/help full command list shown to admin {chat_id}")

    else:
        # 👤 Normal user short guide
        short_msg = (
            "✅ Just send .txt file with cards.\n\n"
            "For more command /start."
        )
        safe_send(bot, "send_message", chat_id, short_msg, parse_mode="HTML")
        logging.debug(f"/help short help shown to user {chat_id}")




# ================================================================
# /botdel — Delete an existing sub-bot folder
# ================================================================
@bot.message_handler(commands=["botdel"])
def delete_bot_folder(message):
    import shutil
    import os

    chat_id = str(message.chat.id)
    if chat_id != ADMIN_ID:
        bot.reply_to(message, "🚫 Admin only.")
        return

    args = message.text.strip().split()
    if len(args) != 2:
        bot.reply_to(message, "Usage: /botdel <USER_ID>")
        return

    user_id = args[1]
    folder = os.path.join(os.getcwd(), "bots", user_id)

    try:
        if os.path.exists(folder):
            shutil.rmtree(folder)
            bot.reply_to(message, f"🗑 Bot for {user_id} deleted successfully.")
        else:
            bot.reply_to(message, f"⚠️ No bot found for {user_id}.")
    except Exception as e:
        bot.reply_to(message, f"❌ Error deleting bot: {e}")






# ================================================================
# Admin User Management
# ================================================================
@bot.message_handler(commands=["add"])
def add_user(message):
    chat_id = str(message.chat.id)
    if chat_id != ADMIN_ID:
        safe_send(bot, "reply_to", message, "🚫 Admin only")
        return

    args = message.text.split()
    if len(args) < 2:
        safe_send(bot, "reply_to", message, "Usage: /add USER_ID")
        return

    new_user_id = args[1]
    if new_user_id not in allowed_users:
        allowed_users.append(new_user_id)
        save_allowed_users(allowed_users)
        safe_send(bot, "reply_to", message, f"✅ User {new_user_id} added successfully")
        logging.debug(f"Added new user: {new_user_id}")
    else:
        safe_send(bot, "reply_to", message, f"⚠️ User {new_user_id} already exists")


@bot.message_handler(commands=["delete", "del"])
def delete_user(message):
    chat_id = str(message.chat.id)
    if chat_id != ADMIN_ID:
        bot.reply_to(message, "🚫 Admin only")
        return

    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "Usage: /delete USER_ID")
        return

    user_id_to_delete = args[1]
    if user_id_to_delete in allowed_users:
        allowed_users.remove(user_id_to_delete)
        save_allowed_users(allowed_users)

        if os.path.exists(f"live_ccs_{user_id_to_delete}.json"):
            os.remove(f"live_ccs_{user_id_to_delete}.json")

        safe_send(bot, "reply_to", message, f"✅ User {user_id_to_delete} removed")
        logging.debug(f"Deleted user: {user_id_to_delete}")
    else:
        safe_send(bot, "reply_to", message, "⚠️ User not found")
# ================================================================
# Redeem Codes
# ================================================================
@bot.message_handler(commands=["code"])
def generate_code(message):
    chat_id = str(message.chat.id)
    if chat_id != ADMIN_ID:
        bot.reply_to(message, "🚫 Admin only")
        return

    new_code = generate_redeem_code()
    valid_redeem_codes.append(new_code)
    save_redeem_codes(valid_redeem_codes)

    bot.reply_to(
        message,
        f"<b>🎉 New Redeem Code 🎉</b>\n\n<code>{new_code}</code>",
        parse_mode="HTML",
    )
    logging.debug(f"Generated redeem code: {new_code}")


@bot.message_handler(commands=["redeem"])
def redeem_code(message):
    chat_id = str(message.chat.id)
    args = message.text.split()
    if len(args) < 2:
        safe_send(bot, "reply_to", message, "Usage: /redeem CODE")
        return

    code = args[1]
    if code in valid_redeem_codes:
        if chat_id not in allowed_users:
            allowed_users.append(chat_id)
            save_allowed_users(allowed_users)
            valid_redeem_codes.remove(code)
            save_redeem_codes(valid_redeem_codes)
            safe_send(bot, "reply_to", message, "✅ Access granted!")
            logging.debug(f"User {chat_id} redeemed code {code}")
        else:
            safe_send(bot, "reply_to", message, "⚠️ You already have access")
    else:
        safe_send(bot, "reply_to", message, "❌ Invalid code")


# ================================================================
# /request — user access request with Approve / Decline buttons
# ================================================================

@bot.message_handler(commands=["request"])
def handle_request(message):
    chat_id = str(message.chat.id)
    user = message.from_user

    # Already approved
    if chat_id in allowed_users:
        bot.reply_to(message, "✅ You already have access. Use /start to continue.")
        return

    username = f"@{user.username}" if user.username else user.first_name or "User"
    user_info = f"👤 <b>{username}</b>\n🆔 <code>{chat_id}</code>"

    # Notify requester
    bot.reply_to(message, "⌛ Your access request has been sent to the admin.")

    # Build inline keyboard
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✅ Approve", callback_data=f"approve_{chat_id}"),
        types.InlineKeyboardButton("❌ Decline", callback_data=f"decline_{chat_id}")
    )

    # Send to admin + channel
    text = f"📨 <b>New Access Request</b>\n\n{user_info}"
    try:
        bot.send_message(ADMIN_ID, text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logging.error(f"[REQUEST ERROR] {e}")




# ================================================================
# /gen Preview Builder
# ================================================================
def build_gen_preview_html(bin_prefix, expiry, lines, bin_info, username_display):
    """Build HTML preview for /gen output."""

    def escape_html(x):
        return html.escape(str(x))

    lines_preview = "\n".join(f"<code>{escape_html(l)}</code>" for l in lines)
    flag = bin_info.get("country_flag", "")
    bank = escape_html(bin_info.get("bank", "Unknown Bank"))
    display = escape_html(bin_info.get("display_clean", "Unknown"))
    country = escape_html(bin_info.get("country", "Unknown Country"))

    html_content = (
        "<b>✅ Card Generated Successfully ✅</b>\n\n"
        f"<b>BIN →</b> <code>{escape_html(bin_info.get('bin'))}</code>\n"
        f"<b>Amount →</b> {len(lines)}\n"
        f"<b>Expiry →</b> {escape_html(expiry)}\n\n"
        f"{lines_preview}\n\n"
        f"<b>Info:</b> {display}\n"
        f"<b>Issuer:</b> {bank}\n"
        f"<b>Country:</b> {country} {flag}\n\n"
        f"<b>Generated By:</b> {escape_html(username_display)}"
    )

    return html_content


# ================================================================
# /gen command — Always 10 valid cards + correct BIN info
# ================================================================
@bot.message_handler(commands=["gen"])
def handle_gen(message):
    chat_id = str(message.chat.id)
    set_active_command(chat_id, "gen")
    reset_user_states(chat_id)

    if chat_id not in allowed_users:
        bot.reply_to(
            message,
            "🚫 You don't have access to this command.\nUse /request to ask the admin.",
        )
        return

    text = message.text.replace("|", " ").strip()
    parts = text.split()

    # Support `/gen BIN` or `/gen BIN MM YY`
    if len(parts) == 2:
        _, bin_prefix = parts
        expiry_text = "Random per card"
        use_random_expiry = True
        mm = yy = None
    elif len(parts) >= 4:
        _, bin_prefix, mm, yy = parts[:4]
        expiry_text = f"{mm}|{yy}"
        use_random_expiry = False
    else:
        msg = bot.reply_to(
            message,
            "❌ Usage: /gen [BIN] [MM YY]\nExample: /gen 123456 12 29\nOr: /gen 123456 (random expiry)",
        )
        _auto_delete_message_later(bot, chat_id, msg.message_id, delay=6)
        return

    # ✅ Ensure we always get 10 cards
    try:
        cards = []
        max_retries = 15
        while len(cards) < 10 and max_retries > 0:
            needed = 10 - len(cards)
            new_cards = (
                generate_luhn_cards_parallel(bin_prefix, needed)
                if use_random_expiry
                else generate_luhn_cards_fixed_expiry(bin_prefix, mm, yy, needed)
            )
            cards.extend(new_cards)
            max_retries -= 1

        cards = cards[:10]
        if len(cards) < 10:
            raise RuntimeError(f"Only generated {len(cards)} cards after retries.")
    except Exception as e:
        msg = bot.reply_to(message, f"⚠️ Error generating cards: {e}")
        _auto_delete_message_later(bot, chat_id, msg.message_id, delay=6)
        clear_active_command(chat_id)
        return

    # ✅ Use correct BIN info from bininfo.py
    try:
        bin_info = round_robin_bin_lookup(bin_prefix)
    except Exception as e:
        logging.warning(f"BIN lookup failed: {e}")
        bin_info = {
            "bin": bin_prefix[:6],
            "display_clean": "Unknown",
            "bank": "Unknown Bank",
            "country": "Unknown Country",
            "country_flag": "",
        }

    # ✅ Get username display
    try:
        user = bot.get_chat(chat_id)
        username_display = (
            f"@{user.username}" if user.username else user.first_name or f"User {chat_id}"
        )
    except Exception:
        username_display = f"User {chat_id}"

    # ✅ Build HTML preview
    html_preview = build_gen_preview_html(
        bin_prefix, expiry_text, cards, bin_info, username_display
    )

    keyboard = types.InlineKeyboardMarkup(row_width=1)
    cb_data = f"regen|{bin_prefix}|{'RANDOM' if use_random_expiry else expiry_text}"
    keyboard.add(types.InlineKeyboardButton("🎲 Regenerate CC", callback_data=cb_data))

    bot.send_message(chat_id, html_preview, parse_mode="HTML", reply_markup=keyboard)
    clear_active_command(chat_id)



# ================================================================
# /regen callback handler — Always 10 valid cards + correct BIN info
# ================================================================
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("regen|"))
def handle_regenerate_callback(call):
    try:
        _, bin_prefix, expiry = call.data.split("|", 2)

        # ✅ Generate exactly 10 valid cards (same as /gen)
        try:
            cards = []
            max_retries = 15
            if expiry == "RANDOM":
                use_random_expiry = True
            else:
                use_random_expiry = False
                mm, yy = expiry.split("|")

            while len(cards) < 10 and max_retries > 0:
                needed = 10 - len(cards)
                new_cards = (
                    generate_luhn_cards_parallel(bin_prefix, needed)
                    if use_random_expiry
                    else generate_luhn_cards_fixed_expiry(bin_prefix, mm, yy, needed)
                )
                cards.extend(new_cards)
                max_retries -= 1

            cards = cards[:10]
            if len(cards) < 10:
                raise RuntimeError(f"Only generated {len(cards)} cards after retries.")
        except Exception as e:
            bot.answer_callback_query(call.id, f"⚠️ Card generation failed: {e}", show_alert=True)
            return

        # ✅ Use your proper bininfo.py lookup
        try:
            from bininfo import round_robin_bin_lookup
            bin_info = round_robin_bin_lookup(bin_prefix)
        except Exception as e:
            logging.warning(f"BIN lookup failed during regen: {e}")
            bin_info = {
                "bin": bin_prefix[:6],
                "display_clean": "Unknown",
                "bank": "Unknown Bank",
                "country": "Unknown Country",
                "country_flag": "",
            }

        # ✅ Get user display name
        try:
            user = bot.get_chat(call.from_user.id)
            username_display = (
                f"@{user.username}"
                if user.username
                else user.first_name or f"User {call.from_user.id}"
            )
        except Exception:
            username_display = f"User {call.from_user.id}"

        # ✅ Build HTML output
        display_expiry = "Random per card" if expiry == "RANDOM" else expiry
        new_html = build_gen_preview_html(
            bin_prefix, display_expiry, cards, bin_info, username_display
        )

        # ✅ Update message inline
        bot.edit_message_text(
            new_html,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode="HTML",
            reply_markup=call.message.reply_markup,
        )
        bot.answer_callback_query(call.id)

    except Exception as e:
        bot.answer_callback_query(call.id, f"⚠️ Error: {e}", show_alert=True)

@bot.message_handler(commands=["gens"])
def handle_gens(message):
    chat_id = str(message.chat.id)
    set_active_command(chat_id, "gens")
    reset_user_states(chat_id)

    if chat_id not in allowed_users:
        bot.reply_to(
            message,
            "🚫 You don't have access to this command.\nUse /request to ask the admin.",
        )
        return

    text = message.text.replace("|", " ").strip()
    parts = text.split()

    # /gens BIN COUNT
    # /gens BIN MM YY COUNT
    if len(parts) == 3:
        _, bin_prefix, count_str = parts
        use_random_expiry = True
        mm, yy = None, None
    elif len(parts) == 5:
        _, bin_prefix, mm, yy, count_str = parts
        use_random_expiry = False
    else:
        msg = bot.reply_to(
            message,
            "❌ Usage: /gens [BIN] [COUNT]\n   or /gens [BIN] [MM YY] [COUNT]\n"
            "Example:\n/gens 123456 100\n/gens 123456 12 29 100",
        )
        _auto_delete_message_later(bot, chat_id, msg.message_id, delay=6)
        return

    try:
        count = int(count_str)
        if count <= 0 or count > 5000:
            raise ValueError("Count must be between 1 and 5000")
    except Exception as e:
        msg = bot.reply_to(message, f"⚠️ Invalid count: {e}")
        _auto_delete_message_later(bot, chat_id, msg.message_id, delay=6)
        return

    msg = bot.reply_to(message, f"⏳ Generating {count} cards for BIN {bin_prefix}...")
    now = datetime.now()

    def background():
        try:
            cards = []
            max_retries = 20
            expiry_text = None

            while len(cards) < count and max_retries > 0:
                needed = count - len(cards)
                if use_random_expiry:
                    new_cards = generate_luhn_cards_parallel(bin_prefix, needed)
                    expiry_text = "Random per card"
                else:
                    new_cards = generate_luhn_cards_fixed_expiry(bin_prefix, mm, yy, needed)
                    expiry_text = f"{mm}|{yy}"

                cards.extend(new_cards)
                # Deduplicate cards
                cards = list(dict.fromkeys(cards))
                max_retries -= 1

            cards = cards[:count]

            if len(cards) < count:
                bot.send_message(
                    chat_id,
                    f"⚠️ Warning: Only generated {len(cards)} cards out of requested {count}.",
                )

            path = save_cards_to_file(message.from_user.id, cards)

            try:
                bin_info = get_bin_info(bin_prefix)
            except Exception:
                bin_info = {"bin": bin_prefix[:6]}

            username = (
                f"@{message.from_user.username}"
                if message.from_user.username
                else message.from_user.first_name or "User"
            )

            caption = (
                f"📦 Generated {len(cards)} cards!\n\n"
                f"BIN: <code>{bin_info.get('bin')}</code>\n"
                f"Expiry: <b>{expiry_text}</b>\n"
                f"Generated by: <b>{username}</b>"
            )

            from cardgen import delete_generated_file
            import threading

            with open(path, "rb") as f:
                bot.send_document(chat_id, f, caption=caption, parse_mode="HTML")

            # Optional: log to your channel
            try:
                with open(path, "rb") as f:
                    bot.send_document(
                        CHANNEL_ID,
                        f,
                        caption=f"📤 New BIN generation\n\n{caption}",
                        parse_mode="HTML",
                    )
            except Exception:
                pass

            # 🧹 Auto-delete generated file after sending (safe 3s delay)
            threading.Timer(3.0, delete_generated_file, args=(path,)).start()


        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Error: {e}")
        finally:
            try:
                bot.delete_message(chat_id, msg.message_id)
            except Exception:
                pass
            clear_active_command(chat_id)

    threading.Thread(target=background, daemon=True).start()


# ================================================================
# /check site — Single reply, auto-update like sitechk.py
# ================================================================
@bot.message_handler(commands=["check"])
def handle_check(message):
    import asyncio
    import types
    from sitechk import check_command, get_base_url

    chat_id = str(message.chat.id)
    set_active_command(chat_id, "check")
    reset_user_states(chat_id)

    if chat_id not in allowed_users:
        bot.reply_to(message, "🚫 You don't have access to this command.\nUse /request to ask the admin.")
        return

    text = message.text.strip()
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        msg = bot.reply_to(message, "❌ Usage: /check <url>\nExample: /check https://example.com", parse_mode="HTML")
        _auto_delete_message_later(bot, chat_id, msg.message_id, delay=5)
        return

    user_url = parts[1].strip()
    if not user_url.startswith(("http://", "https://")):
        user_url = "https://" + user_url

    base_url = get_base_url(user_url)

    # ✅ Reply once to the user's message
    sent_msg = bot.reply_to(message, f"⏳ Checking site: <code>{base_url}</code>\nPlease wait...", parse_mode="HTML")

    # ✅ Emulate python-telegram-bot message with edit capability
    class DummyMessage:
        def __init__(self, chat_id, message_id):
            self.chat = types.SimpleNamespace(id=chat_id)
            self.message_id = message_id

        async def reply_text(self, text, **kwargs):
            # Use the original message's reply instead of new message
            bot.edit_message_text(
                text, chat_id=self.chat.id, message_id=self.message_id, **kwargs
            )
            return self

        async def edit_text(self, text, **kwargs):
            bot.edit_message_text(
                text, chat_id=self.chat.id, message_id=self.message_id, **kwargs
            )

    class DummyUpdate:
        def __init__(self, chat_id, message_id):
            self.message = DummyMessage(chat_id, message_id)

    class DummyContext:
        def __init__(self, args):
            self.args = args

    async def run_check():
        try:
            update = DummyUpdate(chat_id, sent_msg.message_id)
            context = DummyContext([user_url])
            await check_command(update, context)
        except Exception as e:
            bot.edit_message_text(
                f"⚠️ Error while checking site: {e}",
                chat_id=chat_id,
                message_id=sent_msg.message_id,
                parse_mode="HTML",
            )
        finally:
            clear_active_command(chat_id)

    asyncio.run(run_check())



# ================================================================
# Access Request Approvals (Approve / Decline)
# ================================================================
@bot.callback_query_handler(func=lambda call: call.data.startswith(("approve_", "decline_")))
def handle_access_callback(call):
    try:
        action, user_id = call.data.split("_", 1)
        user_id = str(user_id)

        # Only admin can approve or decline
        if str(call.from_user.id) != str(ADMIN_ID):
            bot.answer_callback_query(call.id, "🚫 Not allowed", show_alert=True)
            return

        # Remove inline buttons
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception:
            pass

        # Approve user
        if action == "approve":
            if user_id not in allowed_users:
                allowed_users.append(user_id)
                save_allowed_users(allowed_users)
                ensure_user_default_site(user_id)
                # Send messages
                bot.send_message(user_id, "✅ Your access request was approved!\nUse /start to begin.")
                bot.send_message(ADMIN_ID, f"✅ Approved access for {user_id}")
            else:
                bot.send_message(ADMIN_ID, f"⚠️ {user_id} is already approved.")

        # Decline user
        elif action == "decline":
            bot.send_message(user_id, "❌ Your access request was declined by the admin.")
            bot.send_message(ADMIN_ID, f"❌ Declined access for {user_id}")

        bot.answer_callback_query(call.id, "Done ✅")

    except Exception as e:
        logging.error(f"[ACCESS CALLBACK ERROR] {e}")
        bot.answer_callback_query(call.id, "⚠️ Error processing request", show_alert=True)



# ================================================================
# Per-User Site Management
# ================================================================
user_site_last_instruction = {}


def get_user_site(chat_id):
    """
    Returns the first site URL for this user from their JSON,
    or DEFAULT_API_URL if none found.
    """
    chat_id = str(chat_id)
    state = _load_state(chat_id)
    user_data = state.get(chat_id, {})

    # 🧠 Look inside "sites"
    sites_dict = user_data.get("sites", {})
    if sites_dict:
        # Return first site key
        return next(iter(sites_dict.keys()))

    # fallback if user has no sites
    from runtime_config import get_default_site
    return get_default_site()




def set_user_site(chat_id, site_url):
    """Assign a site to the user and ensure it exists in sites.json."""
    manager = SiteAuthManager(site_url, chat_id)
    manager._ensure_entry()


def normalize_site_url(site_url: str) -> str:
    parsed = urlparse(site_url.strip())
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else site_url.rstrip("/")


def replace_user_sites(chat_id, new_sites):
    """Replace user sites with a new list of URLs (per-user JSON only)."""
    chat_id = str(chat_id)
    state = _load_state(chat_id)
    state[chat_id] = {}

    for site_url in new_sites:
        site_url = normalize_site_url(site_url)
        state[chat_id][site_url] = {
            "accounts": [],
            "cookies": None,
            "payment_count": 0,
            "mode": "rotate"
        }

    _save_state(state, chat_id)



# ================================================================
# /site command — Manage Site List
# ================================================================
@bot.message_handler(commands=["site"])
def site_command(message):
    chat_id = str(message.chat.id)
    set_active_command(chat_id, "site")
    reset_user_states(chat_id)

    if is_user_busy(chat_id):
        msg = bot.reply_to(
            message,
            "🚫 You are currently running a checking. Please wait until it finish."
        )

        # 🧹 Auto-delete after 5 seconds
        import threading
        threading.Timer(5.0, lambda: bot.delete_message(message.chat.id, msg.message_id)).start()

        return

    if chat_id not in allowed_users:
        bot.reply_to(
            message,
            "🚫 You don't have access to this command.\nUse /request to ask the admin.",
        )
        return

    # Build inline keyboard
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("➕ Replace", callback_data="replace_site"),
        types.InlineKeyboardButton("❌ Cancel", callback_data="finish_site"),
        types.InlineKeyboardButton("♻ Default", callback_data="reset_site"),
        types.InlineKeyboardButton("⚙ Mode", callback_data="mode_menu"),
    )

    sent_msg = bot.send_message(chat_id, "⚙ Manage site list:", reply_markup=keyboard)
    user_site_last_instruction[chat_id] = sent_msg.message_id
    

from site_auth_manager import replace_user_sites  # add this at the top of main.py

@bot.message_handler(func=lambda message: message.chat.id in user_sites)
def collect_sites(message):
    chat_id = message.chat.id
    text = (message.text or "").strip()

    # Handle “done”, “finish”, or “cancel”
    if text.lower() in ["done", "finish", "cancel"]:
        sites = user_sites.pop(chat_id, [])
        if sites:
            saved_sites = replace_user_sites(chat_id, sites)
            bot.send_message(
                chat_id,
                "✅ Sites saved successfully:\n"
                + "\n".join(f"<code>{s}</code>" for s in saved_sites),
                parse_mode="HTML"
            )
        else:
            bot.send_message(chat_id, "⚠ No sites were added.")
        return

    # Handle user messages containing URLs
    urls = []
    for word in text.replace(",", "\n").split():
        if "http" in word or "." in word:
            urls.append(word.strip())

    if urls:
        for url in urls:
            user_sites[chat_id].append(url)
        bot.send_message(
            chat_id,
            "🆕 Added:\n" + "\n".join(f"<code>{u}</code>" for u in urls),
            parse_mode="HTML"
        )
    else:
        bot.send_message(
            chat_id,
            "⚠ Please send valid URLs or type <b>done</b> when finished.",
            parse_mode="HTML"
        )



from runtime_config import (
    set_default_sites,
    get_default_site,
    get_all_default_sites,
    RUNTIME_CONFIG
)
from telebot import types
import re, json, os, threading
from importlib import reload

# Temporary admin state
admin_default_editing = {}

# ================================================================
# 🧩 Admin Command — Manage Default Sites
# ================================================================
@bot.message_handler(commands=["default"])
def handle_default_sites(message):
    """Admin command to manage multiple default sites."""
    chat_id = str(message.chat.id)

    # 🔒 Only admin can use
    if chat_id != str(ADMIN_ID):
        bot.reply_to(message, "🚫 Only the admin can manage default sites.")
        return

    current_sites = get_all_default_sites()
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("🔄 Replace", callback_data="default_replace"),
        types.InlineKeyboardButton("❌ Cancel", callback_data="default_cancel")
    )

    sent_msg = bot.send_message(
        chat_id,
        f"<code>Current default sites:</code>\n"
        + "\n".join(f"• <code>{s}</code>" for s in current_sites)
        + "\n\n<code>Do you want to replace them?</code>",
        parse_mode="HTML",
        reply_markup=keyboard
    )

    # Track message ID for cleanup after action
    user_site_last_instruction[chat_id] = sent_msg.message_id



# ================================================================
# ⚙️ Inline button handling for /default
# ================================================================
@bot.callback_query_handler(func=lambda c: c.data in ["default_replace", "default_cancel"])
def handle_default_buttons(call):
    chat_id = str(call.from_user.id)

    # 🔒 Restrict to admin
    if chat_id != str(ADMIN_ID):
        bot.answer_callback_query(call.id, "🚫 You are not the admin.")
        return

    # 🧹 Auto-delete the old menu message
    try:
        if chat_id in user_site_last_instruction:
            msg_id = user_site_last_instruction.pop(chat_id)
            bot.delete_message(chat_id, msg_id)
    except Exception as e:
        logging.debug(f"[AUTO-DELETE DEFAULT MESSAGE] {e}")

    # ❌ Cancel
    if call.data == "default_cancel":
        bot.answer_callback_query(call.id, "❌ Cancelled.")
        bot.send_message(chat_id, "❌ Default site edit cancelled.")
        return

    # ✅ Replace selected
    bot.answer_callback_query(call.id, "Send your new sites")
    admin_default_editing[chat_id] = True
    bot.send_message(
        chat_id,
        "📩 Please send your new sites now (one per line or comma-separated).\n\n"
        "Example:\n"
        "`https://site1.com`\n"
        "`https://site2.com`\n"
        "`https://site3.com`\n",
        parse_mode="Markdown"
    )


# ================================================================
# 📩 Capture admin input for new default sites
# ================================================================
@bot.message_handler(func=lambda m: str(m.chat.id) in admin_default_editing)
def capture_default_sites(message):
    chat_id = str(message.chat.id)
    text = message.text.strip()

    # ❌ Cancel
    if text.lower() in ["cancel", "stop", "done"]:
        del admin_default_editing[chat_id]
        bot.send_message(chat_id, "❌ Cancelled default site setup.")
        return

    # 🌐 Extract all valid URLs
    urls = re.findall(r'https?://[^\s,]+', text)
    if not urls:
        bot.send_message(chat_id, "⚠️ No valid URLs found. Try again.")
        return

    # 🧹 Normalize base domains
    from urllib.parse import urlparse
    cleaned = []
    for u in urls:
        parsed = urlparse(u.strip())
        base = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        if base not in cleaned:
            cleaned.append(base)

    # 💾 Save to runtime_config.json
    saved = set_default_sites(cleaned)
    del admin_default_editing[chat_id]

    msg = "\n".join(f"• <code>{s}</code>" for s in saved)
    confirmation_msg = bot.send_message(
        chat_id,
        f"✅ Default sites updated successfully:\n{msg}",
        parse_mode="HTML"
    )

    # 🕒 Auto-delete confirmation after 8 seconds
    def delete_later():
        time.sleep(8)
        try:
            bot.delete_message(chat_id, confirmation_msg.message_id)
        except Exception:
            pass

    threading.Thread(target=delete_later, daemon=True).start()


# ================================================================
# ♻️ Admin Command — Reset Default Sites to config.py default
# ================================================================
@bot.message_handler(commands=["resetdefault"])
def handle_reset_default_sites(message):
    """Reset runtime_config.json to the single-site default from config.py"""
    chat_id = str(message.chat.id)

    # 🔒 Admin-only
    if chat_id != str(ADMIN_ID):
        bot.reply_to(message, "🚫 Only the admin can reset defaults.")
        return

    import runtime_config, site_auth_manager, mass_check, manual_check

    # 🗑️ Delete runtime_config.json
    if os.path.exists(RUNTIME_CONFIG):
        try:
            os.remove(RUNTIME_CONFIG)
            bot.send_message(chat_id, "🗑️ runtime_config.json removed.")
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Failed to delete runtime_config.json: <code>{e}</code>", parse_mode="HTML")

    # 🔁 Reload dependent modules
    try:
        reload(runtime_config)
        reload(site_auth_manager)
        reload(mass_check)
        reload(manual_check)
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ Reload failed: <code>{e}</code>", parse_mode="HTML")
        return

    new_default = get_default_site()
    confirmation_msg = bot.send_message(
        chat_id,
        f"♻️ Default sites reset to <code>{new_default}</code>",
        parse_mode="HTML"
    )

    # 🕒 Auto-delete after 8 seconds
    def delete_later():
        time.sleep(8)
        try:
            bot.delete_message(chat_id, confirmation_msg.message_id)
        except Exception:
            pass

    threading.Thread(target=delete_later, daemon=True).start()


# ================================================================
# 👀 Show Current Default Sites
# ================================================================
@bot.message_handler(commands=["showdefault"])
def handle_show_default_sites(message):
    """Show all current default sites (admin only)."""
    chat_id = str(message.chat.id)

    # 🔒 Restrict to admin only
    if chat_id != str(ADMIN_ID):
        bot.reply_to(message, "🚫 Only the admin can view the default sites.")
        return

    sites = get_all_default_sites()
    msg = "\n".join(f"• <code>{s}</code>" for s in sites)
    sent_msg = bot.send_message(
        chat_id,
        f"🌐 <b>Current default sites ({len(sites)} total):</b>\n{msg}",
        parse_mode="HTML"
    )

    # 🕒 Auto-delete after 10 seconds
    def delete_later():
        time.sleep(10)
        try:
            bot.delete_message(chat_id, sent_msg.message_id)
        except Exception:
            pass

    threading.Thread(target=delete_later, daemon=True).start()




@bot.message_handler(commands=["sitelist"])
def sitelist(message):
    """Show all sites for user/admin with correct default/custom handling."""
    import threading, time, logging
    from html import escape
    from runtime_config import get_all_default_sites
    from site_auth_manager import _load_state

    chat_id = str(message.chat.id)
    is_admin = (chat_id == str(ADMIN_ID))

    # 🚫 Restrict access
    if chat_id not in allowed_users and not is_admin:
        bot.reply_to(
            message,
            "🚫 You don't have access to this command.\nUse /request to ask the admin.",
        )
        return

    runtime_defaults = [s.rstrip("/") for s in get_all_default_sites()]
    state = _load_state(chat_id)
    user_data = state.get(chat_id, {}) if state else {}
    user_sites = [s.rstrip("/") for s in user_data.get("sites", {}).keys()]
    defaults_snapshot = [s.rstrip("/") for s in user_data.get("defaults_snapshot", [])]

    # ADMIN LOGIC
    if is_admin:
        if not user_sites:
            sites_text = "\n".join(
                f"{i+1}. <code>{escape(s)}</code>" for i, s in enumerate(runtime_defaults)
            )
            msg = (
                "<b>Default Site List</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"{sites_text}\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "You are using default sites."
            )
        else:
            sites_text = "\n".join(
                f"{i+1}. <code>{escape(s)}</code>" for i, s in enumerate(user_sites)
            )
            msg = (
                "<b>Default Site List</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"{sites_text}\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "These are your uploaded sites."
            )
        sent_msg = bot.send_message(chat_id, msg, parse_mode="HTML")

    # NORMAL USER LOGIC
    else:
        if (
            not user_sites
            or (defaults_snapshot and set(user_sites) == set(defaults_snapshot))
            or (not defaults_snapshot and set(user_sites) == set(runtime_defaults))
        ):
            sent_msg = bot.send_message(
                chat_id,
                "<code>Default site in use.</code>\n"
                "<code>Please add your own site using</code> /site.",
                parse_mode="HTML",
            )
        else:
            custom_sites = [
                s
                for s in user_sites
                if s not in runtime_defaults and s not in defaults_snapshot
            ]
            if not custom_sites and user_sites:
                custom_sites = user_sites

            sites_text = "\n".join(
                f"{i+1}. <code>{escape(s)}</code>"
                for i, s in enumerate(custom_sites)
            )
            sent_msg = bot.send_message(
                chat_id,
                f"<b>Your current active site(s):</b>\n{sites_text}",
                parse_mode="HTML",
            )

    # 🕒 Auto-delete for ALL users (including admin)
    def delete_later(chat_id, msg_id):
        time.sleep(8)
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception as e:
            print(f"[DELETE ERROR] Could not delete {msg_id}: {e}")

    threading.Thread(
        target=delete_later,
        args=(chat_id, sent_msg.message_id),
        daemon=True
    ).start()








# ================================================================
# Inline Site Management Buttons
# ================================================================
@bot.callback_query_handler(
    func=lambda call: call.data.startswith("finish_replace_")
    or call.data
    in [
        "replace_site",
        "reset_site",
        "finish_site",
        "mode_menu",
        "set_mode_rotate",
        "set_mode_all",
        "mode_menu_after_replace",
        "set_mode_rotate_after",
        "set_mode_all_after",
        "site_back",
    ]
)
def handle_site_buttons(call):
    """
    Inline button handling for site management.
    - replace_site → ask user for new URL
    - reset_site → reset to default site
    - finish_site → cancel
    """
    chat_id = str(call.from_user.id)

    # ------------------------------------------------------------
    # Replace Site
    # ------------------------------------------------------------
    if call.data == "replace_site":
        bot.answer_callback_query(call.id, "Send your new site URL")
        instr_msg = bot.send_message(
            call.message.chat.id,
            "Please send your new site URL now.\nSend as many as you can."
        )

        # Save BOTH menu + prompt IDs
        user_site_last_instruction[chat_id] = {
            "menu": call.message.message_id,
            "prompt": instr_msg.message_id,
        }

        # Change menu to Cancel-only mode
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        keyboard.add(types.InlineKeyboardButton("⬅️ Cancel", callback_data="finish_site"))
        try:
            bot.edit_message_reply_markup(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=keyboard,
            )
        except Exception as e:
            logging.warning(f"Could not update site menu: {e}")

    # ------------------------------------------------------------
    # Reset Site (Per User Only)
    # ------------------------------------------------------------
    elif call.data == "reset_site":
        try:
            from site_auth_manager import reset_user_sites
            from runtime_config import get_default_site

            # ✅ Reset site folder properly (uses new internal structure)
            reset_user_sites(chat_id)

            live_default = get_default_site()
            logging.info(f"[RESET_SITE] Reset site for user {chat_id} → {live_default}")

            # ✅ Notify user
            bot.answer_callback_query(call.id, "✅ Site reset to default")
            sent_msg = bot.send_message(
                call.message.chat.id,
                f"<code>Your site has been reset to the default.</code>",
                parse_mode="HTML"
            )

        except Exception as e:
            logging.error(f"[RESET_SITE ERROR] {chat_id}: {e}")
            bot.send_message(
                call.message.chat.id,
                f"❌ Error resetting site: <code>{html.escape(str(e))}</code>",
                parse_mode="HTML"
            )
            return

        # 🧹 Cleanup old menu and confirmation messages
        def cleanup():
            time.sleep(2)
            try:
                # Delete inline menu and confirmation messages
                bot.delete_message(call.message.chat.id, call.message.message_id)
                bot.delete_message(call.message.chat.id, sent_msg.message_id)

                # Delete any leftover instruction messages
                if chat_id in user_site_last_instruction:
                    instr = user_site_last_instruction.pop(chat_id)
                    if isinstance(instr, dict):
                        for mid in instr.values():
                            try:
                                bot.delete_message(call.message.chat.id, mid)
                            except Exception:
                                pass
            except Exception as e:
                logging.debug(f"[RESET_SITE CLEANUP] {chat_id}: {e}")

            clear_active_command(chat_id)
            reset_user_states(chat_id)

        threading.Thread(target=cleanup, daemon=True).start()



    # ------------------------------------------------------------
    # Finish / Cancel Site Management
    # ------------------------------------------------------------
    elif call.data == "finish_site":
        bot.answer_callback_query(call.id, "❌ Site management canceled.")

        def cleanup():
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
                if chat_id in user_site_last_instruction:
                    try:
                        bot.delete_message(
                            call.message.chat.id, user_site_last_instruction[chat_id]
                        )
                    except Exception:
                        pass
                    del user_site_last_instruction[chat_id]
                sent_msg = bot.send_message(
                    call.message.chat.id, "❌ Site management canceled."
                )
                time.sleep(2)
                bot.delete_message(call.message.chat.id, sent_msg.message_id)
            except Exception as e:
                logging.debug(f"Could not auto-delete finish_site messages: {e}")

        threading.Thread(target=cleanup, daemon=True).start()
        clear_active_command(chat_id)

    # ------------------------------------------------------------
    # Mode Menu
    # ------------------------------------------------------------
    elif call.data == "mode_menu":
        keyboard = types.InlineKeyboardMarkup(row_width=2)
        keyboard.add(
            types.InlineKeyboardButton("🔄 Rotate", callback_data="set_mode_rotate"),
            types.InlineKeyboardButton("📋 All", callback_data="set_mode_all"),
            types.InlineKeyboardButton("⬅️ Back", callback_data="site_back"),
        )
        try:
            bot.edit_message_text(
                "⚙ Choose site mode:",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=keyboard,
            )
        except Exception as e:
            logging.error(f"Error showing mode menu: {e}")

    # ------------------------------------------------------------
    # Set Mode to Rotate
    # ------------------------------------------------------------
    elif call.data == "set_mode_rotate":
        try:
            state = _load_state(chat_id)
            chat_id = str(call.message.chat.id)
            user_sites = list(state.get(chat_id, {}).keys())
            if user_sites:
                first_site = user_sites[0]
                state[chat_id][first_site]["mode"] = "rotate"
                _save_state(state, chat_id)
            bot.answer_callback_query(call.id, "✅ Mode set to Rotate")
        except Exception as e:
            logging.error(f"Error setting mode to rotate: {e}")

        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception as e:
            logging.warning(f"Auto-delete failed for site mode message: {e}")

    # ------------------------------------------------------------
    # Set Mode to All
    # ------------------------------------------------------------
    elif call.data == "set_mode_all":
        try:
            state = _load_state(chat_id)
            chat_id = str(call.message.chat.id)
            user_sites = list(state.get(chat_id, {}).keys())
            if user_sites:
                first_site = user_sites[0]
                state[chat_id][first_site]["mode"] = "all"
                _save_state(state, chat_id)
            bot.answer_callback_query(call.id, "✅ Mode set to All")
        except Exception as e:
            logging.error(f"Error setting mode to all: {e}")

        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception as e:
            logging.warning(f"Auto-delete failed for site mode message: {e}")
    # ------------------------------------------------------------
    # Back to Main Site Menu
    # ------------------------------------------------------------
    elif call.data == "site_back":
        keyboard = types.InlineKeyboardMarkup(row_width=2)
        keyboard.add(
            types.InlineKeyboardButton("➕ Replace", callback_data="replace_site"),
            types.InlineKeyboardButton("❌ Cancel", callback_data="finish_site"),
            types.InlineKeyboardButton("♻ Default", callback_data="reset_site"),
            types.InlineKeyboardButton("⚙ Mode", callback_data="mode_menu"),
        )
        try:
            bot.edit_message_text(
                "⚙ Manage site list:",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=keyboard,
            )
        except Exception as e:
            logging.error(f"Error restoring main menu: {e}")

    # ------------------------------------------------------------
    # Mode Menu After Replace
    # ------------------------------------------------------------
    elif call.data == "mode_menu_after_replace":
        keyboard = types.InlineKeyboardMarkup(row_width=2)
        keyboard.add(
            types.InlineKeyboardButton("🔄 Rotate", callback_data="set_mode_rotate_after"),
            types.InlineKeyboardButton("📋 All", callback_data="set_mode_all_after"),
            types.InlineKeyboardButton("⬅️ Back", callback_data="site_back"),
        )
        try:
            bot.edit_message_text(
                "⚙ Choose site mode:",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=keyboard,
            )
        except Exception as e:
            logging.error(f"Error showing mode menu after replace: {e}")

    # ------------------------------------------------------------
    # Set Mode to Rotate (After Replace)
    # ------------------------------------------------------------
    elif call.data == "set_mode_rotate_after":
        try:
            state = _load_state(chat_id)
            chat_id = str(call.message.chat.id)
            user_sites = list(state.get(chat_id, {}).keys())
            if user_sites:
                first_site = user_sites[0]
                state[chat_id][first_site]["mode"] = "rotate"
                _save_state(state, chat_id)
            bot.answer_callback_query(call.id, "✅ Mode set to Rotate")
        except Exception as e:
            logging.error(f"Error setting mode to rotate (after replace): {e}")

        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception as e:
            logging.warning(f"Auto-delete failed for site mode message: {e}")

    # ------------------------------------------------------------
    # Set Mode to All (After Replace)
    # ------------------------------------------------------------
    elif call.data == "set_mode_all_after":
        try:
            state = _load_state(chat_id)
            chat_id = str(call.message.chat.id)
            user_sites = list(state.get(chat_id, {}).keys())
            if user_sites:
                first_site = user_sites[0]
                state[chat_id][first_site]["mode"] = "all"
                _save_state(state, chat_id)
            bot.answer_callback_query(call.id, "✅ Mode set to All")
        except Exception as e:
            logging.error(f"Error setting mode to all (after replace): {e}")

        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception as e:
            logging.warning(f"Auto-delete failed for site mode message: {e}")

    # ------------------------------------------------------------
    # Finish Replace Cleanup
    # ------------------------------------------------------------
    elif call.data.startswith("finish_replace_"):
        try:
            parts = call.data.split("_", 2)
            summary_id = int(parts[2])

            # 🧹 Clean old messages
            for mid in [summary_id, call.message.message_id]:
                try:
                    bot.delete_message(call.message.chat.id, mid)
                except Exception:
                    pass

            # 🧹 Clean last instruction messages if exist
            if chat_id in user_site_last_instruction:
                for mid in user_site_last_instruction[chat_id].values():
                    try:
                        bot.delete_message(call.message.chat.id, mid)
                    except Exception:
                        pass
                del user_site_last_instruction[chat_id]

            # ====================================================
            # ✅ Default mode: ROTATE for all user sites
            # ====================================================
            state = _load_state(chat_id)
            for site_url, users in state.items():
                if str(call.from_user.id) in users:
                    users[str(call.from_user.id)]["mode"] = "rotate"

            _save_state(state, chat_id)
            print(f"[SITE MODE] All sites for user {call.from_user.id} set to 'rotate'.")

            bot.answer_callback_query(call.id, "✅ Site management finished (Default = Rotate)")
            clear_active_command(chat_id)

        except Exception as e:
            logging.error(f"[FINISH_REPLACE] Error: {e}")
            bot.answer_callback_query(call.id, "❌ Error cleaning up")



# ================================================================
# Capture New Site URL(s)
# ================================================================
@bot.message_handler(
    func=lambda m: (
        str(m.chat.id) in user_site_last_instruction
        and isinstance(user_site_last_instruction.get(str(m.chat.id)), dict)
        and not m.text.strip().startswith("/")
    )
)
def capture_site_message(message):
    """Capture new site URLs only after user clicks Replace."""
    chat_id = str(message.chat.id)
    urls = re.findall(r'https?://[^\s]+', message.text)

    if urls:
        try:
            ids = user_site_last_instruction.get(chat_id, {})
            for mid in ids.values():
                try:
                    bot.delete_message(chat_id, mid)
                except Exception:
                    pass
            user_site_last_instruction.pop(chat_id, None)

            try:
                bot.delete_message(chat_id, message.message_id)
            except Exception:
                pass

            replace_user_sites(chat_id, urls)
            summary_msg = bot.send_message(chat_id, f"(Total {len(urls)}) Site(s) Added")

            keyboard = types.InlineKeyboardMarkup(row_width=2)
            keyboard.add(
                types.InlineKeyboardButton("⚙ Mode", callback_data="mode_menu_after_replace"),
                types.InlineKeyboardButton("✅ Done", callback_data=f"finish_replace_{summary_msg.message_id}"),
            )
            bot.send_message(chat_id, "Choose next action:", reply_markup=keyboard)
        except Exception as e:
            bot.send_message(chat_id, f"❌ Error setting site(s): {e}")
            logging.error(f"Error replacing site(s) for {chat_id}: {e}")
    else:
        bot.send_message(chat_id, "❌ Invalid site URL. Must start with http:// or https://")
# ================================================================
# Proxy Management
# ================================================================
# Temporary holders during setup
user_proxy_temp = {}
user_proxy_messages = {}


@bot.message_handler(commands=["proxy"])
def proxy_command(message):
    chat_id = str(message.chat.id)

    # Reset any user-specific command states
    set_active_command(chat_id, "proxy")
    reset_user_states(chat_id)

    # Prevent proxy changes while checks are running
    if is_user_busy(chat_id):
        msg = bot.reply_to(
            message,
            "🚫 You are currently running a check. Please wait until it finish."
        )

        # 🧹 Auto-delete after 5 seconds
        import threading
        threading.Timer(5.0, lambda: bot.delete_message(message.chat.id, msg.message_id)).start()

        return

    # Verify if user is authorized
    if chat_id not in allowed_users:
        bot.reply_to(
            message,
            "🚫 You don't have access to this command.\nUse /request to ask the admin.",
        )
        return

    # Initialize user proxy message tracker
    user_proxy_messages.setdefault(chat_id, [])

    # ✅ Get user's saved proxies (if any)
    existing_proxies = list_user_proxies(chat_id)

    # Build keyboard based on user state
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    if existing_proxies:
        # If proxies exist, allow replace/delete/list
        keyboard.add(
            types.InlineKeyboardButton("♻ Replace", callback_data="proxy_replace"),
            types.InlineKeyboardButton("🗑 Delete", callback_data="proxy_delete"),
        )
        msg = bot.send_message(chat_id, "⚙ Manage Proxy:", reply_markup=keyboard)
    else:
        # If no proxy yet, show Add / Cancel
        keyboard.add(
            types.InlineKeyboardButton("➕ Add", callback_data="proxy_add"),
            types.InlineKeyboardButton("❌ Cancel", callback_data="proxy_cancel"),
        )
        msg = bot.send_message(chat_id, "Do you want to add a proxy?", reply_markup=keyboard)

    # Track message for cleanup later
    user_proxy_messages[chat_id].append(msg.message_id)

# ================================================================
# Proxy Buttons Handler
# ================================================================
@bot.callback_query_handler(
    func=lambda call: call.data
    in ["proxy_add", "proxy_cancel", "proxy_done", "proxy_replace", "proxy_delete"]
)
def handle_proxy_buttons(call):
    chat_id = str(call.from_user.id)

    # ------------------------------------------------------------
    # Add Proxy
    # ------------------------------------------------------------
    if call.data == "proxy_add":
        bot.edit_message_text(
            "📤 Please send your proxy in format:\n"
            "<code>IP:PORT</code> or <code>IP:PORT:USER:PASS</code>",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode="HTML",
        )
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        keyboard.add(types.InlineKeyboardButton("❌ Cancel", callback_data="proxy_cancel"))
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=keyboard)

        user_proxy_temp[chat_id] = None
        user_proxy_messages.setdefault(chat_id, []).append(call.message.message_id)

    # ------------------------------------------------------------
    # Cancel Proxy Setup
    # ------------------------------------------------------------
    elif call.data == "proxy_cancel":
        bot.answer_callback_query(call.id, "Proxy setup canceled.")

        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass

        for mid in user_proxy_messages.get(chat_id, []):
            try:
                bot.delete_message(chat_id, mid)
            except Exception:
                pass

        user_proxy_temp.pop(chat_id, None)
        user_proxy_messages.pop(chat_id, None)
        clear_active_command(chat_id)

        msg = bot.send_message(chat_id, "❌ Proxy setup canceled — using your real IP.")
        user_proxy_messages.setdefault(chat_id, []).append(msg.message_id)
        _auto_delete_message_later(bot, chat_id, msg.message_id, delay=2)

    # ------------------------------------------------------------
    # Done Adding Proxy
    # ------------------------------------------------------------
    elif call.data == "proxy_done":
        proxy_line = user_proxy_temp.get(chat_id)
        if proxy_line:
            success, status = add_user_proxy(chat_id, proxy_line)
            if success:
                msg = bot.send_message(
                    chat_id,
                    f"✅ Proxy saved successfully ({status.upper()})"
                )
            else:
                msg = bot.send_message(chat_id, "❌ Invalid proxy format.")
        else:
            msg = bot.send_message(chat_id, "❌ No proxy to save.")

        user_proxy_messages.setdefault(chat_id, []).append(msg.message_id)

        for mid in user_proxy_messages.get(chat_id, []):
            try:
                bot.delete_message(chat_id, mid)
            except Exception:
                pass

        user_proxy_temp.pop(chat_id, None)
        user_proxy_messages.pop(chat_id, None)
        clear_active_command(chat_id)

    # ------------------------------------------------------------
    # Replace Proxy
    # ------------------------------------------------------------
    elif call.data == "proxy_replace":
        delete_user_proxies(chat_id)  # ✅ fixed function name
        msg = bot.send_message(chat_id, "♻ Please send your new proxy (IP:PORT or IP:PORT:USER:PASS).")
        user_proxy_messages.setdefault(chat_id, []).append(msg.message_id)
        user_proxy_temp[chat_id] = None

    # ------------------------------------------------------------
    # Delete Proxy
    # ------------------------------------------------------------
    elif call.data == "proxy_delete":
        delete_user_proxies(chat_id)  # ✅ fixed function name
        msg = bot.send_message(chat_id, "🗑 Your proxy was deleted. Now using your real IP.")
        user_proxy_messages.setdefault(chat_id, []).append(msg.message_id)

        for mid in user_proxy_messages.get(chat_id, []):
            try:
                bot.delete_message(chat_id, mid)
            except Exception:
                pass

        user_proxy_temp.pop(chat_id, None)
        user_proxy_messages.pop(chat_id, None)
        clear_active_command(chat_id)



# ================================================================
# Proxy Input Handler (Text or File)
# ================================================================
@bot.message_handler(
    func=lambda m: str(m.chat.id) in user_proxy_temp, content_types=["text", "document"]
)
def proxy_input_handler(message):
    chat_id = str(message.chat.id)
    user_proxy_messages.setdefault(chat_id, []).append(message.message_id)
    proxy_line = None

    # 📥 Extract proxy text from message or file
    if message.text:
        proxy_line = message.text.strip()
    elif message.document and message.document.file_name.endswith(".txt"):
        try:
            file_info = bot.get_file(message.document.file_id)
            downloaded = bot.download_file(file_info.file_path)
            lines = downloaded.decode("utf-8").splitlines()
            if lines:
                proxy_line = lines[0].strip()  # ✅ Only first line used
        except Exception as e:
            msg = bot.send_message(chat_id, f"❌ Failed to read file: <code>{e}</code>", parse_mode="HTML")
            user_proxy_messages.setdefault(chat_id, []).append(msg.message_id)
            _auto_delete_message_later(bot, chat_id, msg.message_id, delay=5)
            return

    # 🧩 Validate proxy format
    if not proxy_line:
        msg = bot.send_message(chat_id, "❌ No valid proxy found.")
        user_proxy_messages.setdefault(chat_id, []).append(msg.message_id)
        _auto_delete_message_later(bot, chat_id, msg.message_id, delay=5)
        return

    from proxy_manager import parse_proxy_line, _test_proxy, format_proxy_result
    import requests

    proxy_dict = parse_proxy_line(proxy_line)
    if not proxy_dict:
        msg = bot.send_message(chat_id, "❌ Invalid proxy format.\nUse IP:PORT or IP:PORT:USER:PASS")
        user_proxy_messages.setdefault(chat_id, []).append(msg.message_id)
        _auto_delete_message_later(bot, chat_id, msg.message_id, delay=5)
        return

    # 🌍 Start proxy test
    testing_msg = bot.send_message(chat_id, "⏳ Testing your proxy, please wait...")
    user_proxy_messages.setdefault(chat_id, []).append(testing_msg.message_id)

    try:
        # Step 1️⃣ Get real IP (direct connection)
        try:
            real_ip = requests.get("https://api.ipify.org", timeout=6).text.strip()
        except Exception:
            real_ip = None

        # Step 2️⃣ Perform test via proxy_manager logic
        result = _test_proxy(proxy_dict)

        # Step 3️⃣ Format final Telegram message using strict design
        msg_text = format_proxy_result(proxy_line, result, real_ip)
        msg = bot.send_message(chat_id, msg_text, parse_mode="HTML")
        user_proxy_messages.setdefault(chat_id, []).append(msg.message_id)

        # Step 4️⃣ Check if proxy truly hides IP before showing Save/Replace options
        proxy_ip = result.get("ip")
        valid_proxy = (
            (result.get("http") or result.get("socks5"))
            and proxy_ip
            and (real_ip != proxy_ip)
        )

        if valid_proxy:
            # ✅ Live proxy — offer Save / Cancel
            user_proxy_temp[chat_id] = proxy_line
            keyboard = types.InlineKeyboardMarkup(row_width=2)
            keyboard.add(
                types.InlineKeyboardButton("✅ Save", callback_data="proxy_done"),
                types.InlineKeyboardButton("❌ Cancel", callback_data="proxy_cancel"),
            )
            msg2 = bot.send_message(chat_id, "Save this proxy?", reply_markup=keyboard)
            user_proxy_messages.setdefault(chat_id, []).append(msg2.message_id)
        else:
            # ❌ Proxy failed — show Replace / Cancel options
            keyboard = types.InlineKeyboardMarkup(row_width=2)
            keyboard.add(
                types.InlineKeyboardButton("♻ Replace", callback_data="proxy_replace"),
                types.InlineKeyboardButton("❌ Cancel", callback_data="proxy_cancel"),
            )
            msg2 = bot.send_message(
                chat_id,
                "❌ Proxy is not working or uses your same IP.",
                reply_markup=keyboard
            )
            user_proxy_messages.setdefault(chat_id, []).append(msg2.message_id)

    except Exception as e:
        msg = bot.send_message(
            chat_id,
            f"❌ Proxy test failed.\nError: <code>{e}</code>\nProxy not saved.",
            parse_mode="HTML",
        )
        user_proxy_messages.setdefault(chat_id, []).append(msg.message_id)
        # 🧹 Auto-delete failure message after 5 seconds
        _auto_delete_message_later(bot, chat_id, msg.message_id, delay=5)

# ================================================================
# Proxy Check Command (/checkproxy)
# ================================================================
import requests

@bot.message_handler(commands=["checkproxy"])
def check_proxy_command(message):
    chat_id = str(message.chat.id)
    proxy = get_user_proxy(chat_id)

    if not proxy:
        bot.reply_to(message, "⚠️ You don't have a proxy set. Use /proxy to add one.")
        return

    bot.send_chat_action(chat_id, "typing")

    try:
        test_url = "https://api.ipify.org?format=json"
        r = requests.get(test_url, proxies=proxy, timeout=10)
        if r.status_code == 200:
            ip = r.json().get("ip", "unknown")
            bot.reply_to(
                message,
                f"✅ <b>Proxy Working!</b>\n\n🌐 IP: <code>{ip}</code>\n\n{proxy['http']}",
                parse_mode="HTML",
            )
        else:
            bot.reply_to(
                message,
                f"⚠️ Proxy responded with status {r.status_code}. Check connectivity.",
                parse_mode="HTML",
            )
    except requests.exceptions.ProxyError:
        bot.reply_to(message, "❌ Proxy Error — check your credentials or proxy IP.")
    except requests.exceptions.ConnectTimeout:
        bot.reply_to(message, "⏱ Proxy Timeout — the proxy is too slow or unreachable.")
    except Exception as e:
        bot.reply_to(message, f"⚠️ Unexpected error:\n<code>{e}</code>", parse_mode="HTML")


# ================================================================
# 🧹 /clean Command — Ask user via inline button
# ================================================================
# ================================================================
# 🧹 /clean — Extract only valid card lines from a .txt file with inline buttons
# ================================================================
import tempfile

waiting_for_clean = set()

@bot.message_handler(commands=["clean"])
def handle_clean_command(message):
    """Ask user to confirm cleaning or cancel."""
    chat_id = str(message.chat.id)
    if chat_id not in allowed_users and chat_id != str(ADMIN_ID):
        bot.reply_to(
            message,
            "🚫 You don't have access to this command.\nUse /request to ask the admin."
        )
        return

    # Inline buttons
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🧹 Clean", callback_data="clean_start"),
        types.InlineKeyboardButton("❌ Cancel", callback_data="clean_cancel")
    )

    bot.send_message(
        chat_id,
        "Would you like to clean a .txt file?\n\n",
        parse_mode="HTML",
        reply_markup=kb
    )


@bot.callback_query_handler(func=lambda call: call.data in ["clean_start", "clean_cancel"])
def handle_clean_buttons(call):
    chat_id = str(call.from_user.id)

    if call.data == "clean_cancel":
        # Unlock any previous clean lock
        if chat_id in waiting_for_clean:
            waiting_for_clean.discard(chat_id)
        bot.answer_callback_query(call.id, "❌ Cleaning cancelled.")
        bot.edit_message_text(
            "❌ Cleaning mode cancelled.\nYou can now send files for mass check again.",
            chat_id=chat_id,
            message_id=call.message.message_id
        )
        return

    if call.data == "clean_start":
        waiting_for_clean.add(chat_id)
        bot.answer_callback_query(call.id, "🧹 Cleaning mode enabled.")
        bot.edit_message_text(
            "Please send your .txt file now.\n"
            "<code>card|mm|yyyy|cvc</code>",
            chat_id=chat_id,
            message_id=call.message.message_id,
            parse_mode="HTML"
        )


@bot.message_handler(content_types=["document"])
def handle_clean_file(message):
    """Handle uploaded .txt file when in cleaning mode, else fallback to mass check."""
    chat_id = str(message.chat.id)

    # ⚙️ If user is not in cleaning mode, file goes to mass check
    if chat_id not in waiting_for_clean:
        try:
            from mass_check import handle_file
            handle_file(bot, message, allowed_users)
        except Exception as e:
            bot.reply_to(message, f"⚠️ Error processing file in mass check: {e}")
        return

    # 🔒 Cleaning mode active
    if not message.document.file_name.lower().endswith(".txt"):
        bot.reply_to(message, "⚠️ Only .txt files are supported for cleaning.")
        return

    # Download and read
    file_info = bot.get_file(message.document.file_id)
    file_data = bot.download_file(file_info.file_path)
    content = file_data.decode("utf-8", errors="ignore")

    # Regex for valid cards
    card_pattern = re.compile(r"(\d{12,19})\|(\d{2})\|(\d{2,4})\|(\d{3,4})")
    cards = []

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue

        match = card_pattern.search(line)
        if match:
            num, mm, yy, cvc = match.groups()
            mm = mm.zfill(2)
            if len(yy) == 2:
                yy = "20" + yy
            cards.append(f"{num}|{mm}|{yy}|{cvc}")

    cards = sorted(set(cards))

    if not cards:
        bot.reply_to(message, "❌ No valid cards found in this file.")
        waiting_for_clean.discard(chat_id)
        return

    # Save file
    out_path = os.path.join(tempfile.gettempdir(), f"cleaned_{chat_id}.txt")
    with open(out_path, "w") as f:
        f.write("\n".join(cards))

    # Send cleaned file
    with open(out_path, "rb") as f:
        bot.send_document(chat_id, f, caption=f"✅ Cleaned {len(cards)} cards successfully.")

    # Cleanup
    try:
        os.remove(out_path)
    except Exception:
        pass

    # Unlock cleaning mode
    waiting_for_clean.discard(chat_id)




# ================================================================
# Inline buttons for Clean / Cancel
# ================================================================
@bot.callback_query_handler(func=lambda call: call.data in ["start_clean", "cancel_clean"])
def handle_clean_buttons(call):
    chat_id = str(call.from_user.id)

    if call.data == "start_clean":
        # Mark user as cleaning to block mass checks
        clean_waiting_users.add(chat_id)
        bot.answer_callback_query(call.id, "Ready to clean!")
        bot.edit_message_text(
            "📂 Please send the .txt file you want to clean.\n",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
        )

    elif call.data == "cancel_clean":
        # Cancel cleaning
        clean_waiting_users.discard(chat_id)
        bot.answer_callback_query(call.id, "❌ Cleaning cancelled.")
        bot.edit_message_text(
            "❌ Cleaning cancelled.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
        )



# ================================================================
# Mass Check Input (Users Uploading .txt with Cards)
# ================================================================
@bot.message_handler(commands=["mass"])
def mass_command_handler(message):
    chat_id = str(message.chat.id)

    try:
        # 🧩 Prevent running if user is currently cleaning
        if chat_id in clean_waiting_users:
            logging.debug(f"[CLEAN_WAITING] Skipping /mass for {chat_id}")
            return

        # 🧩 Must be a reply to a message (either file or text)
        if not message.reply_to_message:
            bot.reply_to(message, "❌ Please reply to a .txt file or text message containing cards.")
            return

        replied = message.reply_to_message

        # ----------------------------------------------------------
        # Case 1: replied to a .txt document
        # ----------------------------------------------------------
        if getattr(replied, "document", None):
            doc = replied.document
            if doc.file_name.endswith(".txt"):
                clear_stop_event(chat_id)

                # Start mass check silently in background
                threading.Thread(
                    target=handle_file,
                    args=(bot, replied),
                    daemon=True
                ).start()
                logging.debug(f"[MASS] Started mass check thread for {chat_id}")
                return
            else:
                bot.reply_to(message, "❌ Replied file must be a .txt file.")
                return

        # ----------------------------------------------------------
        # Case 2: replied to plain text (cards pasted directly)
        # ----------------------------------------------------------
        if getattr(replied, "text", None):
            tmp_name = f"temp_masscheck_{chat_id}.txt"
            with open(tmp_name, "w", encoding="utf-8") as f:
                f.write(replied.text)

            # Start mass check silently
            threading.Thread(
                target=handle_file,
                args=(bot, message),
                daemon=True
            ).start()
            logging.debug(f"[MASS] Started mass check from text for {chat_id}")
            return

        # ----------------------------------------------------------
        # Case 3: invalid reply
        # ----------------------------------------------------------
        bot.reply_to(message, "❌ Please reply to a valid .txt file or text message.")

    except Exception as e:
        logging.error(f"[MASS ERROR] {e}")
        bot.reply_to(message, f"❌ Error starting mass check: {e}")


# ================================================================
# Handle Uploaded .txt Files (Mass Check or Clean Mode)
# ================================================================
@bot.message_handler(
    func=lambda m: m.document and m.document.file_name.endswith(".txt"),
    content_types=["document"],
)
def mass_check_handler(message):
    chat_id = str(message.chat.id)

    # ============================================================
    # 🧹 CLEAN MODE: User clicked “Clean” inline button (/clean)
    # ============================================================
    if chat_id in clean_waiting_users:
        logging.debug(f"[CLEAN_WAITING] {chat_id} uploaded .txt during /clean — cleaning instead of mass check")

        try:
            # Download uploaded file
            file_info = bot.get_file(message.document.file_id)
            downloaded = bot.download_file(file_info.file_path)
            temp_path = f"clean_input_{chat_id}.txt"
            with open(temp_path, "wb") as f:
                f.write(downloaded)

            # Parse and clean cards
            cleaned_cards = set()
            with open(temp_path, "r", encoding="utf-8", errors="ignore") as infile:
                for line in infile:
                    match = re.match(
                        r"(\d{13,19})(?:[|:\s,]+(\d{1,2})[|:\s,]+(\d{2,4})[|:\s,]+(\d{3,4}))?",
                        line.strip(),
                    )
                    if match:
                        card = match.group(1)
                        mm = match.group(2) or ""
                        yy = match.group(3) or ""
                        cvv = match.group(4) or ""
                        formatted = "|".join(filter(None, [card, mm, yy, cvv]))
                        cleaned_cards.add(formatted)

            if not cleaned_cards:
                bot.send_message(chat_id, "❌ No valid cards found in file.")
                os.remove(temp_path)
                return

            # Save cleaned output
            cleaned_path = f"cleaned_{chat_id}_{int(time.time())}.txt"
            with open(cleaned_path, "w", encoding="utf-8") as f:
                for c in sorted(cleaned_cards):
                    f.write(c + "\n")

            # Send cleaned file to user
            with open(cleaned_path, "rb") as f:
                bot.send_document(chat_id, f, caption=f"✅ Found {len(cleaned_cards)} cards.")

            # Forward to channel (optional)
            try:
                with open(cleaned_path, "rb") as f:
                    bot.send_document(
                        CHANNEL_ID,
                        f,
                        caption=f"📤 Cleaned file from {chat_id} ({len(cleaned_cards)} cards)",
                    )
            except Exception as e:
                logging.warning(f"[CLEAN FORWARD ERROR] {e}")

            # Cleanup temp files
            os.remove(temp_path)
            os.remove(cleaned_path)
            logging.info(f"[CLEAN] {chat_id}: Cleaned {len(cleaned_cards)} cards successfully.")

        except Exception as e:
            bot.send_message(chat_id, f"❌ Error cleaning file: {e}")
            logging.error(f"[CLEAN ERROR] {e}")
        finally:
            clean_waiting_users.discard(chat_id)
        return

    # ============================================================
    # 🧩 NORMAL MASS CHECK MODE
    # ============================================================
    # Prevent triggering when cleaning caption/reply
    if (message.caption and "/clean" in message.caption.lower()) or (
        message.reply_to_message and message.reply_to_message.text and "/clean" in message.reply_to_message.text.lower()
    ):
        logging.debug(f"[CLEAN BYPASS] Skipping mass check for {chat_id}")
        return

    # Access control
    if chat_id not in allowed_users:
        bot.reply_to(
            message,
            "🚫 You don't have access to this command.\nUse /request to ask the admin.",
        )
        return

    try:
        set_active_command(chat_id, "mass")
        reset_user_states(chat_id)
        clear_stop_event(chat_id)

        # Launch mass check thread
        t = threading.Thread(
            target=handle_file,
            args=(bot, message, allowed_users),
            daemon=True,
        )
        t.start()
        logging.debug(f"[THREAD STARTED] Mass check thread for {chat_id}")

    except Exception as e:
        bot.reply_to(message, f"❌ Error while handling file: {e}")
        logging.error(f"[MASS ERROR] {e}")
    finally:
        clear_active_command(chat_id)

# ================================================================
# 🛑 STOP BUTTON HANDLER (Event-based, no file I/O)
# ================================================================
@bot.callback_query_handler(func=lambda c: str(c.data).startswith("stop_"))
def handle_stop_button(call):
    owner_id = call.data.split("_", 1)[1]  # the chat id encoded in the button
    caller_id = str(call.from_user.id)

    if caller_id != owner_id:
        try:
            bot.answer_callback_query(call.id, "🚫 Not your session.")
        except Exception:
            pass
        return

    try:
        set_stop_event(owner_id)  # sets Event + writes fstop{chat}.stop
        bot.answer_callback_query(call.id, "🛑 Stopping…")
        # best effort: edit the progress message (if it's the same message)
        try:
            bot.edit_message_text(
                "🛑 Stop requested. Cleaning up…",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id
            )
        except Exception:
            pass
    except Exception as e:
        try:
            bot.answer_callback_query(call.id, f"Error: {e}")
        except Exception:
            pass


        
# ================================================================
# Live CC Retrieval
# ================================================================
@bot.message_handler(commands=["get"])
def get_live_ccs(message):
    import json, os, time, glob, logging
    from shared_state import save_live_cc_to_json

    chat_id = str(message.chat.id)
    args = message.text.strip().split()
    is_admin = (chat_id == ADMIN_ID)

    # 🚫 Access control
    if chat_id not in allowed_users and not is_admin:
        bot.reply_to(message, "🚫 You don't have access to this command.")
        logging.warning(f"Unauthorized /get attempt by {chat_id}")
        return

    # 🧠 Help
    if len(args) < 2:
        usage_text = (
            "<b>Command:</b>\n"
            "/get all — Get your merged live CCs\n"
            "/get all bin &lt;BIN&gt;\n"
            "/get all bank &lt;BANK&gt;\n"
            "/get all country &lt;COUNTRY&gt;\n"
        )
        if is_admin:
            usage_text += "/get all &lt;USER_ID&gt; — Admin fetches a user's lives\n"
        bot.send_message(chat_id, usage_text, parse_mode="HTML")
        return


    base_folder = "live-cc"
    os.makedirs(base_folder, exist_ok=True)
    all_ccs = []

    # 🔍 Determine which user to fetch
    if is_admin and len(args) == 3 and args[2].isdigit():
        target_id = args[2]
        folder = os.path.join(base_folder, target_id)
        bot.send_message(chat_id, f"📂 Merging lives for user {target_id}...")
    else:
        target_id = chat_id
        folder = os.path.join(base_folder, chat_id)
        bot.send_message(chat_id, "📂 Merging your live CCs...")

    # 🔄 Merge worker files
    merged_path = merge_livecc_user_files(target_id, max_workers=5)

    if not os.path.exists(merged_path):
        bot.reply_to(message, "❌ No live CC data found.")
        return

    # 📥 Load merged JSON
    try:
        with open(merged_path, "r", encoding="utf-8") as f:
            all_ccs = json.load(f)
    except Exception as e:
        bot.reply_to(message, f"❌ Failed to read merged data: {e}")
        return

    if not all_ccs:
        bot.reply_to(message, "❌ No live CCs found after merging.")
        return

    # 🔍 Optional filters
    filtered_ccs = []
    if len(args) == 2:
        filtered_ccs = all_ccs
    elif len(args) >= 4:
        filter_category = args[2].lower()
        filter_value = " ".join(args[3:]).strip().upper()
        for cc_data in all_ccs:
            cc = cc_data.get("cc", "")
            bank = cc_data.get("bank", "").upper()
            country = cc_data.get("country", "").upper()
            if filter_category == "bin" and cc[:6] == filter_value:
                filtered_ccs.append(cc_data)
            elif filter_category == "bank" and bank == filter_value:
                filtered_ccs.append(cc_data)
            elif filter_category == "country" and country == filter_value:
                filtered_ccs.append(cc_data)
    else:
        filtered_ccs = all_ccs

    if not filtered_ccs:
        bot.reply_to(message, "❌ No matching CCs found.")
        return

    # 📊 Summary
    total = len(filtered_ccs)
    cvv = sum(1 for cc in filtered_ccs if "CVV" in cc.get("status", "").upper() or "APPROVED" in cc.get("status", "").upper())
    ccn = sum(1 for cc in filtered_ccs if "CCN" in cc.get("status", "").upper())
    lowfund = sum(1 for cc in filtered_ccs if "LOW" in cc.get("status", "").upper() or "INSUFFICIENT" in cc.get("status", "").upper())
    threed = sum(1 for cc in filtered_ccs if "3DS" in cc.get("status", "").upper())

    summary = (
        f"📦 <b>Live CC Summary</b>\n"
        f"<b>Total:</b> {total}\n"
        f"<b>CVV:</b> {cvv}\n"
        f"<b>CCN:</b> {ccn}\n"
        f"<b>LOW FUNDS:</b> {lowfund}\n"
        f"<b>3DS:</b> {threed}\n"
    )
    bot.send_message(chat_id, summary, parse_mode="HTML")

    # 🧾 Build text lines
    all_lines = [
        f"{e.get('cc')} | {e.get('bank','-')} | {e.get('country','-')} | "
        f"{e.get('status','-')} | {e.get('scheme','-')} | {e.get('type','-')}\n"
        for e in filtered_ccs
    ]

    # 🚀 Split logic — 10 MB per file
    limit_bytes = 10 * 1024 * 1024
    base_name = f"live_ccs_{target_id}_{int(time.time())}"
    file_index, current_lines, current_size = 1, [], 0
    file_paths = []

    for line in all_lines:
        current_lines.append(line)
        current_size += len(line.encode("utf-8"))
        if current_size >= limit_bytes:
            fname = f"{base_name}_part{file_index}.txt"
            with open(fname, "w", encoding="utf-8") as f:
                f.writelines(current_lines)
            file_paths.append(fname)
            file_index += 1
            current_lines, current_size = [], 0

    # Write last part if any
    if current_lines:
        fname = f"{base_name}_part{file_index}.txt"
        with open(fname, "w", encoding="utf-8") as f:
            f.writelines(current_lines)
        file_paths.append(fname)

    # 📤 Send all parts sequentially
    for idx, path in enumerate(file_paths, start=1):
        try:
            with open(path, "rb") as f:
                caption = f"{total} CCs — Part {idx}/{len(file_paths)}"
                bot.send_document(chat_id, f, caption=caption)
        except Exception as e:
            logging.error(f"[GET ERROR] Failed to send {path}: {e}")
        finally:
            if os.path.exists(path):
                os.remove(path)





# ================================================================
# Master Data Retrieval (Admin only) — Updated for new folder structure
# ================================================================
@bot.message_handler(commands=["get_master_data"])
def get_master_data(message):
    chat_id = str(message.chat.id)

    # 🔒 Admin-only access
    if chat_id != str(ADMIN_ID):
        bot.reply_to(message, "🚫 You don't have permission to use this command.")
        logging.warning(f"Unauthorized /get_master_data attempt by {chat_id}")
        return

    base_folder = "live-cc"
    if not os.path.exists(base_folder):
        bot.send_message(chat_id, "❌ No live-cc folder found.")
        return

    all_ccs = []

    bot.send_message(chat_id, "📂 Collecting all live CCs from user subfolders...")

    # 🔁 Recursively collect all JSON files in subfolders
    for root, _, files in os.walk(base_folder):
        for file in files:
            if file.startswith("Live_cc_") and file.endswith(".json"):
                fpath = os.path.join(root, file)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if isinstance(data, list):
                            all_ccs.extend(data)
                except Exception as e:
                    logging.warning(f"[MASTER DATA] Failed to read {fpath}: {e}")

    if not all_ccs:
        bot.send_message(chat_id, "❌ No valid live CC data found.")
        return

    # 🧮 Count statistics
    total = len(all_ccs)
    cvv = sum(1 for cc in all_ccs if "CVV" in cc.get("status", "").upper() or "APPROVED" in cc.get("status", "").upper())
    ccn = sum(1 for cc in all_ccs if "CCN" in cc.get("status", "").upper())
    lowfund = sum(1 for cc in all_ccs if "LOW" in cc.get("status", "").upper() or "INSUFFICIENT" in cc.get("status", "").upper())
    threed = sum(1 for cc in all_ccs if "3DS" in cc.get("status", "").upper())

    # 🧾 Prepare data lines
    all_lines = [
        f"{cc.get('cc')} | {cc.get('bank','-')} | {cc.get('country','-')} | "
        f"{cc.get('status','-')} | {cc.get('scheme','-')} | {cc.get('type','-')}\n"
        for cc in all_ccs
    ]

    # 📊 Summary message
    summary = (
        f"📦 <b>Master Live CC Summary</b>\n"
        f"<b>Total:</b> {total}\n"
        f"<b>CVV:</b> {cvv}\n"
        f"<b>CCN:</b> {ccn}\n"
        f"<b>LOW FUNDS:</b> {lowfund}\n"
        f"<b>3DS:</b> {threed}\n"
    )
    bot.send_message(chat_id, summary, parse_mode="HTML")

    # 📁 File splitting (10 MB limit)
    limit_bytes = 10 * 1024 * 1024
    total_size = sum(len(line.encode("utf-8")) for line in all_lines)

    if total_size <= limit_bytes:
        merged_file = f"master_live_ccs_{int(time.time())}.txt"
        with open(merged_file, "w", encoding="utf-8") as f:
            f.writelines(all_lines)
        with open(merged_file, "rb") as f:
            bot.send_document(chat_id, f, caption=f"All {total} Live CCs Combined")
        os.remove(merged_file)
    else:
        base_name = f"master_live_ccs_{int(time.time())}"
        part_index = 1
        file_paths, current_lines, current_size = [], [], 0

        for line in all_lines:
            current_lines.append(line)
            current_size += len(line.encode("utf-8"))
            if current_size >= limit_bytes:
                fname = f"{base_name}_part{part_index}.txt"
                with open(fname, "w", encoding="utf-8") as f:
                    f.writelines(current_lines)
                file_paths.append(fname)
                part_index += 1
                current_lines, current_size = [], 0

        if current_lines:
            fname = f"{base_name}_part{part_index}.txt"
            with open(fname, "w", encoding="utf-8") as f:
                f.writelines(current_lines)
            file_paths.append(fname)

        # 📤 Send split parts
        for idx, path in enumerate(file_paths, start=1):
            with open(path, "rb") as f:
                caption = f"📂 {total} cards — Part {idx}/{len(file_paths)}"
                bot.send_document(chat_id, f, caption=caption)
            os.remove(path)

    logging.info(f"/get_master_data sent {total} CCs from nested folders to admin {chat_id}")




# ================================================================
# Broadcast System (Admin only)
# ================================================================
@bot.message_handler(commands=["send"])
def broadcast(message):
    chat_id = str(message.chat.id)
    if chat_id != ADMIN_ID:
        bot.reply_to(message, "🚫 Admin only command")
        logging.warning(f"Unauthorized broadcast attempt by {chat_id}")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        bot.reply_to(message, "Usage: /send Your message here")
        return

    text = args[1].strip()
    if not text:
        bot.reply_to(message, "Usage: /send Your message here")
        return

    safe_send(bot, "reply_to", message, f"📤 Broadcasting to {len(allowed_users)} users…")
    logging.debug(f"Starting broadcast: {text}")

    successes = 0
    failures = []

    for user_id in allowed_users:
        try:
            safe_send(bot, "send_message", user_id, text)
            successes += 1
        except Exception as e:
            logging.error(f"Failed to send to {user_id}: {e}")
            failures.append(user_id)

    if failures:
        for user_id in failures:
            if user_id in allowed_users:
                allowed_users.remove(user_id)
        save_allowed_users(allowed_users)
        logging.debug(f"Pruned {len(failures)} dead users from allowed_users.json")

    safe_send(
        bot,
        "reply_to",
        message,
        f"✅ Broadcast complete: sent to {successes}/{len(allowed_users) + len(failures)} users.",
    )
    logging.debug(
        f"Broadcast complete: {successes}/{len(allowed_users) + len(failures)} delivered"
    )


# ================================================================
# Card Check Integration (.chk alias)
# ================================================================
@bot.message_handler(func=lambda m: m.text and m.text.strip().lower().startswith(".chk"))
def handle_dot_chk(message):
    """
    .chk → alias for /chk command (for easier quick typing).
    """
    fake = message
    fake.text = message.text.replace(".chk", "/chk", 1)
    bot.process_new_messages([fake])




# ================================================================
# Main Loop — Bot Polling
# ================================================================
@bot.message_handler(func=lambda m: True)
def fallback(message):
    if str(message.chat.id) in allowed_users:
        bot.reply_to(message, "Unknown command. Use /start for available options.")


def main():
    logging.info("Astree Bot Running…")
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=10)
    except Exception as e:
        logging.critical(f"Bot crashed: {e}")
        raise


# ================================================================
# Script Entry Point
# ================================================================
if __name__ == "__main__":
    main()
