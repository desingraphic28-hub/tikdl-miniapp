# TikDL License Manager — Telegram Mini App

A modern web-based interface for the TikDL license management system. No more bot commands! Just tap and go.

## 🎯 Features

✅ **No Commands** - Full UI instead of `/commands`
✅ **Real-time Updates** - License status and expiry tracking
✅ **One-Click Purchase** - Quick plan selection
✅ **Mobile Optimized** - Perfect for Telegram's mini app
✅ **Dark Mode Support** - Adapts to user's Telegram theme
✅ **Bakong QR Payment** - Built-in payment method
✅ **Admin Control** - Full license management

## 📱 What Changed from Bot Version

| Bot Version | Mini App Version |
|---|---|
| `/start` command | 🏠 Home tab with summary |
| `/get_license` text input | 💎 Plans tab with buttons |
| `/my_licenses` text output | 📋 Interactive license cards |
| `/account` text info | 👤 Formatted account page |
| Reply keyboards | Tab navigation |
| Text responses | Beautiful cards & badges |

## 🚀 Quick Start

### 1. Clone & Setup
```bash
git clone <your-repo>
cd tikdl-miniapp
pip install -r requirements.txt
```

### 2. Environment Variables
```bash
cp .env.example .env
# Edit .env with your values:
# - BOT_TOKEN from @BotFather
# - ADMIN_ID (your Telegram ID)
# - PUBLIC_URL (your deployed app URL)
```

### 3. Local Development
```bash
python main.py
# Visit: http://localhost:5000
```

### 4. Deploy to Railway
```bash
# Create account at railway.app
railway login
railway link
railway up
```

### 5. Register with Telegram BotFather

Use @BotFather to:
1. Create a new bot (if needed)
2. Set menu button: `Web App` with your app URL
3. Set commands:
   - `/start` - Open mini app

## 📋 Configuration

### Payment Methods

**Bakong QR (Recommended for Cambodia)**
```
BAKONG_TOKEN=your_token
BAKONG_ACCOUNT_ID=your_account_id
BAKONG_MERCHANT_NAME=Shop Name
BAKONG_USE_RBK=true
```

**Manual Admin Approval**
```
BAKONG_TOKEN=  # Leave empty
```

### License Plans

Edit plans by modifying `config.py`:
```python
PLANS = [
    {
        "id": "trial",
        "name": "Trial",
        "days": 7,
        "price": 0.00,
        "emoji": "🆓",
    },
    # Add more plans...
]
```

## 🏗️ Project Structure

```
tikdl-miniapp/
├── main.py                 # Flask backend
├── templates/
│   └── index.html         # Mini app frontend (HTML/CSS/JS)
├── config.py              # Configuration
├── db.py                  # Database (from bot)
├── license.py             # License generation (from bot)
├── bakong.py              # Bakong payment (from bot)
├── telegram_scraper.py    # (from bot)
├── requirements.txt       # Python dependencies
├── .env.example          # Configuration template
└── Dockerfile            # Docker deployment
```

## 🔧 API Endpoints

### Authentication
- `POST /api/auth` - Authenticate user with Telegram init data

### Licenses
- `GET /api/licenses?user_id=ID` - Get user licenses
- `POST /api/get-license` - Get/purchase license
- `POST /api/renew-license` - Renew existing license
- `POST /api/verify-license` - Verify license key

### Plans
- `GET /api/plans` - Get available plans
- `GET /api/payment-info` - Get payment instructions

### Account
- `GET /api/user-info?user_id=ID` - Get user info

### System
- `GET /api/health` - Health check

## 📱 Mini App Setup in Telegram

### Option 1: Menu Button (Recommended)
```
@BotFather → /setmenubutton
Choose: Web App
Label: License Manager
URL: https://your-app-url.here
```

### Option 2: Inline Button
Send an inline button to users:
```python
InlineKeyboardButton(
    "🔐 Open License Manager",
    web_app=WebAppInfo(url="https://your-app-url.here")
)
```

### Option 3: Deep Link
```
https://t.me/your_bot_username?start=license_manager
```

## 🔐 Security

- ✅ Telegram signature verification
- ✅ CORS enabled for mini app
- ✅ Secret key for sessions
- ✅ SQLAlchemy ORM protection
- ✅ No plaintext passwords

## 📊 Database

Default: SQLite (`tikdl.db`)

To use PostgreSQL (Railway):
```bash
DATABASE_URL=postgresql://user:pass@host/db
```

## 🐳 Docker Deployment

### Local
```bash
docker build -t tikdl-miniapp .
docker run -p 5000:5000 --env-file .env tikdl-miniapp
```

### Railway
```bash
railway login
railway link
railway up
```

## 🎨 Customization

### Change Colors
Edit `index.html` CSS variables:
```css
:root {
    --tg-theme-button-color: #0088cc;
    --tg-theme-text-color: #000000;
    /* ... */
}
```

### Add New Tab
1. Add button in `<div class="tabs">`
2. Add content `<div id="tab-name" class="tab-content">`
3. Add load function `loadTabName()`

### Modify License Plans
Edit `config.py` `PLANS` list and redeploy.

## 🆘 Troubleshooting

### "Invalid signature"
- Check `BOT_TOKEN` in `.env`
- Ensure mini app URL matches in BotFather

### Database errors
- Delete `tikdl.db` to reset
- Check PostgreSQL credentials if using cloud

### Payment QR not showing
- Ensure `BAKONG_TOKEN` is set
- Verify Bakong credentials

## 📞 Support

For issues:
1. Check logs: `railway logs`
2. Verify `.env` configuration
3. Test `/api/health` endpoint
4. Check Telegram BotFather settings

## 📄 License

Same as original TikDL bot project

## 🔄 Migration from Bot Version

All bot features are preserved:
- License verification
- Payment processing
- Admin controls
- User management
- Database compatibility

Just switch the mini app URL in BotFather!

---

**Ready to go? Deploy now!** 🚀
