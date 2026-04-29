"""
config.py — TikDL License Bot
Reads all secrets from environment variables (for Railway / any cloud host).
Set these in Railway's Variables tab — never hardcode tokens in source code.
"""

import os

# ── Required — set these in Railway Variables ─────────────────────────────────
BOT_TOKEN    = os.environ["BOT_TOKEN"]          # From BotFather
ADMIN_ID     = int(os.environ["ADMIN_ID"])       # Your Telegram user ID
BOT_USERNAME = os.environ.get("BOT_USERNAME", "")

# ── License defaults ──────────────────────────────────────────────────────────
DEFAULT_DAYS      = int(os.environ.get("DEFAULT_DAYS",      "30"))
MAX_KEYS_PER_USER = int(os.environ.get("MAX_KEYS_PER_USER", "1"))

# ── HTTP server — Railway sets PORT automatically ─────────────────────────────
HTTP_HOST  = "0.0.0.0"
HTTP_PORT  = int(os.environ.get("PORT", "8080"))
PUBLIC_URL = os.environ.get("PUBLIC_URL", f"http://localhost:{HTTP_PORT}")

# ── Security ──────────────────────────────────────────────────────────────────
SECRET_KEY = os.environ.get(
    "SECRET_KEY", "TikDL@Secret#2025!ChangeThisNow!"
).encode("utf-8")

# ── License Plans ─────────────────────────────────────────────────────────────
PLANS = [
    {
        "id":          "trial",
        "name":        "Trial",
        "days":        7,
        "price":       0.00,
        "currency":    "USD",
        "description": "Try TikDL free for 7 days",
        "emoji":       "🆓",
        "badge":       "Free",
    },
    {
        "id":          "monthly",
        "name":        "Monthly",
        "days":        30,
        "price":       4.99,
        "currency":    "USD",
        "description": "Full access for 30 days",
        "emoji":       "📅",
        "badge":       "$4.99",
    },
    {
        "id":          "quarterly",
        "name":        "3 Months",
        "days":        90,
        "price":       9.99,
        "currency":    "USD",
        "description": "Best value — save 33%",
        "emoji":       "💎",
        "badge":       "$9.99",
    },
    {
        "id":          "yearly",
        "name":        "Yearly",
        "days":        365,
        "price":       29.99,
        "currency":    "USD",
        "description": "Full year — save 50%",
        "emoji":       "🏆",
        "badge":       "$29.99",
    },
    {
        "id":          "lifetime",
        "name":        "Lifetime",
        "days":        36500,
        "price":       49.99,
        "currency":    "USD",
        "description": "Pay once, use forever",
        "emoji":       "♾️",
        "badge":       "$49.99",
    },
]

PAYMENT_INFO = (
    "💳 <b>Payment Instructions</b>\n\n"
    "Send your payment via <b>QR Code</b>:"
    #"• <b>Bakong ID:</b> <code>thoem_sen@bkrt</code>\n\n"
    "After paying, send a <b>screenshot</b> or <b>transaction ID</b> "
    "to the admin for manual approval.\n"
    "Your license will be issued within 24 hours."
)

AUTO_ISSUE_TRIAL = os.environ.get("AUTO_ISSUE_TRIAL", "true").lower() == "true"

# ── Plans override — admin panel can save custom plans to this file ────────────
import json as _json

_BAKONG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bakong_override.json")
_PLANS_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plans_override.json")

def load_bakong_override() -> dict:
    """Load Bakong config from override file if it exists."""
    if os.path.exists(_BAKONG_FILE):
        try:
            with open(_BAKONG_FILE) as f:
                return _json.load(f)
        except Exception:
            pass
    return {}

def save_bakong_override(data: dict):
    """Persist Bakong config to override file and apply to module env vars."""
    with open(_BAKONG_FILE, "w") as f:
        _json.dump(data, f, indent=2)
    # Apply immediately to running process
    if data.get("token"):
        os.environ["BAKONG_TOKEN"] = data["token"]
    if data.get("account_id"):
        os.environ["BAKONG_ACCOUNT_ID"] = data["account_id"]
    if data.get("merchant_name"):
        os.environ["BAKONG_MERCHANT_NAME"] = data["merchant_name"]
    if data.get("merchant_city"):
        os.environ["BAKONG_MERCHANT_CITY"] = data["merchant_city"]
    if "currency" in data:
        os.environ["BAKONG_CURRENCY"] = data["currency"]
    if "use_rbk" in data:
        os.environ["BAKONG_USE_RBK"] = "true" if data["use_rbk"] else "false"

LIFETIME_PLAN = {
    "id":          "lifetime",
    "name":        "Lifetime",
    "days":        36500,
    "price":       49.99,
    "currency":    "USD",
    "description": "Pay once, use forever",
    "emoji":       "♾️",
    "badge":       "$49.99",
}

def load_plans_override():
    """Load plans from override file if it exists, else return default PLANS.
    Always ensures Lifetime plan is present."""
    base = PLANS
    if os.path.exists(_PLANS_FILE):
        try:
            with open(_PLANS_FILE) as f:
                data = _json.load(f)
                if data.get("plans"):
                    base = data["plans"]
        except Exception:
            pass
    # Ensure Lifetime plan is always available
    if not any(p.get("id") == "lifetime" for p in base):
        base = list(base) + [LIFETIME_PLAN]
    return base

def save_plans_override(plans: list, payment_info: str = "", auto_trial: bool = True):
    """Persist plans + payment info to override file."""
    with open(_PLANS_FILE, "w") as f:
        _json.dump({
            "plans": plans,
            "payment_info": payment_info,
            "auto_trial": auto_trial,
        }, f, indent=2)

def load_payment_info_override() -> str:
    if os.path.exists(_PLANS_FILE):
        try:
            with open(_PLANS_FILE) as f:
                data = _json.load(f)
                if data.get("payment_info"):
                    return data["payment_info"]
        except Exception:
            pass
    return PAYMENT_INFO

def load_auto_trial_override() -> bool:
    if os.path.exists(_PLANS_FILE):
        try:
            with open(_PLANS_FILE) as f:
                data = _json.load(f)
                if "auto_trial" in data:
                    return data["auto_trial"]
        except Exception:
            pass
    return AUTO_ISSUE_TRIAL

def load_payment_verification_mode() -> str:
    """Load payment verification mode from override file.
    Modes:
      'bakong' — Auto-verify via Bakong QR (requires API token)
      'manual' — Manual admin approval (no QR, admin bot inbox)
    Returns 'bakong' by default if bakong token exists, else 'manual'.
    """
    if os.path.exists(_PLANS_FILE):
        try:
            with open(_PLANS_FILE) as f:
                data = _json.load(f)
                mode = data.get("payment_verification_mode")
                if mode in ("bakong", "manual"):
                    return mode
        except Exception:
            pass
    # Auto-detect: use bakong if token set, else manual
    bakong_cfg = load_bakong_override()
    if bakong_cfg.get("token") or os.environ.get("BAKONG_TOKEN"):
        return "bakong"
    return "manual"

def save_payment_verification_mode(mode: str):
    """Save payment verification mode to override file."""
    if mode not in ("bakong", "manual"):
        raise ValueError("Mode must be 'bakong' or 'manual'")
    
    # Load existing plans file
    plans_data = {}
    if os.path.exists(_PLANS_FILE):
        try:
            with open(_PLANS_FILE) as f:
                plans_data = _json.load(f)
        except Exception:
            pass
    
    # Update mode
    plans_data["payment_verification_mode"] = mode
    
    # Save back
    with open(_PLANS_FILE, "w") as f:
        _json.dump(plans_data, f, indent=2)
