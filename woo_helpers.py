import re
from typing import Dict, Optional, Tuple


def _extract_attr(tag: str, attr: str) -> Optional[str]:
    attr_regex = rf'{attr}\s*=\s*["\']([^"\']*)["\']'
    match = re.search(attr_regex, tag, re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(rf'{attr}\s*=\s*([^\s>]+)', tag, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def _extract_hidden_inputs(html: str) -> Dict[str, str]:
    hidden_inputs: Dict[str, str] = {}
    for match in re.finditer(r'<input[^>]+type=["\']hidden["\'][^>]*>', html, re.IGNORECASE):
        tag = match.group(0)
        name = _extract_attr(tag, "name")
        if not name:
            continue
        value = _extract_attr(tag, "value") or ""
        hidden_inputs[name] = value
    return hidden_inputs


def _match_input_by_type(html: str, input_type: str) -> Optional[str]:
    pattern = rf'<input[^>]+type=["\']{input_type}["\'][^>]*name=["\']([^"\']+)["\']'
    match = re.search(pattern, html, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def _find_field_name(html: str, candidates: Tuple[str, ...], input_type: Optional[str] = None) -> Optional[str]:
    for name in candidates:
        if re.search(rf'name=["\']{re.escape(name)}["\']', html, re.IGNORECASE):
            return name
    if input_type:
        type_match = _match_input_by_type(html, input_type)
        if type_match:
            return type_match
    return candidates[0] if candidates else input_type


def _find_submit_control(html: str) -> Tuple[Optional[str], Optional[str]]:
    for match in re.finditer(r'<input[^>]+type=["\']submit["\'][^>]*>', html, re.IGNORECASE):
        tag = match.group(0)
        name = _extract_attr(tag, "name")
        value = _extract_attr(tag, "value") or "Submit"
        if name:
            return name, value
    for match in re.finditer(r'<button[^>]*type=["\']submit["\'][^>]*>(.*?)</button>', html, re.IGNORECASE | re.DOTALL):
        tag = match.group(0)
        name = _extract_attr(tag, "name")
        value = re.sub(r"<.*?>", "", match.group(1)).strip() or "Submit"
        if name:
            return name, value
    return None, None


def build_registration_payload(
    html: str,
    email: str,
    username: str,
    password: str,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
) -> Dict[str, str]:
    """
    Construct a WooCommerce registration payload that respects dynamic field names
    and hidden nonce inputs.
    """
    payload = _extract_hidden_inputs(html)

    email_field = _find_field_name(
        html,
        ("account_email", "email", "user_email", "billing_email"),
        input_type="email",
    ) or "email"

    username_field = _find_field_name(
        html,
        ("account_username", "username", "user_login", "login", "customer_login"),
    )

    password_field = _find_field_name(
        html,
        ("account_password", "password", "passwd", "pass", "customer_password"),
        input_type="password",
    ) or "password"

    password_confirm_field = _find_field_name(
        html,
        ("account_password-2", "password2", "confirm_password", "account_password_confirm"),
    )

    payload[email_field] = email
    payload[password_field] = password

    if username_field:
        payload[username_field] = username

    if password_confirm_field and password_confirm_field != password_field:
        payload[password_confirm_field] = password

    first_name_field = _find_field_name(
        html,
        ("account_first_name", "first_name", "billing_first_name", "fname"),
    )
    if first_name and first_name_field and first_name_field not in payload:
        payload[first_name_field] = first_name

    last_name_field = _find_field_name(
        html,
        ("account_last_name", "last_name", "billing_last_name", "lname"),
    )
    if last_name and last_name_field and last_name_field not in payload:
        payload[last_name_field] = last_name

    if "register" not in payload:
        payload["register"] = "Register"

    # Some sites expect wp-submit or other submit keys
    submit_name, submit_value = _find_submit_control(html)
    if submit_name and submit_name not in payload:
        payload[submit_name] = submit_value or "Register"

    return payload


def build_login_payload(html: str, username: str, password: str) -> Dict[str, str]:
    """
    Construct a WooCommerce login payload respecting dynamic field names.
    """
    payload = _extract_hidden_inputs(html)

    username_field = _find_field_name(
        html,
        ("username", "user_login", "log", "account_username", "email", "account_email"),
    ) or "username"

    password_field = _find_field_name(
        html,
        ("password", "user_pass", "pwd", "account_password"),
        input_type="password",
    ) or "password"

    payload[username_field] = username
    payload[password_field] = password

    if "rememberme" in html.lower():
        payload.setdefault("rememberme", "forever")

    submit_name, submit_value = _find_submit_control(html)
    if submit_name and submit_name not in payload:
        payload[submit_name] = submit_value or "Log in"
    elif "login" not in payload:
        payload["login"] = "Log in"

    return payload


def is_logged_in(html: str) -> bool:
    """
    Determine whether the WooCommerce account page indicates a logged-in state.
    """
    if not html:
        return False
    lowered = html.lower()
    return any(
        token in lowered
        for token in (
            "my account",
            "logout",
            "log out",
            "dashboard",
            "orders",
        )
    )
