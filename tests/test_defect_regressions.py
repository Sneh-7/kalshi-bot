"""Regression tests for the four defects found in the source implementation.

Each test here corresponds to a specific bug that was present in the Kalshi
conversion of polymarket-sentiment-agent. They exist so the bugs cannot
silently return — every one of them was invisible in normal operation.
"""
from __future__ import annotations

import base64
import math

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshi_bot import fees
from kalshi_bot.config import settings
from kalshi_bot.modules.kalshi import KalshiClient, to_dollars


# --- Defect 1: signature path must include /trade-api/v2 -----------------

@pytest.fixture
def signing_client(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_path = tmp_path / "kalshi.pem"
    key_path.write_bytes(pem)

    monkeypatch.setattr(settings, "kalshi_key_id", "test-key-id")
    monkeypatch.setattr(settings, "kalshi_private_key_path", str(key_path))
    monkeypatch.setattr(
        settings, "kalshi_base_url", "https://external-api.demo.kalshi.co/trade-api/v2"
    )
    return KalshiClient(), key.public_key()


def test_signed_message_includes_api_prefix(signing_client):
    """THE bug: signing '/portfolio/balance' instead of the full path.

    Kalshi documents the message as e.g.
        1703123456789GET/trade-api/v2/portfolio/balance
    Signing without the prefix 401s on every authenticated request, which is
    easy to miss because all market-data endpoints are public.
    """
    client, public_key = signing_client
    assert client.path_prefix == "/trade-api/v2"

    headers = client._sign("GET", "/portfolio/balance")
    ts = headers["KALSHI-ACCESS-TIMESTAMP"]
    signature = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])

    correct = f"{ts}GET/trade-api/v2/portfolio/balance".encode()
    buggy = f"{ts}GET/portfolio/balance".encode()

    # The signature must verify against the full path...
    public_key.verify(
        signature,
        correct,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    # ...and must NOT verify against the old, prefix-less message.
    with pytest.raises(Exception):
        public_key.verify(
            signature,
            buggy,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )


def test_missing_credentials_raise_not_silently_noop(monkeypatch):
    """The source returned {} for unsigned headers, so misconfiguration looked
    like a working request until it 401'd somewhere far away."""
    monkeypatch.setattr(settings, "kalshi_key_id", "")
    monkeypatch.setattr(settings, "kalshi_private_key_path", "")
    client = KalshiClient()
    assert not client.has_credentials
    with pytest.raises(Exception):
        client._sign("GET", "/portfolio/balance")


# --- Defect 5: price units ----------------------------------------------

def test_to_dollars_parses_string_dollar_fields():
    """Verified against the live API 2026-07-24: `*_dollars` fields are STRINGS
    already in dollars, e.g. yes_bid_dollars == '0.1490'. Neither the source's
    integer-cent assumption nor a naive float() default is safe."""
    assert to_dollars("0.1490") == pytest.approx(0.1490)
    assert to_dollars("0.8250") == pytest.approx(0.8250)
    assert to_dollars(None, default=1.0) == 1.0
    assert to_dollars("", default=0.0) == 0.0
    assert to_dollars("garbage", default=0.5) == 0.5


def test_no_bid_inverts_to_yes_ask_without_going_negative():
    """The source computed `1.0 - float(p)`; if p were a cents value (62) that
    yields -61.0, a negative implied probability flowing into edge math.

    Real values from the live API: yes_bid 0.1490 with best no_bid 0.8250
    implies yes_ask 0.1750, which is exactly what the API reports.
    """
    no_bid = to_dollars("0.8250")
    yes_ask = round(1.0 - no_bid, 4)
    assert yes_ask == pytest.approx(0.1750)
    assert 0.0 < yes_ask < 1.0


# --- Defect 3: fees must exist and be correct ---------------------------

def test_taker_fee_matches_kalshi_formula():
    """taker = 0.07 * C * (1 - C); peaks at C = 0.50."""
    assert fees.taker_fee_per_contract(0.50) == pytest.approx(0.0175)
    assert fees.taker_fee_per_contract(0.25) == pytest.approx(0.013125)
    assert fees.taker_fee_per_contract(0.90) == pytest.approx(0.0063)
    # Peak is at the midpoint.
    peak = max(
        (fees.taker_fee_per_contract(c / 100) for c in range(1, 100))
    )
    assert fees.taker_fee_per_contract(0.50) == pytest.approx(peak)


def test_maker_fee_is_quarter_of_taker():
    for price in (0.1, 0.3, 0.5, 0.75):
        assert fees.maker_fee_per_contract(price) == pytest.approx(
            0.25 * fees.taker_fee_per_contract(price)
        )


def test_fees_are_never_zero_for_a_real_trade():
    """`Trade.fees_usdc` was hardcoded to 0.0 in the source."""
    assert fees.fee_for_order(0.50, 100, is_taker=True) > 0


def test_round_trip_costs_more_than_holding():
    """Holding to resolution pays the entry fee only — the structural argument
    for hold-to-resolution over round-tripping.

    Note the exit price must be strictly inside (0, 1): the fee formula
    0.07*C*(1-C) is zero at C=0 and C=1, so a position sold at exactly $1.00
    incurs no exit fee and the two paths cost the same. That is correct
    behaviour, not a bug — settlement is free.
    """
    held = fees.realized_pnl(0.50, 0.70, 100, held_to_resolution=True)
    flipped = fees.realized_pnl(0.50, 0.70, 100, held_to_resolution=False)
    assert held > flipped
    assert (held - flipped) == pytest.approx(
        fees.fee_for_order(0.70, 100, is_taker=True)
    )


def test_settlement_at_one_dollar_is_free():
    """Kalshi charges no settlement fee; the formula already encodes this."""
    assert fees.taker_fee_per_contract(1.0) == 0.0
    assert fees.taker_fee_per_contract(0.0) == 0.0


# --- Defect 4 + edge math: net edge must subtract costs ------------------

def test_net_edge_subtracts_costs():
    """A model probability of 0.55 against a market at 0.50 is roughly
    break-even, not a 5-point edge."""
    b = fees.net_edge(
        model_probability=0.55, entry_price=0.50, best_bid=0.48, best_ask=0.52
    )
    assert b.raw_edge == pytest.approx(0.05)
    assert b.net_edge < b.raw_edge
    assert b.total_cost > 0
    # fee 0.0175 + half-spread 0.02 + slippage 0.005 = 0.0425 -> net ~0.0075
    assert b.net_edge == pytest.approx(0.05 - 0.0175 - 0.02 - 0.005, abs=1e-6)


def test_small_raw_edge_goes_negative_after_costs():
    b = fees.net_edge(
        model_probability=0.52, entry_price=0.50, best_bid=0.48, best_ask=0.52
    )
    assert b.raw_edge == pytest.approx(0.02)
    assert b.net_edge < 0, "a 2-point raw edge cannot survive costs at 50c"


def test_contracts_for_budget_accounts_for_fees_and_is_whole():
    n = fees.contracts_for_budget(25.0, 0.50)
    assert isinstance(n, int)
    # Naive 25/0.50 = 50; fees must reduce it.
    assert n < 50
    total = n * (0.50 + fees.taker_fee_per_contract(0.50))
    assert total <= 25.0
