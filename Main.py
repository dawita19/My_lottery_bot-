import os
import logging
import random
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)
import firebase_admin
from firebase_admin import credentials, firestore
from firebase_admin.firestore import SERVER_TIMESTAMP

# ==================== CONFIGURATION ====================
class Config:
    # Telegram Configuration - IMPORTANT: Use environment variables, do NOT hardcode sensitive tokens!
    # For local testing, you can temporarily put a token, but remove it before committing to Git.
    BOT_TOKEN = os.getenv('BOT_TOKEN') 
    ADMIN_IDS = [int(id) for id in os.getenv('ADMIN_IDS', '').split(',') if id.strip()] # No default admin for security
    CHANNEL_ID = int(os.getenv('CHANNEL_ID', '-1002585009335')) # Default channel for development

    # Firebase App ID for collection path isolation
    # This should match your Firebase Project ID (e.g., 'telegram-lottery-bot-58291')
    APP_ID = os.getenv('APP_ID', 'default-lottery-app-id')

    # Payment Configuration
    PAYMENT_METHOD = {
        "bank": "Commercial Bank of Ethiopia (CBE)",
        "account_number": "1000295626473",
        "account_name": "Dawit Tsegaw Ayele",
        "branch": "Main Branch",
        "contact_phone": "+251998137593",
        "contact_username": "@lij_hailemichael"
    }
    
    # Lottery Configuration
    TICKET_VALUES = [100, 200, 300]
    TOTAL_TICKETS_PER_VALUE = 100
    REWARDS = {
        100: [5000, 2000, 1000],
        200: [10000, 4000, 2000],
        300: [15000, 6000, 3000]
    }
    REFERRAL_REQUIRED_COUNT = 10
    REFERRAL_BONUS_TICKET_VALUE = 200
    LOYALTY_BONUS_PURCHASE_COUNT = 10 # Get 1 free ticket for every 10 identical tickets bought
    
    # System Configuration
    # Path to your Firebase service account key file on PythonAnywhere.
    # MUST be uploaded to your home directory or specified path.
    FIREBASE_CREDENTIALS = os.path.join(os.path.expanduser('~'), 'serviceAccountKey.json')
    LOG_FILE = os.path.join(os.path.expanduser('~'), 'grandlottery.log')
    DATA_RETENTION_DAYS = 180 # Not actively implemented in this bot, but good for planning

# ==================== INITIALIZATION ====================
# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler(Config.LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Initialize Firebase
db = None
try:
    if not os.path.exists(Config.FIREBASE_CREDENTIALS):
        raise FileNotFoundError(f"Firebase credentials file not found at: {Config.FIREBASE_CREDENTIALS}")
        
    cred = credentials.Certificate(Config.FIREBASE_CREDENTIALS)
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    logger.info("Firebase initialized successfully")
except Exception as e:
    logger.critical(f"Firebase initialization failed: {str(e)}")
    # Raising an exception here will stop the bot if Firebase fails to initialize, which is desired.
    raise

# --- Firestore Collection Path Helper ---
def get_firestore_collection_path(collection_name: str, user_id: Optional[str] = None) -> str:
    """
    Constructs the Firestore collection path based on APP_ID and user_id for isolation.
    Public data: artifacts/{APP_ID}/public_data/{collection_name}
    User-specific data: artifacts/{APP_ID}/users/{user_id}/{collection_name}
    """
    if user_id:
        return f"artifacts/{Config.APP_ID}/users/{user_id}/{collection_name}"
    return f"artifacts/{Config.APP_ID}/public_data/{collection_name}"


# ==================== DATABASE MODELS ====================
class UserModel:
    @staticmethod
    async def get_or_create(user_id: str, username: str, first_name: str, referrer_code: Optional[str] = None) -> Dict:
        user_ref = db.collection(get_firestore_collection_path("users", user_id)).document(user_id)
        user_doc = await user_ref.get()
        
        if not user_doc.exists:
            user_data = {
                "id": user_id,
                "username": username,
                "first_name": first_name,
                "join_date": SERVER_TIMESTAMP,
                "referral_code": f"ref_{user_id[:8]}", # Generate unique referral code
                "balance": 0,
                "tickets_purchased_count": {}, # e.g., {'100': 5, '200': 2}
                "referral_count": 0,
                "referral_bonus_claimed": False,
                "last_active": SERVER_TIMESTAMP
            }
            await user_ref.set(user_data)
            logger.info(f"New user {user_id} created.")

            # If a referrer code exists, track it
            if referrer_code and referrer_code.startswith('ref_'):
                await db.collection(get_firestore_collection_path("referrals")).add({
                    "referrer_code": referrer_code,
                    "referred_user_id": user_id,
                    "timestamp": SERVER_TIMESTAMP
                })
                # Increment referrer's count (find the referrer by their code)
                referrer_query = db.collectionGroup("users").where("referral_code", "==", referrer_code)
                referrer_docs = await referrer_query.get()
                if referrer_docs:
                    for doc in referrer_docs: # Should be only one
                        referrer_ref = doc.reference
                        await referrer_ref.update({"referral_count": firestore.Increment(1)})
                        logger.info(f"Referral counted for {referrer_ref.id} by {user_id}")
                        break
        
        # Always update last_active
        await user_ref.update({"last_active": SERVER_TIMESTAMP})
        return (await user_ref.get()).to_dict()

    @staticmethod
    async def update_user_balance(user_id: str, amount: int):
        user_ref = db.collection(get_firestore_collection_path("users", user_id)).document(user_id)
        await user_ref.update({"balance": firestore.Increment(amount)})
    
    @staticmethod
    async def update_tickets_purchased_count(user_id: str, ticket_value: int):
        user_ref = db.collection(get_firestore_collection_path("users", user_id)).document(user_id)
        await user_ref.update({
            f"tickets_purchased_count.{ticket_value}": firestore.Increment(1)
        })

class TicketModel:
    @staticmethod
    async def get_available(ticket_value: int) -> List[int]:
        tickets_ref = db.collection(get_firestore_collection_path(f"tickets_{ticket_value}"))
        query = tickets_ref.where("is_sold", "==", False)
        # Fetch all available tickets to show more than just 10
        return [int(doc.id) async for doc in query.stream()]

    @staticmethod
    async def mark_as_sold(ticket_value: int, ticket_number: int, user_id: str, is_free: bool = False, free_reason: Optional[str] = None):
        ticket_doc_ref = db.collection(get_firestore_collection_path(f"tickets_{ticket_value}")).document(str(ticket_number))
        await ticket_doc_ref.update({
            "is_sold": True,
            "buyer_id": user_id,
            "sold_at": SERVER_TIMESTAMP,
            "is_free": is_free,
            "free_reason": free_reason
        })
        # Record the sale in registrations collection for historical data / draw
        await db.collection(get_firestore_collection_path("lottery_registrations")).add({
            "ticket_value": ticket_value,
            "ticket_number": ticket_number,
            "buyer_id": user_id,
            "purchase_date": SERVER_TIMESTAMP,
            "is_free": is_free,
            "free_reason": free_reason
        })

# ==================== CORE FUNCTIONS ====================
class LotterySystem:
    @staticmethod
    async def initialize_tickets():
        """Create all tickets in Firestore if they don't exist or if a new batch is needed."""
        for value in Config.TICKET_VALUES:
            coll_ref = db.collection(get_firestore_collection_path(f"tickets_{value}"))
            # Check if any tickets exist for the current batch/round
            
            # Simple check: if less than TOTAL_TICKETS_PER_VALUE exist, re-initialize the whole set
            # For a more robust system, you'd check for a 'batch_id' or 'round_id'
            existing_tickets_count = len([doc async for doc in coll_ref.stream()])
            
            if existing_tickets_count < Config.TOTAL_TICKETS_PER_VALUE:
                logger.info(f"Re-initializing/creating {Config.TOTAL_TICKETS_PER_VALUE} tickets for {value} Birr.")
                batch = db.batch()
                # Clear existing tickets for this value (optional, but good for fresh rounds)
                for doc in ([doc async for doc in coll_ref.stream()]):
                    batch.delete(doc.reference)
                
                for num in range(1, Config.TOTAL_TICKETS_PER_VALUE + 1):
                    doc_ref = coll_ref.document(str(num))
                    batch.set(doc_ref, {
                        "number": num,
                        "value": value,
                        "is_sold": False,
                        "batch_id": datetime.now().strftime("%Y%m%d%H%M%S") # New batch ID for tracking
                    })
                await batch.commit()
                logger.info(f"Initialized {Config.TOTAL_TICKETS_PER_VALUE} new tickets for {value} Birr.")
            else:
                logger.info(f"{value} Birr tickets already fully initialized for current batch.")

    @staticmethod
    async def conduct_draw(ticket_value: int, context: ContextTypes.DEFAULT_TYPE):
        """Automatically conduct lottery draw"""
        try:
            # Get all sold tickets for the current batch (ensure they are actual purchases, not just reserved)
            # Fetch from lottery_registrations as it holds confirmed sales
            registrations_ref = db.collection(get_firestore_collection_path("lottery_registrations"))
            sold_tickets_query = registrations_ref.where("ticket_value", "==", ticket_value)
            
            sold_tickets = [doc.to_dict() async for doc in sold_tickets_query.stream()]
            
            if len(sold_tickets) < 3: # Need at least 3 tickets to draw 3 winners
                logger.warning(f"Attempted draw for {ticket_value} Birr but only {len(sold_tickets)} tickets sold. Draw postponed.")
                await context.bot.send_message(
                    chat_id=Config.CHANNEL_ID,
                    text=f"⚠️ Draw postponed for {ticket_value} Birr tickets. Not enough tickets sold yet ({len(sold_tickets)}/{Config.TOTAL_TICKETS_PER_VALUE})."
                )
                return

            # Select winners (ensure unique tickets and possibly unique buyers for top prizes)
            random.shuffle(sold_tickets)
            winners_data = []
            drawn_ticket_numbers = set()
            drawn_buyer_ids = set()

            for i in range(Config.TOTAL_TICKETS_PER_VALUE): # Iterate up to total tickets to find 3 unique winners
                if len(winners_data) >= 3:
                    break # Found 3 winners

                candidate_ticket = sold_tickets[i]
                ticket_number = candidate_ticket["ticket_number"]
                buyer_id = candidate_ticket["buyer_id"]

                # Ensure the ticket number hasn't been drawn and the buyer hasn't already won a higher prize
                if ticket_number not in drawn_ticket_numbers and buyer_id not in drawn_buyer_ids:
                    prize = Config.REWARDS[ticket_value][len(winners_data)] # Use len(winners_data) as index for rank
                    winners_data.append({
                        "position": len(winners_data) + 1,
                        "ticket_number": ticket_number,
                        "user_id": buyer_id,
                        "prize": prize
                    })
                    drawn_ticket_numbers.add(ticket_number)
                    drawn_buyer_ids.add(buyer_id) # Mark buyer as having won a prize

            # Record draw
            draw_record = {
                "ticket_value": ticket_value,
                "timestamp": SERVER_TIMESTAMP,
                "winners": winners_data,
                "status": "completed"
            }
            await db.collection(get_firestore_collection_path("draws")).add(draw_record)
            logger.info(f"Draw for {ticket_value} Birr recorded.")
            
            # Public announcement
            announcement_text = f"🏆 <b>{ticket_value} Birr Lottery Draw Results!</b> 🏆\n"
            for winner_info in winners_data:
                user_doc = await db.collection(get_firestore_collection_path("users", winner_info['user_id'])).document(winner_info['user_id']).get()
                winner_username = user_doc.to_dict().get('username', 'N/A') if user_doc.exists else 'N/A'
                announcement_text += (
                    f"{['🥇','🥈','🥉'][winner_info['position']-1]} Winner: Ticket #{winner_info['ticket_number']} "
                    f"(User: @{winner_username or 'Hidden'}) - <b>{winner_info['prize']} Birr</b>\n"
                )
            await context.bot.send_message(
                chat_id=Config.CHANNEL_ID,
                text=announcement_text,
                parse_mode='HTML'
            )
            logger.info(f"Draw results for {ticket_value} Birr announced.")

            # Notify individual winners and update balance
            for winner_info in winners_data:
                await context.bot.send_message(
                    chat_id=winner_info["user_id"],
                    text=f"🎉 Congratulations! You won {winner_info['prize']} Birr with ticket #{winner_info['ticket_number']} in the {ticket_value} Birr lottery!"
                )
                await UserModel.update_user_balance(winner_info["user_id"], winner_info['prize'])
                logger.info(f"User {winner_info['user_id']} notified and balance updated for {winner_info['prize']} Birr win.")
            
            # Reset tickets for a new round
            await LotterySystem.reset_tickets(ticket_value)
            
        except Exception as e:
            logger.error(f"Draw error for {ticket_value} Birr: {str(e)}", exc_info=True)
            await AdminSystem.notify_admins(context, f"🚨 Draw failed for {ticket_value} Birr tickets! Error: {str(e)}")

    @staticmethod
    async def reset_tickets(ticket_value: int):
        """Reset all tickets for a new round after a draw."""
        batch = db.batch()
        tickets_ref = db.collection(get_firestore_collection_path(f"tickets_{ticket_value}"))
        
        # Delete old tickets (fetch all documents in the collection)
        async for doc in tickets_ref.stream():
            batch.delete(doc.reference)
        await batch.commit()
        logger.info(f"Deleted old tickets for {ticket_value} Birr.")
        
        # Delete old lottery registrations for this value (optional, depends on desired history retention)
        registrations_ref = db.collection(get_firestore_collection_path("lottery_registrations"))
        old_registrations_query = registrations_ref.where("ticket_value", "==", ticket_value)
        batch_registrations = db.batch()
        async for doc in old_registrations_query.stream():
            batch_registrations.delete(doc.reference)
        await batch_registrations.commit()
        logger.info(f"Deleted old lottery registrations for {ticket_value} Birr.")

        # Create new tickets for the next round
        batch = db.batch() # New batch for new tickets
        for num in range(1, Config.TOTAL_TICKETS_PER_VALUE + 1):
            doc_ref = tickets_ref.document(str(num))
            batch.set(doc_ref, {
                "number": num,
                "value": ticket_value,
                "is_sold": False,
                "batch_id": datetime.now().strftime("%Y%m%d%H%M%S") # New batch ID for tracking
            })
        await batch.commit()
        logger.info(f"Created new tickets for {ticket_value} Birr.")
        if application and application.bot:
            await application.bot.send_message(
                chat_id=Config.CHANNEL_ID,
                text=f"🔄 New Round! All {ticket_value} Birr tickets are now available for purchase again!"
            )


    @staticmethod
    async def award_loyalty_bonus(user_id: str, purchased_ticket_value: int, context: ContextTypes.DEFAULT_TYPE):
        """Awards a free ticket for loyalty."""
        if not db: return

        # Find an available ticket of the same value
        available_free_numbers = await TicketModel.get_available(purchased_ticket_value)
        
        if available_free_numbers:
            free_ticket_number = random.choice(available_free_numbers)
            
            await TicketModel.mark_as_sold(purchased_ticket_value, free_ticket_number, user_id, is_free=True, free_reason="Loyalty Bonus")
            await UserModel.update_tickets_purchased_count(user_id, purchased_ticket_value) # Count free ticket towards loyalty too if desired
            
            await context.bot.send_message(
                chat_id=user_id,
                text=f"🎉 <b>Loyalty Bonus!</b> You've earned a FREE <b>{purchased_ticket_value} Birr</b> Ticket! Your free ticket number is <b>{free_ticket_number}</b>.",
                parse_mode='HTML'
            )
            await context.bot.send_message(
                chat_id=Config.CHANNEL_ID,
                text=f"🎁 Loyalty Bonus: User ID <code>{user_id[:8]}...</code> received a free {purchased_ticket_value} Birr ticket (Number #{free_ticket_number})!"
            )
            logger.info(f"User {user_id} awarded loyalty bonus for {purchased_ticket_value} Birr ticket #{free_ticket_number}.")
        else:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"You qualified for a free {purchased_ticket_value} Birr ticket, but no numbers are currently available. Please contact support."
            )
            logger.warning(f"User {user_id} qualified for loyalty bonus for {purchased_ticket_value} Birr, but no tickets available.")

    @staticmethod
    async def award_referral_bonus(user_id: str, context: ContextTypes.DEFAULT_TYPE):
        """Awards a free 200 Birr ticket for referrals."""
        if not db: return

        # Find an available 200 Birr ticket
        available_free_numbers = await TicketModel.get_available(Config.REFERRAL_BONUS_TICKET_VALUE)
        
        if available_free_numbers:
            free_ticket_number = random.choice(available_free_numbers)
            
            await TicketModel.mark_as_sold(Config.REFERRAL_BONUS_TICKET_VALUE, free_ticket_number, user_id, is_free=True, free_reason="Referral Bonus")
            await UserModel.update_tickets_purchased_count(user_id, Config.REFERRAL_BONUS_TICKET_VALUE) # Count free ticket towards loyalty too if desired

            user_ref = db.collection(get_firestore_collection_path("users", user_id)).document(user_id)
            await user_ref.update({"referral_bonus_claimed": True}) # Mark bonus as claimed
            
            await context.bot.send_message(
                chat_id=user_id,
                text=f"🎉 <b>Referral Bonus!</b> You've earned a FREE <b>{Config.REFERRAL_BONUS_TICKET_VALUE} Birr</b> Ticket! Your free ticket number is <b>{free_ticket_number}</b>.",
                parse_mode='HTML'
            )
            await context.bot.send_message(
                chat_id=Config.CHANNEL_ID,
                text=f"👥 Referral Bonus: User ID <code>{user_id[:8]}...</code> received a free {Config.REFERRAL_BONUS_TICKET_VALUE} Birr ticket (Number #{free_ticket_number}) for inviting {Config.REFERRAL_REQUIRED_COUNT} users!"
            )
            logger.info(f"User {user_id} awarded referral bonus.")
        else:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"You qualified for a free {Config.REFERRAL_BONUS_TICKET_VALUE} Birr ticket, but no numbers are currently available. Please contact support."
            )
            logger.warning(f"User {user_id} qualified for referral bonus, but no tickets available.")


# ==================== HANDLERS ====================
class UserHandlers:
    @staticmethod
    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command with referral tracking"""
        user = update.effective_user
        # Extract referrer code from deep linking, if any
        referrer_code = None
        if context.args and len(context.args) > 0:
            potential_referrer = context.args[0]
            if potential_referrer.startswith('ref_'):
                referrer_code = potential_referrer
        
        await UserModel.get_or_create(str(user.id), user.username, user.first_name, referrer_code)
        
        await update.message.reply_text(
            "🎉 Welcome to Grand Lottery!",
            reply_markup=Buttons.main_menu()
        )

    @staticmethod
    async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Display help information"""
        help_text = f"""
ℹ️ Grand Lottery Help Center

🛒 How to Buy Tickets:
1. Select ticket value (100/200/300 Birr)
2. Choose available number
3. Pay to our CBE account:
    • Account Name: {Config.PAYMENT_METHOD['account_name']}
    • Account Number: {Config.PAYMENT_METHOD['account_number']}
    • Branch: {Config.PAYMENT_METHOD['branch']}
4. Upload payment receipt

📞 Need Help? Contact:
• Phone: {Config.PAYMENT_METHOD['contact_phone']}
• Telegram: {Config.PAYMENT_METHOD['contact_username']}
"""
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(
                help_text,
                reply_markup=Buttons.help_menu(),
                parse_mode='HTML'
            )
        else:
            await update.message.reply_text(
                help_text,
                reply_markup=Buttons.help_menu(),
                parse_mode='HTML'
            )
    
    @staticmethod
    async def show_my_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Displays user's purchased tickets and recent draw results."""
        user_id = str(update.effective_user.id)
        chat_id = update.effective_chat.id

        if not db:
            await context.bot.send_message(chat_id, "Bot is currently undergoing maintenance or Firebase is not configured. Please try again later.", parse_mode='HTML')
            return

        try:
            tickets_info = []
            registrations_ref = db.collection(get_firestore_collection_path("lottery_registrations"))
            user_tickets_query = registrations_ref.where("buyer_id", "==", user_id)
            user_tickets_docs = [doc async for doc in user_tickets_query.stream()]

            if not user_tickets_docs:
                tickets_info.append("You haven't purchased any tickets yet. Use '🎟 Buy Tickets' to buy one!")
            else:
                tickets_info.append("<b>Your Purchased Tickets:</b>\n")
                for doc in user_tickets_docs:
                    ticket = doc.to_dict()
                    free_status = f" (FREE: {ticket['free_reason']})" if ticket.get('is_free') else ""
                    purchase_date = datetime.fromisoformat(ticket['purchase_date'].isoformat()) if hasattr(ticket['purchase_date'], 'isoformat') else "N/A"
                    tickets_info.append(f"• Value: {ticket['ticket_value']} Birr, Number: {ticket['ticket_number']}{free_status} (Purchased: {purchase_date.strftime('%Y-%m-%d %H:%M')})")

            tickets_message = "\n".join(tickets_info)
            await context.bot.send_message(chat_id, tickets_message, parse_mode='HTML')

            # Display recent draw results
            draws_info = ["\n<b>Recent Lottery Draws:</b>\n"]
            draws_ref = db.collection(get_firestore_collection_path("draws"))
            
            all_draws_docs = [doc async for doc in draws_ref.stream()]
            all_draws = sorted([doc.to_dict() for doc in all_draws_docs], key=lambda x: x.get('timestamp', datetime.min).isoformat(), reverse=True) # Sort by timestamp

            if not all_draws:
                draws_info.append("No lottery draws have occurred yet.")
            else:
                for draw in all_draws[:3]: # Show only latest 3 draws
                    draw_date = datetime.fromisoformat(draw['timestamp'].isoformat()) if hasattr(draw['timestamp'], 'isoformat') else "N/A"
                    draws_info.append(f"<b>--- {draw['ticket_value']} Birr Lottery Draw ---</b>")
                    draws_info.append(f"Date: {draw_date.strftime('%Y-%m-%d %H:%M')}")
                    for winner in draw['winners']:
                        user_id_short = winner['user_id'][:8] + "..." if winner['user_id'] else "N/A"
                        draws_info.append(f"  🏆 {winner['position']}. Winner: Ticket #{winner['ticket_number']} (User: <code>{user_id_short}</code>) - {winner['prize']} Birr")
                    draws_info.append("\n") # Add a newline for spacing

            draws_message = "\n".join(draws_info)
            await context.bot.send_message(chat_id, draws_message, parse_mode='HTML')

        except Exception as e:
            logger.error(f"Error fetching user tickets or draw results for {user_id}: {e}", exc_info=True)
            await context.bot.send_message(chat_id, "An error occurred while fetching your tickets or draw results. Please try again later.")

    @staticmethod
    async def show_referral_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Displays user's referral code and status."""
        user_id = str(update.effective_user.id)
        chat_id = update.effective_chat.id

        if not db:
            await context.bot.send_message(chat_id, "Bot is currently undergoing maintenance or Firebase is not configured. Please try again later.", parse_mode='HTML')
            return

        user_data = await UserModel.get_or_create(user_id, update.effective_user.username, update.effective_user.first_name)
        referral_code = user_data.get("referral_code", f"ref_{user_id[:8]}")
        referral_count = user_data.get("referral_count", 0)
        referral_bonus_claimed = user_data.get("referral_bonus_claimed", False)

        message_text = (
            f"<b>👥 Invite & Earn!</b>\n\n"
            f"Share your unique referral link to invite friends:\n"
            f"🔗 <code>https://t.me/{context.bot.username}?start={referral_code}</code>\n\n"
            f"Invited Active Users: <b>{referral_count} / {Config.REFERRAL_REQUIRED_COUNT}</b>\n"
            f"<i>(Note: 'Active user' is tracked via new signups using your link.)</i>\n\n"
        )

        keyboard = []
        if referral_count >= Config.REFERRAL_REQUIRED_COUNT and not referral_bonus_claimed:
            message_text += "You have qualified for a FREE 200 Birr ticket!\n"
            keyboard.append([InlineKeyboardButton("Claim FREE 200 Birr Ticket!", callback_data="claim_referral_bonus")])
        elif referral_bonus_claimed:
            message_text += "You have already claimed your referral bonus.\n"
        else:
            message_text += f"Invite {Config.REFERRAL_REQUIRED_COUNT - referral_count} more active users to claim your bonus!\n"
        
        # Add a simulation button for testing (only visible to admins)
        if user_id in map(str, Config.ADMIN_IDS): # Convert admin IDs to string for comparison
            keyboard.append([InlineKeyboardButton("Simulate New Invited User (Admin)", callback_data="simulate_invite")])

        keyboard.append([InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")])

        reply_markup = InlineKeyboardMarkup(keyboard)
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(message_text, reply_markup=reply_markup, parse_mode='HTML')
        else:
            await update.message.reply_text(message_text, reply_markup=reply_markup, parse_mode='HTML')

    @staticmethod
    async def claim_referral_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer("Claiming bonus...")
        user_id = str(query.from_user.id)
        
        if not db:
            await query.edit_message_text("Bot is currently undergoing maintenance. Please try again later.", parse_mode='HTML')
            return

        user_data = await UserModel.get_or_create(user_id, query.from_user.username, query.from_user.first_name)
        referral_count = user_data.get("referral_count", 0)
        referral_bonus_claimed = user_data.get("referral_bonus_claimed", False)

        if referral_count >= Config.REFERRAL_REQUIRED_COUNT and not referral_bonus_claimed:
            try:
                await LotterySystem.award_referral_bonus(user_id, context)
                # After awarding, re-show referral info to update status
                await UserHandlers.show_referral_info(update, context) 
            except Exception as e:
                logger.error(f"Error claiming referral bonus for {user_id}: {e}", exc_info=True)
                await query.edit_message_text("An error occurred while claiming your bonus. Please try again later.")
        else:
            await query.edit_message_text("You are not eligible to claim the referral bonus yet, or you have already claimed it.", reply_markup=Buttons.main_menu())

    @staticmethod
    async def simulate_invite_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        user_id = str(query.from_user.id)

        if user_id not in map(str, Config.ADMIN_IDS): # Ensure only admins can use this
            await query.answer("Access Denied.")
            return

        await query.answer("Simulating invited user...")
        
        # Increment referral count for the admin themselves (for testing their own bonus)
        admin_user_data = await UserModel.get_or_create(user_id, query.from_user.username, query.from_user.first_name)
        await db.collection(get_firestore_collection_path("users", user_id)).document(user_id).update({
            "referral_count": firestore.Increment(1)
        })
        logger.info(f"Admin {user_id} simulated a new invited user.")
        
        # Re-show referral info to update the count
        await UserHandlers.show_referral_info(update, context)


class PurchaseHandlers:
    @staticmethod
    async def start_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Start ticket purchase flow"""
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(
                "Select ticket value:",
                reply_markup=Buttons.ticket_values()
            )
        else: # From /buy command
            await update.message.reply_text(
                "Select ticket value:",
                reply_markup=Buttons.ticket_values()
            )
        return "SELECT_VALUE"

    @staticmethod
    async def select_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Handle ticket value selection and display available numbers."""
        query = update.callback_query
        await query.answer()
        
        ticket_value = int(query.data.split('_')[1])
        context.user_data['ticket_value'] = ticket_value
        
        available = await TicketModel.get_available(ticket_value)
        
        if not available:
            await query.edit_message_text("All tickets for this value are currently sold out. Please try another value or wait for a new round.", reply_markup=Buttons.main_menu())
            return ConversationHandler.END # End conversation if no tickets

        # Display up to 20 numbers for selection, provide pagination if more.
        # For simplicity, let's just show a chunk or indicate more available.
        # A more complex UI would involve paginating numbers.
        display_numbers = available[:50] # Show up to 50 numbers directly
        
        keyboard_rows = []
        for i in range(0, len(display_numbers), 10): # 10 numbers per row
            row = [InlineKeyboardButton(str(num), callback_data=f"number_{num}") for num in display_numbers[i:i+10]]
            keyboard_rows.append(row)
        
        if len(available) > len(display_numbers):
            keyboard_rows.append([InlineKeyboardButton("More numbers coming soon...", callback_data="no_op")]) # Placeholder for pagination
        
        keyboard_rows.append([InlineKeyboardButton("🔙 Back to Values", callback_data="start_purchase")])
        keyboard_rows.append([InlineKeyboardButton("❌ Cancel Purchase", callback_data="cancel_purchase")])

        await query.edit_message_text(
            f"Available {ticket_value} Birr tickets (first {len(display_numbers)} displayed):",
            reply_markup=InlineKeyboardMarkup(keyboard_rows)
        )
        return "SELECT_NUMBER"

    @staticmethod
    async def select_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Handles selected ticket number and prompts for payment."""
        query = update.callback_query
        await query.answer()

        selected_number = int(query.data.split('_')[1])
        ticket_value = context.user_data.get('ticket_value')

        if not ticket_value:
            await query.edit_message_text("Error: Please select a ticket value first. Use '🎟 Buy Tickets'.", reply_markup=Buttons.main_menu())
            return ConversationHandler.END

        # Re-check availability just in case it was sold between selections
        ticket_doc_ref = db.collection(get_firestore_collection_path(f"tickets_{ticket_value}")).document(str(selected_number))
        ticket_doc = await ticket_doc_ref.get()
        if not ticket_doc.exists or ticket_doc.to_dict().get("is_sold"):
            await query.edit_message_text(
                f"Sorry, ticket number {selected_number} is no longer available. Please select another.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Choose another number", callback_data=f"select_{ticket_value}")],
                                                [InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]])
            )
            return "SELECT_NUMBER" # Stay in select number state or go back

        context.user_data['ticket_number'] = selected_number
        
        payment_info_text = (
            f"You selected: <b>{ticket_value} Birr</b> Ticket, Number <b>{selected_number}</b>.\n\n"
            "Please send the exact amount to our bank account:\n"
            f"• <b>Bank:</b> {Config.PAYMENT_METHOD['bank']}\n"
            f"• <b>Account Name:</b> {Config.PAYMENT_METHOD['account_name']}\n"
            f"• <b>Account Number:</b> {Config.PAYMENT_METHOD['account_number']}\n"
            f"• <b>Branch:</b> {Config.PAYMENT_METHOD['branch']}\n\n"
            "After payment, send a <b>photo or document</b> of your payment proof (screenshot/transaction ID) to this chat to confirm your purchase."
        )
        await query.edit_message_text(payment_info_text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel Purchase", callback_data="cancel_purchase")]
        ]))
        return "UPLOAD_RECEIPT"


    @staticmethod
    async def process_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle payment receipt upload"""
        user_id = str(update.effective_user.id)
        ticket_value = context.user_data.get('ticket_value')
        ticket_number = context.user_data.get('ticket_number')
        
        if not ticket_value or not ticket_number:
            await update.message.reply_text("It seems there was an issue with your ticket selection. Please start again from the main menu.", reply_markup=Buttons.main_menu())
            return ConversationHandler.END

        # Validate receipt
        payment_proof_file_id = None
        if update.message.photo:
            payment_proof_file_id = update.message.photo[-1].file_id # Get the largest photo
        elif update.message.document and update.message.document.mime_type.startswith('image'): # Ensure it's an image document
            payment_proof_file_id = update.message.document.file_id
        else:
            await update.message.reply_text("Please send your payment receipt as a <b>photo or an image document</b>.", parse_mode='HTML')
            return "UPLOAD_RECEIPT" # Stay in this state until valid proof is sent

        await update.message.reply_text("✅ Payment proof received! Your purchase is pending verification. Thank you for your patience.")
        
        try:
            # Record transaction (ticket is still "reserved" by user_data context until admin verifies)
            await db.collection(get_firestore_collection_path("transactions")).add({
                "user_id": user_id,
                "ticket_value": ticket_value,
                "ticket_number": ticket_number,
                "amount": ticket_value,
                "payment_proof_file_id": payment_proof_file_id,
                "status": "pending_verification",
                "timestamp": SERVER_TIMESTAMP
            })
            logger.info(f"Transaction recorded for user {user_id}, ticket {ticket_value} Birr #{ticket_number}.")
            
            # Notify admin
            admin_message = (
                f"🛒 New Payment to Verify:\n"
                f"User: <a href='tg://user?id={user_id}'>{update.effective_user.full_name}</a> (@{update.effective_user.username or 'N/A'})\n"
                f"Ticket: <b>{ticket_value} Birr (#{ticket_number})</b>\n"
                f"To verify, use command: <code>/verify {user_id} {ticket_number} {ticket_value}</code>"
            )
            await AdminSystem.notify_admins(
                context,
                admin_message,
                photo_file_id=payment_proof_file_id # Send the proof to admin
            )
            
        except Exception as e:
            logger.error(f"Error processing payment for user {user_id}: {str(e)}", exc_info=True)
            await update.message.reply_text("An error occurred while processing your payment. Please contact support.")
        
        context.user_data.clear() # Clear user-specific data after purchase
        return ConversationHandler.END # End the conversation


    @staticmethod
    async def cancel_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Cancels the current purchase flow."""
        await update.message.reply_text("Ticket purchase cancelled.", reply_markup=Buttons.main_menu())
        context.user_data.clear()
        return ConversationHandler.END


class AdminSystem:
    @staticmethod
    async def verify_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin payment verification command: /verify <user_id> <ticket_number> <ticket_value>"""
        if str(update.effective_user.id) not in map(str, Config.ADMIN_IDS): # Ensure admin check is string-based
            await update.message.reply_text("❌ Access Denied: You are not authorized to use this command.")
            logger.warning(f"Unauthorized access attempt to /verify by user {update.effective_user.id}")
            return
        
        if len(context.args) != 3:
            await update.message.reply_text("Usage: `/verify <user_id> <ticket_number> <ticket_value>`", parse_mode='MarkdownV2')
            return

        user_id_to_verify = context.args[0]
        ticket_number_to_verify = int(context.args[1])
        ticket_value_to_verify = int(context.args[2])
        
        try:
            transactions_ref = db.collection(get_firestore_collection_path("transactions"))
            # Find the pending transaction
            query = transactions_ref.where("user_id", "==", user_id_to_verify) \
                                   .where("ticket_number", "==", ticket_number_to_verify) \
                                   .where("ticket_value", "==", ticket_value_to_verify) \
                                   .where("status", "==", "pending_verification")
            
            transaction_docs = [doc async for doc in query.stream()]
            
            if not transaction_docs:
                await update.message.reply_text("No pending transaction found for this user and ticket combination.")
                return
            
            # Process the first matching pending transaction
            transaction_doc_ref = transaction_docs[0].reference
            
            # Update transaction status
            await transaction_doc_ref.update({
                "status": "verified",
                "verified_by": str(update.effective_user.id),
                "verified_at": SERVER_TIMESTAMP
            })
            
            # Mark ticket as sold in the tickets_ collection
            await TicketModel.mark_as_sold(ticket_value_to_verify, ticket_number_to_verify, user_id_to_verify, is_free=False, free_reason=None)
            
            # Update user stats
            await UserModel.update_tickets_purchased_count(user_id_to_verify, ticket_value_to_verify)
            
            await update.message.reply_text("✅ Payment verified and ticket activated successfully.")
            logger.info(f"Payment verified by admin {update.effective_user.id} for user {user_id_to_verify}, ticket {ticket_value_to_verify} Birr #{ticket_number_to_verify}.")

            # Notify user that their ticket is verified
            await context.bot.send_message(
                chat_id=user_id_to_verify,
                text=f"🎉 Your <b>{ticket_value_to_verify} Birr</b> ticket number <b>{ticket_number_to_verify}</b> has been verified and is now active!",
                parse_mode='HTML'
            )
            await context.bot.send_message(
                chat_id=Config.CHANNEL_ID,
                text=f"🎟 Ticket Activated: A {ticket_value_to_verify} Birr ticket (Number #{ticket_number_to_verify}) for User ID <code>{user_id_to_verify[:8]}...</code> is now active!"
            )
            
            # Check for loyalty bonus for the user
            user_data = await UserModel.get_or_create(user_id_to_verify, '', '') # Get user data to check purchase count
            purchased_count_for_value = user_data.get('tickets_purchased_count', {}).get(str(ticket_value_to_verify), 0)
            if purchased_count_for_value % Config.LOYALTY_BONUS_PURCHASE_COUNT == 0:
                await LotterySystem.award_loyalty_bonus(user_id_to_verify, ticket_value_to_verify, context)

            # Check for draw condition
            sold_count_query = db.collection(get_firestore_collection_path(f"tickets_{ticket_value_to_verify}")).where("is_sold", "==", True)
            actual_sold_count = len([doc async for doc in sold_count_query.stream()])
            
            if actual_sold_count >= Config.TOTAL_TICKETS_PER_VALUE:
                await LotterySystem.conduct_draw(ticket_value_to_verify, context)
            
        except Exception as e:
            logger.error(f"Verification error by admin {update.effective_user.id}: {str(e)}", exc_info=True)
            await update.message.reply_text(f"Error during verification: {str(e)}")

    @staticmethod
    async def notify_admins(context: ContextTypes.DEFAULT_TYPE, message: str, photo_file_id: Optional[str] = None):
        """Send notification to all admins, optionally with a photo."""
        for admin_id in Config.ADMIN_IDS:
            try:
                if photo_file_id:
                    await context.bot.send_photo(chat_id=admin_id, photo=photo_file_id, caption=message, parse_mode='HTML')
                else:
                    await context.bot.send_message(chat_id=admin_id, text=message, parse_mode='HTML')
            except Exception as e:
                logger.error(f"Failed to notify admin {admin_id}: {str(e)}")

# ==================== BUTTON GENERATORS ====================
class Buttons:
    @staticmethod
    def main_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🎟 Buy Tickets", callback_data="start_purchase")],
            [InlineKeyboardButton("📋 My Tickets", callback_data="my_tickets")],
            # [InlineKeyboardButton("🔔 Notifications", callback_data="notifications")], # Not implemented yet
            [InlineKeyboardButton("👥 Refer Friends", callback_data="refer_friends")],
            [InlineKeyboardButton("ℹ️ Help", callback_data="help")]
        ])

    @staticmethod
    def ticket_values() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{value} Birr", callback_data=f"select_{value}") 
             for value in Config.TICKET_VALUES]
        ])

    @staticmethod
    def help_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📞 Call Support", url=f"tel:{Config.PAYMENT_METHOD['contact_phone']}")],
            [InlineKeyboardButton("📱 Message on Telegram", url=f"https://t.me/{Config.PAYMENT_METHOD['contact_username'].replace('@', '')}")],
            [InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]
        ])

# ==================== APPLICATION SETUP ====================
def create_application() -> Application:
    """Configure and return the Telegram application"""
    application = (
        ApplicationBuilder()
        .token(Config.BOT_TOKEN)
        .concurrent_updates(True) # Process updates concurrently
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    # Register handlers
    application.add_handler(CommandHandler('start', UserHandlers.start))
    application.add_handler(CommandHandler('help', UserHandlers.show_help))
    application.add_handler(CommandHandler('verify', AdminSystem.verify_payment))
    
    # Main menu callback handler (for "🔙 Main Menu" button)
    application.add_handler(CallbackQueryHandler(UserHandlers.start, pattern="^main_menu$"))
    application.add_handler(CallbackQueryHandler(UserHandlers.show_my_tickets, pattern="^my_tickets$"))
    application.add_handler(CallbackQueryHandler(UserHandlers.show_referral_info, pattern="^refer_friends$"))
    application.add_handler(CallbackQueryHandler(UserHandlers.claim_referral_bonus, pattern="^claim_referral_bonus$"))
    application.add_handler(CallbackQueryHandler(UserHandlers.simulate_invite_admin, pattern="^simulate_invite$"))
    application.add_handler(CallbackQueryHandler(UserHandlers.show_help, pattern="^help$")) # For help button from main menu

    # Purchase conversation handler
    purchase_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(PurchaseHandlers.start_purchase, pattern="^start_purchase$"),
            CommandHandler('buy', PurchaseHandlers.start_purchase)
        ],
        states={
            "SELECT_VALUE": [
                CallbackQueryHandler(PurchaseHandlers.select_value, pattern="^select_")
            ],
            "SELECT_NUMBER": [
                CallbackQueryHandler(PurchaseHandlers.select_number, pattern="^number_"),
                CallbackQueryHandler(PurchaseHandlers.start_purchase, pattern="^start_purchase$"), # Allow going back to values
                CallbackQueryHandler(PurchaseHandlers.cancel_purchase, pattern="^cancel_purchase$")
            ],
            "UPLOAD_RECEIPT": [
                MessageHandler(filters.PHOTO | filters.Document.ALL, PurchaseHandlers.process_payment), # Allow any document type, then filter mime_type in handler
                CommandHandler('cancel', PurchaseHandlers.cancel_purchase),
                CallbackQueryHandler(PurchaseHandlers.cancel_purchase, pattern="^cancel_purchase$")
            ]
        },
        fallbacks=[CommandHandler('cancel', PurchaseHandlers.cancel_purchase), CallbackQueryHandler(PurchaseHandlers.cancel_purchase, pattern="^cancel_purchase$")]
    )
    application.add_handler(purchase_handler)
    
    return application

async def on_startup(app: Application) -> None:
    """Run on application startup"""
    logger.info("Bot starting up...")
    await LotterySystem.initialize_tickets()
    await app.bot.set_my_commands([
        ("start", "Start the bot and see main menu"),
        ("buy", "Quickly start purchasing tickets"),
        ("help", "Get help and support information"),
        ("my_tickets", "View your purchased tickets and draw results"),
        ("refer", "Access referral program details")
    ])
    await app.bot.send_message(
        chat_id=Config.CHANNEL_ID,
        text="📢 Lottery Bot is now online!"
    )


async def on_shutdown(app: Application) -> None:
    """Run on application shutdown"""
    logger.info("Bot shutting down...")
    # Attempt to notify admins if the bot token is valid and chat ID exists
    if Config.ADMIN_IDS and Config.BOT_TOKEN:
        try:
            await app.bot.send_message(
                chat_id=Config.ADMIN_IDS[0], # Only notify the first admin for shutdown
                text="⚠️ Lottery Bot is shutting down! Please restart if unexpected."
            )
        except Exception as e:
            logger.error(f"Failed to notify admin on shutdown: {str(e)}")

# ==================== MAIN EXECUTION ====================
if __name__ == '__main__':
    try:
        app = create_application()
        
        # --- PythonAnywhere Specific Run Configuration ---
        # For PythonAnywhere free tier, we MUST use polling.
        # Webhooks are not natively supported without a separate Flask web app setup.
        logger.info("Running bot in polling mode (for PythonAnywhere free tier)...")
        app.run_polling(allowed_updates=Update.ALL_TYPES)
        
    except Exception as e:
        logger.critical(f"Application failed: {str(e)}", exc_info=True)
        # Re-raise to ensure PythonAnywhere sees the error and logs it.
        raise

