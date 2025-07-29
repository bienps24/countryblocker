import os
import logging
import sqlite3
import threading
from datetime import datetime
from typing import Optional, List, Dict, Any
from contextlib import contextmanager
import phonenumbers
from phonenumbers import NumberParseException

from telegram import (
    Update, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove,
    ChatMemberUpdated, ChatMember, ChatJoinRequest
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ChatMemberHandler,
    ChatJoinRequestHandler, ContextTypes, filters, PicklePersistence
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import Forbidden, BadRequest

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))

class DatabaseManager:
    """Manages all interactions with the SQLite database using best practices."""
    def __init__(self, db_path: str = "filipino_bot.db"):
        self.db_path = db_path
        self.init_database()

    @contextmanager
    def get_conn(self):
        """Provides a database connection using a context manager to ensure it's always closed."""
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def init_database(self):
        """Initializes the database schema if tables don't exist."""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS verified_users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    phone_number TEXT,
                    verified_date TIMESTAMP,
                    is_banned BOOLEAN DEFAULT FALSE
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS join_requests (
                    user_id INTEGER,
                    chat_id INTEGER,
                    request_date TIMESTAMP,
                    status TEXT DEFAULT 'pending',
                    PRIMARY KEY (user_id, chat_id)
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS managed_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    description TEXT,
                    link TEXT UNIQUE NOT NULL,
                    chat_id INTEGER UNIQUE
                )
            ''')
            conn.commit()

    def add_verified_user(self, user_id: int, username: str, first_name: str, phone_number: str):
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO verified_users 
                (user_id, username, first_name, phone_number, verified_date, is_banned)
                VALUES (?, ?, ?, ?, ?, FALSE)
            ''', (user_id, username or "", first_name or "", phone_number, datetime.now()))
            conn.commit()

    def is_verified(self, user_id: int) -> bool:
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT 1 FROM verified_users WHERE user_id = ? AND is_banned = FALSE', (user_id,))
            return cursor.fetchone() is not None

    def ban_user(self, user_id: int):
        with self.get_conn() as conn:
            conn.cursor().execute('UPDATE verified_users SET is_banned = TRUE WHERE user_id = ?', (user_id,))
            conn.commit()

    def get_all_groups(self) -> List[Dict[str, Any]]:
        with self.get_conn() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT id, name, description, link, chat_id FROM managed_groups ORDER BY id')
            return [dict(row) for row in cursor.fetchall()]

    def add_group(self, name: str, description: str, link: str) -> bool:
        """Adds input validation before database insertion."""
        if not name.strip() or not link.strip():
            logger.error("Group name and link cannot be empty.")
            return False
        if not link.startswith(('https://t.me/', 'http://t.me/')):
            logger.error(f"Invalid Telegram link format: {link}")
            return False
            
        try:
            with self.get_conn() as conn:
                conn.cursor().execute('INSERT INTO managed_groups (name, description, link) VALUES (?, ?, ?)', (name, description, link))
                conn.commit()
            return True
        except sqlite3.IntegrityError:
            logger.warning(f"Attempted to add a group with a duplicate link: {link}")
            return False

    def remove_group(self, group_id: int) -> Optional[Dict[str, Any]]:
        with self.get_conn() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM managed_groups WHERE id = ?', (group_id,))
            group = cursor.fetchone()
            if group:
                cursor.execute('DELETE FROM managed_groups WHERE id = ?', (group_id,))
                conn.commit()
                return dict(group)
            return None
    
    def update_chat_id_by_link(self, link: str, chat_id: int):
        with self.get_conn() as conn:
            conn.cursor().execute('UPDATE managed_groups SET chat_id = ? WHERE link = ?', (chat_id, link))
            conn.commit()
            logger.info(f"Updated chat_id for group with link {link} to {chat_id}")

    def add_join_request(self, user_id: int, chat_id: int):
        with self.get_conn() as conn:
            conn.cursor().execute("INSERT OR REPLACE INTO join_requests (user_id, chat_id, request_date, status) VALUES (?, ?, ?, 'pending')", (user_id, chat_id, datetime.now()))
            conn.commit()

    def update_join_request_status(self, user_id: int, chat_id: int, status: str):
        with self.get_conn() as conn:
            conn.cursor().execute("UPDATE join_requests SET status = ? WHERE user_id = ? AND chat_id = ?", (status, user_id, chat_id))
            conn.commit()

    def get_user_info(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Get user information from verified_users table."""
        with self.get_conn() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM verified_users WHERE user_id = ?', (user_id,))
            result = cursor.fetchone()
            return dict(result) if result else None

class PhoneVerifier:
    @staticmethod
    def verify_phone_number(phone_number: str) -> dict:
        """Verify if a phone number is from the Philippines using a region hint."""
        try:
            parsed = phonenumbers.parse(phone_number, 'PH')
            is_valid = phonenumbers.is_valid_number(parsed)
            is_ph = phonenumbers.region_code_for_number(parsed) == 'PH'
            
            return {'is_filipino': is_valid and is_ph, 'formatted_number': phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)}
        except NumberParseException:
            return {'is_filipino': False, 'formatted_number': phone_number}

class FilipinoBotManager:
    def __init__(self):
        if not BOT_TOKEN: raise ValueError("BOT_TOKEN environment variable is required!")
        if not ADMIN_ID: raise ValueError("ADMIN_ID environment variable is required!")
            
        self.db = DatabaseManager()
        self.verifier = PhoneVerifier()
        self._groups_lock = threading.Lock()
        self.filipino_groups = []
        self.refresh_groups_cache()

    def refresh_groups_cache(self):
        """Reloads the group list from the database safely."""
        with self._groups_lock:
            self.filipino_groups = self.db.get_all_groups()
            logger.info("Refreshed groups cache from database.")
            
    def format_available_groups(self) -> str:
        """Reads from the thread-safe cache to format the group list."""
        with self._groups_lock:
            if not self.filipino_groups:
                return "🔍 No groups available at the moment."
            
            message = "🇵🇭 **Available Filipino Groups:**\n\n"
            for group in self.filipino_groups:
                message += f"**- {group['name']}**\n"
                message += f"  📝 {group['description']}\n"
                message += f"  🔗 {group['link']}\n\n"
            
            message += "💡 **Tip:** Verified users are auto-approved!"
            return message

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if self.db.is_verified(user.id):
            await update.message.reply_text(
                "✅ *Na-verify ka na!*\n\n" + self.format_available_groups(),
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True
            )
        else:
            contact_keyboard = [[KeyboardButton("📱 Share My Phone Number", request_contact=True)]]
            contact_markup = ReplyKeyboardMarkup(contact_keyboard, one_time_keyboard=True, resize_keyboard=True)
            await update.message.reply_text(
                f"🇵🇭 *Filipino Verification*\n\nHi {user.first_name}! To join our exclusive Filipino groups, please verify your identity by sharing your Philippine phone number.",
                reply_markup=contact_markup,
                parse_mode=ParseMode.MARKDOWN
            )

    async def handle_contact_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        contact = update.message.contact
        user = update.effective_user

        if contact.user_id != user.id:
            await update.message.reply_text("❌ Please share your own contact information.", reply_markup=ReplyKeyboardRemove())
            return
        
        phone_result = self.verifier.verify_phone_number(contact.phone_number)
        
        if phone_result['is_filipino']:
            self.db.add_verified_user(user.id, user.username, user.first_name, contact.phone_number)
            success_msg = f"✅ **VERIFIED!** 🇵🇭\n\nWelcome, {user.first_name}!\n\nYour number {phone_result['formatted_number']} is verified. You now have access to all our groups and will be auto-approved.\n\n{self.format_available_groups()}"
            await update.message.reply_text(success_msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True, reply_markup=ReplyKeyboardRemove())
            await context.bot.send_message(ADMIN_ID, f"✅ New Verified User: {user.first_name} (@{user.username or 'N/A'}), ID: `{user.id}`", parse_mode=ParseMode.MARKDOWN)
            
            # Auto-approve any pending join requests for this newly verified user
            await self.approve_pending_requests(context, user.id)
            
        else:
            fail_msg = f"❌ **Verification Failed**\n\nThe number you provided ({phone_result['formatted_number']}) is not recognized as a Philippine number. Please try again with a valid PH number."
            await update.message.reply_text(fail_msg, reply_markup=ReplyKeyboardRemove())

    async def approve_pending_requests(self, context: ContextTypes.DEFAULT_TYPE, user_id: int):
        """Auto-approve any pending join requests for a newly verified user."""
        try:
            with self.db.get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT chat_id FROM join_requests WHERE user_id = ? AND status = 'pending'", 
                    (user_id,)
                )
                pending_requests = cursor.fetchall()
                
                for (chat_id,) in pending_requests:
                    try:
                        # Try to approve the pending request
                        await context.bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
                        self.db.update_join_request_status(user_id, chat_id, "approved")
                        
                        # Get chat info for welcome message
                        try:
                            chat = await context.bot.get_chat(chat_id)
                            await context.bot.send_message(
                                chat_id=user_id,
                                text=f"🎉 **Automatically Approved!**\n\nYou've been approved to join **{chat.title}** since you're now a verified Filipino user! 🇵🇭",
                                parse_mode=ParseMode.MARKDOWN
                            )
                            
                            # Notify admin
                            await context.bot.send_message(
                                ADMIN_ID,
                                f"🎉 Auto-approved pending request: User {user_id} for {chat.title}",
                                parse_mode=ParseMode.MARKDOWN
                            )
                            
                        except Exception as e:
                            logger.warning(f"Could not send auto-approval message: {e}")
                            
                        logger.info(f"Auto-approved pending request for user {user_id} to chat {chat_id}")
                        
                    except Exception as e:
                        logger.error(f"Failed to approve pending request for user {user_id} to chat {chat_id}: {e}")
                        self.db.update_join_request_status(user_id, chat_id, "error")
                        
        except Exception as e:
            logger.error(f"Error checking pending requests for user {user_id}: {e}")

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        help_text = "🤖 **Bot Commands:**\n\n`/start` - Start the verification process.\n`/groups` - View available Filipino groups (for verified users).\n`/help` - Show this help message."
        
        if update.effective_user.id == ADMIN_ID:
            help_text += "\n\n**Admin Commands:**\n`/ban <user_id>` - Ban a user\n`/manage_groups` - Manage groups\n`/stats` - Show bot statistics"
        
        await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

    async def groups_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if self.db.is_verified(update.effective_user.id):
            await update.message.reply_text(self.format_available_groups(), parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        else:
            await update.message.reply_text("❌ You must be a verified user to see the list of groups. Please use /start to begin verification.")

    async def ban_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID: 
            await update.message.reply_text("❌ You don't have permission to use this command.")
            return
            
        if not context.args:
            await update.message.reply_text("Usage: `/ban <user_id>`", parse_mode=ParseMode.MARKDOWN)
            return
            
        try:
            user_id = int(context.args[0])
            self.db.ban_user(user_id)
            await update.message.reply_text(f"🚫 User `{user_id}` is now banned.", parse_mode=ParseMode.MARKDOWN)
            
            # Remove banned user from all groups
            with self._groups_lock:
                for group in self.filipino_groups:
                    if group['chat_id']:
                        try:
                            await context.bot.ban_chat_member(chat_id=group['chat_id'], user_id=user_id)
                            logger.info(f"Banned user {user_id} from group {group['name']}")
                        except Exception as e:
                            logger.error(f"Failed to kick banned user {user_id} from {group['name']}: {e}")
        except (ValueError, IndexError):
            await update.message.reply_text("❌ Invalid user ID. Please provide a valid numeric user ID.")

    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show bot statistics (Admin only)."""
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("❌ You don't have permission to use this command.")
            return

        with self.db.get_conn() as conn:
            cursor = conn.cursor()
            
            # Get verified users count
            cursor.execute('SELECT COUNT(*) FROM verified_users WHERE is_banned = FALSE')
            verified_count = cursor.fetchone()[0]
            
            # Get banned users count
            cursor.execute('SELECT COUNT(*) FROM verified_users WHERE is_banned = TRUE')
            banned_count = cursor.fetchone()[0]
            
            # Get groups count
            cursor.execute('SELECT COUNT(*) FROM managed_groups')
            groups_count = cursor.fetchone()[0]
            
            # Get pending join requests
            cursor.execute('SELECT COUNT(*) FROM join_requests WHERE status = "pending"')
            pending_requests = cursor.fetchone()[0]

        stats_text = f"""📊 **Bot Statistics**

👥 **Users:**
• Verified: {verified_count}
• Banned: {banned_count}

🏢 **Groups:** {groups_count}

⏳ **Pending Join Requests:** {pending_requests}
"""
        await update.message.reply_text(stats_text, parse_mode=ParseMode.MARKDOWN)
            
    async def manage_groups_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin command to manage groups."""
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("❌ You don't have permission to use this command.")
            return

        if not context.args:
            # Show help for manage_groups command
            help_text = """🏢 **Group Management Commands:**

**Add Group:**
`/manage_groups add "Group Name" "Description" "https://t.me/grouplink"`

**Remove Group:**
`/manage_groups remove <group_id>`

**List Groups:**
`/manage_groups list`

**Refresh Cache:**
`/manage_groups refresh`
"""
            await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)
            return

        action = context.args[0].lower()

        if action == "add":
            if len(context.args) < 4:
                await update.message.reply_text("❌ Usage: `/manage_groups add \"Group Name\" \"Description\" \"https://t.me/grouplink\"`", parse_mode=ParseMode.MARKDOWN)
                return
            
            name = context.args[1].strip('"')
            description = context.args[2].strip('"')
            link = context.args[3].strip('"')
            
            if self.db.add_group(name, description, link):
                self.refresh_groups_cache()
                await update.message.reply_text(f"✅ Group **{name}** added successfully!", parse_mode=ParseMode.MARKDOWN)
            else:
                await update.message.reply_text("❌ Failed to add group. Check if the link is valid and not already in use.")

        elif action == "remove":
            if len(context.args) < 2:
                await update.message.reply_text("❌ Usage: `/manage_groups remove <group_id>`", parse_mode=ParseMode.MARKDOWN)
                return
            
            try:
                group_id = int(context.args[1])
                removed_group = self.db.remove_group(group_id)
                if removed_group:
                    self.refresh_groups_cache()
                    await update.message.reply_text(f"✅ Group **{removed_group['name']}** removed successfully!", parse_mode=ParseMode.MARKDOWN)
                else:
                    await update.message.reply_text("❌ Group not found.")
            except ValueError:
                await update.message.reply_text("❌ Please provide a valid group ID.")

        elif action == "list":
            groups = self.db.get_all_groups()
            if not groups:
                await update.message.reply_text("📝 No groups found.")
                return
            
            message = "📋 **Managed Groups:**\n\n"
            for group in groups:
                message += f"**ID:** {group['id']}\n"
                message += f"**Name:** {group['name']}\n"
                message += f"**Description:** {group['description']}\n"
                message += f"**Link:** {group['link']}\n"
                message += f"**Chat ID:** {group['chat_id'] or 'Not set'}\n\n"
            
            await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

        elif action == "refresh":
            self.refresh_groups_cache()
            await update.message.reply_text("✅ Groups cache refreshed successfully!")

        else:
            await update.message.reply_text("❌ Unknown action. Use: add, remove, list, or refresh")
            
    async def handle_join_request(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle join requests to groups."""
        join_request: ChatJoinRequest = update.chat_join_request
        user = join_request.from_user
        chat = join_request.chat
        
        logger.info(f"Join request from {user.first_name} (@{user.username}) to {chat.title}")
        
        # Log the join request
        self.db.add_join_request(user.id, chat.id)
        
        # Check if user is verified
        if self.db.is_verified(user.id):
            try:
                # Auto-approve verified users
                await context.bot.approve_chat_join_request(chat_id=chat.id, user_id=user.id)
                self.db.update_join_request_status(user.id, chat.id, "approved")
                
                # Welcome message
                try:
                    await context.bot.send_message(
                        chat_id=user.id,
                        text=f"✅ Welcome to **{chat.title}**! You've been automatically approved as a verified Filipino user. 🇵🇭",
                        parse_mode=ParseMode.MARKDOWN
                    )
                except Exception as e:
                    logger.warning(f"Could not send welcome message to {user.id}: {e}")
                
                # Notify admin
                await context.bot.send_message(
                    ADMIN_ID,
                    f"✅ Auto-approved verified user: {user.first_name} (@{user.username or 'N/A'}) to {chat.title}",
                    parse_mode=ParseMode.MARKDOWN
                )
                
                logger.info(f"Auto-approved verified user {user.id} to {chat.title}")
                
            except Exception as e:
                logger.error(f"Failed to approve join request: {e}")
                self.db.update_join_request_status(user.id, chat.id, "error")
        else:
            # DON'T decline - keep request pending and guide user to verify
            try:
                # Just inform user how to get verified - DON'T decline the request
                await context.bot.send_message(
                    chat_id=user.id,
                    text=f"⏳ **Verification Required for {chat.title}**\n\nHi {user.first_name}! Your join request is **pending**.\n\nTo get automatically approved, you need to verify your Philippine phone number first.\n\n👉 Start verification by messaging me with /start\n\n✅ Once verified, you'll be **automatically approved** without needing to request again!",
                    parse_mode=ParseMode.MARKDOWN
                )
                
                # Notify admin about pending request
                await context.bot.send_message(
                    ADMIN_ID,
                    f"⏳ Pending verification: {user.first_name} (@{user.username or 'N/A'}) wants to join {chat.title}",
                    parse_mode=ParseMode.MARKDOWN
                )
                
                logger.info(f"User {user.id} request pending verification for {chat.title}")
                
            except Exception as e:
                logger.warning(f"Could not send verification message to {user.id}: {e}")
                # Still notify admin even if we can't message the user
                try:
                    await context.bot.send_message(
                        ADMIN_ID,
                        f"⏳ Pending verification (no DM): {user.first_name} (@{user.username or 'N/A'}) wants to join {chat.title}",
                        parse_mode=ParseMode.MARKDOWN
                    )
                except:
                    pass
        
    async def handle_chat_member_update(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle chat member updates (users joining/leaving groups)."""
        chat_member_update: ChatMemberUpdated = update.chat_member
        user = chat_member_update.from_user
        chat = chat_member_update.chat
        old_status = chat_member_update.old_chat_member.status
        new_status = chat_member_update.new_chat_member.status
        
        # Log member status changes
        if old_status != new_status:
            logger.info(f"User {user.first_name} ({user.id}) status changed from {old_status} to {new_status} in {chat.title}")
            
            # If user was banned, update their status
            if new_status == ChatMemberStatus.BANNED:
                self.db.ban_user(user.id)
                await context.bot.send_message(
                    ADMIN_ID,
                    f"🚫 User {user.first_name} (@{user.username or 'N/A'}) was banned from {chat.title}",
                    parse_mode=ParseMode.MARKDOWN
                )

    async def handle_my_chat_member_update(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle updates to the bot's own membership status."""
        chat_member_update: ChatMemberUpdated = update.my_chat_member
        chat = chat_member_update.chat
        old_status = chat_member_update.old_chat_member.status
        new_status = chat_member_update.new_chat_member.status
        
        logger.info(f"Bot status changed from {old_status} to {new_status} in {chat.title}")
        
        # If bot was added to a group, try to update the chat_id in database
        if new_status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR]:
            # Get the actual invite link from the chat if available
            invite_link = None
            try:
                # Try to get the primary invite link
                invite_link = await context.bot.export_chat_invite_link(chat.id)
                logger.info(f"Got invite link for {chat.title}: {invite_link}")
            except Exception as e:
                logger.warning(f"Could not get invite link for {chat.title}: {e}")
            
            # Try to match with stored groups
            groups = self.db.get_all_groups()
            updated = False
            
            for group in groups:
                # Check multiple matching criteria
                match_found = False
                
                # 1. Try to match by invite link (works for both public and private groups)
                if invite_link and group['link'] == invite_link:
                    match_found = True
                
                # 2. Try to match by username (for public groups only)
                elif 't.me/' in group['link'] and not group['link'].startswith('t.me/+'):
                    stored_username = group['link'].split('t.me/')[-1].split('?')[0]  # Remove query params
                    if chat.username and chat.username.lower() == stored_username.lower():
                        match_found = True
                
                # 3. Try to match by chat title (fallback, less reliable)
                elif not updated and group['name'].lower() == chat.title.lower():
                    match_found = True
                    logger.warning(f"Matched group by title (less reliable): {chat.title}")
                
                if match_found:
                    self.db.update_chat_id_by_link(group['link'], chat.id)
                    self.refresh_groups_cache()
                    updated = True
                    logger.info(f"Updated chat_id for group '{group['name']}' to {chat.id}")
                    break
            
            if not updated:
                logger.warning(f"Could not match group {chat.title} (ID: {chat.id}) with any stored group")
            
            await context.bot.send_message(
                ADMIN_ID,
                f"🤖 Bot added to group: **{chat.title}** (ID: `{chat.id}`)\n{'✅ Matched with stored group' if updated else '⚠️ No matching stored group found'}",
                parse_mode=ParseMode.MARKDOWN
            )
        
        elif new_status in [ChatMemberStatus.LEFT, ChatMemberStatus.BANNED]:
            await context.bot.send_message(
                ADMIN_ID,
                f"👋 Bot removed from group: **{chat.title}** (ID: `{chat.id}`)",
                parse_mode=ParseMode.MARKDOWN
            )

    def run(self):
        persistence = PicklePersistence(filepath="filipino_bot_persistence")
        application = Application.builder().token(BOT_TOKEN).persistence(persistence).build()

        # Command handlers
        application.add_handler(CommandHandler("start", self.start_command))
        application.add_handler(CommandHandler("help", self.help_command))
        application.add_handler(CommandHandler("groups", self.groups_command))
        application.add_handler(CommandHandler("ban", self.ban_command))
        application.add_handler(CommandHandler("stats", self.stats_command))
        application.add_handler(CommandHandler("manage_groups", self.manage_groups_command))
        
        # Message handlers
        application.add_handler(MessageHandler(filters.CONTACT, self.handle_contact_message))
        
        # Chat member handlers
        application.add_handler(ChatJoinRequestHandler(self.handle_join_request))
        application.add_handler(ChatMemberHandler(self.handle_chat_member_update, ChatMemberHandler.CHAT_MEMBER))
        application.add_handler(ChatMemberHandler(self.handle_my_chat_member_update, ChatMemberHandler.MY_CHAT_MEMBER))

        logger.info("🚀 Filipino Verification Bot (v3.1 - Complete) is starting...")
        application.run_polling(allowed_updates=Update.ALL_TYPES)

def main():
    try:
        bot_manager = FilipinoBotManager()
        bot_manager.run()
    except (ValueError, Exception) as e:
        logger.critical(f"❌ A fatal error occurred: {e}", exc_info=True)

if __name__ == "__main__":
    main()
