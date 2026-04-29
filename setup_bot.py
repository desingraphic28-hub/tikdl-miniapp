"""
setup_bot.py — Register Telegram Bot Mini App
Run this script once to register your mini app with Telegram BotFather
"""

import asyncio
from telegram import BotCommand
from telegram.ext import Application

import config

async def setup_bot():
    """Register bot commands and set up mini app."""
    app = Application.builder().token(config.BOT_TOKEN).build()
    
    # Get bot info
    bot = app.bot
    
    # Mini app URL (set this to your deployed app URL)
    mini_app_url = config.PUBLIC_URL
    
    print(f"🤖 Bot Setup")
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"Mini App URL: {mini_app_url}")
    print(f"\n📱 Telegram BotFather Commands:")
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"\n1. Set menu button:")
    print(f"   /setmenubutton")
    print(f"   Choose 'Web App'")
    print(f"   Label: License Manager")
    print(f"   URL: {mini_app_url}")
    print(f"\n2. Set default admin rights:")
    print(f"   /setdefaultadminrights")
    print(f"\n3. Set default member rights:")
    print(f"   /setdefaultmemberrights")
    print(f"\n✅ Setup complete!")

if __name__ == '__main__':
    asyncio.run(setup_bot())
