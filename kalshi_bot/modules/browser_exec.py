"""Browser fallback execution channel ("Claude-in-Chrome" / Playwright).

A GUARDED SAFETY NET. It is used only when the Kalshi API order path fails
(execution._place_with_fallback). It drives a logged-in Kalshi web session with
Playwright to place an order the API couldn't. It is inherently slower and more
brittle than the API — the site's DOM can change at any time — so it is NEVER
the primary path and is disabled by default.

Setup:
  1. pip install playwright==1.48.0 && python -m playwright install chromium
  2. Log into Kalshi once and save the session:
       python -m kalshi_bot.modules.browser_exec --login
     (opens a browser; log in, then press Enter to save storage state)
  3. Set BROWSER_EXEC_ENABLED=true. Keep BROWSER_DRY_RUN=true until you have
     confirmed the click-through works on your account, then set it false.

While BROWSER_DRY_RUN is true, place_order navigates and locates the controls
but stops BEFORE the final submit, returning None (so the API remains the only
channel that can actually trade). This prevents an untested selector path from
firing real orders.

Selectors are intentionally centralized in _SELECTORS and matched by role/text
so they are easy to re-tune when Kalshi changes its UI.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from ..config import settings
from ..fees import fee_for_order
from ..models import Trade
from .risk import TradePlan

log = logging.getLogger("browser")

# Text/role hints for the order ticket. Re-tune these if Kalshi changes its UI.
_SELECTORS = {
    "yes_button": "Yes",
    "no_button": "No",
    "quantity": "Quantity",
    "review": "Review",
    "submit": "Submit",
    "confirmation": "Order placed",
}


def _available() -> bool:
    if not settings.browser_exec_enabled:
        return False
    try:
        import playwright  # noqa: F401
    except ImportError:
        log.warning(
            "Browser fallback enabled but `playwright` is not installed. "
            "pip install playwright==1.48.0 && python -m playwright install chromium"
        )
        return False
    if not os.path.exists(settings.browser_storage_state_path):
        log.warning(
            "Browser fallback enabled but no saved session at %s. Run "
            "`python -m kalshi_bot.modules.browser_exec --login` first.",
            settings.browser_storage_state_path,
        )
        return False
    return True


def _build_trade(plan: TradePlan, status: str, note: str) -> Trade:
    fill = min(max(plan.entry_price, 0.01), 0.99)
    contracts = int(plan.contracts)
    fee = fee_for_order(fill, contracts, is_taker=True)
    return Trade(
        idem_key=plan.idem_key,
        mode="BROWSER",
        status=status,
        ticker=plan.ticker,
        market_question=plan.market_question,
        outcome=plan.outcome,
        side=plan.side,
        price=fill,
        contracts=contracts,
        size_usd=round(fill * contracts, 2),
        entry_fee_usd=fee,
        is_taker=True,
        model_probability=plan.model_probability,
        raw_edge=plan.raw_edge,
        net_edge=plan.net_edge,
        market_mid=plan.market_mid,
        signal_id=plan.signal_id,
        snapshot_id=plan.snapshot_id,
        notes=note,
    )


async def place_order(plan: TradePlan) -> Optional[Trade]:
    """Attempt to place `plan` through the Kalshi web UI. Returns a Trade on a
    confirmed fill, or None if unavailable / dry-run / unconfirmed."""
    if not _available():
        return None

    from playwright.async_api import async_playwright

    url = f"{settings.browser_base_url.rstrip('/')}/markets/{plan.ticker}"
    side_label = _SELECTORS["yes_button"] if plan.outcome.upper() == "YES" else _SELECTORS["no_button"]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=settings.browser_headless)
        try:
            context = await browser.new_context(
                storage_state=settings.browser_storage_state_path
            )
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # Locate the ticket controls (best-effort, role/text based).
            await page.get_by_role("button", name=side_label).first.click(timeout=10000)
            qty = page.get_by_label(_SELECTORS["quantity"]).first
            await qty.fill(str(int(plan.contracts)), timeout=10000)

            if settings.browser_dry_run:
                log.warning(
                    "BROWSER DRY-RUN: would submit %s %s x%d on %s (not clicking submit)",
                    plan.side, plan.outcome, plan.contracts, plan.ticker,
                )
                return None

            await page.get_by_role("button", name=_SELECTORS["review"]).first.click(timeout=10000)
            await page.get_by_role("button", name=_SELECTORS["submit"]).first.click(timeout=10000)
            await page.get_by_text(_SELECTORS["confirmation"], exact=False).first.wait_for(timeout=15000)

            log.info("Browser fallback placed order on %s", plan.ticker)
            return _build_trade(plan, "FILLED", "Browser fallback fill (confirmed)")
        finally:
            await browser.close()


async def _login_flow() -> None:
    """Interactive one-time login to save a Playwright storage state."""
    from playwright.async_api import async_playwright

    os.makedirs(os.path.dirname(settings.browser_storage_state_path) or ".", exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(f"{settings.browser_base_url.rstrip('/')}/login")
        print("Log into Kalshi in the opened browser, then press Enter here to save…")
        try:
            input()
        except EOFError:
            pass
        await context.storage_state(path=settings.browser_storage_state_path)
        await browser.close()
    print(f"Saved session to {settings.browser_storage_state_path}")


if __name__ == "__main__":
    import asyncio
    import sys

    if "--login" in sys.argv:
        asyncio.run(_login_flow())
    else:
        print("Usage: python -m kalshi_bot.modules.browser_exec --login")
