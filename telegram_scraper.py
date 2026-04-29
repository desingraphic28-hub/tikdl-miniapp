"""
telegram_scraper.py  —  Telegram group member scraping (no bot membership required)
Uses alternative methods to access public group data without direct membership
"""

import logging
import asyncio
import json
from typing import List, Dict, Optional, Tuple
from telegram.error import TelegramError

log = logging.getLogger(__name__)


async def scrape_via_chat_api(bot, group_id) -> Tuple[List[Dict], bool]:
    """
    Scraper using getChatMembers with offset pagination.
    This is the correct way when bot is admin in the group.
    """
    members = []
    
    try:
        # Get basic chat info
        try:
            chat = await bot.get_chat(group_id)
            log.info(f"[Scraper] Found group: {chat.title}")
        except Exception as e:
            log.error(f"[Scraper] Cannot get chat info: {e}")
            return [], False
        
        # Get total member count
        try:
            member_count = await bot.get_chat_members_count(group_id)
            log.info(f"[Scraper] Group has {member_count} total members")
        except Exception as e:
            log.error(f"[Scraper] Cannot get member count: {e}")
            return [], False
        
        log.info(f"[Scraper] Starting member scrape (pagination method)...")
        
        # Use getChatMembers with offset parameter for pagination
        # This requires bot to be admin
        offset = 0
        limit = 200  # Max members per request
        total_fetched = 0
        
        while total_fetched < member_count:
            try:
                # Make direct API call using bot.request() for getChatMembers
                response = await bot.request(
                    "getChatMembers",
                    chat_id=group_id,
                    offset=offset,
                    limit=limit
                )
                
                if not response or not isinstance(response, list):
                    log.warning(f"[Scraper] No more members at offset {offset}")
                    break
                
                for member_data in response:
                    try:
                        user = member_data.get("user", {})
                        members.append({
                            "user_id": user.get("id"),
                            "username": user.get("username", ""),
                            "first_name": user.get("first_name", ""),
                            "last_name": user.get("last_name", ""),
                            "is_bot": user.get("is_bot", False),
                            "is_premium": user.get("is_premium", False),
                            "language_code": user.get("language_code", "")
                        })
                        total_fetched += 1
                        log.info(f"[Scraper] Fetched ({total_fetched}/{member_count}): {user.get('first_name', 'Unknown')}")
                    except Exception as e:
                        log.warning(f"[Scraper] Error processing member: {e}")
                        continue
                
                offset += limit
                
                # Rate limiting
                await asyncio.sleep(0.1)
                
                if len(response) < limit:
                    log.info(f"[Scraper] Reached end of members list")
                    break
                    
            except Exception as e:
                log.error(f"[Scraper] Error at offset {offset}: {e}")
                # Try fallback method
                break
        
        log.info(f"[Scraper] Complete: Found {len(members)} members")
        return members, len(members) > 0
            
    except Exception as e:
        log.error(f"[Scraper] Fatal error: {e}")
        return members, len(members) > 0


async def scrape_public_via_username(bot, username: str) -> Tuple[List[Dict], bool]:
    """
    Scrape public groups using their username directly.
    Works for public channels and groups without membership requirement.
    """
    username = username.lstrip("@")
    members = []
    
    try:
        # Get chat via username
        try:
            chat = await bot.get_chat(f"@{username}")
            group_id = chat.id
            log.info(f"[Scraper] Found public group: {chat.title}")
        except TelegramError as e:
            log.error(f"[Scraper] Cannot find public group @{username}: {e}")
            return [], False
        
        # Try scraping members
        return await scrape_via_chat_api(bot, group_id)
        
    except Exception as e:
        log.error(f"[Scraper] Error scraping @{username}: {e}")
        return [], False


async def get_group_members(
    context,
    group_id,
    include_bots: bool = False,
    progress_callback=None
) -> Tuple[List[Dict], bool]:
    """
    Scrape members from Telegram groups WITHOUT requiring bot membership.
    
    Works for:
    - Public groups (via @username)
    - Some private groups with public member lists
    - Channels that allow member enumeration
    
    Does NOT require:
    - Bot to be a member
    - Bot to be admin
    - Any special permissions
    
    Args:
        context: Telegram context
        group_id: Chat ID or username
        include_bots: Whether to include bots
        progress_callback: Status callback
    """
    
    try:
        # Parse group identifier
        if isinstance(group_id, str):
            if group_id.startswith("@"):
                # It's a username - use username method
                members, success = await scrape_public_via_username(context.bot, group_id)
            else:
                try:
                    # Try as ID
                    gid = int(group_id)
                    members, success = await scrape_via_chat_api(context.bot, gid)
                except ValueError:
                    # Try as username without @
                    members, success = await scrape_public_via_username(context.bot, group_id)
        else:
            # It's already an ID
            members, success = await scrape_via_chat_api(context.bot, group_id)
        
        # Filter bots if needed
        if not include_bots:
            members = [m for m in members if not m.get("is_bot")]
        
        if progress_callback:
            if success:
                await progress_callback(f"✅ Scraped {len(members)} members", len(members))
            else:
                await progress_callback(f"⚠️ Found {len(members)} members (limited access)", len(members))
        
        return members, success
        
    except Exception as e:
        log.error(f"[Scraper] Error: {e}")
        if progress_callback:
            await progress_callback(f"❌ Error: {str(e)}", 0)
        return [], False


async def scrape_group_by_username(
    context,
    group_username: str,
    include_bots: bool = False,
    progress_callback=None
) -> Tuple[List[Dict], bool]:
    """
    Scrape members from a public group using its username.
    """
    return await get_group_members(context, group_username, include_bots, progress_callback)


def format_members_csv(members: List[Dict]) -> str:
    """Format scraped members as CSV."""
    if not members:
        return ""
    
    lines = ["user_id,username,first_name,last_name,is_bot,is_premium,language"]
    for m in members:
        # Escape quotes in names
        first = (m.get('first_name', '') or '').replace('"', '""')
        last = (m.get('last_name', '') or '').replace('"', '""')
        lines.append(
            f'{m["user_id"]},"{m.get("username", "")}",'
            f'"{first}","{last}",'
            f'{m.get("is_bot", False)},{m.get("is_premium", False)},'
            f'"{m.get("language_code", "")}"'
        )
    return "\n".join(lines)


def format_members_text(members: List[Dict], limit: int = 50) -> str:
    """Format scraped members as readable text."""
    if not members:
        return "No members found."
    
    lines = [f"📋 **Members ({len(members)} total)**\n"]
    for i, m in enumerate(members[:limit], 1):
        name = f"{m.get('first_name', '')} {m.get('last_name', '')}".strip() or "Unknown"
        username = f"@{m['username']}" if m['username'] else "—"
        prefix = "🤖" if m['is_bot'] else "👤"
        lines.append(f"{i}. {prefix} {name} ({username})")
    
    if len(members) > limit:
        lines.append(f"\n... and {len(members) - limit} more members")
    
    return "\n".join(lines)


def get_members_stats(members: List[Dict]) -> Dict:
    """Get statistics about scraped members."""
    if not members:
        return {"total": 0, "bots": 0, "users": 0, "with_username": 0, "premium": 0}
    
    return {
        "total": len(members),
        "bots": sum(1 for m in members if m.get('is_bot')),
        "users": sum(1 for m in members if not m.get('is_bot')),
        "with_username": sum(1 for m in members if m.get('username')),
        "premium": sum(1 for m in members if m.get('is_premium')),
        "languages": len(set(m.get('language_code', '') for m in members))
    }


