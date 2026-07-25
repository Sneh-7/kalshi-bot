"""Telegram notifier + control channel.

Outbound: trade cards, approval requests (with inline Approve/Reject buttons),
status, P&L, and error/kill-switch alerts.

Inbound: a long-poll loop over getUpdates handles /status, /pnl, /positions,
/pause, /resume, /help and the Approve/Reject callback buttons. Approving a
recommendation places the real order via execution.approve_recommendation.

Telegram Bot API is free and pushes straight to your phone. If no token/chat is
configured every send is a no-op log, so the bot runs fine without it.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import func

from ..config import settings
from ..database import session_scope
from ..models import AgentState, Recommendation, Trade

log = logging.getLogger("notifier")

_API = "https://api.telegram.org/bot{token}/{method}"


# --- low-level send ------------------------------------------------------

async def _call(method: str, payload: Dict[str, Any]) -> Optional[dict]:
    if not settings.telegram_enabled:
        log.info("[telegram disabled] %s %s", method, payload.get("text", ""))
        return None
    url = _API.format(token=settings.telegram_bot_token, method=method)
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.post(url, json=payload)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        log.warning("Telegram %s failed: %s", method, e)
        return None


async def send_message(text: str, reply_markup: Optional[dict] = None) -> None:
    payload: Dict[str, Any] = {
        "chat_id": settings.telegram_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    await _call("sendMessage", payload)


def send_message_sync(text: str, reply_markup: Optional[dict] = None) -> None:
    """Fire-and-forget from sync code (schedules on the running loop if any)."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(send_message(text, reply_markup))
    except RuntimeError:
        asyncio.run(send_message(text, reply_markup))


# --- formatted messages --------------------------------------------------

async def recommendation_alert(rec: Recommendation) -> None:
    text = (
        f"🟡 <b>Bet recommendation #{rec.id}</b>\n"
        f"<b>{rec.side} {rec.outcome}</b> — {rec.market_question[:120]}\n"
        f"Ticker: <code>{rec.ticker}</code>\n"
        f"Price: {rec.suggested_price:.2f}  ×{rec.suggested_contracts} "
        f"(${rec.suggested_size_usd:.2f})\n"
        f"Model P: {rec.model_probability:.2f}  net edge: {rec.net_edge:+.3f}\n"
        f"Expires: {rec.expires_at:%H:%M:%S} UTC"
    )
    markup = {
        "inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"approve:{rec.id}"},
            {"text": "❌ Reject", "callback_data": f"reject:{rec.id}"},
        ]]
    }
    await send_message(text, markup)


async def trade_alert(trade: Trade) -> None:
    await send_message(
        f"🟢 <b>{trade.mode} fill</b> {trade.outcome} <code>{trade.ticker}</code>\n"
        f"×{trade.contracts} @ {trade.price:.2f}  fee ${trade.entry_fee_usd:.2f}  "
        f"net edge {trade.net_edge:+.3f}"
    )


async def alert(text: str) -> None:
    await send_message(f"⚠️ {text}")


# --- status / pnl / positions -------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _daily_pnl() -> float:
    cutoff = _now() - timedelta(hours=24)
    with session_scope() as s:
        return float(
            s.query(func.coalesce(func.sum(Trade.net_pnl_usd), 0.0))
            .filter(Trade.created_at >= cutoff)
            .scalar()
            or 0.0
        )


def status_text() -> str:
    with session_scope() as s:
        open_pos = int(
            s.query(func.count(Trade.id))
            .filter(Trade.status == "FILLED", Trade.closed_at.is_(None))
            .scalar() or 0
        )
        pending = int(
            s.query(func.count(Recommendation.id))
            .filter(Recommendation.status == "PENDING")
            .scalar() or 0
        )
        ks = s.get(AgentState, "kill_switch")
        last = s.get(AgentState, "last_loop_at")
        paused = s.get(AgentState, "paused")
    return (
        f"📊 <b>Status</b>\n"
        f"mode: {settings.trading_mode}  "
        f"env: {'DEMO' if settings.is_demo else 'PROD ⚠️'}\n"
        f"kill switch: {(ks.value if ks else 'false')}\n"
        f"paused: {(paused.value if paused else 'false')}\n"
        f"open positions: {open_pos}  pending: {pending}\n"
        f"24h P&L: ${_daily_pnl():+.2f}\n"
        f"last loop: {(last.value if last else 'never')}"
    )


def pnl_text() -> str:
    with session_scope() as s:
        realized = float(
            s.query(func.coalesce(func.sum(Trade.net_pnl_usd), 0.0))
            .filter(Trade.closed_at.isnot(None)).scalar() or 0.0
        )
        unrealized = float(
            s.query(func.coalesce(func.sum(Trade.net_pnl_usd), 0.0))
            .filter(Trade.status == "FILLED", Trade.closed_at.is_(None))
            .scalar() or 0.0
        )
        n = int(s.query(func.count(Trade.id)).scalar() or 0)
    return (
        f"💰 <b>P&L</b>\n"
        f"realized (closed): ${realized:+.2f}\n"
        f"unrealized (open): ${unrealized:+.2f}\n"
        f"total trades: {n}"
    )


def positions_text() -> str:
    with session_scope() as s:
        rows: List[Trade] = (
            s.query(Trade)
            .filter(Trade.status == "FILLED", Trade.closed_at.is_(None))
            .order_by(Trade.created_at.desc())
            .limit(20)
            .all()
        )
        if not rows:
            return "No open positions."
        lines = ["📁 <b>Open positions</b>"]
        for t in rows:
            lines.append(
                f"{t.outcome} <code>{t.ticker[:22]}</code> ×{t.contracts} "
                f"@ {t.price:.2f}  uPnL ${t.net_pnl_usd:+.2f}"
            )
    return "\n".join(lines)


def set_paused(paused: bool) -> None:
    with session_scope() as s:
        row = s.get(AgentState, "paused")
        if row is None:
            s.add(AgentState(key="paused", value="true" if paused else "false"))
        else:
            row.value = "true" if paused else "false"


def is_paused() -> bool:
    with session_scope() as s:
        row = s.get(AgentState, "paused")
        return bool(row and row.value.lower() == "true")


# --- inbound control loop ------------------------------------------------

_HELP = (
    "🤖 <b>kalshi-bot</b>\n"
    "/status – mode, positions, P&L, kill switch\n"
    "/pnl – realized + unrealized P&L\n"
    "/calibration – Brier/log-loss vs market\n"
    "/positions – open positions\n"
    "/pause – stop opening new trades\n"
    "/resume – resume trading\n"
    "/kill – engage the kill switch\n"
    "/help – this message"
)


async def _handle_command(text: str) -> None:
    cmd = text.strip().split()[0].lower().lstrip("/").split("@")[0]
    if cmd == "status":
        await send_message(status_text())
    elif cmd == "pnl":
        await send_message(pnl_text())
    elif cmd == "calibration":
        from . import settlement
        await send_message(settlement.calibration_text())
    elif cmd == "positions":
        await send_message(positions_text())
    elif cmd == "pause":
        set_paused(True)
        await send_message("⏸ Paused. No new trades will open.")
    elif cmd == "resume":
        set_paused(False)
        await send_message("▶️ Resumed.")
    elif cmd == "kill":
        from . import risk
        risk.set_kill_switch(True)
        await send_message("🛑 Kill switch ENGAGED.")
    else:
        await send_message(_HELP)


async def _handle_callback(cb: dict) -> None:
    data = cb.get("data", "")
    cb_id = cb.get("id")
    action, _, rid = data.partition(":")
    from . import execution

    reply = "…"
    try:
        rec_id = int(rid)
    except ValueError:
        await _call("answerCallbackQuery", {"callback_query_id": cb_id, "text": "bad id"})
        return

    if action == "approve":
        trade = await execution.approve_recommendation(rec_id)
        reply = (
            f"✅ Approved #{rec_id} — order placed"
            if trade is not None
            else f"⚠️ #{rec_id} not placed (expired/failed)"
        )
    elif action == "reject":
        execution.reject_recommendation(rec_id)
        reply = f"❌ Rejected #{rec_id}"

    await _call("answerCallbackQuery", {"callback_query_id": cb_id, "text": reply})
    await send_message(reply)


class NotifierControl:
    """Background long-poll loop for inbound Telegram updates."""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._offset: Optional[int] = None

    async def _poll_once(self) -> None:
        payload: Dict[str, Any] = {"timeout": 25}
        if self._offset is not None:
            payload["offset"] = self._offset
        resp = await _call("getUpdates", payload)
        if not resp or not resp.get("ok"):
            return
        for upd in resp.get("result", []):
            self._offset = upd["update_id"] + 1
            try:
                if "callback_query" in upd:
                    await _handle_callback(upd["callback_query"])
                elif "message" in upd and "text" in upd["message"]:
                    await _handle_command(upd["message"]["text"])
            except Exception:
                log.exception("Failed handling Telegram update")

    async def _run(self) -> None:
        # Skip backlog on start: consume pending updates without acting.
        pending = await _call("getUpdates", {"timeout": 0})
        if pending and pending.get("result"):
            self._offset = pending["result"][-1]["update_id"] + 1
        while not self._stop.is_set():
            try:
                await self._poll_once()
            except Exception:
                log.exception("Telegram poll crashed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=settings.telegram_poll_seconds
                )
            except asyncio.TimeoutError:
                pass

    def start(self) -> None:
        if not settings.telegram_enabled:
            log.info("Telegram control not started (no token/chat configured)")
            return
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run())
        log.info("Telegram control loop started")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()


control = NotifierControl()
