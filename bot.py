"""
bot.py
Telegram bot for tracking a DSE (Dhaka Stock Exchange) portfolio.

Access control (NEW):
  - Only the Telegram user ID in AUTHORIZED_USER_ID can use this bot at all.
    Everyone else is rejected on first contact, no matter what they send.
  - Even the authorized user must send /start and then type the correct
    password (BOT_PASSWORD) before any command or button works. This
    authentication is held in memory only — it resets whenever the bot
    process restarts, so you'll need to /start + enter the password again
    after a redeploy or crash.

Interactive flows (via buttons):
  🟢 Buy   → asks code → asks qty → asks price → logs buy
  🔴 Sell  → shows held codes → asks qty → asks price → logs sell
  💰 Price → shows live prices for portfolio codes + "Other" button
  🔍 Other → asks for a specific code → shows its price

Slash commands (still available):
  /buy CODE QTY PRICE
  /sell CODE QTY PRICE
  /portfolio
  /price CODE
  /alert CODE above|below PRICE
  /alerts
  /report
  /history [CODE]
  /cancel
  /help

Setup:
  1. pip install -r requirements.txt
  2. Create a bot with @BotFather, copy the token
  3. Copy .env.example to .env and fill in:
       BOT_TOKEN=...
       CHAT_ID=...                (optional, for alert push notifications)
       AUTHORIZED_USER_ID=...     (your numeric Telegram user ID — get it
                                    from @userinfobot or similar)
       BOT_PASSWORD=...           (a password only you know)
  4. python bot.py
"""

from __future__ import annotations

import logging
import os
from typing import Any

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from alerts import AlertStore
from dse_client import get_price, fetch_all_prices, search_codes
from portfolio import Portfolio

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
ALERT_CHECK_INTERVAL = int(os.getenv("ALERT_CHECK_INTERVAL_SECONDS", "300"))

_raw_authorized_id = os.getenv("AUTHORIZED_USER_ID", "").strip()
AUTHORIZED_USER_ID = int(_raw_authorized_id) if _raw_authorized_id else None
BOT_PASSWORD = os.getenv("BOT_PASSWORD")

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", level=logging.INFO
)
logger = logging.getLogger("dse-bot")

portfolio = Portfolio()
alert_store = AlertStore()


# ---------------------------------------------------------------- menu / keyboards

BTN_BUY = "🟢 Buy"
BTN_SELL = "🔴 Sell"
BTN_PRICE = "💰 Price"
BTN_PORTFOLIO = "📊 Portfolio"
BTN_REPORT = "📈 Report"
BTN_ALERTS = "🔔 Alerts"
BTN_HISTORY = "📜 History"
BTN_HELP = "❓ Help"
BTN_CANCEL = "❌ Cancel"

MAIN_MENU_KEYBOARD = ReplyKeyboardMarkup(
    [
        [BTN_BUY, BTN_SELL],
        [BTN_PRICE, BTN_PORTFOLIO],
        [BTN_REPORT, BTN_ALERTS],
        [BTN_HISTORY, BTN_HELP],
    ],
    resize_keyboard=True,
)

CANCEL_KEYBOARD = ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True)


def refresh_button(callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔄 Refresh", callback_data=callback_data)]]
    )


# ---------------------------------------------------------------- helpers

def fmt_money(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"৳{v:,.2f}"


def pl_emoji(v: float) -> str:
    return "🟢" if v >= 0 else "🔴"


def clear_flow(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Wipe any in-progress interactive flow for this user."""
    for key in ("flow", "flow_code", "flow_qty"):
        context.user_data.pop(key, None)


async def cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_flow(context)
    await update.message.reply_text(
        "❌ Cancelled.", reply_markup=MAIN_MENU_KEYBOARD
    )


# ---------------------------------------------------------------- access control (NEW)

def _is_authorized_id(user_id: int | None) -> bool:
    return AUTHORIZED_USER_ID is not None and user_id == AUTHORIZED_USER_ID


def _is_authenticated(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return bool(context.user_data.get("authenticated"))


def _is_start_command(update: Update) -> bool:
    if not update.message or not update.message.text:
        return False
    first_word = update.message.text.strip().split()[0]
    # Matches "/start" and "/start@YourBotName"
    return first_word.lstrip("/").split("@")[0].lower() == "start"


async def auth_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Runs before every other handler (added in an earlier handler group).
    Blocks the update (raises ApplicationHandlerStop) unless the sender is
    the authorized user AND has already entered the correct password this
    session — with a couple of narrow exceptions to let /start and the
    password itself through.
    """
    user = update.effective_user
    if user is None:
        return  # not a user-originated update (e.g. a poll answer, etc.)

    # --- Reject anyone who isn't the authorized Telegram user ID ---
    if not _is_authorized_id(user.id):
        logger.warning("Blocked access attempt from user_id=%s (@%s)", user.id, user.username)
        if update.callback_query:
            await update.callback_query.answer("⛔ You are not authorized to use this bot.", show_alert=True)
        elif update.message:
            await update.message.reply_text("⛔ You are not authorized to use this bot.")
        raise ApplicationHandlerStop

    # --- Authorized user, already authenticated this session: let it through ---
    if _is_authenticated(context):
        return

    # --- Authorized user, not yet authenticated: only a few things are allowed ---
    if _is_start_command(update):
        return  # let cmd_start handle prompting for the password

    if context.user_data.get("awaiting_password") and update.message and update.message.text:
        submitted = update.message.text.strip()
        if BOT_PASSWORD and submitted == BOT_PASSWORD:
            context.user_data["authenticated"] = True
            context.user_data.pop("awaiting_password", None)
            await update.message.reply_text(
                "✅ Access granted. Welcome!", reply_markup=MAIN_MENU_KEYBOARD
            )
        else:
            await update.message.reply_text(
                "❌ Incorrect password. Try again, or send /start to restart."
            )
        raise ApplicationHandlerStop  # never let the password text itself fall through

    # --- Anything else while unauthenticated: block with a reminder ---
    if update.callback_query:
        await update.callback_query.answer(
            "🔒 Please send /start and enter the password first.", show_alert=True
        )
    elif update.message:
        await update.message.reply_text(
            "🔒 Please send /start and enter the password to use this bot."
        )
    raise ApplicationHandlerStop


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry point — prompts for the password if not yet authenticated this session."""
    if _is_authenticated(context):
        await cmd_help(update, context)
        return
    clear_flow(context)
    context.user_data["awaiting_password"] = True
    await update.message.reply_text("🔐 Please enter the password to access this bot:")


# ---------------------------------------------------------------- commands (slash)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_flow(context)
    await update.message.reply_text(
        "*DSE Portfolio Bot*\n\n"
        "Tap the buttons below for guided flows, or use slash commands:\n\n"
        "/buy CODE QTY PRICE\n"
        "/sell CODE QTY PRICE\n"
        "/portfolio\n"
        "/price CODE\n"
        "/alert CODE above|below PRICE\n"
        "/alerts\n"
        "/report\n"
        "/history [CODE]\n"
        "/cancel — cancel current flow\n",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=MAIN_MENU_KEYBOARD,
    )


async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_flow(context)
    try:
        code = context.args[0].upper()
        qty = float(context.args[1])
        price = float(context.args[2])
    except (IndexError, ValueError):
        await update.message.reply_text(
            "Usage: /buy CODE QTY PRICE  e.g. /buy BRACBANK 100 45.5"
        )
        return
    try:
        tx = portfolio.buy(code, qty, price)
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return
    await update.message.reply_text(
        f"✅ Bought {tx.qty:g} {tx.code} @ {fmt_money(tx.price)} "
        f"(total {fmt_money(tx.total)})",
        reply_markup=MAIN_MENU_KEYBOARD,
    )


async def cmd_sell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_flow(context)
    try:
        code = context.args[0].upper()
        qty = float(context.args[1])
        price = float(context.args[2])
    except (IndexError, ValueError):
        await update.message.reply_text(
            "Usage: /sell CODE QTY PRICE  e.g. /sell BRACBANK 50 48.0"
        )
        return
    try:
        tx = portfolio.sell(code, qty, price)
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return
    await update.message.reply_text(
        f"✅ Sold {tx.qty:g} {tx.code} @ {fmt_money(tx.price)} "
        f"(total {fmt_money(tx.total)})",
        reply_markup=MAIN_MENU_KEYBOARD,
    )


# ---------------------------------------------------------------- portfolio / price builders

def build_portfolio_text() -> str | None:
    holdings = portfolio.get_holdings()
    if not holdings:
        return None

    live_prices = fetch_all_prices()

    lines = ["*Your Portfolio*\n"]
    total_cost = 0.0
    total_value = 0.0

    for code, h in sorted(holdings.items()):
        live = live_prices.get(code)
        ltp = live.ltp if live else None
        cost = h.qty * h.avg_cost
        total_cost += cost
        if ltp is not None:
            value = h.qty * ltp
            total_value += value
            upl = value - cost
            upl_pct = (upl / cost * 100) if cost else 0
            lines.append(
                f"{pl_emoji(upl)} *{code}*: {h.qty:g} @ avg {fmt_money(h.avg_cost)} "
                f"→ LTP {fmt_money(ltp)}  ({upl_pct:+.1f}%, {fmt_money(upl)})"
            )
        else:
            lines.append(
                f"⚪ *{code}*: {h.qty:g} @ avg {fmt_money(h.avg_cost)} (no live price)"
            )

    total_upl = total_value - total_cost
    lines.append("")
    lines.append(f"Cost: {fmt_money(total_cost)}  |  Value: {fmt_money(total_value)}")
    lines.append(f"{pl_emoji(total_upl)} Unrealized P/L: {fmt_money(total_upl)}")

    return "\n".join(lines)


def build_price_text(code: str) -> str:
    p = get_price(code)
    if p is None:
        matches = search_codes(code)
        suggestion = f"\nDid you mean: {', '.join(matches)}?" if matches else ""
        return f"No data for {code}.{suggestion}"
    change_str = f"{p.change:+.2f}" if p.change is not None else "n/a"
    change_pct_str = f"({p.change_pct:+.1f}%)" if p.change_pct is not None else ""
    change_line = " ".join(filter(None, [change_str, change_pct_str])) or "n/a"
    return (
        f"*{p.trading_code}*\nLTP: {fmt_money(p.ltp)}\nChange: {change_line}\n"
        f"High: {fmt_money(p.high)}  Low: {fmt_money(p.low)}"
    )


def build_portfolio_prices_text() -> str:
    """Live prices for codes currently held in the portfolio."""
    holdings = portfolio.get_holdings()
    if not holdings:
        return "You don't hold any positions yet. Use /buy or tap 🟢 Buy."

    try:
        live_prices = fetch_all_prices()
    except Exception as e:
        logger.warning("price fetch failed: %s", e)
        return "⚠️ Couldn't fetch live prices right now. Try again shortly."

    lines = ["*💰 Prices of your holdings*\n"]
    for code in sorted(holdings.keys()):
        p = live_prices.get(code)
        if not p:
            lines.append(f"⚪ *{code}*: n/a")
            continue
        change = f"{p.change:+.2f}" if p.change is not None else "n/a"
        pct = f"({p.change_pct:+.1f}%)" if p.change_pct is not None else ""
        arrow = "🟢" if (p.change or 0) >= 0 else "🔴"
        lines.append(f"{arrow} *{code}*: {fmt_money(p.ltp)}  {change} {pct}")

    lines.append("\nTap *🔍 Other* below to look up a different code.")
    return "\n".join(lines)


def price_other_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔍 Other (enter a code)", callback_data="price_other")]]
    )


# ---------------------------------------------------------------- portfolio / price commands

async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_flow(context)
    try:
        text = build_portfolio_text()
    except Exception as e:
        logger.exception("price fetch failed")
        await update.message.reply_text(f"⚠️ Couldn't fetch live prices: {e}")
        return
    if text is None:
        await update.message.reply_text(
            "You don't have any open positions yet. Try 🟢 Buy.",
            reply_markup=MAIN_MENU_KEYBOARD,
        )
        return
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=refresh_button("refresh_portfolio"),
    )


async def cb_refresh_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        text = build_portfolio_text()
    except Exception as e:
        await query.answer(f"⚠️ Couldn't fetch live prices: {e}", show_alert=True)
        return
    if text is None:
        await query.answer("No open positions.", show_alert=True)
        return
    await query.answer("Refreshed ✅")
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=refresh_button("refresh_portfolio"),
    )


async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_flow(context)
    if not context.args:
        # Same behavior as pressing the Price button
        await show_portfolio_prices(update, context)
        return
    code = context.args[0].upper()
    try:
        text = build_price_text(code)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Couldn't fetch prices: {e}")
        return
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=refresh_button(f"refresh_price:{code}"),
    )


async def show_portfolio_prices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shared by the 💰 Price button and bare /price command."""
    text = build_portfolio_prices_text()
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=price_other_keyboard(),
    )


async def cb_refresh_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    code = query.data.split(":", 1)[1]
    try:
        text = build_price_text(code)
    except Exception as e:
        await query.answer(f"⚠️ Couldn't fetch prices: {e}", show_alert=True)
        return
    await query.answer("Refreshed ✅")
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=refresh_button(f"refresh_price:{code}"),
    )


# ---------------------------------------------------------------- BUY FLOW (guided)

async def start_buy_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_flow(context)
    context.user_data["flow"] = "buy_code"
    await update.message.reply_text(
        "🟢 *Buy — Step 1 of 3*\n\nSend the stock *code* (e.g. `BRACBANK`).",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=CANCEL_KEYBOARD,
    )


async def buy_got_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    code = update.message.text.strip().upper()
    if not code:
        await update.message.reply_text("Please send a valid code.")
        return
    context.user_data["flow_code"] = code
    context.user_data["flow"] = "buy_qty"
    await update.message.reply_text(
        f"🟢 *Buy {code} — Step 2 of 3*\n\nSend the *quantity* (e.g. `100`).",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=CANCEL_KEYBOARD,
    )


async def buy_got_qty(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        qty = float(update.message.text.strip())
        if qty <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please send a positive number for quantity.")
        return
    context.user_data["flow_qty"] = qty
    context.user_data["flow"] = "buy_price"
    await update.message.reply_text(
        f"🟢 *Buy {context.user_data['flow_code']} x {qty:g} — Step 3 of 3*\n\n"
        f"Send the *price per share* (e.g. `45.5`).",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=CANCEL_KEYBOARD,
    )


async def buy_got_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        price = float(update.message.text.strip())
        if price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please send a positive number for price.")
        return

    code = context.user_data.get("flow_code")
    qty = context.user_data.get("flow_qty")
    clear_flow(context)

    try:
        tx = portfolio.buy(code, qty, price)
    except ValueError as e:
        await update.message.reply_text(
            f"⚠️ {e}", reply_markup=MAIN_MENU_KEYBOARD
        )
        return

    await update.message.reply_text(
        f"✅ Bought {tx.qty:g} {tx.code} @ {fmt_money(tx.price)} "
        f"(total {fmt_money(tx.total)})",
        reply_markup=MAIN_MENU_KEYBOARD,
    )


# ---------------------------------------------------------------- SELL FLOW (guided)

async def start_sell_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_flow(context)
    holdings = portfolio.get_holdings()
    if not holdings:
        await update.message.reply_text(
            "You don't hold any positions to sell.",
            reply_markup=MAIN_MENU_KEYBOARD,
        )
        return

    buttons = [
        [InlineKeyboardButton(f"{code}  ({h.qty:g})", callback_data=f"sell_pick:{code}")]
        for code, h in sorted(holdings.items())
    ]
    await update.message.reply_text(
        "🔴 *Sell — pick a code*\n\nChoose one of your holdings:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cb_sell_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    code = query.data.split(":", 1)[1]

    holdings = portfolio.get_holdings()
    h = holdings.get(code)
    if not h:
        await query.answer("You no longer hold this code.", show_alert=True)
        return

    context.user_data["flow"] = "sell_qty"
    context.user_data["flow_code"] = code
    await query.answer()
    await query.edit_message_text(
        f"🔴 *Sell {code}* (you hold {h.qty:g})\n\n"
        f"Step 1 of 2 — send the *quantity* to sell.",
        parse_mode=ParseMode.MARKDOWN,
    )
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=f"Type the quantity to sell for *{code}*:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=CANCEL_KEYBOARD,
    )


async def sell_got_qty(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        qty = float(update.message.text.strip())
        if qty <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please send a positive number for quantity.")
        return
    context.user_data["flow_qty"] = qty
    context.user_data["flow"] = "sell_price"
    code = context.user_data["flow_code"]
    await update.message.reply_text(
        f"🔴 *Sell {code} x {qty:g} — Step 2 of 2*\n\n"
        f"Send the *price per share* (e.g. `48.0`).",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=CANCEL_KEYBOARD,
    )


async def sell_got_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        price = float(update.message.text.strip())
        if price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please send a positive number for price.")
        return

    code = context.user_data.get("flow_code")
    qty = context.user_data.get("flow_qty")
    clear_flow(context)

    try:
        tx = portfolio.sell(code, qty, price)
    except ValueError as e:
        await update.message.reply_text(
            f"⚠️ {e}", reply_markup=MAIN_MENU_KEYBOARD
        )
        return

    await update.message.reply_text(
        f"✅ Sold {tx.qty:g} {tx.code} @ {fmt_money(tx.price)} "
        f"(total {fmt_money(tx.total)})",
        reply_markup=MAIN_MENU_KEYBOARD,
    )


# ---------------------------------------------------------------- OTHER (from Price) FLOW

async def start_other_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_flow(context)
    context.user_data["flow"] = "price_other"
    await update.message.reply_text(
        "🔍 Send the stock *code* you want a price for (e.g. `GP`).",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=CANCEL_KEYBOARD,
    )


async def price_other_got_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    code = update.message.text.strip().upper()
    clear_flow(context)
    if not code:
        await update.message.reply_text(
            "Please send a valid code.", reply_markup=MAIN_MENU_KEYBOARD
        )
        return
    try:
        text = build_price_text(code)
    except Exception as e:
        await update.message.reply_text(
            f"⚠️ Couldn't fetch prices: {e}", reply_markup=MAIN_MENU_KEYBOARD
        )
        return
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=refresh_button(f"refresh_price:{code}"),
    )


# ---------------------------------------------------------------- alerts / report / history

async def cmd_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_flow(context)
    try:
        code, direction, price = context.args[0], context.args[1].lower(), float(context.args[2])
        if direction not in ("above", "below"):
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text(
            "Usage: /alert CODE above|below PRICE  e.g. /alert BRACBANK above 50"
        )
        return
    alert_store.add(code, direction, price)
    await update.message.reply_text(
        f"🔔 Alert set: {code.upper()} {direction} {fmt_money(price)}"
    )


def build_alerts_view() -> tuple[str, InlineKeyboardMarkup | None]:
    active = alert_store.list_active()
    if not active:
        return "No active alerts.", None
    text = "*Active alerts*\nTap ❌ to remove one."
    buttons = [
        [
            InlineKeyboardButton(
                f"❌ {a.code} {a.direction} {fmt_money(a.target_price)}",
                callback_data=f"del_alert:{i}",
            )
        ]
        for i, a in enumerate(active)
    ]
    return text, InlineKeyboardMarkup(buttons)


async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_flow(context)
    text, markup = build_alerts_view()
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)


async def cb_delete_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    index = int(query.data.split(":", 1)[1])
    active = alert_store.list_active()
    if index >= len(active):
        await query.answer("That alert is gone already.", show_alert=True)
    else:
        target = active[index]
        if hasattr(alert_store, "remove"):
            alert_store.remove(target)
            await query.answer("Alert removed ✅")
        else:
            await query.answer(
                "Can't delete yet — AlertStore needs a remove() method.", show_alert=True
            )

    text, markup = build_alerts_view()
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)


def build_report_text() -> str:
    holdings = portfolio.get_holdings()
    realized = portfolio.get_realized_pl_total()

    try:
        live_prices = fetch_all_prices()
    except Exception:
        live_prices = {}

    unrealized = 0.0
    for code, h in holdings.items():
        live = live_prices.get(code)
        if live:
            unrealized += (live.ltp - h.avg_cost) * h.qty

    total_tx = len(portfolio.get_transactions())
    lines = [
        "*Portfolio Report*",
        f"Open positions: {len(holdings)}",
        f"Total transactions logged: {total_tx}",
        "",
        f"Realized P/L: {fmt_money(realized)}",
        f"Unrealized P/L: {fmt_money(unrealized)}",
        f"{pl_emoji(realized + unrealized)} Total P/L: {fmt_money(realized + unrealized)}",
    ]
    return "\n".join(lines)


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_flow(context)
    await update.message.reply_text(
        build_report_text(),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=refresh_button("refresh_report"),
    )


async def cb_refresh_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Refreshed ✅")
    await query.edit_message_text(
        build_report_text(),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=refresh_button("refresh_report"),
    )


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_flow(context)
    code = context.args[0].upper() if context.args else None
    txs = portfolio.get_transactions(code=code, limit=15)
    if not txs:
        await update.message.reply_text("No transactions found.")
        return
    lines = [
        f"{t.ts}  {t.side.upper():4}  {t.code:10}  {t.qty:g} @ {fmt_money(t.price)}"
        for t in txs
    ]
    await update.message.reply_text(
        "```\n" + "\n".join(lines) + "\n```", parse_mode=ParseMode.MARKDOWN
    )


# ---------------------------------------------------------------- routers

async def cb_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline-button dispatcher."""
    data = update.callback_query.data or ""

    if data == "refresh_portfolio":
        await cb_refresh_portfolio(update, context)
    elif data.startswith("refresh_price:"):
        await cb_refresh_price(update, context)
    elif data == "refresh_report":
        await cb_refresh_report(update, context)
    elif data.startswith("del_alert:"):
        await cb_delete_alert(update, context)
    elif data.startswith("sell_pick:"):
        await cb_sell_pick(update, context)
    elif data == "price_other":
        await update.callback_query.answer()
        await start_other_flow_cb(update, context)
    elif data == "show_alerts":
        query = update.callback_query
        await query.answer()
        text, markup = build_alerts_view()
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=markup,
        )
    else:
        await update.callback_query.answer()


async def start_other_flow_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Same as start_other_flow but called from a callback query."""
    clear_flow(context)
    context.user_data["flow"] = "price_other"
    query = update.callback_query
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=(
            "🔍 Send the stock *code* you want a price for (e.g. `GP`)."
        ),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=CANCEL_KEYBOARD,
    )


async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply-keyboard menu dispatcher."""
    text = update.message.text

    if text == BTN_CANCEL:
        await cancel_flow(update, context)
        return

    if text == BTN_BUY:
        await start_buy_flow(update, context)
    elif text == BTN_SELL:
        await start_sell_flow(update, context)
    elif text == BTN_PRICE:
        clear_flow(context)
        await show_portfolio_prices(update, context)
    elif text == BTN_PORTFOLIO:
        await cmd_portfolio(update, context)
    elif text == BTN_REPORT:
        await cmd_report(update, context)
    elif text == BTN_ALERTS:
        await cmd_alerts(update, context)
    elif text == BTN_HISTORY:
        await cmd_history(update, context)
    elif text == BTN_HELP:
        await cmd_help(update, context)


async def flow_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """If a guided flow is in progress, route the typed text to the right step."""
    flow = context.user_data.get("flow")

    if flow == "buy_code":
        await buy_got_code(update, context)
    elif flow == "buy_qty":
        await buy_got_qty(update, context)
    elif flow == "buy_price":
        await buy_got_price(update, context)
    elif flow == "sell_qty":
        await sell_got_qty(update, context)
    elif flow == "sell_price":
        await sell_got_price(update, context)
    elif flow == "price_other":
        await price_other_got_code(update, context)
    # else: no flow active — ignore


# ---------------------------------------------------------------- alert job

async def check_alerts_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    active = alert_store.list_active()
    if not active:
        return
    try:
        prices = fetch_all_prices()
    except Exception as e:
        logger.warning("Alert check: price fetch failed: %s", e)
        return

    for alert in active:
        live = prices.get(alert.code)
        if not live:
            continue
        hit = (
            (alert.direction == "above" and live.ltp >= alert.target_price)
            or (alert.direction == "below" and live.ltp <= alert.target_price)
        )
        if hit:
            alert_store.mark_triggered(alert)
            if CHAT_ID:
                await context.bot.send_message(
                    chat_id=CHAT_ID,
                    text=(
                        f"🔔 *{alert.code}* is now {fmt_money(live.ltp)} "
                        f"({alert.direction} your target {fmt_money(alert.target_price)})"
                    ),
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("🔔 View alerts", callback_data="show_alerts")]]
                    ),
                )


# ---------------------------------------------------------------- main

def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN in your .env file first (see .env.example).")
    if AUTHORIZED_USER_ID is None:
        raise SystemExit(
            "Set AUTHORIZED_USER_ID in your .env file to your numeric Telegram "
            "user ID (e.g. from @userinfobot) before running this bot."
        )
    if not BOT_PASSWORD:
        raise SystemExit("Set BOT_PASSWORD in your .env file before running this bot.")

    app = Application.builder().token(BOT_TOKEN).build()

    # --- Access control gate — runs BEFORE everything else below (group=-1) ---
    app.add_handler(MessageHandler(filters.ALL, auth_guard), group=-1)
    app.add_handler(CallbackQueryHandler(auth_guard), group=-1)

    # --- Slash commands ---
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("buy", cmd_buy))
    app.add_handler(CommandHandler("sell", cmd_sell))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("cancel", cancel_flow))
    app.add_handler(CommandHandler("alert", cmd_alert))
    app.add_handler(CommandHandler("alerts", cmd_alerts))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("history", cmd_history))

    # --- Inline buttons ---
    app.add_handler(CallbackQueryHandler(cb_router))

    # --- Reply-keyboard menu (Buy, Sell, Price, Portfolio, ...) ---
    menu_labels = "|".join(
        [
            BTN_BUY, BTN_SELL, BTN_PRICE, BTN_PORTFOLIO,
            BTN_REPORT, BTN_ALERTS, BTN_HISTORY, BTN_HELP, BTN_CANCEL,
        ]
    )
    app.add_handler(MessageHandler(filters.Regex(f"^({menu_labels})$"), menu_router))

    # --- Free-text step handler for in-progress flows (LAST) ---
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, flow_text_router)
    )

    # --- Alert job ---
    if app.job_queue:
        app.job_queue.run_repeating(
            check_alerts_job, interval=ALERT_CHECK_INTERVAL, first=15
        )
    else:
        logger.warning(
            "JobQueue not available — install with: pip install "
            "\"python-telegram-bot[job-queue]\" to enable price alerts."
        )

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()