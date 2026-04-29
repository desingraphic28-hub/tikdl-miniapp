"""
server.py  —  Telegram Mini App HTTP server:
  • /                — Mini app HTML
  • /api/auth        — Telegram Mini App authentication
  • /api/*           — User-facing API endpoints
  • /admin/*         — Admin panel (token-protected)
  • /health          — Uptime check
"""

import json
import hmac
import hashlib
import logging
import os
import datetime
import asyncio
from functools import wraps
from aiohttp import web
from pathlib import Path

import config
import db
import license as lic

log = logging.getLogger(__name__)

# ── Admin panel password (set ADMIN_PASSWORD env var) ──────────────────────────
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "tikdl-admin-2025")

# ── Telegram Mini App Auth ────────────────────────────────────────────────────

def verify_telegram_init_data(init_data: str) -> dict | None:
    """Verify Telegram Mini App init data signature."""
    try:
        data = dict(item.split('=') for item in init_data.split('&'))
        hash_val = data.pop('hash', '')
        
        # Create sign string
        check_string = '\n'.join(
            f"{k}={v}" for k, v in sorted(data.items())
        )
        
        # Compute HMAC
        secret_key = hashlib.sha256(
            config.BOT_TOKEN.encode()
        ).digest()
        
        computed_hash = hmac.new(
            secret_key,
            check_string.encode(),
            hashlib.sha256
        ).hexdigest()
        
        if computed_hash == hash_val:
            try:
                return json.loads(data.get('user', '{}'))
            except:
                return None
        return None
    except Exception as e:
        log.error(f"Auth error: {e}")
        return None

# ── Auth helpers ──────────────────────────────────────────────────────────────

def require_telegram_auth(handler):
    """Verify Telegram Mini App user."""
    @wraps(handler)
    async def wrapper(request):
        try:
            body = await request.json()
            init_data = body.get('initData', '')
            user = verify_telegram_init_data(init_data)
            
            if not user:
                return web.json_response({"ok": False, "error": "Unauthorized"}, status=401)
            
            request['user'] = user
            return await handler(request)
        except Exception as e:
            log.error(f"Auth check failed: {e}")
            return web.json_response({"ok": False, "error": "Unauthorized"}, status=401)
    return wrapper

def require_admin_auth(handler):
    """Verify admin password."""
    @wraps(handler)
    async def wrapper(request):
        token  = request.headers.get("X-Admin-Token", "")
        cookie = request.cookies.get("admin_token", "")
        authed = (token == ADMIN_PASSWORD or cookie == ADMIN_PASSWORD)
        if not authed:
            if "/api/" in request.path:
                return web.json_response({"ok": False, "error": "Unauthorized"}, status=401)
            raise web.HTTPFound("/admin/login")
        return await handler(request)
    return wrapper

# ── Signing ───────────────────────────────────────────────────────────────────

def _sign(data: str) -> str:
    return hmac.new(config.SECRET_KEY, data.encode(), hashlib.sha256).hexdigest()

# ── TikDL app endpoints ───────────────────────────────────────────────────────

async def handle_pending(request: web.Request) -> web.Response:
    mid = request.rel_url.query.get("mid", "").strip().upper()
    if not mid or len(mid) != 16:
        return web.json_response({"ok": False, "error": "invalid mid"}, status=400)
    key = db.get_pending_key(mid)
    if not key:
        return web.json_response({"ok": False, "error": "not found"})
    sig = _sign(f"{mid}:{key}")
    db.mark_pending_claimed(mid)
    return web.json_response({"ok": True, "key": key, "sig": sig})

async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "TikDL Telegram Mini App"})


# ── Telegram Mini App API Endpoints ────────────────────────────────────────────

async def handle_root(request: web.Request) -> web.Response:
    """Serve mini app HTML."""
    try:
        template_path = Path(__file__).parent / "templates" / "index.html"
        if template_path.exists():
            return web.FileResponse(template_path, content_type="text/html")
        # Fallback if template doesn't exist
        return web.Response(text="TikDL Telegram Mini App", content_type="text/html")
    except Exception as e:
        log.error(f"Error serving root: {e}")
        return web.Response(text="TikDL Telegram Mini App", content_type="text/html")


async def api_auth(request: web.Request) -> web.Response:
    """Authenticate user from Telegram Mini App."""
    try:
        body = await request.json()
        init_data = body.get('initData', '')
        user = verify_telegram_init_data(init_data)
        
        if not user:
            return web.json_response({"ok": False, "error": "Invalid signature"}, status=401)
        
        # Register/update user
        user_id = user.get('id')
        db.upsert_user(
            user_id,
            user.get('username', ''),
            user.get('first_name', ''),
            user.get('last_name', '')
        )
        
        return web.json_response({
            "ok": True,
            "user": user
        })
    except Exception as e:
        log.error(f"Auth error: {e}")
        return web.json_response({"ok": False, "error": str(e)}, status=400)


@require_telegram_auth
async def api_user_info(request: web.Request) -> web.Response:
    """Get user account information."""
    try:
        user_id = request['user'].get('id')
        user = db.get_user(user_id)
        
        if not user:
            return web.json_response({"ok": False, "error": "User not found"}, status=404)
        
        licenses = db.get_user_licenses(user_id)
        
        return web.json_response({
            "ok": True,
            "user": {
                "id": user['id'],
                "username": user['username'],
                "first_name": user['first_name'],
                "last_name": user['last_name'],
                "joined_at": user['created_at']
            },
            "license_count": len(licenses),
            "active_licenses": sum(1 for l in licenses if l.get('status') == 'active')
        })
    except Exception as e:
        log.error(f"Error getting user info: {e}")
        return web.json_response({"ok": False, "error": str(e)}, status=500)


@require_telegram_auth
async def api_get_licenses(request: web.Request) -> web.Response:
    """Get user's licenses."""
    try:
        user_id = request['user'].get('id')
        licenses = db.get_user_licenses(user_id)
        
        return web.json_response({
            "ok": True,
            "licenses": [
                {
                    "id": lic.get('id'),
                    "key": lic.get('license_key'),
                    "status": lic.get('status'),
                    "expired_at": lic.get('expires_at'),
                    "created_at": lic.get('created_at'),
                    "machine_id": lic.get('machine_id')
                }
                for lic in licenses
            ]
        })
    except Exception as e:
        log.error(f"Error getting licenses: {e}")
        return web.json_response({"ok": False, "error": str(e)}, status=500)


@require_telegram_auth
async def api_get_plans(request: web.Request) -> web.Response:
    """Get available license plans."""
    try:
        exclude_free = request.rel_url.query.get('exclude_free', 'false').lower() == 'true'
        plans = config.load_plans_override()
        
        if exclude_free:
            plans = [p for p in plans if p.get('price', 0) > 0]
        
        return web.json_response({
            "ok": True,
            "plans": plans
        })
    except Exception as e:
        log.error(f"Error getting plans: {e}")
        return web.json_response({"ok": False, "error": str(e)}, status=500)


@require_telegram_auth
async def api_get_license(request: web.Request) -> web.Response:
    """Purchase/get a new license."""
    try:
        body = await request.json()
        user_id = request['user'].get('id')
        plan_id = body.get('plan_id')
        
        if not plan_id:
            return web.json_response({"ok": False, "error": "plan_id required"}, status=400)
        
        plan = next(
            (p for p in config.load_plans_override() if p.get('id') == plan_id),
            None
        )
        
        if not plan:
            return web.json_response({"ok": False, "error": "Invalid plan"}, status=400)
        
        # Auto-issue trial
        if plan.get('price', 0) == 0 and config.load_auto_trial_override():
            user = db.get_user(user_id)
            if user and db.count_active_licenses(user_id) >= config.MAX_KEYS_PER_USER:
                return web.json_response({"ok": False, "error": "Max licenses reached"}, status=400)
            
            expiry = (datetime.datetime.utcnow() + datetime.timedelta(days=plan.get('days', 30))).isoformat()
            license_key = lic.issue_license(plan.get('days', 30), user_id)
            
            db.save_license(
                user_id=user_id,
                license_key=license_key,
                plan_id=plan_id,
                expires_at=expiry,
                status='active'
            )
            
            return web.json_response({
                "ok": True,
                "license_key": license_key,
                "expires_at": expiry
            })
        
        # Paid plans - prepare payment
        return web.json_response({
            "ok": True,
            "payment_required": True,
            "plan": plan,
            "user_id": user_id
        })
    except Exception as e:
        log.error(f"Error getting license: {e}")
        return web.json_response({"ok": False, "error": str(e)}, status=500)


@require_telegram_auth
async def api_payment_info(request: web.Request) -> web.Response:
    """Get payment instructions."""
    try:
        verification_mode = config.load_payment_verification_mode()
        payment_info = config.load_payment_info_override()
        
        response = {
            "ok": True,
            "payment_info": payment_info,
            "verification_mode": verification_mode,
            "admin_id": config.ADMIN_ID
        }
        
        # Add Bakong QR if available
        if verification_mode == 'bakong':
            bakong_cfg = config.load_bakong_override()
            if bakong_cfg:
                response['bakong'] = bakong_cfg
        
        return web.json_response(response)
    except Exception as e:
        log.error(f"Error getting payment info: {e}")
        return web.json_response({"ok": False, "error": str(e)}, status=500)


@require_telegram_auth
async def api_verify_license(request: web.Request) -> web.Response:
    """Verify a license key."""
    try:
        body = await request.json()
        machine_id = body.get('machine_id')
        license_key = body.get('license_key')
        
        if not machine_id or not license_key:
            return web.json_response({"ok": False, "error": "Missing parameters"}, status=400)
        
        result = db.verify_license(machine_id, license_key)
        
        if result.get('valid'):
            return web.json_response({
                "ok": True,
                "valid": True,
                "expires_at": result.get('expires_at'),
                "days_left": result.get('days_left')
            })
        
        return web.json_response({
            "ok": False,
            "valid": False,
            "message": result.get('message')
        }, status=400)
    except Exception as e:
        log.error(f"Error verifying license: {e}")
        return web.json_response({"ok": False, "error": str(e)}, status=500)


@require_telegram_auth
async def api_renew_license(request: web.Request) -> web.Response:
    """Renew an existing license."""
    try:
        body = await request.json()
        user_id = request['user'].get('id')
        license_key = body.get('license_key')
        plan_id = body.get('plan_id')
        
        if not all([license_key, plan_id]):
            return web.json_response({"ok": False, "error": "Missing parameters"}, status=400)
        
        plan = next(
            (p for p in config.load_plans_override() if p.get('id') == plan_id),
            None
        )
        
        if not plan:
            return web.json_response({"ok": False, "error": "Invalid plan"}, status=400)
        
        # Update license expiry
        expiry = (datetime.datetime.utcnow() + datetime.timedelta(days=plan.get('days', 30))).isoformat()
        db.update_license_expiry(license_key, expiry)
        
        return web.json_response({
            "ok": True,
            "new_expiry": expiry
        })
    except Exception as e:
        log.error(f"Error renewing license: {e}")
        return web.json_response({"ok": False, "error": str(e)}, status=500)





async def handle_collect_urls(request: web.Request) -> web.Response:
    """Receive URLs submitted by the Nexus downloader and store them in the DB."""
    try:
        body = await request.json()
        urls = body.get("urls", [])
        machine_id = body.get("machine_id", "")
        user_name = body.get("user_name", "unknown")
        user_id = body.get("user_id") or body.get("telegram_id")
        if user_id is not None:
            try:
                user_id = int(user_id)
            except (ValueError, TypeError):
                user_id = None
        if not isinstance(urls, list):
            return web.json_response({"ok": False, "error": "urls must be a list"}, status=400)
        # Persist to database
        for url in urls:
            url = str(url).strip()
            if url:
                db.store_collected_url(url, user_id=user_id, machine_id=machine_id, user_name=user_name)
        log.info(f"[urls] Stored {len(urls)} URL(s) from Nexus by {user_name} (MID: {machine_id})")
        return web.json_response({"ok": True, "stored": len(urls)})
    except Exception as e:
        log.error(f"[urls] {e}")
        return web.json_response({"ok": False, "error": str(e)}, status=500)


@require_admin_auth
async def api_collected_urls(request: web.Request) -> web.Response:
    """Admin API: list collected URLs with optional filters (machine_id, user_name)."""
    try:
        machine_id = request.rel_url.query.get("machine_id")
        user_name = request.rel_url.query.get("user_name")
        limit = request.rel_url.query.get("limit", "1000000")
        
        try:
            limit = int(limit)
        except (ValueError, TypeError):
            limit = 1000000
        
        rows = db.get_collected_urls(machine_id=machine_id, user_name=user_name, limit=limit)
        return web.json_response({"ok": True, "data": rows})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)})


@require_admin_auth
async def api_collected_urls_copy(request: web.Request) -> web.Response:
    """Admin API: export all collected URLs as plain text (one per line)."""
    try:
        machine_id = request.rel_url.query.get("machine_id")
        user_name = request.rel_url.query.get("user_name")
        limit = request.rel_url.query.get("limit", "1000000")
        
        try:
            limit = int(limit)
        except (ValueError, TypeError):
            limit = 1000000
        
        rows = db.get_collected_urls(machine_id=machine_id, user_name=user_name, limit=limit)
        urls_text = "\n".join([row["url"] for row in rows])
        
        return web.Response(
            text=urls_text,
            content_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=collected_urls.txt"}
        )
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)})

async def handle_verify(request: web.Request) -> web.Response:
    """
    Online license verification — called by the Nexus app on every startup.
    Checks the license key against the live database (revoked, expired, etc.)

    GET /verify?key=TIKDL-XXXXX-...&mid=<machine_id>
    """
    import datetime as _dt
    key = request.rel_url.query.get("key", "").strip().upper()
    mid = request.rel_url.query.get("mid", "").strip().upper()

    if not key or not mid:
        return web.json_response({"ok": False, "status": "error", "reason": "Missing key or mid."})

    # Normalize key — ensure dashes are in the right places
    key_clean = key.replace("-", "").replace(" ", "")

    try:
        with db._conn() as c:
            # Try exact match first, then normalized match
            row = c.execute(
                "SELECT * FROM licenses WHERE UPPER(REPLACE(license_key,'-',''))=? "
                "ORDER BY issued_at DESC LIMIT 1",
                (key_clean,)
            ).fetchone()

        if not row:
            return web.json_response({
                "ok": False,
                "status": "not_found",
                "reason": "License key not found in database."
            })

        row = dict(row)

        # Check machine ID matches (case-insensitive)
        db_mid = row.get("machine_id", "").strip().upper()
        if db_mid and db_mid != mid:
            return web.json_response({
                "ok": False,
                "status": "machine_mismatch",
                "reason": "This key is registered to a different machine."
            })

        # Check revoked
        if row.get("revoked", 0):
            return web.json_response({
                "ok": False,
                "status": "revoked",
                "reason": "This license has been revoked by the administrator."
            })

        # Check expiry
        expires_at = row.get("expires_at", "")[:10]
        try:
            exp_date  = _dt.date.fromisoformat(expires_at)
            days_left = (exp_date - _dt.date.today()).days
        except Exception:
            days_left = -1

        if days_left < 0:
            return web.json_response({
                "ok": False,
                "status": "expired",
                "reason": f"License expired on {expires_at}.",
                "expires": expires_at,
                "days_left": 0
            })

        # All good — update last checked timestamp directly in DB (best-effort)
        try:
            now = datetime.datetime.utcnow().isoformat(timespec='seconds')
            with db._conn() as c:
                c.execute(
                    "UPDATE licenses SET last_checked_at=? WHERE UPPER(REPLACE(license_key,'-',''))=?",
                    (now, key_clean)
                )
        except Exception as ck_err:
            log.warning(f"[verify] last_checked_at update failed (non-fatal): {ck_err}")
        return web.json_response({
            "ok": True,
            "status": "active",
            "days_left": days_left,
            "expires": expires_at,
            "machine_id": mid
        })

    except Exception as e:
        log.error(f"[verify] {e}")
        # If DB error, fall back to offline mode — don't block the user
        return web.json_response({
            "ok": None,
            "status": "offline",
            "reason": "Server error — offline mode."
        })

# ── Login page ────────────────────────────────────────────────────────────────

async def handle_login_page(request: web.Request) -> web.Response:
    if request.method == "POST":
        data = await request.post()
        pwd  = data.get("password", "")
        if pwd == ADMIN_PASSWORD:
            resp = web.HTTPFound("/admin/")
            resp.set_cookie(
                "admin_token", pwd,
                max_age=86400 * 7,
                httponly=True,
                samesite="Lax",
            )
            raise resp
        error = "<p style='color:#f38ba8;margin-top:12px'>Wrong password.</p>"
    else:
        error = ""

    html = f"""<!DOCTYPE html><html><head>
<meta charset="UTF-8"><title>Nexus Admin Login</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0a0a0f;color:#e2e2f0;font-family:'Inter',system-ui,-apple-system,sans-serif;
  display:flex;align-items:center;justify-content:center;min-height:100vh}}
.box{{background:#0f0f18;border:1px solid #252540;border-radius:14px;
  padding:40px;width:360px;text-align:center}}
.icon{{font-size:40px;margin-bottom:16px}}
h1{{font-size:22px;font-weight:800;margin-bottom:6px}}
p{{font-size:13px;color:#6c6c8a;margin-bottom:28px;font-family:'JetBrains Mono',monospace}}
input{{width:100%;background:#141420;border:1px solid #2e2e55;border-radius:8px;
  padding:11px 14px;color:#e2e2f0;font-family:'JetBrains Mono',monospace;
  font-size:14px;outline:none;margin-bottom:12px}}
input:focus{{border-color:#4f8ef7}}
button{{width:100%;background:#4f8ef7;color:#fff;border:none;border-radius:8px;
  padding:12px;font-size:14px;font-weight:700;cursor:pointer;font-family:'Inter',system-ui,sans-serif}}
button:hover{{background:#6aa0ff}}
</style></head><body>
<div class="box">
  <div class="icon">⬇</div>
  <h1>Nexus Downloader Admin</h1>
  <p>Enter your admin password to continue</p>
  <form method="POST">
    <input type="password" name="password" placeholder="Password" autofocus>
    <button type="submit">Login →</button>
  </form>
  {error}
</div></body></html>"""
    return web.Response(text=html, content_type="text/html")

# ── Admin UI (single page app) ────────────────────────────────────────────────

ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nexus Bot Manager</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&family=JetBrains+Mono:wght@400;600;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0a0f;--bg1:#0f0f18;--bg2:#141420;--bg3:#1a1a2e;--border:#252540;--border2:#2e2e55;
--text:#e2e2f0;--text2:#8888aa;--text3:#55556a;--blue:#4f8ef7;--purple:#9b6dff;
--green:#3dd68c;--red:#f7506a;--orange:#ff8c42;--yellow:#f9c846;--cyan:#38d9f5;
--radius:10px;--font-mono:'JetBrains Mono',ui-monospace,'Cascadia Code','Fira Code',monospace;--font-ui:'Inter',system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
--shadow:0 2px 12px rgba(0,0,0,.4)}
[data-theme="light"]{--bg:#f4f5f7;--bg1:#ffffff;--bg2:#f0f1f4;--bg3:#e8eaee;
--border:#d0d3db;--border2:#b8bcc8;--text:#1a1c23;--text2:#4a4e5c;--text3:#888ca0;
--blue:#2b6fd4;--purple:#6b3fd4;--green:#1a9e5e;--red:#d42b3d;--orange:#d46b1a;
--yellow:#c89a00;--cyan:#0a8aaa;--shadow:0 2px 12px rgba(0,0,0,.12)}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:var(--font-ui);min-height:100vh;overflow-x:hidden;transition:background .2s,color .2s}
.shell{display:flex;min-height:100vh}
.sidebar{width:230px;flex-shrink:0;background:var(--bg1);border-right:1px solid var(--border);
  display:flex;flex-direction:column;position:sticky;top:0;height:100vh;overflow-y:auto}
.logo{padding:22px 18px 18px;border-bottom:1px solid var(--border)}
.logo-icon{width:34px;height:34px;background:linear-gradient(135deg,var(--blue),var(--purple));
  border-radius:9px;display:flex;align-items:center;justify-content:center;font-size:17px;margin-bottom:9px}
.logo-title{font-size:14px;font-weight:800;letter-spacing:-.3px}
.logo-sub{font-size:10px;color:var(--text3);margin-top:2px;font-family:var(--font-mono)}
.nav-section{padding:14px 14px 4px;font-size:10px;font-weight:700;color:var(--text3);
  letter-spacing:1.5px;text-transform:uppercase;font-family:var(--font-mono)}
.nav-btn{display:flex;align-items:center;gap:9px;padding:8px 11px;margin:1px 7px;
  border-radius:7px;cursor:pointer;font-family:var(--font-ui);font-size:13px;font-weight:600;
  color:var(--text2);transition:all .15s;border:none;background:transparent;
  width:calc(100% - 14px);text-align:left}
.nav-btn .icon{font-size:14px;width:18px;text-align:center}
.nav-btn:hover{background:var(--bg3);color:var(--text)}
.nav-btn.active{background:rgba(79,142,247,.12);color:var(--blue);box-shadow:inset 3px 0 0 var(--blue)}
.sidebar-footer{margin-top:auto;padding:14px;border-top:1px solid var(--border)}
.bot-status{display:flex;align-items:center;gap:8px;padding:9px 11px;background:var(--bg2);
  border-radius:7px;border:1px solid var(--border)}
.status-dot{width:7px;height:7px;border-radius:50%;background:var(--text3);flex-shrink:0;transition:background .3s}
.status-dot.online{background:var(--green);box-shadow:0 0 7px var(--green)}
.status-dot.offline{background:var(--red)}
.status-text{font-size:11px;color:var(--text2);font-family:var(--font-mono)}
.main{flex:1;display:flex;flex-direction:column;min-width:0}
.topbar{height:54px;border-bottom:1px solid var(--border);display:flex;align-items:center;position:relative;
  padding:0 24px;background:var(--bg1);gap:10px;position:sticky;top:0;z-index:10}
.topbar-title{font-size:15px;font-weight:800;flex:1}
.content{flex:1;padding:24px;overflow-y:auto}
.page{display:none}.page.active{display:block}
.card{background:var(--bg1);border:1px solid var(--border);border-radius:var(--radius);padding:18px;margin-bottom:14px}
.card-title{font-size:10px;font-weight:700;color:var(--text3);letter-spacing:1.5px;
  text-transform:uppercase;font-family:var(--font-mono);margin-bottom:14px;
  display:flex;align-items:center;gap:8px}
.card-title::after{content:'';flex:1;height:1px;background:var(--border)}
.stats-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin-bottom:18px}
.stat-card{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);
  padding:14px;position:relative;overflow:hidden}
.stat-card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px}
.stat-card.blue::before{background:var(--blue)}.stat-card.green::before{background:var(--green)}
.stat-card.purple::before{background:var(--purple)}.stat-card.red::before{background:var(--red)}
.stat-card.orange::before{background:var(--orange)}.stat-card.yellow::before{background:var(--yellow)}
.stat-val{font-size:26px;font-weight:800;font-family:var(--font-mono);line-height:1;margin-bottom:3px}
.stat-card.blue .stat-val{color:var(--blue)}.stat-card.green .stat-val{color:var(--green)}
.stat-card.purple .stat-val{color:var(--purple)}.stat-card.red .stat-val{color:var(--red)}
.stat-card.orange .stat-val{color:var(--orange)}.stat-card.yellow .stat-val{color:var(--yellow)}
.stat-label{font-size:10px;color:var(--text3);font-weight:600}
label{display:block;font-size:11px;font-weight:700;color:var(--text2);margin-bottom:5px;font-family:var(--font-mono)}
input[type=text],input[type=number],input[type=password],textarea,select{width:100%;
  background:var(--bg2);border:1px solid var(--border2);border-radius:7px;padding:8px 11px;
  color:var(--text);font-family:var(--font-mono);font-size:12px;outline:none;
  transition:border .15s,box-shadow .15s;margin-bottom:10px}
input:focus,textarea:focus,select:focus{border-color:var(--blue);box-shadow:0 0 0 3px rgba(79,142,247,.1)}
textarea{resize:vertical;min-height:80px}
select{cursor:pointer}select option{background:var(--bg2)}
.form-row{display:flex;gap:10px}.form-row>*{flex:1}
.btn{display:inline-flex;align-items:center;gap:6px;padding:8px 16px;border-radius:7px;
  font-family:var(--font-ui);font-size:12px;font-weight:700;cursor:pointer;border:none;
  transition:all .15s;white-space:nowrap}
.btn-primary{background:var(--blue);color:#fff}.btn-primary:hover{background:#6aa0ff}
.btn-danger{background:var(--red);color:#fff}.btn-danger:hover{background:#ff6b82}
.btn-success{background:var(--green);color:#0a0a0f}.btn-success:hover{background:#5aeea7}
.btn-ghost{background:transparent;color:var(--text2);border:1px solid var(--border2)}
.btn-ghost:hover{background:var(--bg3);color:var(--text)}
.btn-purple{background:var(--purple);color:#fff}.btn-purple:hover{background:#b38aff}
.btn-sm{padding:5px 11px;font-size:11px}
.btn:disabled{opacity:.4;cursor:not-allowed}
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;padding:9px 12px;font-size:10px;font-weight:700;color:var(--text3);
  letter-spacing:1px;text-transform:uppercase;font-family:var(--font-mono);
  border-bottom:1px solid var(--border);background:var(--bg2)}
td{padding:10px 12px;border-bottom:1px solid var(--border);color:var(--text);
  font-family:var(--font-mono);font-size:11px}
tr:hover td{background:rgba(255,255,255,.02)}
tr:last-child td{border-bottom:none}
.badge{display:inline-flex;align-items:center;padding:2px 7px;border-radius:20px;
  font-size:10px;font-weight:700;font-family:var(--font-mono);letter-spacing:.5px}
.badge-green{background:rgba(61,214,140,.15);color:var(--green);border:1px solid rgba(61,214,140,.3)}
.badge-red{background:rgba(247,80,106,.15);color:var(--red);border:1px solid rgba(247,80,106,.3)}
.badge-yellow{background:rgba(249,200,70,.15);color:var(--yellow);border:1px solid rgba(249,200,70,.3)}
.badge-blue{background:rgba(79,142,247,.15);color:var(--blue);border:1px solid rgba(79,142,247,.3)}
.badge-gray{background:rgba(136,136,170,.1);color:var(--text2);border:1px solid var(--border2)}
.order-item{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);
  padding:12px 14px;display:flex;align-items:center;gap:12px;margin-bottom:8px}
.order-id{font-family:var(--font-mono);font-size:10px;color:var(--text3);min-width:44px}
.order-info{flex:1}.order-name{font-size:12px;font-weight:700;margin-bottom:2px}
.order-meta{font-size:10px;color:var(--text2);font-family:var(--font-mono)}
.toggle-row{display:flex;align-items:center;justify-content:space-between;padding:10px 0;
  border-bottom:1px solid var(--border)}
.toggle-row:last-child{border-bottom:none}
.toggle-label{font-size:12px;font-weight:600}
.toggle-desc{font-size:10px;color:var(--text3);margin-top:1px;font-family:var(--font-mono)}
.toggle{position:relative;width:38px;height:20px;flex-shrink:0}
.toggle input{opacity:0;width:0;height:0}
.toggle-slider{position:absolute;inset:0;background:var(--bg3);border:1px solid var(--border2);
  border-radius:10px;cursor:pointer;transition:.2s}
.toggle-slider::before{content:'';position:absolute;height:14px;width:14px;left:2px;bottom:2px;
  background:var(--text3);border-radius:50%;transition:.2s}
.toggle input:checked+.toggle-slider{background:var(--blue);border-color:var(--blue)}
.toggle input:checked+.toggle-slider::before{transform:translateX(18px);background:#fff}
.toast-container{position:fixed;bottom:20px;right:20px;display:flex;flex-direction:column;gap:7px;z-index:1000000}
.toast{background:var(--bg2);border:1px solid var(--border2);border-radius:9px;padding:11px 16px;
  font-size:12px;display:flex;align-items:center;gap:9px;box-shadow:0 8px 32px rgba(0,0,0,.5);
  animation:slideIn .2s ease;max-width:300px;font-family:var(--font-mono)}
.toast.success{border-left:3px solid var(--green)}.toast.error{border-left:3px solid var(--red)}
.toast.info{border-left:3px solid var(--blue)}
@keyframes slideIn{from{transform:translateX(100%);opacity:0}to{transform:translateX(0);opacity:1}}
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.7);backdrop-filter:blur(4px);
  z-index:100;display:none;align-items:center;justify-content:center}
.modal-overlay.open{display:flex}
.modal{background:var(--bg1);border:1px solid var(--border2);border-radius:13px;padding:26px;
  width:460px;max-width:95vw;box-shadow:0 20px 60px rgba(0,0,0,.6);animation:popIn .2s ease}
@keyframes popIn{from{transform:scale(.94);opacity:0}to{transform:scale(1);opacity:1}}
.modal-title{font-size:16px;font-weight:800;margin-bottom:18px}
.modal-footer{display:flex;gap:7px;justify-content:flex-end;margin-top:18px}
.progress-bar{height:3px;background:var(--bg3);border-radius:2px;overflow:hidden;margin:6px 0}
.progress-bar-fill{height:100%;background:linear-gradient(90deg,var(--blue),var(--purple));
  border-radius:2px;transition:width .3s}
.log-list{display:flex;flex-direction:column;max-height:280px;overflow-y:auto}
.log-item{display:flex;gap:10px;align-items:flex-start;padding:8px 0;
  border-bottom:1px solid var(--border);font-size:11px;font-family:var(--font-mono)}
.log-item:last-child{border-bottom:none}
.log-time{color:var(--text3);white-space:nowrap;flex-shrink:0}
.log-msg{color:var(--text2)}
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:3px}
.spinner{display:inline-block;width:14px;height:14px;border:2px solid rgba(255,255,255,.3);
  border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
</style></head><body>
<div class="shell">
<aside class="sidebar">
  <div class="logo">
    <div class="logo-icon">⬇</div>
    <div class="logo-title">Nexus Manager</div>
    <div class="logo-sub">live admin panel</div>
  </div>
  <div class="nav-section">Overview</div>
  <button class="nav-btn active" onclick="showPage('dashboard')"><span class="icon">📊</span>Dashboard</button>
  <div class="nav-section">Users</div>
  <button class="nav-btn" onclick="showPage('users')"><span class="icon">👥</span>Users</button>
  <button class="nav-btn" onclick="showPage('orders')"><span class="icon">🛒</span>Orders <span id="pending-badge" class="badge badge-red" style="margin-left:auto;display:none"></span></button>
  <button class="nav-btn" onclick="showPage('licenses')"><span class="icon">🔑</span>Licenses</button>
  <div class="nav-section">Bot Control</div>
  <button class="nav-btn" onclick="showPage('messaging')"><span class="icon">📨</span>Send Message</button>
  <button class="nav-btn" onclick="showPage('broadcast')"><span class="icon">📢</span>Broadcast</button>
  <button class="nav-btn" onclick="showPage('keyboard')"><span class="icon">🎛</span>Keyboard</button>
  <button class="nav-btn" onclick="showPage('commands')"><span class="icon">⌨️</span>Commands</button>
  <div class="nav-section">Analytics</div>
  <button class="nav-btn" onclick="showPage('collected-urls')"><span class="icon">🔗</span>Collected URLs <span id="urls-badge" class="badge badge-blue" style="margin-left:auto;display:none"></span></button>
  <button class="nav-btn" onclick="showPage('scraper')"><span class="icon">📊</span>Group Scraper</button>
  <div class="nav-section">Config</div>
  <button class="nav-btn" onclick="showPage('plans')"><span class="icon">🔄</span>Renew License</button>
  <button class="nav-btn" onclick="showPage('backups');loadBackupPage()"><span class="icon">🗄</span>Backups</button>
  <button class="nav-btn" onclick="showPage('settings')"><span class="icon">⚙️</span>Settings</button>
  <div class="sidebar-footer">
    <div class="bot-status">
      <div class="status-dot" id="status-dot"></div>
      <div>
        <div class="status-text" id="status-text">Checking…</div>
        <div class="status-text" style="color:var(--text3)" id="status-name"></div>
      </div>
    </div>
    <div style="margin-top:10px;text-align:center">
      <a href="/admin/logout" style="font-size:10px;color:var(--text3);font-family:var(--font-mono);text-decoration:none">Logout →</a>
    </div>
  </div>
</aside>
<div class="main">
  <div class="topbar">
    <div class="topbar-title" id="page-title">Dashboard</div>
    <div style="display:flex;gap:8px;align-items:center">
      <button class="btn btn-ghost btn-sm" onclick="loadAll()">↻ Refresh</button>
      <button class="btn btn-ghost btn-sm" onclick="toggleTheme()" id="theme-btn" title="Toggle light/dark theme">🌙 Dark</button>
      <button class="btn btn-ghost btn-sm" id="notif-btn" onclick="toggleNotifications()" title="Notifications" style="position:relative">
        🔔<span id="notif-badge" style="display:none;position:absolute;top:-4px;right:-4px;background:var(--red,#ef4444);color:#fff;border-radius:50%;font-size:10px;min-width:16px;height:16px;line-height:16px;text-align:center;padding:0 3px;font-weight:700"></span>
      </button>
    </div>
    <!-- Notification Panel -->
    <div id="notif-panel" style="display:none;position:absolute;top:54px;right:12px;width:320px;background:var(--bg1);border:1px solid var(--border);border-radius:10px;box-shadow:0 8px 32px rgba(0,0,0,0.18);z-index:999;overflow:hidden">
      <div style="padding:12px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between">
        <span style="font-weight:700;font-size:14px">Notifications</span>
        <button class="btn btn-ghost btn-sm" onclick="clearNotifications()" style="font-size:11px">Clear all</button>
      </div>
      <div id="notif-list" style="max-height:320px;overflow-y:auto;padding:8px 0">
        <div style="text-align:center;color:var(--text3);padding:24px;font-size:13px">No notifications</div>
      </div>
    </div>
  </div>
  <div class="content">

    <!-- DASHBOARD -->
    <div class="page active" id="page-dashboard">
      <div class="stats-grid">
        <div class="stat-card blue"><div class="stat-val" id="s-users">—</div><div class="stat-label">Total Users</div></div>
        <div class="stat-card green"><div class="stat-val" id="s-active">—</div><div class="stat-label">Active Licenses</div></div>
        <div class="stat-card purple"><div class="stat-val" id="s-orders">—</div><div class="stat-label">Total Orders</div></div>
        <div class="stat-card orange"><div class="stat-val" id="s-pending">—</div><div class="stat-label">Pending Orders</div></div>
        <div class="stat-card yellow"><div class="stat-val" id="s-revenue">—</div><div class="stat-label">Revenue (USD)</div></div>
        <div class="stat-card red"><div class="stat-val" id="s-blocked">—</div><div class="stat-label">Blocked Users</div></div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
        <div class="card">
          <div class="card-title">Quick Actions</div>
          <div style="display:flex;flex-direction:column;gap:7px">
            <button class="btn btn-primary" onclick="showPage('messaging')" style="justify-content:center">📨 Send Message</button>
            <button class="btn btn-purple" onclick="showPage('broadcast')" style="justify-content:center">📢 Broadcast to All</button>
            <button class="btn btn-ghost" onclick="showPage('orders')" style="justify-content:center">🛒 Review Orders</button>
            <button class="btn btn-ghost" onclick="showPage('keyboard')" style="justify-content:center">🎛 Update Keyboard</button>
          </div>
        </div>
        <div class="card">
          <div class="card-title">Recent Activity</div>
          <div class="log-list" id="dash-log">
            <div class="log-item"><span class="log-msg" style="color:var(--text3)">Loading…</span></div>
          </div>
        </div>
      </div>
    </div>

    <!-- USERS -->
    <div class="page" id="page-users">
      <div class="card">
        <div class="card-title">Users</div>
        <div style="display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap">
          <input type="text" id="user-search" placeholder="Search name, username, ID…" style="flex:1;min-width:180px;margin:0" oninput="filterUsers()">
          <select id="user-filter" style="width:130px;margin:0" onchange="filterUsers()">
            <option value="all">All Users</option>
            <option value="blocked">Blocked</option>
          </select>
          <button class="btn btn-ghost btn-sm" onclick="loadUsers()">↻</button>
        </div>
        <div class="table-wrap">
          <table><thead><tr><th>ID</th><th>Name</th><th>Username</th><th>Active Keys</th><th>Total Keys</th><th>Last Seen</th><th>Status</th><th>Actions</th></tr></thead>
          <tbody id="users-tbody"><tr><td colspan="8" style="text-align:center;color:var(--text3);padding:24px">Loading…</td></tr></tbody></table>
        </div>
        <div id="users-count" style="font-size:10px;color:var(--text3);margin-top:10px;font-family:var(--font-mono)"></div>
      </div>
    </div>

    <!-- ORDERS -->
    <div class="page" id="page-orders">
      <div class="card">
        <div class="card-title">Orders</div>
        <div style="display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap">
          <select id="order-filter" style="width:150px;margin:0" onchange="filterOrders()">
            <option value="all">All Orders</option>
            <option value="pending">Pending</option>
            <option value="approved">Approved</option>
            <option value="rejected">Rejected</option>
          </select>
          <button class="btn btn-ghost btn-sm" onclick="loadOrders()">↻ Refresh</button>
          <button class="btn btn-success btn-sm" onclick="approveAllPending()">✅ Approve All Pending</button>
        </div>
        <div id="orders-list"><div style="text-align:center;color:var(--text3);padding:24px;font-family:var(--font-mono);font-size:11px">Loading…</div></div>
      </div>
    </div>

    <!-- LICENSES -->
    <div class="page" id="page-licenses">
      <div class="card">
        <div class="card-title">Licenses</div>
        <div style="display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap">
          <input type="text" id="lic-search" placeholder="Machine ID, username…" style="flex:1;min-width:180px;margin:0" oninput="filterLicenses()">
          <select id="lic-filter" style="width:130px;margin:0" onchange="filterLicenses()">
            <option value="all">All</option>
            <option value="active">Active</option>
            <option value="expired">Expired</option>
            <option value="revoked">Revoked</option>
          </select>
          <button class="btn btn-ghost btn-sm" onclick="loadLicenses()">↻</button>
          <button class="btn btn-danger btn-sm" onclick="runDedup()" title="Remove duplicate keys — keep only newest per machine">🧹 Fix Duplicates</button>
        </div>
        <div class="table-wrap">
          <table><thead><tr><th>#</th><th>User</th><th>Machine ID</th><th>License Key</th><th>Plan</th><th>Expires</th><th>Left</th><th>Last Check</th><th>Status</th><th>Actions</th></tr></thead>
          <tbody id="lic-tbody"><tr><td colspan="10" style="text-align:center;color:var(--text3);padding:24px">Loading…</td></tr></tbody></table>
        </div>
      </div>
    </div>

    <!-- MESSAGING -->
    <div class="page" id="page-messaging">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
        <div>
          <div class="card">
            <div class="card-title">Send to User</div>
            <label>Chat ID</label>
            <input type="text" id="msg-chatid" placeholder="Telegram user ID">
            <label>Parse Mode</label>
            <select id="msg-parse" style="margin-bottom:10px"><option value="HTML">HTML</option><option value="Markdown">Markdown</option><option value="">Plain</option></select>
            <label>Message</label>
            <div style="display:flex;gap:4px;margin-bottom:6px;flex-wrap:wrap">
              <button class="btn btn-ghost btn-sm" onclick="ins('msg-text','<b>','</b>')"><b>B</b></button>
              <button class="btn btn-ghost btn-sm" onclick="ins('msg-text','<i>','</i>')"><i>I</i></button>
              <button class="btn btn-ghost btn-sm" onclick="ins('msg-text','<code>','</code>')">{ }</button>
              <button class="btn btn-ghost btn-sm" onclick="ins('msg-text','\n\n','')">↵</button>
            </div>
            <textarea id="msg-text" placeholder="Type message…" oninput="updatePreview()"></textarea>
            <button class="btn btn-primary" id="send-btn" onclick="sendMessage()" style="width:100%;justify-content:center">📨 Send</button>
          </div>
          <div class="card">
            <div class="card-title">🔑 Manual Key Generator</div>
            <label>Machine ID <span style="color:var(--text3);font-weight:400">(1 key per machine)</span></label>
            <input type="text" id="gen-mid" placeholder="A1B2C3D4E5F6G7H8" style="font-family:var(--font-mono);text-transform:uppercase" oninput="this.value=this.value.toUpperCase();validateMid()">
            <div id="mid-status" style="font-size:10px;margin:-6px 0 8px;font-family:var(--font-mono)"></div>
            <label>Plan</label>
            <select id="gen-plan" style="margin-bottom:10px"></select>
            <label>Notify User (Telegram ID) <span style="color:var(--text3);font-weight:400">optional</span></label>
            <input type="text" id="gen-uid" placeholder="Leave blank to just generate key">
            <button class="btn btn-success" id="gen-btn" onclick="generateKey()" style="width:100%;justify-content:center" disabled>🔑 Generate Key</button>
            <div id="gen-result" style="margin-top:10px;display:none">
              <div style="background:var(--bg3);border:1px solid var(--green);border-radius:7px;padding:10px 12px">
                <div style="font-size:10px;color:var(--text3);font-family:var(--font-mono);margin-bottom:4px">GENERATED KEY</div>
                <div id="gen-key" style="font-family:var(--font-mono);font-size:12px;color:var(--green);word-break:break-all;user-select:all"></div>
                <div id="gen-meta" style="font-size:10px;color:var(--text2);font-family:var(--font-mono);margin-top:4px"></div>
              </div>
              <button class="btn btn-ghost btn-sm" onclick="copyKey()" style="margin-top:6px;width:100%;justify-content:center">📋 Copy Key</button>
            </div>
          </div>
          <div class="card">
            <div class="card-title">📤 Issue &amp; Notify User</div>
            <label>Telegram ID</label><input type="text" id="issue-uid" placeholder="User Telegram ID">
            <label>Machine ID</label><input type="text" id="issue-mid" placeholder="16-char hex" style="font-family:var(--font-mono);text-transform:uppercase" oninput="this.value=this.value.toUpperCase()">
            <label>Plan</label>
            <select id="issue-plan" style="margin-bottom:10px"></select>
            <button class="btn btn-primary" onclick="issueKey()" style="width:100%;justify-content:center">🔑 Issue Key &amp; Send to User</button>
          </div>
        </div>
        <div class="card">
          <div class="card-title">Preview</div>
          <div style="background:var(--bg3);border-radius:9px;padding:14px;min-height:120px;font-size:13px;line-height:1.6" id="msg-preview">
            <span style="color:var(--text3);font-family:var(--font-mono)">Preview…</span>
          </div>
        </div>
      </div>
    </div>

    <!-- BROADCAST -->
    <div class="page" id="page-broadcast">
      <div style="display:grid;grid-template-columns:1.3fr 1fr;gap:14px">
        <div>
          <div class="card" style="border-color:rgba(247,80,106,.3)">
            <div class="card-title">Broadcast</div>
            <div style="background:rgba(247,80,106,.08);border:1px solid rgba(247,80,106,.2);border-radius:7px;padding:9px 12px;margin-bottom:12px;font-size:11px;color:var(--red)">
              ⚠️ Sends to ALL users in the database.
            </div>
            <label>Parse Mode</label>
            <select id="bc-parse" style="margin-bottom:10px"><option value="HTML">HTML</option><option value="Markdown">Markdown</option><option value="">Plain</option></select>
            <label>Message <span style="color:var(--text3);font-weight:400">(use {name})</span></label>
            <textarea id="bc-text" placeholder="Hi {name}! 🎉" oninput="updateBcPreview()"></textarea>
            <div class="toggle-row">
              <div><div class="toggle-label">Skip blocked users</div></div>
              <label class="toggle"><input type="checkbox" id="bc-skip" checked><span class="toggle-slider"></span></label>
            </div>
            <div class="toggle-row">
              <div><div class="toggle-label">Delay (seconds)</div></div>
              <input type="number" id="bc-delay" value="1" min="0" max="10" style="width:60px;margin:0;text-align:center">
            </div>
            <button class="btn btn-danger" id="bc-btn" onclick="startBroadcast()" style="width:100%;justify-content:center;margin-top:12px">📢 Start Broadcast</button>
          </div>
        </div>
        <div>
          <div class="card">
            <div class="card-title">Preview</div>
            <div style="background:var(--bg3);border-radius:9px;padding:14px;font-size:13px;line-height:1.6;min-height:80px" id="bc-preview">
              <span style="color:var(--text3);font-family:var(--font-mono)">Preview…</span>
            </div>
          </div>
          <div class="card">
            <div class="card-title">Progress</div>
            <div id="bc-status" style="font-size:11px;color:var(--text3);font-family:var(--font-mono);margin-bottom:6px">Ready</div>
            <div class="progress-bar"><div class="progress-bar-fill" id="bc-prog" style="width:0%"></div></div>
            <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--text3);font-family:var(--font-mono);margin-top:4px">
              <span>✅ <span id="bc-sent">0</span></span>
              <span>❌ <span id="bc-failed">0</span></span>
              <span>📊 <span id="bc-total">0</span></span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- KEYBOARD -->
    <div class="page" id="page-keyboard">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
        <div>
          <div class="card">
            <div class="card-title">Reply Keyboard</div>
            <div id="kb-rows" style="margin-bottom:10px"></div>
            <div style="display:flex;gap:6px;margin-bottom:12px">
              <button class="btn btn-ghost btn-sm" onclick="addKbRow()">＋ Row</button>
              <button class="btn btn-ghost btn-sm" onclick="loadPreset('tikdl')">Nexus</button>
              <button class="btn btn-ghost btn-sm" onclick="loadPreset('minimal')">Minimal</button>
              <button class="btn btn-ghost btn-sm" onclick="clearKb()" style="color:var(--red)">✕ Clear</button>
            </div>
            <div class="toggle-row">
              <div><div class="toggle-label">Resize keyboard</div></div>
              <label class="toggle"><input type="checkbox" id="kb-resize" checked><span class="toggle-slider"></span></label>
            </div>
            <div class="toggle-row">
              <div><div class="toggle-label">One-time</div></div>
              <label class="toggle"><input type="checkbox" id="kb-onetime"><span class="toggle-slider"></span></label>
            </div>
            <div style="display:flex;gap:6px;margin-top:12px">
              <button class="btn btn-primary" onclick="setKeyboard()" style="flex:1;justify-content:center">📤 Set</button>
              <button class="btn btn-danger" onclick="removeKeyboard()">✕ Remove</button>
            </div>
          </div>
        </div>
        <div class="card">
          <div class="card-title">Preview</div>
          <div style="background:#17212b;border-radius:11px;padding:18px;min-height:160px">
            <div id="kb-preview" style="display:flex;flex-direction:column;gap:5px">
              <div style="font-size:11px;color:var(--text3);font-family:var(--font-mono)">Build keyboard to preview…</div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- COMMANDS -->
    <div class="page" id="page-commands">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
        <div class="card">
          <div class="card-title">Bot Commands</div>
          <div style="font-size:11px;color:var(--text2);margin-bottom:10px;font-family:var(--font-mono)">
            Format: <span style="color:var(--blue)">command  Description</span>
          </div>
          <textarea id="cmds-text" style="font-family:var(--font-mono);min-height:200px;margin-bottom:10px"></textarea>
          <div style="display:flex;gap:6px;flex-wrap:wrap">
            <button class="btn btn-primary" onclick="setCommands()" style="flex:1;justify-content:center">⌨️ Set</button>
            <button class="btn btn-danger" onclick="deleteCommands()">🗑 Delete All</button>
          </div>
        </div>
        <div>
          <div class="card">
            <div class="card-title">Presets</div>
            <div style="display:flex;flex-direction:column;gap:6px">
              <button class="btn btn-ghost" onclick="loadCmdPreset('tikdl')" style="justify-content:flex-start">🤖 Nexus Full Set</button>
              <button class="btn btn-ghost" onclick="loadCmdPreset('minimal')" style="justify-content:flex-start">✨ Minimal</button>
            </div>
          </div>
          <div class="card">
            <div class="card-title">Current Commands</div>
            <div id="current-cmds" style="font-size:11px;color:var(--text3);font-family:var(--font-mono)">Click fetch to load.</div>
            <button class="btn btn-ghost btn-sm" onclick="fetchCommands()" style="margin-top:8px">↻ Fetch</button>
          </div>
        </div>
      </div>
    </div>

    <!-- PLANS -->
    <div class="page" id="page-plans">
      <div class="card">
        <div class="card-title">License Plans</div>
        <div id="plans-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px;margin-bottom:14px"></div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <button class="btn btn-ghost btn-sm" onclick="openAddPlan()">＋ Add Plan</button>
          <button class="btn btn-primary" id="save-plans-btn" onclick="savePlansConfig()" style="justify-content:center;min-width:160px">💾 Save Plans</button>
          <span id="save-plans-msg" style="font-size:11px;color:var(--text3);font-family:var(--font-mono)"></span>
        </div>
      </div>
      <div class="card">
        <div class="card-title">Payment Instructions</div>
        <textarea id="payment-info" style="min-height:120px;font-family:var(--font-mono);font-size:11px"></textarea>
        <div class="toggle-row">
          <div><div class="toggle-label">Auto-issue Trial</div><div class="toggle-desc">Issue free trial keys instantly</div></div>
          <label class="toggle"><input type="checkbox" id="auto-trial" checked><span class="toggle-slider"></span></label>
        </div>
      </div>
    </div>

    <!-- COLLECTED URLS -->
    <div class="page" id="page-collected-urls">
      <div class="card">
        <div class="card-title">Collected URLs from Nexus</div>
        <div style="display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap;align-items:center">
          <input type="text" id="urls-search" placeholder="Search URLs…" style="flex:1;min-width:200px;margin:0" oninput="filterCollectedUrls()">
          <select id="urls-user-filter" style="background:var(--bg2);border:1px solid var(--border2);border-radius:6px;color:var(--text);padding:8px 10px;font-family:var(--font-mono);font-size:12px;min-width:150px" onchange="filterCollectedUrls()">
            <option value="">All Users</option>
          </select>
          <button class="btn btn-ghost btn-sm" onclick="loadCollectedUrls()">↻ Refresh</button>
          <button class="btn btn-info btn-sm" onclick="copyAllCollectedUrls()">📋 Copy All</button>
          <button class="btn btn-success btn-sm" onclick="exportCollectedUrls()">⬇ Export CSV</button>
          <button class="btn btn-danger btn-sm" onclick="clearCollectedUrls()">🗑 Clear All</button>
        </div>
        <div id="urls-count" style="font-size:10px;color:var(--text3);font-family:var(--font-mono);margin-bottom:10px"></div>
        <div class="table-wrap">
          <table><thead><tr><th>#</th><th>User Name</th><th>URL</th><th>Added At</th><th>Action</th></tr></thead>
          <tbody id="urls-tbody"><tr><td colspan="5" style="text-align:center;color:var(--text3);padding:24px">Loading…</td></tr></tbody></table>
        </div>
      </div>
    </div>

    <!-- SCRAPER -->
    <div class="page" id="page-scraper">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
        <div>
          <div class="card">
            <div class="card-title">📊 Telegram Group Scraper</div>
            <div style="font-size:11px;color:var(--text2);margin-bottom:14px;line-height:1.5">
              Scrape members from Telegram groups (public & private). Bot must be admin in the group.
            </div>
            <label>Group ID or @Username</label>
            <input type="text" id="scrape-group" placeholder="e.g. -1001234567890 or @groupname">
            <div style="display:flex;gap:8px;margin-bottom:12px">
              <label class="toggle" style="margin:0;flex:1"><input type="checkbox" id="scrape-no-bots"><span class="toggle-slider"></span><span style="margin-left:8px;font-size:11px">Exclude bots</span></label>
            </div>
            <button class="btn btn-primary" onclick="startScraping()" style="width:100%;justify-content:center;margin-bottom:8px">🔍 Start Scraping</button>
            <div id="scrape-status" style="font-size:11px;color:var(--text2);font-family:var(--font-mono);padding:10px;background:var(--bg2);border-radius:6px;display:none;margin-bottom:12px">
              <span id="scrape-status-text">Ready to scrape...</span>
            </div>
            <div id="scrape-progress" style="display:none">
              <div style="font-size:10px;color:var(--text3);margin-bottom:4px" id="scrape-progress-text">0%</div>
              <div class="progress-bar"><div class="progress-bar-fill" id="scrape-progress-bar" style="width:0%"></div></div>
            </div>
          </div>
          <div class="card">
            <div class="card-title">📈 Statistics</div>
            <div id="scrape-stats" style="font-size:11px;font-family:var(--font-mono);color:var(--text2);display:none">
              <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
                <div style="background:var(--bg2);padding:8px;border-radius:5px;border-left:3px solid var(--blue)"><span style="color:var(--text3)">Total:</span> <span id="stat-total" style="color:var(--blue);font-weight:700">0</span></div>
                <div style="background:var(--bg2);padding:8px;border-radius:5px;border-left:3px solid var(--green)"><span style="color:var(--text3)">Users:</span> <span id="stat-users" style="color:var(--green);font-weight:700">0</span></div>
                <div style="background:var(--bg2);padding:8px;border-radius:5px;border-left:3px solid var(--orange)"><span style="color:var(--text3)">Bots:</span> <span id="stat-bots" style="color:var(--orange);font-weight:700">0</span></div>
                <div style="background:var(--bg2);padding:8px;border-radius:5px;border-left:3px solid var(--purple)"><span style="color:var(--text3)">Premium:</span> <span id="stat-premium" style="color:var(--purple);font-weight:700">0</span></div>
              </div>
              <div style="background:var(--bg2);padding:8px;border-radius:5px;border-left:3px solid var(--cyan)"><span style="color:var(--text3)">With Username:</span> <span id="stat-username" style="color:var(--cyan);font-weight:700">0</span></div>
            </div>
            <div id="scrape-no-stats" style="font-size:11px;color:var(--text3);padding:10px;text-align:center">
              No scraping completed yet
            </div>
          </div>
        </div>
        <div>
          <div class="card">
            <div class="card-title">👥 Members Preview</div>
            <div id="members-list" style="max-height:400px;overflow-y:auto;font-size:10px;font-family:var(--font-mono);display:none">
              <table style="width:100%;font-size:10px"><thead><tr style="position:sticky;top:0"><th style="text-align:left">ID</th><th>Name</th><th>@Username</th></tr></thead>
              <tbody id="members-tbody"></tbody></table>
            </div>
            <div id="members-empty" style="text-align:center;color:var(--text3);padding:24px;font-size:11px">
              No members scraped yet
            </div>
          </div>
          <div class="card" style="display:none" id="export-card">
            <div class="card-title">💾 Export Data</div>
            <div style="display:flex;flex-direction:column;gap:8px">
              <button class="btn btn-primary btn-sm" onclick="exportCSV()" style="justify-content:center">📥 Export as CSV</button>
              <button class="btn btn-primary btn-sm" onclick="exportJSON()" style="justify-content:center">📥 Export as JSON</button>
              <button class="btn btn-success btn-sm" onclick="exportText()" style="justify-content:center">📄 Export as Text</button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- SETTINGS -->
    <div class="page" id="page-backups">
      <div class="card" style="margin-bottom:16px">
        <div class="card-title">📨 Telegram Backup Chat</div>
        <div style="font-size:12px;color:var(--text3);margin-bottom:14px;line-height:1.6">
          Set a Telegram chat or channel where backup files will be automatically sent every 6 hours. Use your personal chat ID, a group ID, or a channel like <code style="background:var(--bg2);padding:1px 5px;border-radius:3px">@mychannel</code>.
        </div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <input id="backup-chat-input" type="text" class="input" placeholder="e.g. 825387661 or @mychannel" style="flex:1;min-width:200px">
          <button class="btn btn-primary btn-sm" onclick="saveBackupChat()" style="white-space:nowrap">💾 Save</button>
          <button class="btn btn-ghost btn-sm" onclick="clearBackupChat()" style="white-space:nowrap">✕ Remove</button>
        </div>
        <div id="backup-chat-status" style="font-size:11px;margin-top:8px;color:var(--text3)"></div>
        <div style="font-size:11px;color:var(--text3);margin-top:10px">
          💡 To find your chat ID, forward a message to <a href="https://t.me/userinfobot" target="_blank" style="color:var(--blue)">@userinfobot</a> on Telegram.
        </div>
      </div>
      <div class="card">
        <div class="card-title">🗄 Database Backups</div>
        <div style="font-size:12px;color:var(--text3);font-family:var(--font-mono);margin-bottom:14px">
          📂 Storage path: <code style="background:var(--bg2);padding:2px 6px;border-radius:4px">/app/backups/</code>
          &nbsp;⚠️ Files are lost on redeploy — configure a Telegram backup chat above.
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px">
          <button class="btn btn-primary btn-sm" onclick="triggerManualBackup()" style="flex:1;justify-content:center">💾 Download Backup Now</button>
          <button class="btn btn-ghost btn-sm" onclick="document.getElementById('restore-file2').click()" style="flex:1;justify-content:center">📂 Restore from File</button>
          <button class="btn btn-ghost btn-sm" onclick="loadBackupPage()" style="justify-content:center">↻ Refresh</button>
        </div>
        <input type="file" id="restore-file2" accept=".db,.sqlite,.sqlite3" style="display:none" onchange="restoreBackup(this)">
        <div id="backup-status2" style="font-size:10px;color:var(--text3);font-family:var(--font-mono);margin-bottom:10px"></div>
        <div class="card-title" style="margin-bottom:10px">📋 Stored Backups</div>
        <div id="backup-page-list">
          <div style="text-align:center;padding:20px;color:var(--text3)">Loading…</div>
        </div>
      </div>
    </div>

    <div class="page" id="page-settings">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
        <div>
          <div class="card">
            <div class="card-title">Telegram</div>
            <label>Bot Token</label><input type="password" id="set-token" placeholder="Token from BotFather">
            <label>Admin Telegram ID</label><input type="text" id="set-admin-id">
            <label>Bot Username</label><input type="text" id="set-username">
          </div>
          <div class="card">
            <div class="card-title">License Defaults</div>
            <div class="form-row">
              <div><label>Default Days</label><input type="number" id="set-days" value="30"></div>
              <div><label>Max Keys/Machine</label><input type="number" id="set-max-keys" value="1" disabled title="Fixed: 1 key per machine ID"></div>
            </div>
            <div style="font-size:10px;color:var(--text3);font-family:var(--font-mono);margin-top:-4px;padding:8px 10px;background:var(--bg2);border-radius:6px;border:1px solid var(--border)">
              🔒 <b style="color:var(--blue)">Permission model:</b> Users can get keys for unlimited machines, but each machine ID can only hold 1 active key at a time.
            </div>
          </div>
          <div class="card">
            <div class="card-title">Security</div>
            <label>Secret Key</label>
            <input type="password" id="set-secret" placeholder="Nexus@Secret#2025!ChangeThisNow!">
            <div style="font-size:10px;color:var(--yellow);font-family:var(--font-mono);margin-top:-4px;margin-bottom:8px">
              ⚠️ Must match the Nexus app exactly. Changing invalidates all keys.
            </div>
          </div>
          <div class="card" style="border-color:rgba(249,200,70,.3)">
            <div class="card-title">🏦 Bakong KHQR Payment</div>
            <label>API Token</label>
            <input type="password" id="set-bakong-token" placeholder="From api-bakong.nbc.gov.kh">
            <label>Account ID</label>
            <input type="text" id="set-bakong-account" placeholder="thoem_sen@bkrt">
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
              <div>
                <label>Merchant Name</label>
                <input type="text" id="set-bakong-name" placeholder="Nexus">
              </div>
              <div>
                <label>Merchant City</label>
                <input type="text" id="set-bakong-city" placeholder="Phnom Penh">
              </div>
            </div>
            <label>Currency</label>
            <select id="set-bakong-currency" style="background:var(--bg2);border:1px solid var(--border2);border-radius:6px;color:var(--text);padding:8px 10px;font-family:var(--font-mono);font-size:12px;width:100%">
              <option value="USD">USD — US Dollar (default)</option>
              <option value="KHR">KHR — Cambodian Riel</option>
            </select>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px">
              <div>
                <label>Payment Timeout (minutes)</label>
                <input type="number" id="set-bakong-timeout" value="15" min="1" max="60"
                  style="font-family:var(--font-mono)">
              </div>
              <div>
                <label>Poll Interval (seconds)</label>
                <input type="number" id="set-bakong-poll" value="8" min="1" max="60"
                  style="font-family:var(--font-mono)">
              </div>
            </div>
            <div class="toggle-row" style="margin-top:10px">
              <div><div class="toggle-label">Use RBK Relay Token</div><div class="toggle-desc">Enable only if server is outside Cambodia</div></div>
              <label class="toggle"><input type="checkbox" id="set-bakong-rbk"><span class="toggle-slider"></span></label>
            </div>
            <div style="margin-top:12px;display:flex;gap:8px;align-items:center">
              <button class="btn btn-primary" id="save-bakong-btn" onclick="saveBakongConfig()" style="justify-content:center;min-width:160px">💾 Save Bakong Config</button>
              <span id="save-bakong-msg" style="font-size:11px;color:var(--text3);font-family:var(--font-mono)"></span>
            </div>
            <div id="bakong-enabled-badge" style="display:none;margin-top:8px;background:rgba(61,214,140,.1);border:1px solid rgba(61,214,140,.3);border-radius:6px;padding:6px 10px;font-size:11px;color:var(--green);font-family:var(--font-mono)">
              ✅ Bakong KHQR is active — payments will use auto-QR
            </div>
            <div id="bakong-disabled-badge" style="margin-top:8px;background:rgba(247,80,106,.08);border:1px solid rgba(247,80,106,.2);border-radius:6px;padding:6px 10px;font-size:11px;color:var(--red);font-family:var(--font-mono)">
              ⚠️ No token set — will use manual payment fallback
            </div>
          </div>
          <div class="card" style="border-color:rgba(120,200,255,.3)">
            <div class="card-title">💳 Payment Verification Mode</div>
            <div style="font-size:11px;color:var(--text2);margin-bottom:14px;line-height:1.6">
              Choose how paid orders are verified and processed:
            </div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px">
              <div style="padding:10px;border:2px solid var(--border2);border-radius:8px;cursor:pointer;transition:all 0.2s" onclick="selectPaymentMode('bakong')" id="payment-mode-bakong-card">
                <div style="font-weight:600;font-size:13px;margin-bottom:4px">🔄 Auto (Bakong)</div>
                <div style="font-size:10px;color:var(--text3)">QR code, auto-verify</div>
              </div>
              <div style="padding:10px;border:2px solid var(--border2);border-radius:8px;cursor:pointer;transition:all 0.2s" onclick="selectPaymentMode('manual')" id="payment-mode-manual-card">
                <div style="font-weight:600;font-size:13px;margin-bottom:4px">✋ Manual</div>
                <div style="font-size:10px;color:var(--text3)">No QR, admin approves</div>
              </div>
            </div>
            <input type="hidden" id="set-payment-mode" value="bakong">
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
              <button class="btn btn-ghost" id="payment-mode-info-btn" onclick="showPaymentModeHelp()" style="justify-content:center">ℹ️ How it works</button>
              <button class="btn btn-primary" id="save-payment-mode-btn" onclick="savePaymentMode()" style="justify-content:center">💾 Save Mode</button>
            </div>
            <div id="save-payment-mode-msg" style="margin-top:10px;font-size:10px;color:var(--text3);font-family:var(--font-mono)"></div>
            <div id="payment-mode-status" style="margin-top:8px;padding:8px 10px;background:var(--bg2);border-radius:6px;border:1px solid var(--border);font-size:10px;color:var(--text2);font-family:var(--font-mono)">
              Loading…
            </div>
          </div>
        </div>
        <div>
          <div class="card">
            <div class="card-title">Admin Panel</div>
            <label>Admin Password</label>
            <input type="password" id="set-admin-pwd" placeholder="From ADMIN_PASSWORD env var">
            <div style="font-size:10px;color:var(--text3);font-family:var(--font-mono);margin-top:-6px;margin-bottom:10px">
              Change ADMIN_PASSWORD in Railway → Variables to update
            </div>
          </div>
          <div class="card">
            <div class="card-title">Bot Info</div>
            <div id="bot-info-box" style="font-size:11px;font-family:var(--font-mono);color:var(--text2)">Loading…</div>
            <button class="btn btn-primary btn-sm" onclick="pingBot()" style="margin-top:10px">🔗 Ping Bot</button>
          </div>
          <div class="card" style="border-color:rgba(79,142,247,.3)">
            <div class="card-title">💾 Save Configuration</div>
            <div style="font-size:11px;color:var(--text2);margin-bottom:12px;font-family:var(--font-mono);line-height:1.6">
              Since Railway uses environment variables, saving generates a summary of values to copy into <b>Railway → Variables</b>.
            </div>
            <button class="btn btn-primary" onclick="saveSettings()" style="width:100%;justify-content:center">💾 Save Settings</button>
            <div id="save-result" style="margin-top:10px;display:none">
              <div style="background:var(--bg3);border:1px solid var(--border2);border-radius:7px;padding:10px 12px">
                <div style="font-size:10px;color:var(--text3);font-family:var(--font-mono);margin-bottom:8px">COPY THESE INTO RAILWAY → VARIABLES</div>
                <div id="save-vars" style="font-family:var(--font-mono);font-size:11px;line-height:1.8;color:var(--text)"></div>
              </div>
              <button class="btn btn-ghost btn-sm" onclick="copyVars()" style="margin-top:6px;width:100%;justify-content:center">📋 Copy All</button>
            </div>
          </div>
        </div>
      </div>
    </div>

  </div>
</div>
</div>

<!-- Modals -->
<div class="modal-overlay" id="plan-modal">
  <div class="modal">
    <div class="modal-title" id="plan-modal-title">Add Plan</div>
    <div class="form-row">
      <div><label>ID</label><input type="text" id="m-id" placeholder="monthly"></div>
      <div><label>Emoji</label><input type="text" id="m-emoji" placeholder="📅" style="width:70px"></div>
    </div>
    <div class="form-row">
      <div><label>Name</label><input type="text" id="m-name" placeholder="Monthly"></div>
      <div><label>Days</label><input type="number" id="m-days" placeholder="30"></div>
    </div>
    <div class="form-row">
      <div><label>Price (USD)</label><input type="number" id="m-price" placeholder="4.99" step="0.01"></div>
      <div><label>Badge</label><input type="text" id="m-badge" placeholder="$4.99"></div>
    </div>
    <label>Description</label><input type="text" id="m-desc" placeholder="Full access for 30 days">
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closePlanModal()">Cancel</button>
      <button class="btn btn-primary" onclick="savePlan()">Save</button>
    </div>
  </div>
</div>

<div class="toast-container" id="toasts"></div>

<script>
// ── State ──────────────────────────────────────────────────────────────────
const S = {
  users:[], orders:[], licenses:[], plans:[], log:[],
  kbRows:[], editPlanIdx:-1,
  token: '', adminId: ''
};

// ── Nav ────────────────────────────────────────────────────────────────────
function showPage(id) {
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('page-'+id).classList.add('active');
  document.querySelectorAll('.nav-btn').forEach(b=>{
    if(b.getAttribute('onclick')===`showPage('${id}')`) b.classList.add('active');
  });
  const titles={dashboard:'Dashboard',users:'Users',orders:'Orders',licenses:'Licenses',
    messaging:'Send Message',broadcast:'Broadcast',keyboard:'Keyboard Builder',
    commands:'Bot Commands',plans:'Renew License',backups:'Backups',settings:'Settings','collected-urls':'Collected URLs'};
  document.getElementById('page-title').textContent=titles[id]||id;
}

// ── Toast ──────────────────────────────────────────────────────────────────
function toast(msg,type='info',ms=4000){
  const icons={success:'✅',error:'❌',info:'ℹ️'};
  const el=document.createElement('div');
  el.className=`toast ${type}`;
  el.innerHTML=`<span>${icons[type]}</span><span>${msg}</span>`;
  document.getElementById('toasts').appendChild(el);
  setTimeout(()=>el.remove(),ms);
  addLog(type==='error'?'❌':type==='success'?'✅':'ℹ️', msg);
}

function addLog(icon,msg){
  const t=new Date().toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
  S.log.unshift({t,icon,msg});
  if(S.log.length>100) S.log.pop();
  renderDashLog();
}

function renderDashLog(){
  document.getElementById('dash-log').innerHTML=S.log.slice(0,8).map(l=>
    `<div class="log-item"><span class="log-time">${l.t}</span><span>${l.icon}</span><span class="log-msg">${l.msg}</span></div>`
  ).join('')||'<div class="log-item"><span class="log-msg" style="color:var(--text3)">No activity yet.</span></div>';
}

// ── API calls ──────────────────────────────────────────────────────────────
const ADMIN_TOKEN = document.cookie.split(';').map(c=>c.trim())
  .find(c=>c.startsWith('admin_token='))?.split('=')[1] || '';

async function api(path, method='GET', body=null){
  const opts={method, credentials:'include', headers:{'X-Admin-Token': ADMIN_TOKEN}};
  if(body){opts.headers['Content-Type']='application/json'; opts.body=JSON.stringify(body);}
  try{
    const r=await fetch('/admin/api'+path, opts);
    if(r.status===401){window.location='/admin/login';return {ok:false,error:'Unauthorized'};}
    const text=await r.text();
    try{return JSON.parse(text);}catch(e){console.error('Bad JSON:',text);return {ok:false,error:'Bad response'};}
  } catch(e){ console.error('Fetch error:',e); return {ok:false,error:e.message}; }
}

async function tgCall(method, payload={}){
  if(!S.token){toast('No bot token set','error');return null;}
  try{
    const r=await fetch(`https://api.telegram.org/bot${S.token}/${method}`,{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)
    });
    return await r.json();
  } catch(e){toast('Telegram API error: '+e.message,'error');return null;}
}

// ── Load data ──────────────────────────────────────────────────────────────
async function loadAll(){
  // First test auth
  const ping=await api('/ping');
  if(!ping.ok){
    toast('Auth failed — try logging out and back in','error');
    return;
  }
  await Promise.all([loadStats(), loadUsers(), loadOrders(), loadLicenses(), loadSettings()]);
  pingBot();
}

async function loadStats(){
  const r=await api('/stats');
  if(!r.ok) return;
  const d=r.data;
  document.getElementById('s-users').textContent=d.total_users||0;
  document.getElementById('s-active').textContent=d.active_licenses||0;
  document.getElementById('s-orders').textContent=d.total_orders||0;
  document.getElementById('s-pending').textContent=d.pending_orders||0;
  document.getElementById('s-revenue').textContent='$'+(d.revenue||0).toFixed(2);
  document.getElementById('s-blocked').textContent=d.blocked_users||0;
  const badge=document.getElementById('pending-badge');
  if(d.pending_orders>0){badge.textContent=d.pending_orders;badge.style.display='';
    const prev=parseInt(badge.dataset.prev||'0');
    if(d.pending_orders > prev) addNotification('Pending Orders','There are '+d.pending_orders+' order(s) awaiting approval.','🛒');
    badge.dataset.prev=d.pending_orders;
  }
  else badge.style.display='none';
}

async function loadUsers(){
  const r=await api('/users');
  if(!r.ok){toast('Failed to load users','error');return;}
  S.users=r.data; renderUsers();
}

function filterUsers(){renderUsers();}
function renderUsers(){
  const search=document.getElementById('user-search').value.toLowerCase();
  const filter=document.getElementById('user-filter').value;
  let list=S.users.filter(u=>{
    const m=!search||String(u.telegram_id).includes(search)||
      (u.username||'').toLowerCase().includes(search)||
      (u.first_name||'').toLowerCase().includes(search);
    const f=filter==='all'||(filter==='blocked'&&u.blocked);
    return m&&f;
  });
  document.getElementById('users-tbody').innerHTML=list.map(u=>`
    <tr>
      <td style="color:var(--text3);font-family:var(--font-mono)">${u.telegram_id}</td>
      <td style="font-weight:700">${u.first_name||'—'}</td>
      <td style="color:var(--text2)">${u.username?'@'+u.username:'—'}</td>
      <td><span class="badge ${u.key_count>0?'badge-green':'badge-gray'}">${u.key_count||0} active</span></td>
      <td><span class="badge badge-blue">${u.total_keys||0} total</span></td>
      <td style="color:var(--text3)">${(u.last_seen||'').slice(0,10)}</td>
      <td><span class="badge ${u.blocked?'badge-red':'badge-green'}">${u.blocked?'Blocked':'Active'}</span></td>
      <td><div style="display:flex;gap:4px">
        <button class="btn btn-ghost btn-sm" onclick="msgUser(${u.telegram_id})">📨</button>
        <button class="btn btn-sm ${u.blocked?'btn-success':'btn-danger'}" onclick="toggleBlock(${u.telegram_id},${!u.blocked})">
          ${u.blocked?'Unblock':'Block'}
        </button>
      </div></td>
    </tr>`).join('')||'<tr><td colspan="8" style="text-align:center;color:var(--text3);padding:20px">No users</td></tr>';
  document.getElementById('users-count').textContent=`${list.length} of ${S.users.length} users`;
}

async function toggleBlock(uid, block){
  const r=await api('/users/block','POST',{telegram_id:uid, blocked:block});
  if(r.ok){toast(`User ${uid} ${block?'blocked':'unblocked'}`, block?'error':'success'); loadUsers(); loadStats();}
  else toast(r.error||'Failed','error');
}

function msgUser(uid){
  document.getElementById('msg-chatid').value=uid;
  showPage('messaging');
}

async function loadOrders(){
  const r=await api('/orders');
  if(!r.ok){toast('Failed to load orders','error');return;}
  S.orders=r.data; filterOrders();
}

function filterOrders(){
  const f=document.getElementById('order-filter').value;
  const list=f==='all'?S.orders:S.orders.filter(o=>o.status===f);
  const emojis={trial:'🆓',monthly:'📅',quarterly:'💎',yearly:'🏆'};
  document.getElementById('orders-list').innerHTML=list.map(o=>`
    <div class="order-item">
      <div class="order-id">#${o.id}</div>
      <div class="order-info">
        <div class="order-name">${o.first_name||o.telegram_id} ${emojis[o.plan_id]||'📦'} ${o.plan_name}</div>
        <div class="order-meta">💳 $${(o.price||0).toFixed(2)} · 📆 ${o.days}d · 🖥 ${o.machine_id} · ${(o.created_at||'').slice(0,16)}</div>
      </div>
      <span class="badge ${o.status==='pending'?'badge-yellow':o.status==='khqr_pending'?'badge-blue':o.status==='approved'?'badge-green':'badge-red'}">${o.status==='khqr_pending'?'🏦 khqr':o.status}</span>
      <div style="display:flex;gap:4px">
        ${o.status==='pending'?`
          <button class="btn btn-success btn-sm" onclick="approveOrder(${o.id})">✅</button>
          <button class="btn btn-danger btn-sm" onclick="rejectOrder(${o.id})">❌</button>
        `:`<span style="font-size:10px;color:var(--text3);font-family:var(--font-mono)">${(o.approved_at||'').slice(0,10)}</span>`}
      </div>
    </div>`).join('')||'<div style="text-align:center;color:var(--text3);padding:24px;font-family:var(--font-mono);font-size:11px">No orders</div>';
}

async function approveOrder(id){
  const r=await api('/orders/approve','POST',{order_id:id});
  if(r.ok){
    if(r.tg_sent){
      toast(`✅ Order #${id} approved — key sent to user!`,'success');
    } else if(r.tg_error){
      toast(`✅ Order #${id} approved — key saved but Telegram failed: ${r.tg_error}`,'info');
    } else {
      toast(`✅ Order #${id} approved`,'success');
    }
    // Show key in a toast so admin can see it too
    if(r.key){
      addLog('🔑', `Order #${id} key: ${r.key}`);
    }
    // Add notification to notification bar
    if(r.notification){
      addNotification(r.notification.title, r.notification.msg, r.notification.icon);
    }
    loadOrders(); loadStats();
  } else toast(r.error||'Failed','error');
}

async function rejectOrder(id){
  const r=await api('/orders/reject','POST',{order_id:id});
  if(r.ok){
    toast(`Order #${id} rejected — user notified`,'error');
    // Add notification to notification bar
    if(r.notification){
      addNotification(r.notification.title, r.notification.msg, r.notification.icon);
    }
    loadOrders(); loadStats();
  } else toast(r.error||'Failed','error');
}

async function approveAllPending(){
  const pending=S.orders.filter(o=>o.status==='pending');
  for(const o of pending) await approveOrder(o.id);
}

async function loadLicenses(){
  const r=await api('/licenses');
  if(!r.ok) return;
  S.licenses=r.data; filterLicenses();
}

function filterLicenses(){
  const search=document.getElementById('lic-search').value.toLowerCase();
  const f=document.getElementById('lic-filter').value;
  const today=new Date();
  let list=S.licenses.filter(l=>{
    const m=!search||(l.machine_id||'').toLowerCase().includes(search)||
      (l.username||'').toLowerCase().includes(search)||
      String(l.telegram_id).includes(search)||
      (l.license_key||'').toLowerCase().includes(search);
    const exp=new Date(l.expires_at);
    const dl=Math.floor((exp-today)/86400000);
    const status=l.revoked?'revoked':dl<0?'expired':'active';
    const fs=f==='all'||status===f;
    l._status=status; l._dl=dl;
    return m&&fs;
  });
  document.getElementById('lic-tbody').innerHTML=list.map(l=>`
    <tr>
      <td style="color:var(--text3);font-family:var(--font-mono)">#${l.id}</td>
      <td>${l.username?'@'+l.username:l.telegram_id}</td>
      <td style="color:var(--cyan);font-size:10px;font-family:var(--font-mono)">${l.machine_id}</td>
      <td style="cursor:pointer;max-width:200px" title="Click to copy" onclick="copyLicKey('${l.license_key||''}')">
        <span style="font-family:var(--font-mono);font-size:10px;color:var(--green);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:block">
          ${l.license_key?l.license_key.slice(0,26)+'…':'—'}
        </span>
      </td>
      <td style="color:var(--text2);font-size:10px">${l.note||'—'}</td>
      <td style="color:var(--text3)">${(l.expires_at||'').slice(0,10)}</td>
      <td style="color:${l._dl<7&&l._dl>=0?'var(--orange)':l._dl<0?'var(--red)':'var(--green)'};font-weight:700">${l._dl>=0?l._dl+'d':'—'}</td>
      <td style="color:var(--text3);white-space:nowrap">${l.last_checked_at?`<span title="${l.last_checked_at}">${fmtAgo(l.last_checked_at)}</span>`:'<span style="color:var(--text3)">Never</span>'}</td>
      <td>
        <div style="display:flex;align-items:center;gap:5px;flex-wrap:nowrap">
          <select style="background:var(--bg2);border:1px solid var(--border2);border-radius:5px;color:var(--text);font-family:var(--font-mono);font-size:10px;padding:3px 6px;cursor:pointer;min-width:90px"
            onchange="licAction(${l.id},this.value,this)">
            <option value="">— Edit —</option>
            ${l._status==='active'?'<option value="revoke">🚫 Revoke</option>':''}
            ${l._status!=='active'?'<option value="activate">✅ Reactivate</option>':''}
            <option value="extend7">+7 days</option>
            <option value="extend30">+30 days</option>
            <option value="extend90">+90 days</option>
            <option value="copy">📋 Copy Key</option>
          </select>
          <span class="badge ${l._status==='active'?'badge-green':l._status==='expired'?'badge-red':'badge-gray'}">${l._status}</span>
        </div>
      </td>
    </tr>`).join('')||'<tr><td colspan="10" style="text-align:center;color:var(--text3);padding:20px">No licenses</td></tr>';
}

function fmtAgo(ts){
  if(!ts) return 'Never';
  // SQLite stores timestamps without Z — treat as UTC
  const normalized = ts.includes('Z') || ts.includes('+') ? ts : ts.replace(' ','T')+'Z';
  const diff=Date.now()-new Date(normalized).getTime();
  if(isNaN(diff)) return ts.slice(0,16);
  const m=Math.floor(diff/60000);
  if(m<1) return 'just now';
  if(m<60) return m+'m ago';
  const h=Math.floor(m/60);
  if(h<24) return h+'h ago';
  const d=Math.floor(h/24);
  if(d<7) return d+'d ago';
  return new Date(normalized).toLocaleDateString();
}

function fmtCambodiaTime(ts){
  if(!ts) return '-';
  const normalized = ts.includes('Z') || ts.includes('+') ? ts : ts.replace(' ','T')+'Z';
  const d = new Date(normalized);
  if(isNaN(d.getTime())) return ts.slice(0,16);
  return d.toLocaleString('en-GB',{timeZone:'Asia/Phnom_Penh',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).replace(',','');
}

function copyLicKey(key){
  if(!key) return;
  navigator.clipboard.writeText(key).then(()=>toast('License key copied!','success'));
}

async function licAction(id, action, selectEl){
  if(!action){return;}
  selectEl.value=''; // reset dropdown

  if(action==='copy'){
    const lic=S.licenses.find(l=>l.id===id);
    if(lic?.license_key) copyLicKey(lic.license_key);
    return;
  }

  let apiAction=action, days=null;
  if(action.startsWith('extend')){
    days=parseInt(action.replace('extend',''));
    apiAction='extend';
  }

  const payload={license_id:id, action:apiAction};
  if(days) payload.days=days;

  const r=await api('/licenses/action','POST',payload);
  if(r.ok){
    toast(r.message||'Done','success');
    loadLicenses(); loadStats();
  } else {
    toast(r.error||'Failed','error');
  }
}

async function runDedup(){
  if(!confirm('Remove all duplicate keys?\n\nFor each machine with multiple active keys, only the newest will be kept. Older ones will be revoked.\n\nThis cannot be undone.')) return;
  const r=await api('/licenses/dedup','POST',{});
  if(r.ok){
    toast(r.message,'success');
    loadLicenses(); loadStats();
  } else {
    toast(r.error||'Failed','error');
  }
}

// ── Messaging ──────────────────────────────────────────────────────────────
function ins(id,o,c){
  const ta=document.getElementById(id);
  const s=ta.selectionStart,e=ta.selectionEnd;
  ta.value=ta.value.substring(0,s)+o+ta.value.substring(s,e)+c+ta.value.substring(e);
  ta.focus(); ta.selectionStart=ta.selectionEnd=s+o.length+(e-s)+c.length;
  updatePreview();
}
function updatePreview(){
  const t=document.getElementById('msg-text').value;
  document.getElementById('msg-preview').innerHTML=t?t.replace(/\n/g,'<br>'):
    '<span style="color:var(--text3);font-family:var(--font-mono)">Preview…</span>';
}
async function sendMessage(){
  const cid=document.getElementById('msg-chatid').value.trim();
  const txt=document.getElementById('msg-text').value.trim();
  const pm=document.getElementById('msg-parse').value;
  if(!cid){toast('Enter Chat ID','error');return;}
  if(!txt){toast('Message empty','error');return;}
  document.getElementById('send-btn').innerHTML='<span class="spinner"></span> Sending…';
  document.getElementById('send-btn').disabled=true;
  const payload={chat_id:cid,text:txt};
  if(pm) payload.parse_mode=pm;
  const r=await tgCall('sendMessage',payload);
  document.getElementById('send-btn').innerHTML='📨 Send';
  document.getElementById('send-btn').disabled=false;
  if(r?.ok){toast('Message sent!','success'); document.getElementById('msg-text').value=''; updatePreview();}
  else toast(r?.description||'Failed','error');
}
async function issueKey(){
  const uid=document.getElementById('issue-uid').value.trim();
  const mid=document.getElementById('issue-mid').value.trim().toUpperCase();
  const planId=document.getElementById('issue-plan').value;
  if(!uid||!mid){toast('Fill in user ID and machine ID','error');return;}
  if(!/^[A-F0-9]{16}$/.test(mid)){toast('Machine ID must be 16 hex chars','error');return;}
  const plan=S.plans.find(p=>p.id===planId);
  const r=await api('/issue-key','POST',{telegram_id:parseInt(uid),machine_id:mid,plan_id:planId});
  if(r.ok){
    toast('Key issued!','success');
    if(S.token&&plan) await tgCall('sendMessage',{chat_id:uid,
      text:`✅ <b>License Key!</b>\n\n${plan.emoji} ${plan.name} (${plan.days}d)\n\n🔑 <code>${r.key}</code>\n\n<i>Paste in Nexus App → Activate License</i>`,
      parse_mode:'HTML'});
    loadLicenses(); loadStats();
  } else toast(r.error||'Failed','error');
}

// ── Broadcast ──────────────────────────────────────────────────────────────
function updateBcPreview(){
  const t=document.getElementById('bc-text').value.replace('{name}','<b>John</b>').replace(/\n/g,'<br>');
  document.getElementById('bc-preview').innerHTML=t||'<span style="color:var(--text3);font-family:var(--font-mono)">Preview…</span>';
}
async function startBroadcast(){
  const txt=document.getElementById('bc-text').value.trim();
  const pm=document.getElementById('bc-parse').value;
  const skip=document.getElementById('bc-skip').checked;
  const delay=parseInt(document.getElementById('bc-delay').value)*1000;
  if(!txt){toast('Message empty','error');return;}
  let users=S.users;
  if(skip) users=users.filter(u=>!u.blocked);
  if(!users.length){toast('No users','error');return;}
  if(!confirm(`Send to ${users.length} users?`)) return;
  const btn=document.getElementById('bc-btn');
  btn.disabled=true;
  document.getElementById('bc-total').textContent=users.length;
  let sent=0,failed=0;
  for(let i=0;i<users.length;i++){
    const u=users[i];
    const msg=txt.replace('{name}',u.first_name||'there');
    const payload={chat_id:u.telegram_id,text:msg};
    if(pm) payload.parse_mode=pm;
    const r=await tgCall('sendMessage',payload);
    if(r?.ok) sent++; else failed++;
    const pct=Math.round(((i+1)/users.length)*100);
    document.getElementById('bc-prog').style.width=pct+'%';
    document.getElementById('bc-sent').textContent=sent;
    document.getElementById('bc-failed').textContent=failed;
    document.getElementById('bc-status').textContent=`${i+1}/${users.length} — ${pct}%`;
    if(delay>0&&i<users.length-1) await new Promise(r=>setTimeout(r,delay));
  }
  toast(`Done — ✅ ${sent} sent, ❌ ${failed} failed`, sent>0?'success':'error');
  btn.disabled=false;
}

// ── Keyboard ───────────────────────────────────────────────────────────────
function addKbRow(btns=['']){S.kbRows.push([...btns]);renderKb();}
function clearKb(){S.kbRows=[];renderKb();}
function renderKb(){
  document.getElementById('kb-rows').innerHTML=S.kbRows.map((row,ri)=>`
    <div style="display:flex;gap:5px;margin-bottom:5px;align-items:center;flex-wrap:wrap">
      ${row.map((b,bi)=>`<input type="text" value="${b}" placeholder="Label"
        style="flex:1;min-width:80px;margin:0;height:30px;padding:0 8px;font-size:12px"
        oninput="S.kbRows[${ri}][${bi}]=this.value;renderKbPreview()">`).join('')}
      <button class="btn btn-ghost btn-sm" onclick="S.kbRows[${ri}].push('');renderKb()">＋</button>
      <button style="background:transparent;border:none;cursor:pointer;color:var(--red);font-size:16px" onclick="S.kbRows.splice(${ri},1);renderKb()">✕</button>
    </div>`).join('')||'<div style="font-size:11px;color:var(--text3);font-family:var(--font-mono)">No rows.</div>';
  renderKbPreview();
}
function renderKbPreview(){
  document.getElementById('kb-preview').innerHTML=S.kbRows.length?
    S.kbRows.map(row=>`<div style="display:flex;gap:5px">${
      row.map(b=>`<div style="flex:1;background:#2b5278;color:#fff;border-radius:7px;padding:7px;text-align:center;font-size:12px">${b||'…'}</div>`).join('')
    }</div>`).join(''):
    '<div style="font-size:11px;color:var(--text3);font-family:var(--font-mono)">Preview…</div>';
}
function loadPreset(name){
  const p={tikdl:[['🔑 Get License','🔄 Renew License'],['📋 My Licenses','👤 My Account'],['❓ Help']],
           minimal:[['🔑 Get License','❓ Help']]};
  S.kbRows=(p[name]||[]).map(r=>[...r]);renderKb();toast(`Loaded ${name} preset`,'info');
}
async function setKeyboard(){
  const rows=S.kbRows.map(r=>r.filter(b=>b.trim()).map(b=>({text:b}))).filter(r=>r.length);
  if(!rows.length){toast('Add buttons first','error');return;}
  const aid=S.adminId||document.getElementById('set-admin-id').value.trim();
  if(!aid){toast('Set Admin ID in Settings','error');return;}
  const r=await tgCall('sendMessage',{chat_id:aid,text:'✅ Keyboard updated.',
    reply_markup:{keyboard:rows,resize_keyboard:document.getElementById('kb-resize').checked,
      one_time_keyboard:document.getElementById('kb-onetime').checked}});
  if(r?.ok) toast('Keyboard set!','success'); else toast(r?.description||'Failed','error');
}
async function removeKeyboard(){
  const aid=S.adminId||document.getElementById('set-admin-id').value.trim();
  if(!aid){toast('Set Admin ID in Settings','error');return;}
  const r=await tgCall('sendMessage',{chat_id:aid,text:'⚙️ Keyboard removed.',reply_markup:{remove_keyboard:true}});
  if(r?.ok) toast('Keyboard removed','success'); else toast(r?.description||'Failed','error');
}

// ── Commands ───────────────────────────────────────────────────────────────
const cmdPresets={
  tikdl:'start  Welcome to Nexus Bot!\nkey    Get your license key\nrenew  Renew your license\naccount  View your account\nclear  Clear chat history\nhelp   Show help',
  minimal:'start  Start the bot\nhelp   Show help'
};
function loadCmdPreset(n){document.getElementById('cmds-text').value=cmdPresets[n]||'';}
async function setCommands(){
  const raw=document.getElementById('cmds-text').value.trim();
  const commands=raw.split('\n').filter(Boolean).map(l=>{
    const [cmd,...rest]=l.trim().split(/\s+/);
    return{command:cmd.replace(/^\//,''),description:rest.join(' ')||cmd};
  }).filter(c=>c.command);
  if(!commands.length){toast('No commands found','error');return;}
  const r=await tgCall('setMyCommands',{commands});
  if(r?.ok) toast(`${commands.length} commands set!`,'success'); else toast(r?.description||'Failed','error');
}
async function deleteCommands(){
  if(!confirm('Delete all commands?')) return;
  const r=await tgCall('deleteMyCommands',{});
  if(r?.ok) toast('Commands deleted','success'); else toast(r?.description||'Failed','error');
}
async function fetchCommands(){
  const r=await tgCall('getMyCommands');
  if(r?.ok) document.getElementById('current-cmds').innerHTML=
    r.result.map(c=>`<div style="padding:4px 0;border-bottom:1px solid var(--border)">/<b>${c.command}</b> — ${c.description}</div>`).join('')||'No commands.';
  else toast(r?.description||'Failed','error');
}

async function savePlansConfig(){
  const btn = document.getElementById('save-plans-btn');
  const msg = document.getElementById('save-plans-msg');
  btn.disabled = true;
  btn.textContent = 'Saving…';
  msg.textContent = '';
  const r = await api('/plans/save','POST',{
    plans:        S.plans,
    payment_info: document.getElementById('payment-info').value,
    auto_trial:   document.getElementById('auto-trial').checked,
  });
  btn.disabled = false;
  btn.textContent = '💾 Save Plans';
  if(r.ok){
    msg.style.color = 'var(--green)';
    msg.textContent = '✓ Saved! Bot will use updated plans immediately.';
    toast('Plans saved!','success');
    setTimeout(()=>msg.textContent='', 4000);
  } else {
    msg.style.color = 'var(--red)';
    msg.textContent = '✗ ' + (r.error||'Save failed');
    toast(r.error||'Save failed','error');
  }
}

// ── Plans ──────────────────────────────────────────────────────────────────
function renderPlans(){
  document.getElementById('plans-grid').innerHTML=S.plans.map((p,i)=>`
    <div style="background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:16px">
      <div style="font-size:22px;margin-bottom:6px">${p.emoji}</div>
      <div style="font-size:14px;font-weight:800;margin-bottom:3px">${p.name}</div>
      <div style="font-size:18px;font-weight:800;font-family:var(--font-mono);color:var(--blue);margin-bottom:3px">${p.price===0?'FREE':'$'+p.price.toFixed(2)}</div>
      <div style="font-size:11px;color:var(--text2);margin-bottom:3px;font-family:var(--font-mono)">${p.days} days</div>
      <div style="font-size:11px;color:var(--text3);margin-bottom:12px">${p.description}</div>
      <div style="display:flex;gap:5px">
        <button class="btn btn-ghost btn-sm" onclick="editPlan(${i})">✏️</button>
        <button class="btn btn-danger btn-sm" onclick="deletePlan(${i})">🗑</button>
      </div>
    </div>`).join('');
  // Update all plan dropdowns
  ['issue-plan','gen-plan'].forEach(id=>{
    const sel=document.getElementById(id);
    if(sel) sel.innerHTML=S.plans.map(p=>`<option value="${p.id}">${p.emoji} ${p.name} (${p.days}d)</option>`).join('');
  });
}
function openAddPlan(){
  S.editPlanIdx=-1;
  ['m-id','m-emoji','m-name','m-days','m-price','m-badge','m-desc'].forEach(id=>document.getElementById(id).value='');
  document.getElementById('m-emoji').value='📦';
  document.getElementById('plan-modal-title').textContent='Add Plan';
  document.getElementById('plan-modal').classList.add('open');
}
function editPlan(i){
  const p=S.plans[i]; S.editPlanIdx=i;
  document.getElementById('m-id').value=p.id;
  document.getElementById('m-emoji').value=p.emoji;
  document.getElementById('m-name').value=p.name;
  document.getElementById('m-days').value=p.days;
  document.getElementById('m-price').value=p.price;
  document.getElementById('m-badge').value=p.badge;
  document.getElementById('m-desc').value=p.description;
  document.getElementById('plan-modal-title').textContent='Edit Plan';
  document.getElementById('plan-modal').classList.add('open');
}
function closePlanModal(){document.getElementById('plan-modal').classList.remove('open');}
function savePlan(){
  const p={id:document.getElementById('m-id').value.trim(),emoji:document.getElementById('m-emoji').value.trim(),
    name:document.getElementById('m-name').value.trim(),days:parseInt(document.getElementById('m-days').value),
    price:parseFloat(document.getElementById('m-price').value)||0,badge:document.getElementById('m-badge').value.trim(),
    description:document.getElementById('m-desc').value.trim(),currency:'USD'};
  if(!p.id||!p.name||!p.days){toast('Fill required fields','error');return;}
  if(S.editPlanIdx>=0) S.plans[S.editPlanIdx]=p; else S.plans.push(p);
  closePlanModal(); renderPlans();
  toast(S.editPlanIdx>=0?'Plan updated':'Plan added','success');
}
function deletePlan(i){
  if(!confirm(`Delete "${S.plans[i].name}"?`)) return;
  S.plans.splice(i,1); renderPlans(); toast('Plan deleted','error');
}

async function saveBakongConfig(){
  const btn = document.getElementById('save-bakong-btn');
  const msg = document.getElementById('save-bakong-msg');
  btn.disabled = true; btn.textContent = 'Saving…'; msg.textContent = '';
  const r = await api('/bakong/save','POST',{
    token:         document.getElementById('set-bakong-token').value.trim(),
    account_id:    document.getElementById('set-bakong-account').value.trim(),
    merchant_name: document.getElementById('set-bakong-name').value.trim(),
    merchant_city: document.getElementById('set-bakong-city').value.trim(),
    currency:      document.getElementById('set-bakong-currency').value,
    use_rbk:       document.getElementById('set-bakong-rbk').checked,
    timeout_mins:  parseInt(document.getElementById('set-bakong-timeout').value)||15,
    poll_secs:     parseInt(document.getElementById('set-bakong-poll').value)||8,
  });
  btn.disabled = false; btn.textContent = '💾 Save Bakong Config';
  if(r.ok){
    msg.style.color = 'var(--green)'; msg.textContent = '✓ Saved!';
    toast(r.message || 'Bakong config saved!','success');
    _updateBakongBadge(r.enabled);
    setTimeout(()=>msg.textContent='',4000);
  } else {
    msg.style.color = 'var(--red)'; msg.textContent = '✗ '+(r.error||'Failed');
    toast(r.error||'Save failed','error');
  }
}

function _updateBakongBadge(enabled){
  document.getElementById('bakong-enabled-badge').style.display  = enabled ? 'block' : 'none';
  document.getElementById('bakong-disabled-badge').style.display = enabled ? 'none'  : 'block';
}

// ── Settings ───────────────────────────────────────────────────────────────
async function loadSettings(){
  const r=await api('/settings');
  if(!r.ok) return;
  const d=r.data;
  S.token=d.token||''; S.adminId=d.admin_id||'';
  document.getElementById('set-token').value=d.token||'';
  document.getElementById('set-admin-id').value=d.admin_id||'';
  document.getElementById('set-username').value=d.bot_username||'';
  document.getElementById('set-days').value=d.default_days||30;
  document.getElementById('set-max-keys').value=1;
  S.plans=d.plans||[]; renderPlans();
  document.getElementById('payment-info').value=d.payment_info||'';
  document.getElementById('auto-trial').checked=d.auto_trial!==false;
  document.getElementById('bot-info-box').innerHTML=
    `Token: <span style="color:var(--blue)">${d.token?d.token.slice(0,12)+'…':'not set'}</span><br>`+
    `Admin ID: <span style="color:var(--blue)">${d.admin_id||'not set'}</span>`;
  // Populate Bakong fields
  const bk = d.bakong||{};
  document.getElementById('set-bakong-token').value   = bk.token||'';
  document.getElementById('set-bakong-account').value = bk.account_id||'thoem_sen@bkrt';
  document.getElementById('set-bakong-name').value    = bk.merchant_name||'Nexus Downloader';
  document.getElementById('set-bakong-city').value    = bk.merchant_city||'Phnom Penh';
  document.getElementById('set-bakong-currency').value= bk.currency||'USD';
  document.getElementById('set-bakong-rbk').checked      = bk.use_rbk||false;
  document.getElementById('set-bakong-timeout').value    = bk.timeout_mins||15;
  document.getElementById('set-bakong-poll').value       = bk.poll_secs||8;
  _updateBakongBadge(bk.enabled||false);
  // Load payment verification mode
  loadPaymentMode();
}

async function saveSettings(){
  const payload={
    token:      document.getElementById('set-token').value.trim(),
    admin_id:   document.getElementById('set-admin-id').value.trim(),
    bot_username:document.getElementById('set-username').value.trim(),
    default_days:parseInt(document.getElementById('set-days').value)||30,
    max_keys:   1,
    secret_key: document.getElementById('set-secret')?.value.trim()||'',
  };
  const r=await api('/settings','POST',payload);
  if(r.ok){
    // Update live state
    if(payload.token) S.token=payload.token;
    if(payload.admin_id) S.adminId=payload.admin_id;
    // Show Railway vars to copy
    const vars=r.vars||{};
    const lines=Object.entries(vars).filter(([k,v])=>v)
      .map(([k,v])=>`<div><b style="color:var(--blue)">${k}</b> = <span style="color:var(--green)">${k.includes('TOKEN')||k.includes('KEY')?v.slice(0,8)+'…':v}</span></div>`).join('');
    document.getElementById('save-vars').innerHTML=lines||'No changes.';
    document.getElementById('save-result').style.display='block';
    toast('Settings saved! Copy values to Railway Variables.','success');
    window._saveVarsRaw=Object.entries(vars).filter(([k,v])=>v).map(([k,v])=>`${k}=${v}`).join('\n');
  } else {
    toast(r.error||'Save failed','error');
  }
}

function copyVars(){
  if(window._saveVarsRaw) navigator.clipboard.writeText(window._saveVarsRaw)
    .then(()=>toast('Copied!','success'));
}

// ── Payment Verification Mode ───────────────────────────────────────────────────

async function loadPaymentMode(){
  try {
    const r=await api('/payment-mode');
    if(r.ok && r.data){
      const mode=r.data.mode||'bakong';
      document.getElementById('set-payment-mode').value=mode;
      updatePaymentModeUI(mode);
      const status=r.data.status||'';
      document.getElementById('payment-mode-status').textContent=status;
    }
  } catch(e){
    console.error('Error loading payment mode:',e);
  }
}

function selectPaymentMode(mode){
  document.getElementById('set-payment-mode').value=mode;
  updatePaymentModeUI(mode);
}

function updatePaymentModeUI(mode){
  const bakongCard=document.getElementById('payment-mode-bakong-card');
  const manualCard=document.getElementById('payment-mode-manual-card');
  
  if(mode==='bakong'){
    bakongCard.style.borderColor='var(--blue)';
    bakongCard.style.background='rgba(79,142,247,.1)';
    manualCard.style.borderColor='var(--border2)';
    manualCard.style.background='transparent';
  } else {
    manualCard.style.borderColor='var(--orange)';
    manualCard.style.background='rgba(247,164,50,.1)';
    bakongCard.style.borderColor='var(--border2)';
    bakongCard.style.background='transparent';
  }
}

async function savePaymentMode(){
  const mode=document.getElementById('set-payment-mode').value;
  const btn=document.getElementById('save-payment-mode-btn');
  const msg=document.getElementById('save-payment-mode-msg');
  
  btn.disabled=true;
  btn.textContent='Saving…';
  msg.textContent='';
  
  try {
    const r=await api('/payment-mode','POST',{mode:mode});
    btn.disabled=false;
    btn.textContent='💾 Save Mode';
    
    if(r.ok){
      msg.style.color='var(--green)';
      msg.textContent='✓ Payment mode saved! Bot will use new mode immediately.';
      document.getElementById('payment-mode-status').textContent=r.data?.status||'Mode updated';
      toast('Payment mode updated!','success');
    } else {
      msg.style.color='var(--red)';
      msg.textContent='✗ Error: '+r.error;
      toast(r.error||'Save failed','error');
    }
  } catch(e){
    btn.disabled=false;
    btn.textContent='💾 Save Mode';
    msg.style.color='var(--red)';
    msg.textContent='✗ Error: '+e.message;
    toast('Error: '+e.message,'error');
  }
}

function showPaymentModeHelp(){
  const mode=document.getElementById('set-payment-mode').value;
  const msgs={
    bakong:'<b>🔄 Auto Mode (Bakong QR)</b><br>• User gets QR code in Telegram<br>• User scans and pays via Bakong app<br>• Bot auto-verifies payment<br>• License issued instantly<br>• Requires Bakong API token',
    manual:'<b>✋ Manual Mode</b><br>• User gets payment instructions (no QR)<br>• User sends payment proof to admin<br>• Admin manually approves in admin panel<br>• License issued after approval<br>• No Bakong token needed'
  };
  alert(msgs[mode]||msgs.bakong);
}

// ── Manual Key Generator ────────────────────────────────────────────────────
function validateMid(){
  const mid=document.getElementById('gen-mid').value.toUpperCase();
  const status=document.getElementById('mid-status');
  const btn=document.getElementById('gen-btn');
  if(/^[A-F0-9]{16}$/.test(mid)){
    status.textContent='✓ Valid Machine ID';
    status.style.color='var(--green)';
    btn.disabled=false;
  } else if(mid.length>0){
    status.textContent=`✗ Must be 16 hex characters (${mid.length}/16)`;
    status.style.color='var(--red)';
    btn.disabled=true;
  } else {
    status.textContent='';
    btn.disabled=true;
  }
}

async function generateKey(){
  const mid=document.getElementById('gen-mid').value.trim().toUpperCase();
  const planId=document.getElementById('gen-plan').value;
  const uid=document.getElementById('gen-uid').value.trim();
  if(!/^[A-F0-9]{16}$/.test(mid)){toast('Invalid Machine ID','error');return;}
  const plan=S.plans.find(p=>p.id===planId)||S.plans[0];
  const btn=document.getElementById('gen-btn');
  btn.innerHTML='<span class="spinner"></span> Generating…'; btn.disabled=true;
  const payload={machine_id:mid, plan_id:planId};
  if(uid) payload.telegram_id=parseInt(uid);
  const r=await api('/generate-key','POST',payload);
  btn.innerHTML='🔑 Generate Key'; btn.disabled=false;
  if(r.ok){
    document.getElementById('gen-key').textContent=r.key;
    const expires=new Date(Date.now()+plan.days*86400000).toLocaleDateString('en-GB',{day:'2-digit',month:'short',year:'numeric'});
    document.getElementById('gen-meta').textContent=`${plan.emoji} ${plan.name} · ${plan.days} days · Expires ${expires}`;
    document.getElementById('gen-result').style.display='block';
    window._lastGenKey=r.key;
    toast('Key generated!','success');
    if(uid&&S.token){
      await tgCall('sendMessage',{chat_id:uid,
        text:`✅ <b>License Key!</b>\n\n${plan.emoji} <b>${plan.name}</b> (${plan.days} days)\n\n🔑 <code>${r.key}</code>\n\n⏰ Expires: ${expires}\n\n<i>Open Nexus Downloader → Activate License → paste the key.</i>`,
        parse_mode:'HTML'});
      toast(`Key sent to user ${uid}`,'success');
    }
    loadLicenses(); loadStats();
  } else {
    toast(r.error||'Generation failed','error');
  }
}

function copyKey(){
  const key=document.getElementById('gen-key').textContent||window._lastGenKey||'';
  if(key) navigator.clipboard.writeText(key).then(()=>toast('Key copied!','success'));
}

async function pingBot(){
  const dot=document.getElementById('status-dot');
  const txt=document.getElementById('status-text');
  const nm=document.getElementById('status-name');
  dot.className='status-dot'; txt.textContent='Checking…'; nm.textContent='';
  try{
    const r=await api('/bot-status');
    if(r.ok&&r.online){
      dot.className='status-dot online';
      txt.textContent='Online';
      nm.textContent='@'+r.username;
      document.getElementById('bot-info-box').innerHTML=
        `✅ <b style="color:var(--green)">${r.first_name}</b><br>@${r.username}<br>ID: ${r.id}`;
    } else {
      dot.className='status-dot offline';
      txt.textContent='Offline';
      nm.textContent='';
      document.getElementById('bot-info-box').innerHTML=
        `<span style="color:var(--red)">✗ ${r.error||'Bot unreachable'}</span>`;
    }
  } catch(e){
    dot.className='status-dot offline'; txt.textContent='Error'; nm.textContent='';
  }
}

// ── Theme ──────────────────────────────────────────────────────────────────
function toggleTheme(){
  const html=document.documentElement;
  const isLight=html.getAttribute('data-theme')==='light';
  html.setAttribute('data-theme', isLight?'':'light');
  const btn=document.getElementById('theme-btn');
  btn.textContent=isLight?'🌙 Dark':'☀️ Light';
  localStorage.setItem('tikdl_theme', isLight?'dark':'light');
}
function initTheme(){
  const saved=localStorage.getItem('tikdl_theme')||'dark';
  if(saved==='light'){
    document.documentElement.setAttribute('data-theme','light');
    const btn=document.getElementById('theme-btn');
    if(btn) btn.textContent='☀️ Light';
  }
}

// ── Backup / Restore ───────────────────────────────────────────────────────
async function downloadBackup(){
  const status=document.getElementById('backup-status');
  if(status) status.textContent='Preparing backup…';
  try{
    const resp=await fetch('/admin/api/backup/download',{credentials:'include',headers:{'X-Admin-Token':ADMIN_TOKEN}});
    if(!resp.ok){toast('Backup failed','error');return;}
    const blob=await resp.blob();
    const now=new Date().toISOString().slice(0,16).replace('T','_').replace(':','-');
    const url=URL.createObjectURL(blob);
    const a=document.createElement('a');
    a.href=url; a.download=`tikdl_backup_${now}.db`;
    document.body.appendChild(a); a.click();
    document.body.removeChild(a); URL.revokeObjectURL(url);
    toast('Backup downloaded!','success');
    if(status) status.textContent=`Last backup: ${new Date().toLocaleString()}`;
  } catch(e){
    toast('Backup error: '+e.message,'error');
    if(status) status.textContent='Error: '+e.message;
  }
}

async function restoreBackupFromHistory(filename){
  if(!confirm(`Restore database from backup:\n"${filename}"?\n\nThis will OVERWRITE the current database. Make sure you have a current backup first!`)) return;
  const status=document.getElementById('backup-status');
  if(status) status.textContent='Restoring from '+filename+'…';
  try{
    const r=await api('/backup/restore-named','POST',{filename});
    if(r.ok){
      toast('Database restored! Reloading data…','success');
      if(status) status.textContent='Restored: '+filename;
      setTimeout(loadAll, 1000);
    } else {
      toast('Restore failed: '+(r.error||'unknown'),'error');
      if(status) status.textContent='Failed: '+r.error;
    }
  } catch(e){
    toast('Restore error: '+e.message,'error');
  }
}

async function loadBackupList(){
  const el=document.getElementById('backup-list');
  if(!el) return;
  el.innerHTML='<div style="text-align:center;padding:10px;color:var(--text3)">Loading…</div>';
  try{
    const r=await api('/backup/list');
    if(!r.ok||!r.files||!r.files.length){
      el.innerHTML='<div style="padding:10px;color:var(--text3)">No auto-backups found yet.</div>';
      return;
    }
    el.innerHTML=r.files.map(b=>`
      <div style="display:flex;align-items:center;gap:8px;padding:7px 0;border-bottom:1px solid var(--border)">
        <span style="flex:1;color:var(--text2);font-size:10px">${b.name}</span>
        <span style="color:var(--text3);font-size:10px;white-space:nowrap">${(b.size/1024).toFixed(1)} KB</span>
        <button class="btn btn-ghost btn-sm" onclick="downloadNamedBackup('${b.name}')" title="Download this backup" style="padding:3px 8px">💾</button>
        <button class="btn btn-danger btn-sm" onclick="restoreBackupFromHistory('${b.name}')" title="Restore this backup" style="padding:3px 8px">↩</button>
      </div>`).join('')+'<div style="font-size:10px;color:var(--text3);margin-top:6px">'+r.files.length+' backup(s) stored locally</div>';
  } catch(e){
    el.innerHTML='<div style="padding:10px;color:var(--red)">Error: '+e.message+'</div>';
  }
}

async function loadBackupPage(){
  loadBackupChatConfig();
  const el=document.getElementById('backup-page-list');
  if(!el) return;
  el.innerHTML='<div style="text-align:center;padding:20px;color:var(--text3)">Loading…</div>';
  try{
    const r=await api('/backup/list');
    if(!r.ok||!r.files||!r.files.length){
      el.innerHTML='<div style="text-align:center;padding:24px;color:var(--text3)">No backups stored yet.<br><span style="font-size:11px">Backups are created automatically every 6 hours.</span></div>';
      return;
    }
    el.innerHTML=`
      <table style="width:100%;border-collapse:collapse;font-size:12px">
        <thead><tr style="color:var(--text3);font-size:11px;text-transform:uppercase;letter-spacing:.05em">
          <th style="text-align:left;padding:6px 8px;border-bottom:1px solid var(--border)">File</th>
          <th style="text-align:right;padding:6px 8px;border-bottom:1px solid var(--border)">Size</th>
          <th style="text-align:right;padding:6px 8px;border-bottom:1px solid var(--border)">Created (KH)</th>
          <th style="text-align:right;padding:6px 8px;border-bottom:1px solid var(--border)">Actions</th>
        </tr></thead>
        <tbody>`+r.files.map(b=>`
          <tr style="border-bottom:1px solid var(--border)">
            <td style="padding:10px 8px;font-family:var(--font-mono);color:var(--text2)">${b.name}</td>
            <td style="padding:10px 8px;text-align:right;color:var(--text3);white-space:nowrap">${(b.size/1024).toFixed(1)} KB</td>
            <td style="padding:10px 8px;text-align:right;color:var(--text3);white-space:nowrap;font-family:var(--font-mono)">${fmtCambodiaTime(new Date(b.mtime*1000).toISOString())}</td>
            <td style="padding:10px 8px;text-align:right;white-space:nowrap">
              <button class="btn btn-ghost btn-sm" onclick="downloadNamedBackup('${b.name}')" title="Download">💾 Download</button>
              <button class="btn btn-danger btn-sm" onclick="restoreBackupFromHistory('${b.name}')" title="Restore" style="margin-left:4px">↩ Restore</button>
            </td>
          </tr>`).join('')+`
        </tbody>
      </table>
      <div style="font-size:11px;color:var(--text3);margin-top:10px;text-align:right">${r.files.length} backup(s) on disk</div>`;
  } catch(e){
    el.innerHTML='<div style="padding:20px;color:var(--red)">Error: '+e.message+'</div>';
  }
}

async function triggerManualBackup(){
  downloadBackup();
}

async function loadBackupChatConfig(){
  try{
    const r = await api('/backup/chat');
    const input = document.getElementById('backup-chat-input');
    const status = document.getElementById('backup-chat-status');
    if(r.ok && r.chat_id){
      input.value = r.chat_id;
      status.innerHTML = '✅ Currently sending backups to: <b>'+r.chat_id+'</b>';
      status.style.color = 'var(--green)';
    } else {
      input.value = '';
      status.textContent = '⚠️ No backup chat configured — backups are only stored locally.';
      status.style.color = 'var(--orange)';
    }
  } catch(e){}
}

async function saveBackupChat(){
  const val = document.getElementById('backup-chat-input').value.trim();
  const status = document.getElementById('backup-chat-status');
  if(!val){ toast('Enter a chat ID or @username','error'); return; }
  const r = await api('/backup/chat','POST',{chat_id: val});
  if(r.ok){
    status.innerHTML = '✅ Saved! Backups will be sent to: <b>'+val+'</b>';
    status.style.color = 'var(--green)';
    toast('Backup chat saved!','success');
  } else {
    toast(r.error||'Failed to save','error');
  }
}

async function clearBackupChat(){
  const status = document.getElementById('backup-chat-status');
  const r = await api('/backup/chat','POST',{chat_id: 'off'});
  if(r.ok){
    document.getElementById('backup-chat-input').value = '';
    status.textContent = '⚠️ Backup chat removed — backups are only stored locally.';
    status.style.color = 'var(--orange)';
    toast('Backup chat removed','success');
  } else {
    toast(r.error||'Failed','error');
  }
}

async function downloadNamedBackup(filename){
  try{
    const resp=await fetch('/admin/api/backup/download?file='+encodeURIComponent(filename),{credentials:'include',headers:{'X-Admin-Token':ADMIN_TOKEN}});
    if(!resp.ok) throw new Error('HTTP '+resp.status);
    const blob=await resp.blob();
    const url=URL.createObjectURL(blob);
    const a=document.createElement('a');
    a.href=url; a.download=filename;
    document.body.appendChild(a); a.click();
    document.body.removeChild(a); URL.revokeObjectURL(url);
    toast('Backup downloaded!','success');
  } catch(e){
    toast('Download error: '+e.message,'error');
  }
}

async function restoreBackup(input){
  const file=input.files[0];
  if(!file) return;
  if(!confirm(`Restore database from "${file.name}"?\n\nThis will OVERWRITE the current database. This cannot be undone.\n\nMake sure to download a backup first!`)){
    input.value=''; return;
  }
  const status=document.getElementById('backup-status');
  if(status) status.textContent='Restoring…';
  const form=new FormData();
  form.append('file',file);
  try{
    const resp=await fetch('/admin/api/backup/restore',{
      method:'POST', credentials:'include',
      headers:{'X-Admin-Token':ADMIN_TOKEN},
      body:form
    });
    const r=await resp.json();
    if(r.ok){
      toast('Database restored! Reloading data…','success');
      if(status) status.textContent='Restored: '+file.name;
      setTimeout(loadAll, 1000);
    } else {
      toast('Restore failed: '+(r.error||'unknown'),'error');
      if(status) status.textContent='Failed: '+r.error;
    }
  } catch(e){
    toast('Restore error: '+e.message,'error');
  }
  input.value='';
}

// ── Notifications ──────────────────────────────────────────────────────────
let _notifications = JSON.parse(localStorage.getItem('admin_notifications') || '[]');

function saveNotifications(){
  try{ localStorage.setItem('admin_notifications', JSON.stringify(_notifications.slice(0,50))); }catch(e){}
}

function renderNotifications(){
  const list = document.getElementById('notif-list');
  const badge = document.getElementById('notif-badge');
  const unread = _notifications.filter(n=>!n.read).length;
  if(unread > 0){ badge.textContent = unread > 9 ? '9+' : unread; badge.style.display=''; }
  else badge.style.display='none';
  if(!_notifications.length){
    list.innerHTML='<div style="text-align:center;color:var(--text3);padding:24px;font-size:13px">No notifications</div>';
    return;
  }
  list.innerHTML = _notifications.map((n,i)=>`
    <div style="padding:10px 16px;border-bottom:1px solid var(--border);background:${n.read?'':'var(--bg2, rgba(99,102,241,0.06))'};cursor:pointer" onclick="markRead(${i})">
      <div style="font-size:13px;font-weight:${n.read?'400':'600'};color:var(--text1)">${n.icon||'🔔'} ${n.title}</div>
      <div style="font-size:12px;color:var(--text3);margin-top:2px">${n.msg}</div>
      <div style="font-size:11px;color:var(--text3);margin-top:3px">${fmtCambodiaTime(n.time)}</div>
    </div>`).join('');
}

function markRead(i){
  if(_notifications[i]) _notifications[i].read = true;
  saveNotifications(); renderNotifications();
}

function addNotification(title, msg, icon='🔔'){
  _notifications.unshift({title, msg, icon, read:false, time: new Date().toISOString()});
  saveNotifications(); renderNotifications();
}

function clearNotifications(){
  _notifications=[];
  saveNotifications(); renderNotifications();
  document.getElementById('notif-panel').style.display='none';
}

function toggleNotifications(){
  const panel = document.getElementById('notif-panel');
  const open = panel.style.display !== 'none';
  panel.style.display = open ? 'none' : 'block';
  if(!open){
    _notifications.forEach(n=>n.read=true);
    saveNotifications(); renderNotifications();
  }
}

document.addEventListener('click', function(e){
  const panel = document.getElementById('notif-panel');
  const btn = document.getElementById('notif-btn');
  if(panel && btn && !panel.contains(e.target) && !btn.contains(e.target)){
    panel.style.display='none';
  }
});

// ── Init ───────────────────────────────────────────────────────────────────

// ── Collected URLs ─────────────────────────────────────────────────────────
let _allCollectedUrls = [];

async function loadCollectedUrls(){
  const r = await api('/collected-urls');
  if(!r.ok){toast('Failed to load URLs','error');return;}
  _allCollectedUrls = r.data || [];
  const badge = document.getElementById('urls-badge');
  if(_allCollectedUrls.length > 0){
    const prev = parseInt(badge.dataset.prev || '0');
    if(_allCollectedUrls.length > prev && prev > 0) addNotification('New URLs Collected', (_allCollectedUrls.length - prev)+' new URL(s) were collected.','🔗');
    badge.dataset.prev = _allCollectedUrls.length;
    badge.textContent=_allCollectedUrls.length;badge.style.display='';
  }
  else badge.style.display='none';
  populateUserFilter();
  filterCollectedUrls();
}

function populateUserFilter(){
  const select = document.getElementById('urls-user-filter');
  const users = [...new Set(_allCollectedUrls.map(u=>u.user_name).filter(Boolean))];
  const currentVal = select.value;
  select.innerHTML = '<option value="">All Users</option>' + users.map(u=>`<option value="${u}">${u}</option>`).join('');
  select.value = currentVal;
}

function filterCollectedUrls(){
  const q = document.getElementById('urls-search').value.toLowerCase();
  const userFilter = document.getElementById('urls-user-filter').value;
  let list = _allCollectedUrls;
  if(userFilter) list = list.filter(u=>u.user_name === userFilter);
  if(q) list = list.filter(u=>u.url.toLowerCase().includes(q) || (u.user_name||'').toLowerCase().includes(q));
  document.getElementById('urls-count').textContent = list.length + ' URL(s) stored';
  document.getElementById('urls-tbody').innerHTML = list.map(u=>`
    <tr>
      <td style="color:var(--text3);font-family:var(--font-mono)">${u.id}</td>
      <td style="color:var(--text3);font-family:var(--font-mono)">${u.user_name || 'unknown'}</td>
      <td style="font-family:var(--font-mono);font-size:11px;word-break:break-all">
        <a href="${u.url}" target="_blank" style="color:var(--blue);text-decoration:none">${u.url}</a>
      </td>
      <td style="color:var(--text3);white-space:nowrap;font-family:var(--font-mono)">${fmtCambodiaTime(u.added_at)}</td>
      <td><button class="btn btn-ghost btn-sm" onclick="navigator.clipboard.writeText('${u.url}').then(()=>toast('Copied!','success'))">📋</button></td>
    </tr>`).join('') || '<tr><td colspan="5" style="text-align:center;color:var(--text3);padding:24px">No URLs stored yet.</td></tr>';
}

async function clearCollectedUrls(){
  if(!confirm('Delete ALL collected URLs? This cannot be undone.')) return;
  const r = await api('/collected-urls/clear','POST',{});
  if(r.ok){_allCollectedUrls=[];filterCollectedUrls();toast('All URLs cleared','success');document.getElementById('urls-badge').style.display='none';}
  else toast(r.error||'Failed','error');
}

function copyAllCollectedUrls(){
  const q = document.getElementById('urls-search').value.toLowerCase();
  const userFilter = document.getElementById('urls-user-filter').value;
  let list = _allCollectedUrls;
  if(userFilter) list = list.filter(u=>u.user_name === userFilter);
  if(q) list = list.filter(u=>u.url.toLowerCase().includes(q) || (u.user_name||'').toLowerCase().includes(q));
  if(!list.length){toast('No URLs to copy','error');return;}
  const urls = list.map(u=>u.url).join('\n');
  navigator.clipboard.writeText(urls).then(()=>toast(`Copied ${list.length} URL(s)!`,'success')).catch(()=>toast('Failed to copy','error'));
}

function exportCollectedUrls(){
  if(!_allCollectedUrls.length){toast('No URLs to export','error');return;}
  const csv = 'id,user_name,url,added_at\n' + _allCollectedUrls.map(u=>`${u.id},"${(u.user_name||'unknown').replace(/"/g, '""')}","${u.url.replace(/"/g, '""')}","${u.added_at||''}"`).join('\n');
  const blob = new Blob([csv],{type:'text/csv'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'collected_urls_'+new Date().toISOString().slice(0,10)+'.csv';
  a.click();
  toast('Exported!','success');
}

function exportCollectedUrls(){
  if(!_allCollectedUrls.length){toast('No URLs to export','error');return;}
  const csv = 'id,user_name,url,added_at\n' + _allCollectedUrls.map(u=>`${u.id},"${(u.user_name||'unknown').replace(/"/g, '""')}","${u.url.replace(/"/g, '""')}","${u.added_at||''}"`).join('\n');
  const blob = new Blob([csv],{type:'text/csv'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'collected_urls_'+new Date().toISOString().slice(0,10)+'.csv';
  a.click();
  toast('Exported!','success');
}

// ── Group Member Scraper Functions ───────────────────────────────────────────

let _scrapedMembers = [];

async function startScraping(){
  const groupInput = document.getElementById('scrape-group').value.trim();
  if(!groupInput){toast('Enter group ID or @username','error');return;}
  
  const noBots = document.getElementById('scrape-no-bots').checked;
  const statusEl = document.getElementById('scrape-status');
  const progressEl = document.getElementById('scrape-progress');
  
  statusEl.style.display = 'block';
  progressEl.style.display = 'block';
  statusEl.innerHTML = '⏳ Initializing scraper...';
  document.getElementById('scrape-progress-bar').style.width = '10%';
  
  try {
    console.log('Starting scrape for:', groupInput);
    const r = await api('/scraper/start','POST',{
      group_id: groupInput,
      exclude_bots: noBots
    });
    
    console.log('Response:', r);
    
    if(!r.ok){
      const errorMsg = r.error || 'Failed to start scraper';
      statusEl.innerHTML = '❌ ' + errorMsg;
      statusEl.style.color = 'var(--red)';
      toast(errorMsg,'error');
      return;
    }
    
    if(!r.data || !r.data.members){
      statusEl.innerHTML = '❌ Invalid response from server';
      toast('Invalid response','error');
      return;
    }
    
    _scrapedMembers = r.data.members || [];
    displayScraperResults(r.data);
    
    statusEl.innerHTML = '✅ Scraping complete! ' + _scrapedMembers.length + ' members found.';
    statusEl.style.color = 'var(--green)';
    document.getElementById('scrape-progress-bar').style.width = '100%';
    toast(`Scraped ${_scrapedMembers.length} members!`,'success');
  } catch(e){
    console.error('Scraper error:', e);
    statusEl.innerHTML = '❌ Error: ' + e.message;
    statusEl.style.color = 'var(--red)';
    toast(e.message,'error');
  }
}

function displayScraperResults(data){
  const members = data.members || [];
  const stats = data.stats || {};
  
  // Show stats
  document.getElementById('scrape-stats').style.display = 'block';
  document.getElementById('scrape-no-stats').style.display = 'none';
  document.getElementById('stat-total').textContent = stats.total || 0;
  document.getElementById('stat-users').textContent = stats.users || 0;
  document.getElementById('stat-bots').textContent = stats.bots || 0;
  document.getElementById('stat-premium').textContent = stats.premium || 0;
  document.getElementById('stat-username').textContent = stats.with_username || 0;
  
  // Show members table
  const tbody = document.getElementById('members-tbody');
  const membersList = document.getElementById('members-list');
  
  tbody.innerHTML = members.slice(0, 100).map(m => `
    <tr>
      <td style="color:var(--blue);font-weight:700">${m.user_id}</td>
      <td>${(m.first_name || '') + ' ' + (m.last_name || '')}</td>
      <td style="color:var(--cyan)">@${m.username || '—'}</td>
    </tr>
  `).join('');
  
  membersList.style.display = members.length > 0 ? 'block' : 'none';
  document.getElementById('members-empty').style.display = members.length === 0 ? 'block' : 'none';
  
  // Show export button
  document.getElementById('export-card').style.display = 'block';
}

function exportCSV(){
  if(!_scrapedMembers.length){toast('No data to export','error');return;}
  let csv = 'user_id,username,first_name,last_name,is_bot,is_premium,language\n';
  csv += _scrapedMembers.map(m => 
    `${m.user_id},"${m.username || ''}","${m.first_name || ''}","${m.last_name || ''}",${m.is_bot},${m.is_premium},"${m.language_code || ''}"`
  ).join('\n');
  
  const blob = new Blob([csv],{type:'text/csv;charset=utf-8'});
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = 'telegram_members_' + new Date().toISOString().slice(0,10) + '.csv';
  link.click();
  toast('Exported '+_scrapedMembers.length+' members as CSV','success');
}

function exportJSON(){
  if(!_scrapedMembers.length){toast('No data to export','error');return;}
  const json = JSON.stringify(_scrapedMembers, null, 2);
  const blob = new Blob([json],{type:'application/json;charset=utf-8'});
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = 'telegram_members_' + new Date().toISOString().slice(0,10) + '.json';
  link.click();
  toast('Exported '+_scrapedMembers.length+' members as JSON','success');
}

function exportText(){
  if(!_scrapedMembers.length){toast('No data to export','error');return;}
  let text = '=== TELEGRAM GROUP MEMBERS ===\n\n';
  text += 'Total: ' + _scrapedMembers.length + '\n\n';
  text += _scrapedMembers.map((m,i) => 
    `${i+1}. ${m.first_name || ''} ${m.last_name || ''}\n   ID: ${m.user_id}\n   @${m.username || 'none'}\n`
  ).join('\n');
  
  const blob = new Blob([text],{type:'text/plain;charset=utf-8'});
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = 'telegram_members_' + new Date().toISOString().slice(0,10) + '.txt';
  link.click();
  toast('Exported '+_scrapedMembers.length+' members as Text','success');
}

window.onload=()=>{
  initTheme();
  renderNotifications();
  loadAll();
  loadBackupList();
  loadCollectedUrls();
  document.getElementById('cmds-text').value=cmdPresets.tikdl;
  loadPreset('tikdl');
  setInterval(loadStats, 30000);
};
</script></body></html>"""

# ── Admin API endpoints ───────────────────────────────────────────────────────

@require_admin_auth
async def handle_admin_ui(request: web.Request) -> web.Response:
    return web.Response(text=ADMIN_HTML, content_type="text/html")

@require_admin_auth
async def api_ping(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "message": "Auth working!"})

@require_admin_auth
async def api_stats(request: web.Request) -> web.Response:
    try:
        today = datetime.date.today().isoformat()
        with db._conn() as c:
            total_users    = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            active_lic     = c.execute("SELECT COUNT(*) FROM licenses WHERE revoked=0 AND expires_at>=?", (today,)).fetchone()[0]
            blocked_users  = c.execute("SELECT COUNT(*) FROM users WHERE blocked=1").fetchone()[0]
            total_orders   = c.execute("SELECT COUNT(*) FROM orders").fetchone()[0] if _table_exists(c, 'orders') else 0
            pending_orders = c.execute("SELECT COUNT(*) FROM orders WHERE status='pending'").fetchone()[0] if _table_exists(c, 'orders') else 0
            revenue        = c.execute("SELECT COALESCE(SUM(price),0) FROM orders WHERE status='approved'").fetchone()[0] if _table_exists(c, 'orders') else 0
        return web.json_response({"ok": True, "data": {
            "total_users": total_users, "active_licenses": active_lic,
            "blocked_users": blocked_users, "total_orders": total_orders,
            "pending_orders": pending_orders, "revenue": float(revenue)
        }})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)})

def _table_exists(c, name):
    r = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return r is not None

@require_admin_auth
async def api_users(request: web.Request) -> web.Response:
    try:
        users = db.get_all_users()
        return web.json_response({"ok": True, "data": users})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)})

@require_admin_auth
async def api_block_user(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        uid  = int(body["telegram_id"])
        blocked = bool(body.get("blocked", True))
        with db._conn() as c:
            c.execute("UPDATE users SET blocked=? WHERE telegram_id=?", (1 if blocked else 0, uid))
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)})

@require_admin_auth
async def api_orders(request: web.Request) -> web.Response:
    try:
        with db._conn() as c:
            if not _table_exists(c, 'orders'):
                return web.json_response({"ok": True, "data": []})
            rows = c.execute("""
                SELECT o.*, u.username, u.first_name FROM orders o
                LEFT JOIN users u ON o.telegram_id=u.telegram_id
                ORDER BY o.created_at DESC LIMIT 200
            """).fetchall()
        return web.json_response({"ok": True, "data": [dict(r) for r in rows]})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)})

@require_admin_auth
async def api_approve_order(request: web.Request) -> web.Response:
    try:
        body     = await request.json()
        order_id = int(body["order_id"])
        order    = db.get_order(order_id)
        if not order:
            return web.json_response({"ok": False, "error": "Order not found"})
        if order["status"] != "pending":
            return web.json_response({"ok": False, "error": f"Order is already {order['status']}"})

        plan = next((p for p in config.load_plans_override() if p["id"] == order["plan_id"]), None)
        days = order["days"]
        mid  = order["machine_id"]
        tid  = order["telegram_id"]
        plan_name  = plan["name"] if plan else order["plan_name"]
        plan_emoji = plan["emoji"] if plan else "📦"

        # Generate the key
        key     = lic.generate_key(mid, days)
        import datetime as _dt
        expires = (_dt.date.today() + _dt.timedelta(days=days)).strftime("%d %b %Y")

        # Save to database
        db.save_license(tid, mid, key, days, f"admin-approved-order#{order_id}")
        db.store_pending_key(mid, key)
        db.approve_order(order_id, "admin-approved")

        # Send key to user via Telegram
        tg_sent = False
        tg_error = ""
        try:
            import urllib.request as _ur
            import json as _json
            msg = (
                f"✅ <b>Your order has been approved!</b>\n\n"
                f"{plan_emoji} <b>Plan:</b> {plan_name} ({days} days)\n\n"
                f"🔑 <code>{key}</code>\n\n"
                f"📋 <b>How to activate:</b>\n"
                f"1. Tap the key above to copy it\n"
                f"2. Open Nexus Downloader → Activate License\n"
                f"3. Paste and click Activate\n\n"
                f"⏰ <b>Expires:</b> {expires}\n"
                f"🆔 Order: <b>#{order_id}</b>\n\n"
                f"<i>Thank you for your purchase! 🙏</i>"
            )
            payload = _json.dumps({
                "chat_id": tid,
                "text": msg,
                "parse_mode": "HTML"
            }).encode("utf-8")
            req = _ur.Request(
                f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
                data=payload,
                headers={"Content-Type": "application/json"}
            )
            with _ur.urlopen(req, timeout=10) as resp:
                result = _json.loads(resp.read())
                tg_sent = result.get("ok", False)
                if not tg_sent:
                    tg_error = result.get("description", "Unknown error")
        except Exception as e:
            tg_error = str(e)

        return web.json_response({
            "ok": True,
            "key": key,
            "expires": expires,
            "tg_sent": tg_sent,
            "tg_error": tg_error,
            "message": f"Key generated and {'sent to user ✅' if tg_sent else f'saved (Telegram failed: {tg_error})'}",
            "notification": {
                "title": f"✅ Order #{order_id} Approved",
                "msg": f"{plan_emoji} {plan_name} for user {tid}",
                "icon": "✅"
            }
        })
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)})

@require_admin_auth
async def api_reject_order(request: web.Request) -> web.Response:
    try:
        body     = await request.json()
        order_id = int(body["order_id"])
        order    = db.get_order(order_id)
        if not order:
            return web.json_response({"ok": False, "error": "Order not found"})
        db.reject_order(order_id, "admin-rejected")

        # Notify user via Telegram
        plan_name = "Plan"
        try:
            import urllib.request as _ur, json as _json
            plan = next((p for p in config.load_plans_override() if p["id"] == order["plan_id"]), None)
            plan_name = plan["name"] if plan else order["plan_name"]
            msg = (
                f"❌ <b>Order #{order_id} was not approved.</b>\n\n"
                f"Your <b>{plan_name}</b> plan request has been rejected.\n"
                f"Please contact the admin for assistance."
            )
            payload = _json.dumps({
                "chat_id": order["telegram_id"],
                "text": msg,
                "parse_mode": "HTML"
            }).encode("utf-8")
            req = _ur.Request(
                f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
                data=payload,
                headers={"Content-Type": "application/json"}
            )
            with _ur.urlopen(req, timeout=10) as resp:
                pass
        except Exception:
            pass

        return web.json_response({
            "ok": True,
            "notification": {
                "title": f"❌ Order #{order_id} Rejected",
                "msg": f"{plan_name} order has been rejected",
                "icon": "❌"
            }
        })
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)})

@require_admin_auth
async def api_licenses(request: web.Request) -> web.Response:
    try:
        rows = db.get_all_licenses_for_user(0) # placeholder
        with db._conn() as c:
            rows = c.execute("""
                SELECT l.*, u.username FROM licenses l
                LEFT JOIN users u ON l.telegram_id=u.telegram_id
                ORDER BY l.issued_at DESC LIMIT 500
            """).fetchall()
        return web.json_response({"ok": True, "data": [dict(r) for r in rows]})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)})

@require_admin_auth
async def api_backup_chat_get(request: web.Request) -> web.Response:
    """Get current backup chat ID."""
    chat_id = db.get_setting("backup_chat_id") or ""
    return web.json_response({"ok": True, "chat_id": chat_id})

@require_admin_auth
async def api_backup_chat_set(request: web.Request) -> web.Response:
    """Set or clear backup chat ID."""
    try:
        body = await request.json()
        chat_id = str(body.get("chat_id", "")).strip()
        if chat_id and chat_id.lower() != "off":
            db.set_setting("backup_chat_id", chat_id)
            return web.json_response({"ok": True, "message": f"Backup chat set to {chat_id}"})
        else:
            db.delete_setting("backup_chat_id")
            return web.json_response({"ok": True, "message": "Backup chat removed"})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)})

@require_admin_auth
async def api_backup_list(request: web.Request) -> web.Response:
    """Admin API: list available backup files."""
    try:
        backup_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")
        if not os.path.isdir(backup_dir):
            return web.json_response({"ok": True, "files": []})
        files = sorted(
            [f for f in os.listdir(backup_dir) if f.endswith(".db")],
            reverse=True
        )
        result = []
        for f in files:
            path = os.path.join(backup_dir, f)
            stat = os.stat(path)
            result.append({"name": f, "size": stat.st_size, "mtime": stat.st_mtime})
        return web.json_response({"ok": True, "files": result})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)})


@require_admin_auth
async def api_backup_download(request: web.Request) -> web.Response:
    """Stream the SQLite database file as a download."""
    import shutil, tempfile
    db_path = db.DB_PATH
    if not os.path.exists(db_path):
        return web.json_response({"ok": False, "error": "Database not found"}, status=404)
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        shutil.copy2(db_path, tmp.name)
        ts = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        fname = f"tikdl_backup_{ts}.db"
        with open(tmp.name, "rb") as f:
            data = f.read()
        os.unlink(tmp.name)
        return web.Response(
            body=data,
            content_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'}
        )
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)

@require_admin_auth
async def api_backup_restore_named(request: web.Request) -> web.Response:
    """Restore database from a named local backup file."""
    try:
        body = await request.json()
        filename = body.get("filename", "").strip()
        if not filename or "/" in filename or "\\" in filename:
            return web.json_response({"ok": False, "error": "Invalid filename"})
        backup_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")
        src = os.path.join(backup_dir, filename)
        if not os.path.exists(src):
            return web.json_response({"ok": False, "error": "Backup file not found"})
        import shutil
        shutil.copy2(src, db.DB_PATH)
        log.info(f"[Restore] Restored DB from local backup: {filename}")
        return web.json_response({"ok": True, "message": f"Restored from {filename}"})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)})

@require_admin_auth
async def api_backup_restore(request: web.Request) -> web.Response:
    """Restore database from uploaded file."""
    try:
        reader = await request.multipart()
        field  = await reader.next()
        if not field or field.name != "file":
            return web.json_response({"ok": False, "error": "No file uploaded"})
        import shutil, tempfile
        # Save upload to temp
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        while True:
            chunk = await field.read_chunk()
            if not chunk:
                break
            tmp.write(chunk)
        tmp.close()
        # Validate it's a SQLite file
        with open(tmp.name, "rb") as f:
            header = f.read(16)
        if not header.startswith(b"SQLite format 3"):
            os.unlink(tmp.name)
            return web.json_response({"ok": False, "error": "Not a valid SQLite database file"})
        # Backup current db before overwriting
        db_path = db.DB_PATH
        if os.path.exists(db_path):
            shutil.copy2(db_path, db_path + ".pre_restore_backup")
        # Replace database
        shutil.copy2(tmp.name, db_path)
        os.unlink(tmp.name)
        log.info("Database restored from upload.")
        return web.json_response({"ok": True, "message": "Database restored successfully"})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)})

@require_admin_auth
async def api_save_plans(request: web.Request) -> web.Response:
    """Save plans + payment info to plans_override.json — persists across restarts."""
    try:
        body         = await request.json()
        plans        = body.get("plans", [])
        payment_info = body.get("payment_info", "")
        auto_trial   = body.get("auto_trial", True)
        if not plans:
            return web.json_response({"ok": False, "error": "Plans list is empty"})
        config.save_plans_override(plans, payment_info, auto_trial)
        log.info(f"Plans saved: {len(plans)} plans")
        return web.json_response({"ok": True, "saved": len(plans)})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)})

@require_admin_auth
async def api_dedup(request: web.Request) -> web.Response:
    """Manually trigger duplicate license cleanup."""
    try:
        count = db.dedup_licenses()
        return web.json_response({
            "ok": True,
            "revoked": count,
            "message": f"Revoked {count} duplicate license(s). Each machine now has at most 1 active key."
        })
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)})

@require_admin_auth
async def api_license_action(request: web.Request) -> web.Response:
    """Revoke, activate, or extend a license."""
    try:
        body       = await request.json()
        license_id = int(body["license_id"])
        action     = body.get("action", "")  # revoke | activate | extend

        if action == "revoke":
            db.revoke_license(license_id, "admin-revoked")
            return web.json_response({"ok": True, "message": f"License #{license_id} revoked"})

        elif action == "activate":
            days = body.get("days")
            db.activate_license(license_id, int(days) if days else None)
            return web.json_response({"ok": True, "message": f"License #{license_id} reactivated"})

        elif action == "extend":
            days = int(body.get("days", 30))
            db.extend_license(license_id, days)
            return web.json_response({"ok": True, "message": f"License #{license_id} extended by {days} days"})

        else:
            return web.json_response({"ok": False, "error": f"Unknown action: {action}"})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)})

@require_admin_auth
async def api_generate_key(request: web.Request) -> web.Response:
    """Generate a key for a machine ID. telegram_id is optional."""
    try:
        body    = await request.json()
        mid     = body.get("machine_id", "").strip().upper()
        plan_id = body.get("plan_id", "monthly")
        uid     = body.get("telegram_id", 0) or 0

        import re
        if not re.match(r'^[A-F0-9]{16}$', mid):
            return web.json_response({"ok": False, "error": "Invalid Machine ID — must be 16 hex characters"})

        # Check if machine already has an active key
        existing = db.get_license_by_machine(mid)
        if existing:
            import datetime as dt
            exp  = dt.date.fromisoformat(existing["expires_at"][:10])
            dl   = (exp - dt.date.today()).days
            return web.json_response({
                "ok": False,
                "error": f"Machine already has an active license (expires in {dl} days). Revoke it first."
            })

        plan = next((p for p in config.load_plans_override() if p["id"] == plan_id), config.load_plans_override()[0])
        days = plan["days"]
        key  = lic.generate_key(mid, days)
        db.save_license(uid, mid, key, days, f"admin-generated-{plan_id}")
        db.store_pending_key(mid, key)
        log.info(f"Admin generated key for machine {mid} plan={plan_id} uid={uid}")
        return web.json_response({"ok": True, "key": key, "plan": plan, "days": days})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)})

@require_admin_auth
async def api_issue_key(request: web.Request) -> web.Response:
    try:
        body    = await request.json()
        uid     = int(body["telegram_id"])
        mid     = body["machine_id"].strip().upper()
        plan_id = body.get("plan_id", "monthly")
        plan    = next((p for p in config.load_plans_override() if p["id"] == plan_id), config.load_plans_override()[0])
        days    = plan["days"]
        key     = lic.generate_key(mid, days)
        db.save_license(uid, mid, key, days, f"admin-issued-{plan_id}")
        db.store_pending_key(mid, key)
        return web.json_response({"ok": True, "key": key})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)})

@require_admin_auth
async def api_bot_status(request: web.Request) -> web.Response:
    """Check bot status by calling Telegram getMe from server — avoids browser CORS."""
    try:
        import urllib.request as _ur, json as _json
        req = _ur.Request(
            f"https://api.telegram.org/bot{config.BOT_TOKEN}/getMe",
            headers={"Content-Type": "application/json"}
        )
        with _ur.urlopen(req, timeout=8) as resp:
            data = _json.loads(resp.read())
        if data.get("ok"):
            bot = data["result"]
            return web.json_response({
                "ok":         True,
                "online":     True,
                "username":   bot.get("username", ""),
                "first_name": bot.get("first_name", ""),
                "id":         bot.get("id", 0),
            })
        return web.json_response({"ok": True, "online": False, "error": data.get("description", "Unknown error")})
    except Exception as e:
        return web.json_response({"ok": True, "online": False, "error": str(e)})

@require_admin_auth
async def api_settings(request: web.Request) -> web.Response:
    bk = config.load_bakong_override()
    payment_mode = config.load_payment_verification_mode()
    return web.json_response({"ok": True, "data": {
        "token":        config.BOT_TOKEN,
        "admin_id":     str(config.ADMIN_ID),
        "bot_username": config.BOT_USERNAME,
        "default_days": config.DEFAULT_DAYS,
        "max_keys":     config.MAX_KEYS_PER_USER,
        "plans":        config.load_plans_override(),
        "payment_info": config.load_payment_info_override(),
        "auto_trial":   config.load_auto_trial_override(),
        "payment_verification_mode": payment_mode,
        "bakong": {
            "token":         bk.get("token", ""),
            "account_id":    bk.get("account_id", "thoem_sen@bkrt"),
            "merchant_name": bk.get("merchant_name", "Nexus Downloader"),
            "merchant_city": bk.get("merchant_city", "Phnom Penh"),
            "currency":      bk.get("currency", "USD"),
            "use_rbk":       bk.get("use_rbk", False),
            "enabled":       bool(bk.get("token", "") or os.environ.get("BAKONG_TOKEN", "")),
            "timeout_mins":  bk.get("timeout_mins", 15),
            "poll_secs":     bk.get("poll_secs", 8),
        },
    }})

@require_admin_auth
async def api_save_bakong(request: web.Request) -> web.Response:
    """Save Bakong config to bakong_override.json — applies immediately."""
    try:
        body = await request.json()
        timeout_mins = max(5, min(60, int(body.get("timeout_mins", 15))))
        poll_secs    = max(5, min(30, int(body.get("poll_secs", 8))))
        data = {
            "token":         body.get("token", "").strip(),
            "account_id":    body.get("account_id", "").strip(),
            "merchant_name": body.get("merchant_name", "").strip(),
            "merchant_city": body.get("merchant_city", "").strip(),
            "currency":      body.get("currency", "USD").upper(),
            "use_rbk":       bool(body.get("use_rbk", False)),
            "timeout_mins":  timeout_mins,
            "poll_secs":     poll_secs,
        }
        config.save_bakong_override(data)
        enabled = bool(data["token"])
        log.info(f"Bakong config saved — enabled={enabled} timeout={timeout_mins}m poll={poll_secs}s")
        return web.json_response({
            "ok": True,
            "enabled": enabled,
            "message": "Bakong config saved! Auto-QR payment is now " + ("active ✅" if enabled else "inactive ⚠️"),
        })
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)})

@require_admin_auth
async def api_save_settings(request: web.Request) -> web.Response:
    """Save settings reminder — Railway uses env vars so we just return guidance."""
    try:
        body = await request.json()
        # In Railway env-var based config, we can't write to config.py directly.
        # Return the values so the user knows what to set in Railway Variables.
        fields = {
            "BOT_TOKEN":         body.get("token", ""),
            "ADMIN_ID":          body.get("admin_id", ""),
            "BOT_USERNAME":      body.get("bot_username", ""),
            "DEFAULT_DAYS":      str(body.get("default_days", 30)),
            "MAX_KEYS_PER_USER": str(body.get("max_keys", 1)),
            "SECRET_KEY":        body.get("secret_key", ""),
        }
        return web.json_response({
            "ok": True,
            "message": "Settings noted. Update these in Railway → Variables to persist.",
            "vars": fields
        })
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)})

@require_admin_auth
async def api_payment_mode(request: web.Request) -> web.Response:
    """Get current payment verification mode."""
    try:
        mode = config.load_payment_verification_mode()
        bk = config.load_bakong_override()
        bakong_enabled = bool(bk.get("token") or os.environ.get("BAKONG_TOKEN"))
        
        status_msg = ""
        if mode == "bakong":
            if bakong_enabled:
                status_msg = "🔄 Auto-verify with Bakong (QR code)"
            else:
                status_msg = "⚠️ Auto mode selected but Bakong not configured — will fallback to manual"
        else:
            status_msg = "✋ Manual approval mode (no QR)"
        
        return web.json_response({
            "ok": True,
            "data": {
                "mode": mode,
                "status": status_msg,
                "bakong_configured": bakong_enabled
            }
        })
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)})

@require_admin_auth
async def api_save_payment_mode(request: web.Request) -> web.Response:
    """Save payment verification mode."""
    try:
        body = await request.json()
        mode = body.get("mode", "bakong").strip().lower()
        
        if mode not in ("bakong", "manual"):
            return web.json_response({"ok": False, "error": "Invalid mode. Must be 'bakong' or 'manual'"})
        
        config.save_payment_verification_mode(mode)
        
        bk = config.load_bakong_override()
        bakong_enabled = bool(bk.get("token") or os.environ.get("BAKONG_TOKEN"))
        
        status_msg = ""
        if mode == "bakong":
            if bakong_enabled:
                status_msg = "🔄 Auto-verify with Bakong QR (active)"
            else:
                status_msg = "⚠️ Bakong mode selected but token not configured"
        else:
            status_msg = "✋ Manual approval mode enabled"
        
        log.info(f"Payment verification mode set to: {mode}")
        
        return web.json_response({
            "ok": True,
            "data": {
                "mode": mode,
                "status": status_msg,
                "bakong_configured": bakong_enabled
            },
            "message": f"Payment mode changed to {mode}"
        })
    except Exception as e:
        log.error(f"Error saving payment mode: {e}")
        return web.json_response({"ok": False, "error": str(e)})

async def handle_logout(request: web.Request) -> web.Response:
    resp = web.HTTPFound("/admin/login")
    resp.del_cookie("admin_token")
    raise resp

# ── App builder ───────────────────────────────────────────────────────────────


@require_admin_auth
async def api_clear_collected_urls(request: web.Request) -> web.Response:
    """Admin API: delete all collected URLs."""
    try:
        with db._conn() as c:
            c.execute("DELETE FROM collected_urls")
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)})

@require_admin_auth
async def api_scraper_start(request: web.Request) -> web.Response:
    """Admin API: /admin/api/scraper/start — scrape members from a Telegram group."""
    try:
        body = await request.json()
        group_id = body.get("group_id", "").strip()
        exclude_bots = body.get("exclude_bots", False)
        
        if not group_id:
            return web.json_response({"ok": False, "error": "group_id required"}, status=400)
        
        if not config.BOT_TOKEN:
            return web.json_response({"ok": False, "error": "Bot token not configured"}, status=500)
        
        from telegram import Bot
        from telegram.ext import Application, ContextTypes
        import telegram_scraper
        
        # Create a minimal app context to use scraper module
        bot = Bot(token=config.BOT_TOKEN)
        
        # Create minimal context
        class MinimalContext:
            def __init__(self, bot):
                self.bot = bot
        
        context = MinimalContext(bot)
        
        try:
            log.info(f"[API] Starting scraper for: {group_id}")
            
            # Call the scraper with timeout
            members, success = await asyncio.wait_for(
                telegram_scraper.get_group_members(
                    context, 
                    group_id, 
                    include_bots=not exclude_bots
                ),
                timeout=30.0  # 30 second timeout
            )
            
            if not members:
                return web.json_response({
                    "ok": True,
                    "data": {
                        "group": group_id,
                        "members": [],
                        "stats": {
                            "total": 0,
                            "bots": 0,
                            "users": 0,
                            "with_username": 0,
                            "premium": 0
                        }
                    }
                })
            
            # Calculate stats
            stats = {
                "total": len(members),
                "bots": sum(1 for m in members if m["is_bot"]),
                "users": sum(1 for m in members if not m["is_bot"]),
                "with_username": sum(1 for m in members if m["username"]),
                "premium": sum(1 for m in members if m["is_premium"]),
            }
            
            log.info(f"[API] Scraper complete: {stats['total']} members")
            
            response = {
                "ok": True,
                "data": {
                    "group": group_id,
                    "members": members,
                    "stats": stats
                }
            }
            return web.json_response(response)
            
        except asyncio.TimeoutError:
            log.warning("[API] Scraper timeout - this may be a very restricted group")
            return web.json_response({
                "ok": False, 
                "error": "Scraping timed out - group may restrict member enumeration or be very large"
            }, status=504)
        except Exception as e:
            log.error(f"[API] Scraper error: {e}")
            return web.json_response({
                "ok": False, 
                "error": f"Scraper error: {str(e)[:100]}"
            }, status=500)
        finally:
            try:
                await bot.session.close()
            except:
                pass
        
    except Exception as e:
        log.error(f"[API] Request error: {e}")
        return web.json_response({
            "ok": False, 
            "error": f"Request error: {str(e)[:100]}"
        }, status=500)

def build_server() -> web.Application:
    app = web.Application()
    
    # Root endpoint
    app.router.add_get("/", handle_root)
    
    # Telegram Mini App endpoints
    app.router.add_post("/api/auth",           api_auth)
    app.router.add_post("/api/user-info",      api_user_info)
    app.router.add_get( "/api/licenses",       api_get_licenses)
    app.router.add_get( "/api/plans",          api_get_plans)
    app.router.add_post("/api/get-license",    api_get_license)
    app.router.add_get( "/api/payment-info",   api_payment_info)
    app.router.add_post("/api/verify-license", api_verify_license)
    app.router.add_post("/api/renew-license",  api_renew_license)
    
    # Legacy endpoints (for desktop app compatibility)
    app.router.add_get("/pending", handle_pending)
    app.router.add_get("/health",  handle_health)
    app.router.add_get("/verify",  handle_verify)
    app.router.add_post("/urls",   handle_collect_urls)
    
    # Admin endpoints
    app.router.add_get("/admin/login",  handle_login_page)
    app.router.add_post("/admin/login", handle_login_page)
    app.router.add_get("/admin/logout", handle_logout)
    app.router.add_get("/admin/",       handle_admin_ui)
    app.router.add_get("/admin",        handle_admin_ui)
    
    # Admin API
    app.router.add_get( "/admin/api/ping",          api_ping)
    app.router.add_get( "/admin/api/stats",         api_stats)
    app.router.add_get( "/admin/api/users",         api_users)
    app.router.add_post("/admin/api/users/block",   api_block_user)
    app.router.add_get( "/admin/api/orders",        api_orders)
    app.router.add_post("/admin/api/orders/approve",api_approve_order)
    app.router.add_post("/admin/api/orders/reject", api_reject_order)
    app.router.add_get( "/admin/api/licenses",      api_licenses)
    app.router.add_post("/admin/api/plans/save",     api_save_plans)
    app.router.add_post("/admin/api/licenses/action",api_license_action)
    app.router.add_get( "/admin/api/backup/list",     api_backup_list)
    app.router.add_get( "/admin/api/backup/chat",     api_backup_chat_get)
    app.router.add_post("/admin/api/backup/chat",     api_backup_chat_set)
    app.router.add_get( "/admin/api/backup/download",api_backup_download)
    app.router.add_post("/admin/api/backup/restore-named", api_backup_restore_named)
    app.router.add_post("/admin/api/backup/restore", api_backup_restore)
    app.router.add_post("/admin/api/licenses/dedup",  api_dedup)
    app.router.add_post("/admin/api/generate-key",  api_generate_key)
    app.router.add_post("/admin/api/issue-key",     api_issue_key)
    app.router.add_get( "/admin/api/bot-status",    api_bot_status)
    app.router.add_get( "/admin/api/settings",      api_settings)
    app.router.add_post("/admin/api/settings",      api_save_settings)
    app.router.add_post("/admin/api/bakong/save",   api_save_bakong)
    app.router.add_get( "/admin/api/payment-mode",  api_payment_mode)
    app.router.add_post("/admin/api/payment-mode",  api_save_payment_mode)
    app.router.add_get( "/admin/api/collected-urls", api_collected_urls)
    app.router.add_get( "/admin/api/collected-urls/copy", api_collected_urls_copy)
    app.router.add_post("/admin/api/collected-urls/clear", api_clear_collected_urls)
    app.router.add_post("/admin/api/scraper/start", api_scraper_start)
    
    return app
