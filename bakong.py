"""
bakong.py — Bakong KHQR payment for TikDL Bot.
Config is loaded dynamically from bakong_override.json (saved from admin panel)
or falls back to Railway environment variables.

Railway Variables (optional — can be set from admin panel instead):
  BAKONG_TOKEN          API token from api-bakong.nbc.gov.kh
  BAKONG_ACCOUNT_ID     e.g. thoem_sen@bkrt
  BAKONG_MERCHANT_NAME  e.g. TikDL
  BAKONG_MERCHANT_CITY  e.g. Phnom Penh
  BAKONG_CURRENCY       USD or KHR (default: USD)
  BAKONG_USE_RBK        true if server is outside Cambodia

NOTE ON RBK / RELAY:
  The new bakong-khqr library selects the API host by token prefix:
    "rbk..." → https://api.bakongrelay.com/v1   (works from any IP)
    anything → https://api-bakong.nbc.gov.kh/v1 (Cambodia IPs only)

  If your server is on Railway (outside Cambodia) AND your token is a
  regular JWT (eyJ...), enable BAKONG_USE_RBK in the admin panel.  This
  bot will then bypass the library and call the relay URL directly using
  your existing JWT token — no rbk-prefixed token required.
"""

import os
import json
import hashlib
import http.client
import logging
from urllib.parse import urlparse

log = logging.getLogger(__name__)

KHR_RATE = 4100   # ~4100 KHR per 1 USD

NBC_API_URL = "https://api-bakong.nbc.gov.kh/v1"
RBK_API_URL = "https://api.bakongrelay.com/v1"


def _get_timeout() -> int:
    try:
        import config as _c
        ov = _c.load_bakong_override()
        return int(ov.get("timeout_mins", 15)) * 60
    except Exception:
        return 15 * 60


def _get_poll() -> int:
    try:
        import config as _c
        ov = _c.load_bakong_override()
        return int(ov.get("poll_secs", 3))
    except Exception:
        return 3


PAYMENT_TIMEOUT_SECS = property(_get_timeout) if False else 5 * 60
POLL_INTERVAL_SECS   = property(_get_poll)    if False else 3


def _cfg() -> dict:
    """Return live Bakong config — env vars take priority, override file fills gaps."""
    try:
        import config as _c
        ov = _c.load_bakong_override()
    except Exception:
        ov = {}
    return {
        "token":         os.environ.get("BAKONG_TOKEN")         or ov.get("token", ""),
        "account_id":    os.environ.get("BAKONG_ACCOUNT_ID")    or ov.get("account_id", "thoem_sen@bkrt"),
        "merchant_name": os.environ.get("BAKONG_MERCHANT_NAME") or ov.get("merchant_name", "Nexus Downloader"),
        "merchant_city": os.environ.get("BAKONG_MERCHANT_CITY") or ov.get("merchant_city", "Phnom Penh"),
        "currency":     (os.environ.get("BAKONG_CURRENCY")      or ov.get("currency", "USD")).upper(),
        "use_rbk":       os.environ.get("BAKONG_USE_RBK", "").lower() == "true" or ov.get("use_rbk", False),
    }


def is_enabled() -> bool:
    return bool(_cfg()["token"])


def usd_to_khr(usd: float) -> int:
    return round(usd * KHR_RATE / 100) * 100


def _api_url() -> str:
    """Return the correct Bakong API base URL based on config."""
    cfg = _cfg()
    token = cfg["token"]
    # New library auto-selects by prefix; we mirror that logic here
    if cfg["use_rbk"] or token.startswith("rbk"):
        return RBK_API_URL
    return NBC_API_URL


def _post(endpoint: str, payload: dict) -> dict:
    """
    Direct HTTP POST to Bakong API — bypasses the library's token-prefix
    restriction so a regular JWT token works with the RBK relay URL when
    BAKONG_USE_RBK is enabled in the admin panel.
    """
    cfg   = _cfg()
    token = cfg["token"]
    base  = _api_url()

    parsed = urlparse(base)
    conn   = http.client.HTTPSConnection(parsed.netloc, timeout=15)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
        "User-Agent":    "tikdl-bot/1.0",
    }
    path = f"{parsed.path}{endpoint}"

    conn.request("POST", path, body=json.dumps(payload), headers=headers)
    resp = conn.getresponse()
    body = resp.read().decode()
    conn.close()

    log.debug(f"[bakong] POST {base}{endpoint} → HTTP {resp.status}: {body[:200]}")

    if resp.status == 200:
        return json.loads(body)
    elif resp.status == 401:
        raise ValueError("Bakong token expired or invalid. Renew it at api-bakong.nbc.gov.kh/developer")
    elif resp.status == 403:
        raise ValueError(
            "Bakong API blocked this IP (HTTP 403). "
            "Enable 'Use RBK Relay Token' in the admin panel Settings tab."
        )
    else:
        raise ValueError(f"Bakong API error HTTP {resp.status}: {body[:300]}")


def _get_khqr():
    """Return an initialised KHQR instance (used for QR generation only)."""
    cfg   = _cfg()
    token = cfg["token"]
    if not token:
        raise RuntimeError("Bakong token not configured — set it in admin panel Settings.")
    try:
        from bakong_khqr import KHQR
        import inspect
        params = inspect.signature(KHQR.__init__).parameters
        if "use_rbk" in params:
            return KHQR(token, use_rbk=cfg["use_rbk"])
        else:
            # New library: relay selected by token prefix internally.
            # We handle relay ourselves via _post() for check_payment,
            # so just instantiate normally here for QR generation.
            return KHQR(token)
    except ImportError:
        raise RuntimeError("bakong-khqr not installed. Add it to requirements.txt")


def generate_qr(amount_usd: float, order_id: int, description: str = "",
                currency: str | None = None) -> dict:
    """
    Generate a dynamic KHQR QR code.
    Returns: { qr_string, md5, qr_image (bytes|None), deeplink, amount, currency }
    """
    cfg = _cfg()
    cur = (currency or cfg["currency"]).upper()
    amount = usd_to_khr(amount_usd) if cur == "KHR" else round(amount_usd, 2)

    khqr = _get_khqr()

    qr_string = khqr.create_qr(
        bank_account   = cfg["account_id"],
        merchant_name  = cfg["merchant_name"],
        merchant_city  = cfg["merchant_city"],
        amount         = amount,
        currency       = cur,
        bill_number    = f"Nexus{order_id:04d}",
        store_label    = cfg["merchant_name"],
        phone_number   = "",
        terminal_label = f"Order#{order_id}",
        static         = False,
    )
    log.info(f"[bakong] create_qr order#{order_id} {cur} {amount} → {repr(str(qr_string)[:40])}")
    if not qr_string:
        raise ValueError("Empty QR string from create_qr")

    # Use the library's generate_md5 to guarantee hash matches Bakong API
    try:
        md5 = khqr.generate_md5(str(qr_string))
    except Exception:
        md5 = hashlib.md5(str(qr_string).encode("utf-8")).hexdigest()

    # QR image
    qr_image = None
    try:
        if hasattr(khqr, "qr_image"):
            qr_image = khqr.qr_image(qr_string, format="bytes")
        elif hasattr(khqr, "generate_qr_image"):
            qr_image = khqr.generate_qr_image(qr_string)
    except Exception as e:
        log.warning(f"[bakong] library qr_image failed: {e}")
    if not qr_image:
        try:
            import qrcode, io as _io2
            img = qrcode.make(str(qr_string))
            buf = _io2.BytesIO()
            img.save(buf, format="PNG")
            qr_image = buf.getvalue()
            log.info("[bakong] QR image generated via qrcode fallback")
        except Exception as e:
            log.warning(f"[bakong] qrcode fallback failed: {e}")

    try:
        deeplink = khqr.generate_deeplink(
            qr_string, callback="", appIconUrl="", appName=cfg["merchant_name"]
        )
    except Exception:
        deeplink = f"bakong://pay?data={qr_string}"

    log.info(f"[bakong] QR order#{order_id} {cur} {amount}")
    return {
        "qr_string": qr_string,
        "md5":       md5,
        "qr_image":  qr_image,
        "deeplink":  deeplink,
        "amount":    amount,
        "currency":  cur,
    }


def check_payment(md5: str) -> bool:
    """
    Returns True when Bakong confirms the transaction is PAID.

    Calls the Bakong API directly via _post() so that:
      1. A regular JWT token works with the RBK relay URL (use_rbk=true).
      2. The "responseCode" == 0 check is correct (PAID), not truthy string.
    """
    cfg = _cfg()
    log.info(f"[bakong] check_payment START — md5={md5[:8]}… api={_api_url()} use_rbk={cfg['use_rbk']} token_set={bool(cfg['token'])}")
    try:
        payload  = {"md5": md5}
        response = _post("/check_transaction_by_md5", payload)

        rc   = response.get("responseCode")
        paid = (rc == 0)
        log.info(f"[bakong] check_payment RESULT — md5={md5[:8]}… responseCode={rc} paid={paid} full={response}")
        return paid

    except Exception as e:
        log.warning(f"[bakong] check_payment ERROR — md5={md5[:8]}… error={e}")
        return False
