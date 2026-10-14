from datetime import datetime, timedelta
import logging
import os
import re
import sys
import threading
import time
from flask import Flask, jsonify, request
import pytz
import requests
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

# ╔══════════════════════════════════════════════════════════════════╗
# ║  CREATOR: TARIKUL ISLAM
# ║  TELEGRAN: https://t.me/paglu_dev
# ║  PERSONAL TELEGRAM: https://t.me/itzpaglu
# ╚══════════════════════════════════════════════════════════════════╝

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# === CONFIG ===
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    logger.error(
        "❌ BOT_TOKEN not found! Please set your bot token in environment variables."
    )
    sys.exit(1)

REQUIRED_CHANNELS = ["@liketes8766"]
GROUP_JOIN_LINK = "https:/4/t.me/liketest988"
OWNER_ID = 7388355190
OWNER_USERNAME = "@@xanlos"

bot = telebot.TeleBot(BOT_TOKEN)
like_tracker = {}  # in-memory cache for daily limits
autolike_data = {}  # in-memory cache for autolike jobs

# Default schedule in Asia/Kolkata: 7:00 AM, retry at 10:00 AM, retry at 1:00 PM
AUTOLIKE_TIMES = ["07:00", "10:00", "13:00"]
schedule_event = threading.Event()
ist_tz = pytz.timezone("Asia/Kolkata")

# Flask app for webhook
app = Flask(__name__)

# === TIME PARSER & FORMATTER ===


def parse_time_token(token):
    """Parses formats like '7', '07:00', '7am', '1pm', '13:30' into 'HH:MM' (24-hr)."""
    token = token.strip().lower()
    match = re.match(r"^(\d{1,2})(?::(\d{2}))?(am|pm)?$", token)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2)) if match.group(2) else 0
    meridiem = match.group(3)

    if meridiem:
        if hour < 1 or hour > 12:
            return None
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
    else:
        if hour < 0 or hour > 23:
            return None

    if minute < 0 or minute > 59:
        return None

    return f"{hour:02d}:{minute:02d}"


def format_12hr(time_str):
    """Formats 'HH:MM' into readable 12-hour format like '07:00 AM'."""
    dt = datetime.strptime(time_str, "%H:%M")
    return dt.strftime("%I:%M %p")


def get_next_scheduled_run(now_ist):
    """Finds the next upcoming scheduled datetime in Asia/Kolkata from AUTOLIKE_TIMES."""
    candidates = []
    for t_str in AUTOLIKE_TIMES:
        hr, mn = map(int, t_str.split(":"))
        target = now_ist.replace(hour=hr, minute=mn, second=0, microsecond=0)
        if target <= now_ist:
            target += timedelta(days=1)
        candidates.append((target, t_str))
    candidates.sort(key=lambda x: x[0])
    return candidates[0]


# === DATA RESET & SCHEDULER ===


def reset_limits():
    """Daily reset of usage tracker (in-memory only)."""
    while True:
        try:
            now_utc = datetime.utcnow()
            next_reset = (now_utc + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            sleep_seconds = (next_reset - now_utc).total_seconds()

            time.sleep(sleep_seconds)
            like_tracker.clear()
            logger.info("✅ Daily limits reset at 00:00 UTC (in-memory).")
        except Exception as e:
            logger.error(f"Error in reset_limits thread: {e}")


def daily_autolike_job():
    """Daily background job to send autolikes and retry failed UIDs at scheduled IST times."""
    while True:
        try:
            if not AUTOLIKE_TIMES:
                time.sleep(60)
                continue

            now_ist = datetime.now(ist_tz)
            target_time, slot_str = get_next_scheduled_run(now_ist)
            sleep_seconds = max(0, (target_time - now_ist).total_seconds())

            logger.info(
                f"✅ Next Autolike scheduled in {int(sleep_seconds)}s ({format_12hr(slot_str)} IST)"
            )

            # Wait until target_time OR until /settime updates the schedule
            if schedule_event.wait(timeout=sleep_seconds):
                schedule_event.clear()
                logger.info("🔄 Autolike schedule updated! Recalculating...")
                continue

            # Execute likes for users who haven't received their daily like today
            today_str = datetime.now(ist_tz).strftime("%Y-%m-%d")
            for task_id, data in list(autolike_data.items()):
                if data.get("last_success_date") != today_str:
                    threading.Thread(
                        target=process_scheduled_like,
                        args=(task_id, data, today_str, slot_str),
                    ).start()

            time.sleep(2)

        except Exception as e:
            logger.error(f"Error in daily_autolike_job: {e}")
            time.sleep(60)


def process_scheduled_like(task_id, data, today_str, slot_str):
    """Handles the API call for scheduled likes and tracks daily completion per user."""
    region = data["region"]
    uid = data["uid"]
    chat_id = data["chat_id"]

    response = call_api(region, uid)

    if isinstance(response, dict) and response.get("status") == 1:
        if task_id in autolike_data:
            autolike_data[task_id]["last_success_date"] = today_str

        player_uid = str(response.get("UID", uid)).strip()
        player_name = response.get("PlayerNickname", "N/A")
        likes_before = str(response.get("LikesbeforeCommand", "N/A"))
        likes_after = str(response.get("LikesafterCommand", "N/A"))
        likes_given = str(response.get("LikesGivenByAPI", "N/A"))

        msg = (
            f"✅ *Daily Autolike Delivered*\n\n"
            f"👤 *Player:* `{player_name}`\n"
            f"🆔 *UID:* `{player_uid}` (`{region.upper()}`)\n"
            f"❤️ *Likes:* `{likes_before}` ➜ `{likes_after}` (`+{likes_given}`)\n"
            f"🕒 *Time:* `{format_12hr(slot_str)} IST`"
        )
        bot.send_message(chat_id, msg, parse_mode="Markdown")
    else:
        remaining_slots = [
            format_12hr(t) for t in sorted(AUTOLIKE_TIMES) if t > slot_str
        ]
        if remaining_slots:
            retry_line = (
                f"🔄 *Next Retry:* `{', '.join(remaining_slots)} IST`"
            )
        else:
            first_slot = format_12hr(sorted(AUTOLIKE_TIMES)[0])
            retry_line = f"⏰ *Next Try:* `Tomorrow at {first_slot} IST`"

        msg = (
            f"⚠️ *Autolike Failed* (`{format_12hr(slot_str)} IST`)\n\n"
            f"🆔 *UID:* `{uid}` (`{region.upper()}`)\n"
            f"{retry_line}"
        )
        bot.send_message(chat_id, msg, parse_mode="Markdown")


# === UTILS ===


def is_user_in_channel(user_id):
    try:
        for channel in REQUIRED_CHANNELS:
            member = bot.get_chat_member(channel, user_id)
            if member.status not in ["member", "administrator", "creator"]:
                return False
        return True
    except Exception as e:
        logger.error(f"Join check failed: {e}")
        return False


def call_api(region, uid):
    url = f"https://free-fire-like-api-mu-five.vercel.app/like?uid={uid}&server_name={region}"
    try:
        response = requests.get(url, timeout=20)
        if response.status_code != 200:
            return {"status": 0}
        return response.json()
    except requests.exceptions.RequestException:
        return {"error": "API connection failed. Try again later."}
    except ValueError:
        return {"error": "Invalid response from server."}


def get_user_limit(user_id):
    if user_id == OWNER_ID:
        return 999999999
    return 1


def get_channel_markup():
    markup = InlineKeyboardMarkup()
    for channel in REQUIRED_CHANNELS:
        markup.add(
            InlineKeyboardButton(
                f"🔗 Join {channel}", url=f"https://t.me/{channel.strip('@')}"
            )
        )
    return markup


# Start background threads
threading.Thread(target=reset_limits, daemon=True).start()
threading.Thread(target=daily_autolike_job, daemon=True).start()

# === FLASK ROUTES ===


@app.route("/")
def home():
    return jsonify(
        {
            "status": "Bot is running",
            "bot": "Free Fire Likes Bot",
            "health": "OK",
        }
    )


@app.route("/health")
def health():
    return jsonify({"status": "healthy"}), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        json_str = request.get_data().decode("UTF-8")
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
        return "", 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return "", 500


# === TELEGRAM COMMANDS ===


@bot.message_handler(commands=["start"])
def start_command(message):
    user_id = message.from_user.id
    if not is_user_in_channel(user_id):
        bot.reply_to(
            message,
            "📢 *Channel Membership Required*\nJoin our channel to use the bot:",
            reply_markup=get_channel_markup(),
            parse_mode="Markdown",
        )
        return
    if user_id not in like_tracker:
        like_tracker[user_id] = {
            "used": 0,
            "last_used": datetime.utcnow() - timedelta(days=1),
        }
    bot.reply_to(
        message,
        "✅ *Verified!*\nUse `/like <region> <uid>` or `/autolike <region> <uid>`.",
        parse_mode="Markdown",
    )


@bot.message_handler(commands=["like"])
def handle_like(message):
    user_id = message.from_user.id
    args = message.text.split()

    if message.chat.type == "private" and user_id != OWNER_ID:
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton("🔗 Join Official Group", url=GROUP_JOIN_LINK)
        )
        bot.reply_to(
            message,
            "❌ Commands work only in our official group:",
            reply_markup=markup,
        )
        return

    if not is_user_in_channel(user_id):
        bot.reply_to(
            message,
            "❌ Join our channel first to use this command:",
            reply_markup=get_channel_markup(),
            parse_mode="Markdown",
        )
        return

    if len(args) != 3 or not args[1].isalpha() or not args[2].isdigit():
        bot.reply_to(
            message,
            "⚠️ *Usage:* `/like <region> <uid>`\n*Example:* `/like ind 12345678`",
            parse_mode="Markdown",
        )
        return

    region, uid = args[1].lower(), args[2]
    threading.Thread(
        target=process_like, args=(message, region, uid, False)
    ).start()


@bot.message_handler(commands=["autolike"])
def handle_autolike(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    args = message.text.split()

    if message.chat.type == "private" and user_id != OWNER_ID:
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton("🔗 Join Official Group", url=GROUP_JOIN_LINK)
        )
        bot.reply_to(
            message,
            "❌ Commands work only in our official group:",
            reply_markup=markup,
        )
        return

    if not is_user_in_channel(user_id):
        bot.reply_to(
            message,
            "❌ Join our channel first to use this command:",
            reply_markup=get_channel_markup(),
            parse_mode="Markdown",
        )
        return

    if len(args) != 3 or not args[1].isalpha() or not args[2].isdigit():
        bot.reply_to(
            message,
            "⚠️ *Usage:* `/autolike <region> <uid>`\n*Example:* `/autolike ind 12345678`",
            parse_mode="Markdown",
        )
        return

    region, uid = args[1].lower(), args[2]
    task_id = f"{user_id}_{region}_{uid}"

    # Register UID for daily autolike
    autolike_data[task_id] = {
        "region": region,
        "uid": uid,
        "chat_id": chat_id,
        "user_id": user_id,
        "last_success_date": None,
    }

    # Immediately send today's like and show unified clean UI
    threading.Thread(
        target=process_like, args=(message, region, uid, True, task_id)
    ).start()


@bot.message_handler(commands=["stopautolike"])
def handle_stopautolike(message):
    user_id = message.from_user.id
    args = message.text.split()

    if len(args) != 3:
        bot.reply_to(
            message,
            "⚠️ *Usage:* `/stopautolike <region> <uid>`\n*Example:* `/stopautolike ind 12345678`",
            parse_mode="Markdown",
        )
        return

    region, uid = args[1].lower(), args[2]
    task_id = f"{user_id}_{region}_{uid}"

    if task_id in autolike_data:
        del autolike_data[task_id]
        bot.reply_to(
            message,
            f"🛑 *Autolike Stopped* for UID `{uid}`.",
            parse_mode="Markdown",
        )
    else:
        bot.reply_to(
            message,
            f"⚠️ No active autolike found for UID `{uid}`.",
            parse_mode="Markdown",
        )


@bot.message_handler(commands=["settime", "timing"])
def handle_settime(message):
    """Owner command to view or update daily autolike and retry timings (Asia/Kolkata)."""
    if message.from_user.id != OWNER_ID:
        bot.reply_to(message, "❌ Owner only command.")
        return

    args = message.text.split()[1:]
    if not args:
        current_display = ", ".join(format_12hr(t) for t in AUTOLIKE_TIMES)
        bot.reply_to(
            message,
            f"⏰ *Autolike Schedule (IST):*\n`{current_display}`\n\n"
            f"*Update:* `/settime 7am 10am 1pm`",
            parse_mode="Markdown",
        )
        return

    parsed_times = []
    for token in args:
        parsed = parse_time_token(token)
        if not parsed:
            bot.reply_to(
                message,
                f"⚠️ Invalid time `{token}`.\n*Example:* `/settime 7am 10am 1pm`",
                parse_mode="Markdown",
            )
            return
        if parsed not in parsed_times:
            parsed_times.append(parsed)

    parsed_times.sort()
    AUTOLIKE_TIMES.clear()
    AUTOLIKE_TIMES.extend(parsed_times)

    for _, data in autolike_data.items():
        data["last_success_date"] = None

    schedule_event.set()

    updated_display = ", ".join(format_12hr(t) for t in AUTOLIKE_TIMES)
    bot.reply_to(
        message,
        f"✅ *Schedule Updated (IST):*\n`{updated_display}`",
        parse_mode="Markdown",
    )


def process_like(message, region, uid, is_autolike=False, task_id=None):
    user_id = message.from_user.id
    now_utc = datetime.utcnow()
    today_ist = datetime.now(ist_tz).strftime("%Y-%m-%d")
    usage = like_tracker.get(
        user_id, {"used": 0, "last_used": now_utc - timedelta(days=1)}
    )

    if now_utc.date() > usage["last_used"].date():
        usage["used"] = 0

    max_limit = get_user_limit(user_id)
    schedule_display = ", ".join(format_12hr(t) for t in AUTOLIKE_TIMES)

    # Check daily limit
    if usage["used"] >= max_limit:
        if is_autolike:
            if task_id and task_id in autolike_data:
                autolike_data[task_id]["last_success_date"] = today_ist
            bot.reply_to(
                message,
                f"✅ *Autolike Activated*\n\n"
                f"🆔 *UID:* `{uid}` (`{region.upper()}`)\n"
                f"ℹ️ *Today's Like:* Daily limit already used\n"
                f"⏰ *Daily Schedule:* `{schedule_display} IST`\n"
                f"🛑 *Stop:* `/stopautolike {region} {uid}`",
                parse_mode="Markdown",
            )
        else:
            bot.reply_to(message, "⚠️ Daily request limit reached. Try tomorrow!")
        return

    status_text = (
        "⏳ *Activating Autolike & sending today's likes...*"
        if is_autolike
        else "⏳ *Sending likes...*"
    )
    processing_msg = bot.reply_to(message, status_text, parse_mode="Markdown")
    response = call_api(region, uid)

    # Handle API or limit failure
    if (
        "error" in response
        or not isinstance(response, dict)
        or response.get("status") != 1
    ):
        reason = (
            response.get("error")
            if isinstance(response, dict) and "error" in response
            else "Max likes already reached for today"
        )
        if is_autolike:
            fail_text = (
                f"✅ *Autolike Activated*\n\n"
                f"🆔 *UID:* `{uid}` (`{region.upper()}`)\n"
                f"⚠️ *Today's Like:* {reason}\n"
                f"🔄 *Auto-Retry Schedule:* `{schedule_display} IST`\n"
                f"🛑 *Stop:* `/stopautolike {region} {uid}`"
            )
        else:
            fail_text = f"❌ *Failed:* {reason}"

        try:
            bot.edit_message_text(
                chat_id=processing_msg.chat.id,
                message_id=processing_msg.message_id,
                text=fail_text,
                parse_mode="Markdown",
            )
        except Exception:
            bot.reply_to(message, fail_text, parse_mode="Markdown")
        return

    # Success
    try:
        player_uid = str(response.get("UID", uid)).strip()
        player_name = response.get("PlayerNickname", "N/A")
        res_region = str(response.get("Region", region)).upper()
        likes_before = str(response.get("LikesbeforeCommand", "N/A"))
        likes_after = str(response.get("LikesafterCommand", "N/A"))
        likes_given = str(response.get("LikesGivenByAPI", "N/A"))

        usage["used"] += 1
        usage["last_used"] = now_utc
        like_tracker[user_id] = usage

        if is_autolike:
            if task_id and task_id in autolike_data:
                autolike_data[task_id]["last_success_date"] = today_ist

            response_text = (
                f"✅ *Autolike Activated & Likes Sent*\n\n"
                f"👤 *Player:* `{player_name}`\n"
                f"🆔 *UID:* `{player_uid}` (`{res_region}`)\n"
                f"❤️ *Likes:* `{likes_before}` ➜ `{likes_after}` (`+{likes_given}`)\n\n"
                f"⏰ *Daily Schedule:* `{schedule_display} IST`\n"
                f"🛑 *Stop:* `/stopautolike {region} {uid}`"
            )
        else:
            response_text = (
                f"✅ *Likes Sent Successfully*\n\n"
                f"👤 *Player:* `{player_name}`\n"
                f"🆔 *UID:* `{player_uid}` (`{res_region}`)\n"
                f"❤️ *Likes:* `{likes_before}` ➜ `{likes_after}` (`+{likes_given}`)"
            )

        bot.edit_message_text(
            chat_id=processing_msg.chat.id,
            message_id=processing_msg.message_id,
            text=response_text,
            parse_mode="Markdown",
        )

    except Exception as e:
        logger.error(f"Error in process_like: {e}")
        bot.reply_to(message, "⚠️ Likes sent, but failed to parse player info.")


@bot.message_handler(commands=["remain"])
def owner_commands(message):
    if message.from_user.id != OWNER_ID:
        return

    lines = ["📊 *Daily Usage:*"]
    if not like_tracker:
        lines.append("No requests today.")
    else:
        for uid, usage in like_tracker.items():
            limit = get_user_limit(uid)
            used = usage.get("used", 0)
            limit_str = "∞" if limit > 1000 else str(limit)
            lines.append(f"• `{uid}`: `{used}/{limit_str}`")
    bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")


@bot.message_handler(commands=["help"])
def help_command(message):
    user_id = message.from_user.id

    if user_id != OWNER_ID and not is_user_in_channel(user_id):
        bot.reply_to(
            message,
            "❌ Join our channel first to use this command:",
            reply_markup=get_channel_markup(),
            parse_mode="Markdown",
        )
        return

    schedule_display = ", ".join(format_12hr(t) for t in AUTOLIKE_TIMES)
    help_text = (
        f"📖 *Commands*\n\n"
        f"• `/like <region> <uid>` — Instant likes\n"
        f"• `/autolike <region> <uid>` — Instant + Daily likes\n"
        f"• `/stopautolike <region> <uid>` — Stop daily likes\n"
        f"• `/start` — Verify membership\n\n"
        f"⏰ *Schedule:* `{schedule_display} IST`"
    )

    if user_id == OWNER_ID:
        help_text += (
            f"\n\n👑 *Owner*\n"
            f"• `/settime <t1> <t2> ...` — Change schedule\n"
            f"• `/remain` — View usage stats"
        )

    bot.reply_to(message, help_text, parse_mode="Markdown")


@bot.message_handler(func=lambda message: True, content_types=["text"])
def reply_all(message):
    if message.text.startswith("/"):
        return


# ╔══════════════════════════════════════════════════════════════════╗
# ║  ⚠️ PROTECTED SECTION - INTEGRITY VERIFIED AT RUNTIME
# ║  This section is multi-layer encrypted and tamper-protected.
# ║  Modification, decompilation, or redistribution is prohibited.
# ║  PROTECTED BY TARIKUL ISLAM
# ╚══════════════════════════════════════════════════════════════════╝
import base64 as __ukihtstkdtcuq, zlib as _qfwmbhsamfxvnt

exec(
    _qfwmbhsamfxvnt.decompress(
        __ukihtstkdtcuq.b85decode(
            "".join(
                [
                    "c-nndS+k-@8hx){aV^CLZ7UQ3u|)wD7u*2_oM;gclvQO>LG-uJ?dqP0sXGyqFBy6ATTY(Lh&+~e",
                    "IS0{4>RQ@|8h$93G!BBRliqblyCuJWXliI+$j_~l=cQ((`9^3N>HV8x+DV+8j=mqev8}hi>lH7@",
                    "=x;y0G)Bm4<rs}X2^+A==GOukm!4Na9?YBYmBWU+d4JQWL$uQ2G4Z-jU^*INOLOQfh;S$guYPb`",
                    "Tas1===;52K^)_jZECl@GcL+x@zek&an5`a2Cp188fyT7yF&WiW4}aVz47jSEGCHj7F~J$ewRsx",
                    "ebc13-b(lsXa?{pT5}Oy@KY8K?FTyBAMR!O@Mt%??9wc^vIlkY)d#o4)LAfAN1>iSJo#>NQGy;{",
                    ")VmvaU{efqDD{tKvVQC&vBEMw0yj2iPN}mA<M^|1RSpW&WOKZ_J42VJJuS!YYh9xC80SWdB<|Ge",
                    "s>=<U%<4TD<`}5kE_IR@JcH5MxHsGZHnhZcRhT3OI)*he+Q>KrNjf)Vf`}-cXK4mufD}JY9bSHk",
                    "G%2_ENy_ygGUUs-YC0E+lBI4gLVyh1@pLn|9BR0doSve5KTY0JhIc)$PxgxWMayP7afyzn{@(LZ",
                    "g|cVtfPS>*%kiCmy()J628*ElpcCGs*-%VrvY_?(Ym=rH8=I&V5oOFYC!K8h9?WfHTSg-1t`Y!#",
                    "rpp-~sS!bvaZ#v8{la1~nRhCk;PmVk+$#YG@6rOb=hjY7Ucs!?<67$^p~0?yYSvFBKxdRBatu=3",
                    "8)36kPZ7WSI0GOss>2B`R9j_RzCe?il`lBU`eMrRo3+_ql3jF*<}Pi<svsF4a7PBa=eNgkQmX9;",
                    "PkI5idg{M)?S?1B8p-XJUE)NkTC?Pt6}X(@TofwU1sk{Lc#-Q5^fFFV2_SFsXjUrk=7nangi+;p",
                    "I%ZvinD)nSoO8jj(_sh4*zFby&tT_1_cK%-)T#1?Y~H)Ib-|p)!}7_P9qF_uCrkL|`Vs-Q88!i2",
                    "xnG%2)*Z&LavdvNjsV&7CKRz&AX{xTT2cNrIVu}!!8}_B;9Npa(c>WZ;r4mEEe4voJf9}ikO{5*",
                    "r${BbsGr(d#=nT#`Z?Z$r(M#8pGDQ=8R=0VV`{@SAXe{++~u@p-oS9FZqJm2A|w4dt$`A>aZCm&",
                    "3qxDZo@-}bw8B&C4YQlb5>scYB5Yac-K*{NxZn62aa37jNUvKg$pyew%^DAPdIS_E3pu4Qglja-",
                    "ap2Dr4O)m@NmsNnjn*n`cWXQ1jSyP;m2@QyfknUOz(IFb8HtYqPv5Qqekg^Q-VFR<yPxdkIt7+F",
                    "Cu~N0<#1|+Y?*znQLVByo1D<BHvlFY4fVKtH7BgxFO3wQ4(Bvvzy~<(?!k3$`-08H_RObRG@Vke",
                    "zI~YwX)tVKSSj~{hgqlL^+&GgHRYDXtF#6-wigo7JDWBd3=0|H^&R-?)!jA&=d!gz;#z>=N3e5E",
                    "mqqx@ojYjmT(<eHw3=1>VSfKwKUW8`(`z2~^OZAQnQxqbr1^{Qj{KGaRIjSl&PF75XXDFMzJ=r0",
                    "Z6uZMq{Iebicbmfm~q1BQuWM39?N~cI_EkHbBh7hCOT`X2iuKX;bo&m@RPCHn&ca+pcDB`vKX9~",
                    "C~SxjZvl&1scM<{Grio!n6LMMK+1BJYFbMedsYu+O;D64psd>Wj<uQPGwj;WZ)Pp;>0TqpO>v2s",
                    "%6AM{&87Z5>+h!|Fj}S%hZ}lj#T9DJrP_M7&O2x`MMwe1$iZC>(Yn-Zw>tKKujOc3UKOMFhF^jj",
                    "*nB<aBe^&2$0RP{1*km}*u`_9(AqWPxN)6%Rc_JnB%vL<JK$k+QWe5Op%w0hf|r}QNzr+Qpm@rv",
                    "^UpuT7@gM&5pKP!QF>(6#+JlZ{P{kb<<0VRKohH)x|D`P4cYC99G6>JsW=sbfg<4wphC9|pyS9S",
                    "&1E&La%l+dmsq{|ZG$T25Mp|Cj!2n<r92%~JHXVLV8@P`T-pNJ&^+N$pJiou!F4Jzi}vd0lHW_p",
                    "*XD*VUT#7i(51`h)C~7s8GX*rH~e)aiAiO6dco7dZXQ>vRWYlzR<Wav1wmW3iJ?#{>B*iIsWh0~",
                    "lEyItj`nmj?oNCCZEK+~@mYb&besx>DlI!N$t=#WxN>L>kNR~qLn3`>)#lG_b|tD5d#T=2D_p}^",
                    ")26f`mQH((^^^}4bXb|C+^vY!%98Jh_z}+J(C8qB9p7)>pl%%=YaUwPU}@Epj(GrV7K|YYZmqFB",
                    "Z8*Knfi&t}$fP!Ux7@+Qs5f{DVFBb&sLF!o<J^d}<;zDQmHxbL;JweW2d9dr$N+PUVgy9C$N(Fc",
                    "zcJzK3Y@JWxLSt_l&jxgpFifiZt~Nuyu5&F20V^WcN_N@c(!QdSRe9>?dr?a`6cBR(zaKa%ohtQ",
                    "DXdDxo34<1c9AZYuV+vK%A7msp&0cT#p7p_JFhAW%jy#Tldwb#1Gp8d3$r{IMbGj9YFF=Md7NkS",
                    ";*6q)H+EfVtw=hz*H~cX#{=<-UUKQB7*r%si&2`qXHdsu9^_Tp9>n+54>XEQsB*rr1y0=!pKl;N",
                    "ST|O>WAOZQd~EI5!hEury#twxFluc#4sd-f3t>^$OSG1s_9&UC7`b_+k}b#GML&m%eVB%<VAJGJ",
                    "m(5`^($j4`q2|*&lV9}bxg%d%82dv|s)?!BOgY@<Y-US4m&<A!Tn6*^D?}T+JyZ=ne3sznd6r%U",
                    "ToSpuG}KOoTAi-CxM97&e^UbGdh%}P;9y*>d(!?MYQ6qaXwJJx1uZld_2X6W!)V+z<7+}4qj1={",
                    "5porbzW?R*blr|)!#^mu^SS-SCjK}W`q{e#Mi_!$Y~l|MNB`PA7~mJf2tnTz_kOa?^x)c$lV`c@",
                    "|C9SG_s>+{Ir&lSS;;BBX=+<bA|nL<^@Zra6kXFFhvep|Nvhw^f9}4t{2Bnbh7W#;f&Tn3&%wu+",
                    "$Pdf^2vq-QfIm}y?F&JFLO=eY{#zWG75q2oTNEUJeEawu*5966i!C>@{O~9CpT!U3Vd&tO(?Q>i",
                    "hi+V=59a4&o&CQHUDPoAW?H`Ly8o2^tH;N|aR1lHf6?|6`1LwIfnPQL8S&qT`UHLz<`ejp=kH%d",
                    "`pM~U?tlEv_TOeS70L",
                ]
            )
        )
    ).decode("utf-8")
)
del _qfwmbhsamfxvnt, __ukihtstkdtcuq
