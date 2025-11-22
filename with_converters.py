import os
from dotenv import load_dotenv
import requests
from time import sleep

import tkinter as tk
from tkinter import messagebox

def popup(title, message):
    root = tk.Tk()
    root.withdraw()
    messagebox.showinfo(title, message)
    root.destroy()

'''
RITC Algorithmic ETF Arbitrage - Traffic Control, Unwind, Passive Hedging,
and Converter Preparation

New features vs previous version:
1. Passive Hedging:
   - When hedging tender fills, first place LIMIT orders at bid/ask to earn rebates
     and reduce slippage.
   - If the limit order cannot be placed (API error), we fall back to robust
     market-hedging with retries.

2. Converter Preparation & Alerts:
   - Monitors positions around 10,000-share packs (converter size).
   - Prints big "popup" alerts (terminal banners + bell) when:
       a) You are close to a converter-sized inventory (RITC or BULL+BEAR).
       b) Spreads suggest ETF Creation or Redemption might be attractive.
   - Tells you explicitly:
       - Which direction: "CREATE" (stocks -> ETF) or "REDEEM" (ETF -> stocks)
       - Which tickers and approximate sizes.

3. Traffic-Light Unwind (unchanged core idea):
   - When gross usage exceeds a threshold (85%), the algo focuses on
     unwinding with passive limit orders instead of aggressive trading.
'''

load_dotenv()

API = "http://localhost:9999/v1"
API_KEY = os.getenv("API_KEY")
HDRS = {"X-API-key": API_KEY}

# Tickers
CAD = "CAD"
USD = "USD"
BULL = "BULL"
BEAR = "BEAR"
RITC = "RITC"

# Position / Risk Parameters
MAX_GROSS = 300000
MAX_NET = 200000
UNWIND_TRIGGER = 0.85  # 85% full -> Start unwinding

ORDER_QTY = 1000
ARB_THRESHOLD_CAD = 0.05  # raw price edge (fees ignored for simplicity)

# Converter Parameters (from case spec)
CONVERTER_SIZE = 10000      # shares of RITC or each stock
CONVERTER_COST_USD = 1500   # flat fee per conversion

s = requests.Session()
s.headers.update(HDRS)


# ------------- BASIC HELPERS -------------

def get_tick_status():
    try:
        r = s.get(f"{API}/case")
        r.raise_for_status()
        j = r.json()
        return j["tick"], j["status"]
    except Exception as e:
        print(f"Error getting status: {e}")
        return 0, "STOPPED"


def best_bid_ask(ticker):
    try:
        r = s.get(f"{API}/securities/book", params={"ticker": ticker})
        r.raise_for_status()
        book = r.json()
        bid = float(book["bids"][0]["price"]) if book["bids"] else 0.0
        ask = float(book["asks"][0]["price"]) if book["asks"] else 1e12
        return bid, ask
    except Exception as e:
        print(f"Error getting book for {ticker}: {e}")
        return 0.0, 1e12


def positions_map():
    try:
        r = s.get(f"{API}/securities")
        r.raise_for_status()
        out = {p["ticker"]: int(p.get("position", 0)) for p in r.json()}
        for k in (BULL, BEAR, RITC, USD, CAD):
            out.setdefault(k, 0)
        return out
    except Exception as e:
        print(f"Error getting positions: {e}")
        return {}


def place_mkt(ticker, action, qty):
    if qty <= 0:
        return False
    try:
        qty = int(qty)
        return s.post(
            f"{API}/orders",
            params={
                "ticker": ticker,
                "type": "MARKET",
                "quantity": qty,
                "action": action
            }
        ).ok
    except Exception as e:
        print(f"Market Order failed: {e}")
        return False


def place_limit(ticker, action, qty, price):
    """
    Places a LIMIT order.
    Used for unwinding and passive hedging to earn rebates.
    """
    if qty <= 0:
        return False
    try:
        qty = int(qty)
        return s.post(
            f"{API}/orders",
            params={
                "ticker": ticker,
                "type": "LIMIT",
                "quantity": qty,
                "action": action,
                "price": price
            }
        ).ok
    except Exception as e:
        print(f"Limit Order failed: {e}")
        return False


# ------------- ROBUST + PASSIVE HEDGING -------------

def force_hedge_trade(ticker, action, qty):
    """
    Robust hedge with MARKET orders & retries.
    (Fallback when passive hedges fail to place.)
    """
    max_retries = 5
    for i in range(max_retries):
        if place_mkt(ticker, action, qty):
            return True
        print(f"!!! HEDGE FAILED ({ticker} {action}) - RETRYING {i+1}/{max_retries} !!!")
        sleep(0.05)
    print(f"CRITICAL ERROR: COULD NOT EXECUTE HEDGE FOR {ticker}")
    return False


def passive_hedge_or_force(ticker, action, qty, bid, ask):
    """
    First tries to place a passive LIMIT hedge at bid/ask to earn rebates.
    If order placement fails (API error), falls back to force_hedge_trade.
    NOTE: We do not check fills (RIT API doesn't give us instant fills here),
          but this at least reduces slippage on average.
    """
    if qty <= 0:
        return

    # Passive price: BUY at ask, SELL at bid
    if action == "BUY":
        limit_price = ask
    else:
        limit_price = bid

    ok = place_limit(ticker, action, qty, limit_price)
    if not ok:
        # If we couldn't place the limit order at all, fall back
        print(f"⚠️ Passive hedge placement failed for {ticker}, using MARKET hedge.")
        force_hedge_trade(ticker, action, qty)


# ------------- LIMIT / RISK LOGIC -------------

def within_limits(ticker, action, qty):
    """
    Smart Limit Logic: allows risk-reducing trades even if gross/net are near limits.
    """
    pos = positions_map()
    if not pos:
        return False

    sign_qty = qty if action == "BUY" else -qty

    curr_bull = pos[BULL]
    curr_bear = pos[BEAR]
    curr_ritc = pos[RITC] * 2  # ETF has multiplier 2 for limits

    curr_gross = abs(curr_bull) + abs(curr_bear) + abs(curr_ritc)
    curr_net = curr_bull + curr_bear + curr_ritc

    # Projected exposures
    if ticker == BULL:
        proj_bull, proj_bear, proj_ritc = curr_bull + sign_qty, curr_bear, curr_ritc
    elif ticker == BEAR:
        proj_bull, proj_bear, proj_ritc = curr_bull, curr_bear + sign_qty, curr_ritc
    elif ticker == RITC:
        proj_bull, proj_bear, proj_ritc = curr_bull, curr_bear, curr_ritc + (sign_qty * 2)
    else:
        # Ignore limits for currency trades (CAD / USD)
        return True

    proj_gross = abs(proj_bull) + abs(proj_bear) + abs(proj_ritc)
    proj_net = proj_bull + proj_bear + proj_ritc

    # 1. Allow if under Max Limits
    if (proj_gross < MAX_GROSS) and (-MAX_NET < proj_net < MAX_NET):
        return True
    # 2. Allow if reducing gross usage
    if proj_gross < curr_gross:
        return True
    # 3. Allow if reducing net imbalance
    if abs(proj_net) < abs(curr_net):
        return True

    return False


def get_gross_usage():
    pos = positions_map()
    if not pos:
        return 0
    usage = abs(pos[BULL]) + abs(pos[BEAR]) + abs(pos[RITC] * 2)
    return usage


# ------------- UNWIND LOGIC -------------

def attempt_unwind():
    """
    UNWIND MODE: Uses LIMIT orders to passively close positions and free risk.
    """
    pos = positions_map()
    if not pos:
        return

    ritc_pos = pos[RITC]
    bull_pos = pos[BULL]
    bear_pos = pos[BEAR]

    if abs(ritc_pos) < 500 and abs(bull_pos) < 500 and abs(bear_pos) < 500:
        # Positions small enough, no need to unwind
        return

    ritc_bid, ritc_ask = best_bid_ask(RITC)
    bull_bid, bull_ask = best_bid_ask(BULL)
    bear_bid, bear_ask = best_bid_ask(BEAR)

    qty = 1000  # unwind in chunks

    if ritc_pos > 0:
        print(f"🚦 UNWIND: LONG RITC={ritc_pos}")
        # Sell RITC at ask, buy stocks at bid
        place_limit(RITC, "SELL", qty, ritc_ask)
        place_limit(BULL, "BUY", qty, bull_bid)
        place_limit(BEAR, "BUY", qty, bear_bid)

    elif ritc_pos < 0:
        print(f"🚦 UNWIND: SHORT RITC={ritc_pos}")
        # Buy RITC at bid, sell stocks at ask
        place_limit(RITC, "BUY", qty, ritc_bid)
        place_limit(BULL, "SELL", qty, bull_ask)
        place_limit(BEAR, "SELL", qty, bear_ask)

    # Also lightly trim standalone stock positions if they are large
    if bull_pos > 2000:
        place_limit(BULL, "SELL", min(qty, bull_pos), bull_ask)
    elif bull_pos < -2000:
        place_limit(BULL, "BUY", min(qty, -bull_pos), bull_bid)

    if bear_pos > 2000:
        place_limit(BEAR, "SELL", min(qty, bear_pos), bear_ask)
    elif bear_pos < -2000:
        place_limit(BEAR, "BUY", min(qty, -bear_pos), bear_bid)


# ------------- TENDER LOGIC (with passive hedging) -------------

def process_tender_offers():
    try:
        r = s.get(f"{API}/tenders")
        r.raise_for_status()
        offers = r.json()
    except Exception:
        return

    if not offers:
        return

    bull_bid, bull_ask = best_bid_ask(BULL)
    bear_bid, bear_ask = best_bid_ask(BEAR)
    usd_bid, usd_ask = best_bid_ask(USD)

    synthetic_bid_cad = bull_bid + bear_bid
    synthetic_ask_cad = bull_ask + bear_ask

    for offer in offers:
        tid = offer['tender_id']
        action = offer['action']   # "BUY" or "SELL" (ETF from our POV)
        price_usd = offer['price']
        quantity = offer['quantity']

        is_profitable = False

        if action == "BUY":
            # Tender wants to BUY from us: we SELL ETF to them at price_usd
            cost_cad = price_usd * usd_ask
            profit = synthetic_bid_cad - cost_cad
            if profit > 0.15 and within_limits(RITC, "SELL", quantity):
                is_profitable = True

        elif action == "SELL":
            # Tender wants to SELL ETF to us: we BUY ETF at price_usd
            proceeds_cad = price_usd * usd_bid
            profit = proceeds_cad - synthetic_ask_cad
            if profit > 0.15 and within_limits(RITC, "BUY", quantity):
                is_profitable = True

        if is_profitable:
            print(f"✅ Accepting Tender {action} {quantity} @ {price_usd} USD...")
            res = s.post(f"{API}/tenders/{tid}")

            if res.ok:
                # After tender, hedge passively where possible
                try:
                    usd_value = quantity * price_usd

                    # Refresh quotes for hedging
                    bull_bid, bull_ask = best_bid_ask(BULL)
                    bear_bid, bear_ask = best_bid_ask(BEAR)
                    usd_bid, usd_ask = best_bid_ask(USD)

                    if action == "BUY":
                        # We bought ETF -> effectively long RITC and short USD
                        # Hedge: SELL USD, SELL BULL, SELL BEAR
                        passive_hedge_or_force(USD, "SELL", usd_value, usd_bid, usd_ask)
                        passive_hedge_or_force(BULL, "SELL", quantity, bull_bid, bull_ask)
                        passive_hedge_or_force(BEAR, "SELL", quantity, bear_bid, bear_ask)

                    elif action == "SELL":
                        # We sold ETF -> effectively short RITC and long USD
                        # Hedge: BUY USD, BUY BULL, BUY BEAR
                        passive_hedge_or_force(USD, "BUY", usd_value, usd_bid, usd_ask)
                        passive_hedge_or_force(BULL, "BUY", quantity, bull_bid, bull_ask)
                        passive_hedge_or_force(BEAR, "BUY", quantity, bear_bid, bear_ask)

                except Exception as e:
                    print(f"CRITICAL HEDGE ERROR: {e}")


# ------------- CORE ARBITRAGE LOGIC -------------

def step_once():
    bull_bid, bull_ask = best_bid_ask(BULL)
    bear_bid, bear_ask = best_bid_ask(BEAR)
    ritc_bid_usd, ritc_ask_usd = best_bid_ask(RITC)
    usd_bid, usd_ask = best_bid_ask(USD)

    ritc_ask_cad = ritc_ask_usd * usd_ask
    ritc_bid_cad = ritc_bid_usd * usd_bid

    basket_sell_value = bull_bid + bear_bid
    basket_buy_cost = bull_ask + bear_ask

    edge1 = basket_sell_value - ritc_ask_cad  # Sell basket / Buy ETF
    edge2 = ritc_bid_cad - basket_buy_cost    # Buy basket / Sell ETF

    # Only trade if edge > threshold AND limits allow
    if edge1 >= ARB_THRESHOLD_CAD and within_limits(RITC, "BUY", ORDER_QTY):
        print(f"Ex1: Sell Basket / Buy ETF. Edge: {edge1:.3f}")
        place_mkt(BULL, "SELL", ORDER_QTY)
        place_mkt(BEAR, "SELL", ORDER_QTY)
        place_mkt(RITC, "BUY", ORDER_QTY)
        # Passively hedge USD
        passive_hedge_or_force(USD, "SELL", ORDER_QTY * ritc_ask_usd, usd_bid, usd_ask)

    elif edge2 >= ARB_THRESHOLD_CAD and within_limits(RITC, "SELL", ORDER_QTY):
        print(f"Ex2: Buy Basket / Sell ETF. Edge: {edge2:.3f}")
        place_mkt(BULL, "BUY", ORDER_QTY)
        place_mkt(BEAR, "BUY", ORDER_QTY)
        place_mkt(RITC, "SELL", ORDER_QTY)
        # Passively hedge USD
        passive_hedge_or_force(USD, "BUY", ORDER_QTY * ritc_bid_usd, usd_bid, usd_ask)


# ------------- CONVERTER PREPARATION & ALERTS -------------

def check_converter_advice():
    pos = positions_map()
    if not pos:
        return

    ritc_pos = pos[RITC]
    bull_pos = pos[BULL]
    bear_pos = pos[BEAR]

    # Get quotes
    ritc_bid, ritc_ask = best_bid_ask(RITC)
    bull_bid, bull_ask = best_bid_ask(BULL)
    bear_bid, bear_ask = best_bid_ask(BEAR)
    usd_bid, usd_ask = best_bid_ask(USD)
    usd_mid = (usd_bid + usd_ask) / 2 if usd_bid > 0 and usd_ask < 1e11 else 0.0

    # spreads
    ritc_spread = ritc_ask - ritc_bid
    bull_spread = bull_ask - bull_bid
    bear_spread = bear_ask - bear_bid
    stock_spread_sum = bull_spread + bear_spread

    # near converter sizes?
    near_ritc_pack = ritc_pos >= 0.8 * CONVERTER_SIZE
    near_stock_pack = min(bull_pos, bear_pos) >= 0.8 * CONVERTER_SIZE

    # decide direction
    # REDEMPTION (RITC → STOCKS) if ETF is wider than stock basket
    if near_ritc_pack and ritc_spread > stock_spread_sum * 1.5:
        popup(
            "REDEEM NOW",
            f"""
You are close to a converter pack of RITC:

RITC position: {ritc_pos}
RITC Spread: {ritc_spread:.4f}
BULL+BEAR combined spread: {stock_spread_sum:.4f}

RECOMMENDATION:
Use ETF REDEMPTION:
    10,000 RITC → 10,000 BULL + 10,000 BEAR

Open RIT Client → Assets → Redemption
"""
        )
        return

    # CREATION (STOCKS → RITC)
    if near_stock_pack and stock_spread_sum > ritc_spread * 1.5:
        popup(
            "CREATE NOW",
            f"""
You are close to a converter pack of BULL+BEAR:

BULL position: {bull_pos}
BEAR position: {bear_pos}
Stock combined spread: {stock_spread_sum:.4f}
RITC spread: {ritc_spread:.4f}

RECOMMENDATION:
Use ETF CREATION:
    10,000 BULL + 10,000 BEAR → 10,000 RITC

Open RIT Client → Assets → Creation
"""
        )
        return

    """
    This does NOT call the converters via API (not allowed),
    but gives you loud console alerts when:

    - You are close to 10,000-share packs in RITC or BULL/BEAR.
    - Spreads suggest Creation or Redemption may be better than
      grinding through the order book.

    You can then manually use:
      - ETF Creation (BULL+BEAR -> RITC)
      - ETF Redemption (RITC -> BULL+BEAR)
    from the RIT Client UI.
    """
    pos = positions_map()
    if not pos:
        return

    ritc_pos = pos[RITC]
    bull_pos = pos[BULL]
    bear_pos = pos[BEAR]

    gross = get_gross_usage()
    gross_pct = gross / MAX_GROSS if MAX_GROSS > 0 else 0.0

    # Get quotes
    ritc_bid, ritc_ask = best_bid_ask(RITC)
    bull_bid, bull_ask = best_bid_ask(BULL)
    bear_bid, bear_ask = best_bid_ask(BEAR)
    usd_bid, usd_ask = best_bid_ask(USD)
    usd_mid = (usd_bid + usd_ask) / 2 if usd_bid > 0 and usd_ask < 1e11 else 0.0

    ritc_spread = max(0.0, ritc_ask - ritc_bid)
    bull_spread = max(0.0, bull_ask - bull_bid)
    bear_spread = max(0.0, bear_ask - bear_bid)
    stock_spread_sum = bull_spread + bear_spread

    # Converter fee per RITC share (USD -> CAD approx)
    converter_fee_cad = CONVERTER_COST_USD * usd_mid if usd_mid > 0 else 0.0
    fee_per_share_cad = converter_fee_cad / CONVERTER_SIZE if CONVERTER_SIZE > 0 else 0.0

    # Helper to ring a bell + banner "popup"
    def popup_banner(title, body_lines):
        print("\a")  # terminal bell (if supported)
        print("\n" + "=" * 80)
        print(title)
        print("-" * 80)
        for line in body_lines:
            print(line)
        print("=" * 80 + "\n")

    # --- 1. Near converter-sized RITC inventory (Redemption candidate) ---
    if ritc_pos >= 0.8 * CONVERTER_SIZE:
        # Check if ETF book is wide vs stocks -> Redemption might be attractive
        if ritc_spread > 1.5 * stock_spread_sum or gross_pct > 0.9:
            approx_pnl_impact = f"Approx converter cost: {converter_fee_cad:.0f} CAD (~{fee_per_share_cad:.3f} CAD/share)"
            popup_banner(
                "🚨 CONVERTER ALERT: CONSIDER ETF REDEMPTION (RITC -> BULL + BEAR)",
                [
                    f"RITC position: {ritc_pos} (>= 0.8 * {CONVERTER_SIZE})",
                    f"Gross usage: {gross} ({gross_pct:.1%} of MAX_GROSS)",
                    f"RITC spread: {ritc_spread:.3f}, BULL+BEAR combined spread: {stock_spread_sum:.3f}",
                    "",
                    "Suggested manual action in RIT Client:",
                    f"  • Use ETF-REDEMPTION on {CONVERTER_SIZE} RITC (or nearest multiple you have).",
                    "  • Then work BULL and BEAR separately using passive limit orders.",
                    "",
                    approx_pnl_impact
                ]
            )

    # --- 2. Near converter-sized stock basket (Creation candidate) ---
    basket_min = min(bull_pos, bear_pos)
    if basket_min >= 0.8 * CONVERTER_SIZE:
        if stock_spread_sum > 1.5 * ritc_spread or gross_pct > 0.9:
            approx_pnl_impact = f"Approx converter cost: {converter_fee_cad:.0f} CAD (~{fee_per_share_cad:.3f} CAD/share)"
            popup_banner(
                "🚨 CONVERTER ALERT: CONSIDER ETF CREATION (BULL + BEAR -> RITC)",
                [
                    f"BULL position: {bull_pos}, BEAR position: {bear_pos}",
                    f"At least {basket_min} shares in both (>= 0.8 * {CONVERTER_SIZE})",
                    f"Gross usage: {gross} ({gross_pct:.1%} of MAX_GROSS)",
                    f"BULL+BEAR combined spread: {stock_spread_sum:.3f}, RITC spread: {ritc_spread:.3f}",
                    "",
                    "Suggested manual action in RIT Client:",
                    f"  • Use ETF-CREATION on 10,000 BULL + 10,000 BEAR to receive 10,000 RITC.",
                    "  • Then work RITC in the ETF book (often tighter).",
                    "",
                    approx_pnl_impact
                ]
            )

    # --- 3. Generic info popup when exactly on a converter multiple ---
    if abs(ritc_pos) >= CONVERTER_SIZE and ritc_pos % CONVERTER_SIZE == 0:
        popup_banner(
            "ℹ️ Converter-Ready RITC Inventory",
            [
                f"RITC position is an exact multiple of {CONVERTER_SIZE}: {ritc_pos}",
                "You can fully convert this position using the converters if liquidity dries up.",
                "Pick direction based on which market is more liquid:",
                "  • If ETF book is thin and stock books are tight -> consider REDEMPTION.",
                "  • If stocks are hard to work and ETF is tight -> consider CREATION."
            ]
        )

    if basket_min >= CONVERTER_SIZE and basket_min % CONVERTER_SIZE == 0:
        popup_banner(
            "ℹ️ Converter-Ready BULL+BEAR Basket",
            [
                f"BULL pos: {bull_pos}, BEAR pos: {bear_pos}",
                f"Both have at least {CONVERTER_SIZE} in the same direction.",
                "You can convert 10,000 BULL + 10,000 BEAR into 10,000 RITC via ETF-CREATION.",
            ]
        )


# ------------- MAIN LOOP -------------

def main():
    print("Starting Traffic-Controlled Algo with Passive Hedging + Converter Alerts...")
    tick, status = get_tick_status()

    while status == "ACTIVE":
        gross_usage = get_gross_usage()
        limit_pct = gross_usage / MAX_GROSS if MAX_GROSS > 0 else 0.0

        if limit_pct > UNWIND_TRIGGER:
            # RED LIGHT: focus on unwinding, avoid new risk
            if tick % 5 == 0:
                print(f"⚠️ LIMIT WARNING ({limit_pct:.1%}) - UNWINDING...")
                attempt_unwind()
        else:
            # GREEN LIGHT: trade tenders + arb
            process_tender_offers()
            step_once()

        # Check converter opportunities every few ticks
        if tick % 10 == 0:
            check_converter_advice()

        sleep(0.2)
        tick, status = get_tick_status()


if __name__ == "__main__":
    main()
