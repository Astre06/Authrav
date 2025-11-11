# ================================================================
# 🚫 BIN BAN MANAGEMENT SYSTEM (Per-User)
# ================================================================
import json
import os
import threading
import re

BAN_BASE_DIR = "ban"
_banned_bins_cache = {}  # {user_id: set(bins)}
_banned_bins_lock = threading.Lock()


def _get_user_ban_file(user_id: str) -> str:
    """Get the ban file path for a specific user."""
    user_id = str(user_id)
    user_dir = os.path.join(BAN_BASE_DIR, user_id)
    os.makedirs(user_dir, exist_ok=True)
    return os.path.join(user_dir, f"ban{user_id}.json")


def _load_banned_bins(user_id: str):
    """Load banned bins for a specific user from file, with caching."""
    user_id = str(user_id)
    
    with _banned_bins_lock:
        # Check cache first
        if user_id in _banned_bins_cache:
            return _banned_bins_cache[user_id]
        
        ban_file = _get_user_ban_file(user_id)
        
        if os.path.exists(ban_file):
            try:
                with open(ban_file, "r", encoding="utf-8") as f:
                    bins_list = json.load(f)
                    _banned_bins_cache[user_id] = set(bins_list) if isinstance(bins_list, list) else set()
            except Exception:
                _banned_bins_cache[user_id] = set()
        else:
            _banned_bins_cache[user_id] = set()
        
        return _banned_bins_cache[user_id]


def _save_banned_bins(user_id: str, bins_set):
    """Save banned bins for a specific user to file."""
    user_id = str(user_id)
    
    with _banned_bins_lock:
        _banned_bins_cache[user_id] = bins_set.copy()
        ban_file = _get_user_ban_file(user_id)
        
        try:
            with open(ban_file, "w", encoding="utf-8") as f:
                json.dump(sorted(list(bins_set)), f, indent=2)
        except Exception as e:
            print(f"[BAN ERROR] Failed to save banned bins for user {user_id}: {e}")


def extract_bin(card_input: str) -> str:
    """
    Extract BIN (first 6 digits) from card input.
    Supports formats:
    - Full card: "5598880397218308|12|2026|989" -> "559888"
    - Just bin: "559888" -> "559888"
    - Card with spaces: "5598 8803 9721 8308" -> "559888"
    """
    # Remove all spaces and pipes
    cleaned = re.sub(r'[\s|]', '', str(card_input).strip())
    
    # Extract first 6 digits
    match = re.search(r'^(\d{6})', cleaned)
    if match:
        return match.group(1)
    
    return ""


def is_bin_banned(bin_code: str, user_id: str) -> bool:
    """Check if a BIN is banned for a specific user."""
    if not bin_code or len(bin_code) < 6:
        return False
    
    # Get first 6 digits
    bin_6 = bin_code[:6]
    banned_bins = _load_banned_bins(user_id)
    return bin_6 in banned_bins


def check_card_banned(card: str, user_id: str) -> tuple[bool, str]:
    """
    Check if a card (in format card|mm|yy|cvc) has a banned BIN for a specific user.
    Returns: (is_banned, bin_code)
    """
    bin_code = extract_bin(card)
    if not bin_code:
        return False, ""
    
    is_banned = is_bin_banned(bin_code, user_id)
    return is_banned, bin_code


def ban_bin(bin_code: str, user_id: str) -> bool:
    """Ban a BIN for a specific user. Returns True if successful, False if already banned."""
    user_id = str(user_id)
    bin_6 = extract_bin(bin_code)
    if not bin_6 or len(bin_6) < 6:
        return False
    
    banned_bins = _load_banned_bins(user_id)
    if bin_6 in banned_bins:
        return False  # Already banned
    
    banned_bins.add(bin_6)
    _save_banned_bins(user_id, banned_bins)
    return True


def unban_bin(bin_code: str, user_id: str) -> bool:
    """Unban a BIN for a specific user. Returns True if successful, False if not banned."""
    user_id = str(user_id)
    bin_6 = extract_bin(bin_code)
    if not bin_6 or len(bin_6) < 6:
        return False
    
    banned_bins = _load_banned_bins(user_id)
    if bin_6 not in banned_bins:
        return False  # Not banned
    
    banned_bins.remove(bin_6)
    _save_banned_bins(user_id, banned_bins)
    return True


def get_banned_bins_list(user_id: str) -> list:
    """Get list of all banned BINs for a specific user (sorted)."""
    user_id = str(user_id)
    banned_bins = _load_banned_bins(user_id)
    return sorted(list(banned_bins))


def get_banned_bins_count(user_id: str) -> int:
    """Get count of banned BINs for a specific user."""
    user_id = str(user_id)
    banned_bins = _load_banned_bins(user_id)
    return len(banned_bins)

