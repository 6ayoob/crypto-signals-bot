# set_commands.py
# ضبط أوامر تيليجرام برمجيًا (Telegram Bot API) باستخدام aiogram v3
# - يضبط أوامر المستخدم كـ Default (تظهر لكل المشتركين)
# - يضبط أوامر الأدمن في محادثات محددة (user_id / chat_id)
# مصادر التوكن/الأدمن:
#   1) من config.py (TELEGRAM_BOT_TOKEN, ADMIN_USER_IDS) إن وُجد
#   2) أو من المتغيرات البيئية: TELEGRAM_BOT_TOKEN و ADMIN_USER_IDS=123,456
#   3) أو من خيارات CLI: --token و --admin

import os
import asyncio
import argparse

try:
    from config import TELEGRAM_BOT_TOKEN as CFG_TOKEN, ADMIN_USER_IDS as CFG_ADMINS
except Exception:
    CFG_TOKEN, CFG_ADMINS = None, []

from aiogram import Bot
from aiogram.types import (
    BotCommand, BotCommandScopeDefault, BotCommandScopeChat
)

DEFAULT_COMMANDS = [
    BotCommand(command="start",      description="البداية والقائمة الرئيسية"),
    BotCommand(command="pay",        description="الدفع USDT (TRC20) + طريقة الإرسال"),
    BotCommand(command="submit_tx",  description="تفعيل الاشتراك عبر رقم المرجع (TxID)"),
    BotCommand(command="status",     description="حالة اشتراكك"),
    BotCommand(command="help",       description="المساعدة وقائمة الأوامر"),
]

ADMIN_COMMANDS = [
    BotCommand(command="admin_help",  description="تعليمات الأدمن"),
    BotCommand(command="approve",     description="تفعيل يدوي: /approve <user_id> <2w|4w> [ref]"),
    BotCommand(command="broadcast",   description="بث رسالة لكل المشتركين"),
    BotCommand(command="force_report",description="إرسال التقرير اليومي فورًا"),
]

def parse_admin_ids(s: str | None):
    if not s:
        return []
    out = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            pass
    return out

async def set_default(bot: Bot):
    await bot.set_my_commands(DEFAULT_COMMANDS, scope=BotCommandScopeDefault())
    me = await bot.get_me()
    print(f"[OK] Default commands set for @{me.username} (id={me.id})")

async def clear_default(bot: Bot):
    await bot.delete_my_commands(scope=BotCommandScopeDefault())
    me = await bot.get_me()
    print(f"[OK] Default commands cleared for @{me.username} (id={me.id})")

async def set_admin_for_ids(bot: Bot, admin_ids: list[int]):
    if not admin_ids:
        print("[WARN] No admin ids provided; skipping admin commands.")
        return
    for aid in admin_ids:
        await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=aid))
        print(f"[OK] Admin commands set for chat_id={aid}")

async def clear_admin_for_ids(bot: Bot, admin_ids: list[int]):
    if not admin_ids:
        print("[WARN] No admin ids provided; skipping clear admin commands.")
        return
    for aid in admin_ids:
        await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=aid))
        print(f"[OK] Admin commands cleared for chat_id={aid}")

async def main():
    parser = argparse.ArgumentParser(description="Setup Telegram bot commands (aiogram v3)")
    parser.add_argument("--token", help="Bot token (overrides config/env)")
    parser.add_argument("--admin", help="Comma-separated admin chat/user IDs (overrides config/env)")
    parser.add_argument("--apply-all", action="store_true", help="Set default commands + admin commands (recommended)")
    parser.add_argument("--default-only", action="store_true", help="Set default commands only")
    parser.add_argument("--admin-only", action="store_true", help="Set admin commands only")
    parser.add_argument("--clear-default", action="store_true", help="Clear default commands")
    parser.add_argument("--clear-admin", action="store_true", help="Clear admin commands for provided IDs")
    args = parser.parse_args()

    token = args.token or CFG_TOKEN or os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("ERROR: TELEGRAM_BOT_TOKEN not provided (config/env/--token).")

    admins = parse_admin_ids(
        args.admin or (",".join(str(a) for a in (CFG_ADMINS or []))) or os.getenv("ADMIN_USER_IDS")
    )

    bot = Bot(token=token)

    if args.apply_all or (not any([
        args.default_only, args.admin_only, args.clear_default, args.clear_admin
    ])):
        # الوضع الافتراضي: طبّق الكل
        await set_default(bot)
        await set_admin_for_ids(bot, admins)
        return

    if args.default_only:
        await set_default(bot)
    if args.admin_only:
        await set_admin_for_ids(bot, admins)
    if args.clear_default:
        await clear_default(bot)
    if args.clear_admin:
        await clear_admin_for_ids(bot, admins)

if __name__ == "__main__":
    asyncio.run(main())
