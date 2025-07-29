markdown# Filipino Telegram Verification Bot

Advanced Telegram bot for filtering and verifying Filipino users in groups and channels.

## Features
- 🇵🇭 Philippine phone number verification (+63)
- 💬 Filipino language test
- 🔒 Secure contact sharing (anti-fake)
- ⚡ Auto-ban system with strikes
- 👑 Admin controls and statistics
- 📊 Activity logging and monitoring

## Deployment

### Railway Deployment:
1. Fork this repository
2. Connect to Railway
3. Set environment variables:
   - `BOT_TOKEN=your_bot_token`
   - `ADMIN_ID=your_telegram_user_id`
4. Deploy!

### Environment Variables:
- `BOT_TOKEN` - Your Telegram bot token from @BotFather
- `ADMIN_ID` - Your Telegram user ID for admin access

## Bot Commands

### User Commands:
- `/start` - Welcome message and bot info
- `/verify` - Start verification process
- `/status` - Check verification status
- `/appeal` - Appeal a ban

### Admin Commands:
- `/stats` - View bot statistics
- `/whitelist <user_id>` - Add user to whitelist
- `/ban <user_id> <reason>` - Manual ban
- `/unban <user_id>` - Remove ban

## Security Features:
- ✅ Secure phone number verification (no fake numbers)
- ✅ Real Telegram contact API integration
- ✅ Strike system with progressive warnings
- ✅ Admin override and whitelist system
- ✅ Activity logging and audit trail

## License
MIT License
