#!/usr/bin/env python3
"""kalshi-bot — headless entry point.

    python3 main.py            run the agent loop continuously
    python3 main.py --once     run a single tick and exit
    python3 main.py --check    verify configuration and connectivity, no trading

Defaults to PAPER mode against Kalshi's DEMO environment. Reaching production
with real money requires deliberately changing both KALSHI_BASE_URL and
TRADING_MODE.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from kalshi_bot.config import settings
from kalshi_bot.database import init_db


def _setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-14s %(message)s",
        datefmt="%H:%M:%S",
    )


async def _check() -> int:
    """Verify config and connectivity without trading."""
    from kalshi_bot.modules.kalshi import kalshi

    print(f"mode          {settings.trading_mode}")
    print(f"base url      {settings.kalshi_base_url}")
    print(f"environment   {'DEMO' if settings.is_demo else 'PRODUCTION ⚠️'}")
    print(f"database      {settings.database_url}")
    print(f"credentials   {'present' if kalshi.has_credentials else 'absent (market data only)'}")
    print(f"signing prefix{kalshi.path_prefix!r}")
    print(f"rss feeds     {len(settings.rss_list)}")
    print()

    ok = True

    try:
        status = await kalshi.get_exchange_status()
        print(f"✅ exchange status: {status}")
    except Exception as e:
        print(f"❌ exchange status failed: {e}")
        ok = False

    try:
        markets = await kalshi.fetch_open_markets(max_pages=1)
        print(f"✅ fetched {len(markets)} markets")
        if markets:
            m = markets[0]
            print(f"   sample: {m.ticker} bid={m.yes_bid:.2f} ask={m.yes_ask:.2f}")
            book = await kalshi.get_orderbook(m.ticker)
            print(
                f"✅ orderbook: bid={book.best_bid:.2f}({book.bid_depth}) "
                f"ask={book.best_ask:.2f}({book.ask_depth}) valid={book.is_valid}"
            )
    except Exception as e:
        print(f"❌ market data failed: {e}")
        ok = False

    if kalshi.has_credentials:
        try:
            bal = await kalshi.get_balance()
            print(f"✅ balance (auth OK): {bal}")
        except Exception as e:
            print(f"❌ authenticated call failed: {e}")
            print("   If this is a 401, check the signed path includes the API prefix.")
            ok = False
    else:
        print("⏭  skipping authenticated checks (no credentials)")

    # --- optional integrations (informational; don't fail the check) ---
    if settings.truth_social_enabled:
        try:
            from kalshi_bot.modules import ingestion
            posts = await ingestion._fetch_truth_social()
            print(f"✅ Truth Social: {len(posts)} recent posts ({settings.truth_social_provider})")
        except Exception as e:
            print(f"⚠️  Truth Social failed: {e}")
    else:
        print("⏭  Truth Social disabled")

    if settings.telegram_enabled:
        try:
            from kalshi_bot.modules import notifier
            await notifier.send_message("kalshi-bot <code>--check</code> ✅ Telegram OK")
            print("✅ Telegram send OK")
        except Exception as e:
            print(f"⚠️  Telegram failed: {e}")
    else:
        print("⏭  Telegram disabled (set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)")

    if not settings.analyst_enabled:
        print("⏭  Claude analyst disabled (ANALYST_ENABLED=false)")
    elif settings.analyst_provider == "cli":
        import shutil
        binary = shutil.which(settings.claude_cli_path)
        if binary is None:
            print(f"⚠️  Claude CLI '{settings.claude_cli_path}' not found on PATH")
        else:
            try:
                proc = await asyncio.create_subprocess_exec(
                    binary, "-p", "--output-format", "json",
                    "--model", settings.analyst_cli_model,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out, err = await asyncio.wait_for(
                    proc.communicate(input=b"Reply with the word OK."), timeout=60
                )
                if proc.returncode == 0:
                    import json as _json
                    res = _json.loads(out.decode()).get("result", "").strip()[:40]
                    print(f"✅ Claude CLI ({settings.analyst_cli_model}): {res}")
                else:
                    print(f"⚠️  Claude CLI error: {err.decode()[:120]}")
            except Exception as e:
                print(f"⚠️  Claude CLI check failed: {e}")
    elif settings.anthropic_api_key:
        try:
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic(api_key=settings.anthropic_api_key)
            r = await client.messages.create(
                model=settings.analyst_model, max_tokens=16,
                messages=[{"role": "user", "content": "Reply with the word OK."}],
            )
            txt = next((b.text for b in r.content if b.type == "text"), "")
            print(f"✅ Claude analyst API ({settings.analyst_model}): {txt.strip()[:40]}")
        except Exception as e:
            print(f"⚠️  Claude API ping failed: {e}")
    else:
        print("⏭  Claude analyst provider=claude but ANTHROPIC_API_KEY unset")

    return 0 if ok else 1


async def _watchdog() -> None:
    """Alert over Telegram if the agent loop stops ticking."""
    from datetime import datetime, timezone

    from kalshi_bot.database import session_scope
    from kalshi_bot.models import AgentState
    from kalshi_bot.modules import notifier

    if settings.heartbeat_minutes <= 0:
        return
    alerted = False
    wd_log = logging.getLogger("watchdog")
    while True:
        await asyncio.sleep(60)
        try:
            with session_scope() as s:
                row = s.get(AgentState, "last_loop_at")
            if not row or not row.value:
                continue
            last = datetime.fromisoformat(row.value)
            age_min = (datetime.now(timezone.utc) - last).total_seconds() / 60.0
            if age_min > settings.heartbeat_minutes:
                if not alerted:
                    await notifier.alert(
                        f"No loop tick for {age_min:.0f} min — agent may be stuck."
                    )
                    alerted = True
            else:
                alerted = False
        except Exception:
            wd_log.exception("watchdog error")


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Kalshi sentiment trading agent")
    parser.add_argument("--once", action="store_true", help="run a single tick and exit")
    parser.add_argument("--check", action="store_true", help="verify config/connectivity")
    args = parser.parse_args()

    _setup_logging()

    if args.check:
        return await _check()

    init_db()

    if settings.trading_mode == "LIVE" and not settings.is_demo:
        logging.warning("=" * 62)
        logging.warning("LIVE MODE ON PRODUCTION — real money is at risk")
        logging.warning("=" * 62)

    from kalshi_bot.modules import notifier
    from kalshi_bot.orchestrator import agent_loop, run_once

    if args.once:
        await run_once()
        return 0

    agent_loop.start()
    notifier.control.start()
    await notifier.send_message(
        f"🚀 kalshi-bot started — {settings.trading_mode} on "
        f"{'DEMO' if settings.is_demo else 'PRODUCTION ⚠️'}"
    )
    watchdog = asyncio.create_task(_watchdog())
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        watchdog.cancel()
        await agent_loop.stop()
        await notifier.control.stop()
        await notifier.send_message("🛑 kalshi-bot stopped")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_main()))
    except KeyboardInterrupt:
        sys.exit(130)
