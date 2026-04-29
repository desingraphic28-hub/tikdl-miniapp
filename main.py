"""
main.py — TikDL Telegram Mini App Backend
Converts bot commands into a web-based mini app interface
"""

import os
import sys
import logging

from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
import hmac
import hashlib
import json
from datetime import datetime, timedelta
import asyncio
from threading import Thread

# Import from bot modules
import config
import db
import license as lic
import bakong
import telegram_scraper

logging.basicConfig(
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# Configure Flask
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    'DATABASE_URL',
    'sqlite:///tikdl.db'
)

# Initialize database
db_instance = SQLAlchemy(app)

# ── Telegram Mini App Authentication ──────────────────────────────────────────

def verify_telegram_init_data(init_data: str) -> dict | None:
    """Verify Telegram Mini App init data signature."""
    try:
        data = dict(item.split('=') for item in init_data.split('&'))
        hash_val = data.pop('hash')
        
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
            return json.loads(data.get('user', '{}'))
        return None
    except Exception as e:
        log.error(f"Auth error: {e}")
        return None

# ── API Routes ────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    """Serve mini app HTML."""
    return render_template('index.html', bot_token=config.BOT_TOKEN)

@app.route('/api/auth', methods=['POST'])
def api_auth():
    """Authenticate user from Telegram Mini App."""
    init_data = request.json.get('initData', '')
    user = verify_telegram_init_data(init_data)
    
    if not user:
        return jsonify({'error': 'Invalid signature'}), 401
    
    # Register/update user
    db.upsert_user(
        user.get('id'),
        user.get('username', ''),
        user.get('first_name', ''),
        user.get('last_name', '')
    )
    
    return jsonify({
        'success': True,
        'user': user
    })

@app.route('/api/licenses', methods=['GET'])
def api_get_licenses():
    """Get user's licenses."""
    user_id = request.args.get('user_id', type=int)
    if not user_id:
        return jsonify({'error': 'user_id required'}), 400
    
    licenses = db.get_user_licenses(user_id)
    return jsonify({
        'licenses': [
            {
                'id': lic['id'],
                'status': lic['status'],
                'expired': lic['expired_at'],
                'created': lic['created_at'],
                'machine_id': lic['machine_id']
            }
            for lic in licenses
        ]
    })

@app.route('/api/plans', methods=['GET'])
def api_get_plans():
    """Get available license plans."""
    plans = config.load_plans_override()
    exclude_free = request.args.get('exclude_free', 'false').lower() == 'true'
    
    if exclude_free:
        plans = [p for p in plans if p['price'] > 0]
    
    return jsonify({'plans': plans})

@app.route('/api/get-license', methods=['POST'])
def api_get_license():
    """Purchase/get a new license."""
    data = request.json
    user_id = data.get('user_id')
    plan_id = data.get('plan_id')
    
    if not user_id or not plan_id:
        return jsonify({'error': 'Missing parameters'}), 400
    
    plan = next(
        (p for p in config.load_plans_override() if p['id'] == plan_id),
        None
    )
    
    if not plan:
        return jsonify({'error': 'Invalid plan'}), 400
    
    # Auto-issue trial
    if plan['price'] == 0 and config.load_auto_trial_override():
        user = db.get_user(user_id)
        if user and db.count_active_licenses(user_id) >= config.MAX_KEYS_PER_USER:
            return jsonify({'error': 'Max licenses reached'}), 400
        
        expiry = (datetime.utcnow() + timedelta(days=plan['days'])).isoformat()
        license_key = lic.issue_license(plan['days'], user_id)
        
        db.save_license(
            user_id=user_id,
            license_key=license_key,
            plan_id=plan_id,
            expired_at=expiry,
            status='active'
        )
        
        return jsonify({
            'success': True,
            'license_key': license_key,
            'expired_at': expiry
        })
    
    # Paid plans - prepare payment
    return jsonify({
        'payment_required': True,
        'plan': plan,
        'user_id': user_id
    })

@app.route('/api/payment-info', methods=['GET'])
def api_payment_info():
    """Get payment instructions."""
    verification_mode = config.load_payment_verification_mode()
    payment_info = config.load_payment_info_override()
    
    response = {
        'payment_info': payment_info,
        'verification_mode': verification_mode,
        'admin_id': config.ADMIN_ID
    }
    
    # Add Bakong QR if available
    if verification_mode == 'bakong':
        bakong_cfg = config.load_bakong_override()
        if bakong_cfg:
            response['bakong'] = bakong_cfg
    
    return jsonify(response)

@app.route('/api/verify-license', methods=['POST'])
def api_verify_license():
    """Verify a license key."""
    data = request.json
    machine_id = data.get('machine_id')
    license_key = data.get('license_key')
    
    if not machine_id or not license_key:
        return jsonify({'error': 'Missing parameters'}), 400
    
    result = db.verify_license(machine_id, license_key)
    
    if result['valid']:
        return jsonify({
            'valid': True,
            'expires_at': result.get('expires_at'),
            'days_left': result.get('days_left')
        })
    
    return jsonify({'valid': False, 'message': result.get('message')}), 400

@app.route('/api/renew-license', methods=['POST'])
def api_renew_license():
    """Renew an existing license."""
    data = request.json
    user_id = data.get('user_id')
    license_key = data.get('license_key')
    plan_id = data.get('plan_id')
    
    if not all([user_id, license_key, plan_id]):
        return jsonify({'error': 'Missing parameters'}), 400
    
    plan = next(
        (p for p in config.load_plans_override() if p['id'] == plan_id),
        None
    )
    
    if not plan:
        return jsonify({'error': 'Invalid plan'}), 400
    
    # Update license expiry
    expiry = (datetime.utcnow() + timedelta(days=plan['days'])).isoformat()
    
    db.update_license_expiry(license_key, expiry)
    
    return jsonify({
        'success': True,
        'new_expiry': expiry
    })

@app.route('/api/user-info', methods=['GET'])
def api_user_info():
    """Get user account information."""
    user_id = request.args.get('user_id', type=int)
    if not user_id:
        return jsonify({'error': 'user_id required'}), 400
    
    user = db.get_user(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    licenses = db.get_user_licenses(user_id)
    
    return jsonify({
        'user': {
            'id': user['id'],
            'username': user['username'],
            'first_name': user['first_name'],
            'last_name': user['last_name'],
            'joined_at': user['created_at']
        },
        'license_count': len(licenses),
        'active_licenses': sum(1 for l in licenses if l['status'] == 'active')
    })

@app.route('/api/health', methods=['GET'])
def health():
    """Health check."""
    return jsonify({'status': 'ok'})

# ── Error Handlers ────────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Not found'}), 404

@app.errorhandler(500)
def internal_error(error):
    log.error(f"Internal error: {error}")
    return jsonify({'error': 'Internal server error'}), 500

# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    # Initialize database
    with app.app_context():
        db_instance.create_all()
    
    port = int(os.environ.get('PORT', 5000))
    app.run(
        host='0.0.0.0',
        port=port,
        debug=os.environ.get('FLASK_ENV') == 'development'
    )
