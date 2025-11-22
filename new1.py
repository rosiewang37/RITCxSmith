import os
import requests
import signal
import sys
from time import sleep
from dotenv import load_dotenv

# --- CONFIGURATION & CONSTANTS ---
load_dotenv()
API_KEY = os.getenv("API_KEY")
API_URL = "http://localhost:9999/v1"
HEADERS = {"X-API-key": API_KEY}

# Tickers
CAD, USD = "CAD", "USD"
BULL, BEAR, RITC = "BULL", "BEAR", "RITC"

# Risk Management
MAX_GROSS_VOLUME = 300000      # Share count limit
MAX_CASH_LIMIT = 10000000      # $10M Gross Notional Limit (CAD)
MAX_NET = 50000                # Tighter net limit to force hedging
UNWIND_TRIGGER = 250000        # Start unwinding at 250k gross volume
UNWIND_CHUNK = 5000

# Trading Constants
ARB_QTY = 1000                 # Size for active market arbitrage
FEE_PER_SHARE = 0.02
SLIPPAGE_BUFFER = 0.03 
MIN_NET_PROFIT = 0.05 

# Session Setup
s = requests.Session()
s.headers.update(HEADERS)

def signal_handler(sig, frame):
    print("\n[STOP] Algorithm stopped by user.")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

# ---------------- HELPERS ----------------

def get_tick_status():
    try:
        r = s.get(f"{API_URL}/case")
        if r.ok:
            j = r.json()
            return j["tick"], j["status"]
    except Exception as e:
        print(f"Connection Error: {e}")
    return 0, "STOPPED"

def best_bid_ask(ticker):
    """ Returns Best Bid and Best Ask. Returns (0, 1000000) if empty book. """
    try:
        r = s.get(f"{API_URL}/securities/book", params={"ticker": ticker, "limit": 1})
        if r.ok:
            book = r.json()
            bid = float(book["bids"][0]["price"]) if book["bids"] else 0.0
            ask = float(book["asks"][0]["price"]) if book["asks"] else 1000000.0
            return bid, ask
    except: pass
    return 0.0, 1000000.0

def positions_map():
    """ Returns a dictionary of current positions. """
    try:
        r = s.get(f"{API_URL}/securities")
        if r.ok:
            out = {p["ticker"]: int(p.get("position", 0)) for p in r.json()}
            for k in (BULL, BEAR, RITC, USD, CAD):
                out.setdefault(k, 0)
            return out
    except: pass
    return {}

def get_gross_usage(pos_map):
    """ Calculates gross limit usage (Volume). RITC counts x2. """
    if not pos_map: return 0
    return abs(pos_map[BULL]) + abs(pos_map[BEAR]) + abs(pos_map[RITC] * 2)

# ---------------- EXECUTION ----------------

def place_order(ticker, action, qty, type="MARKET", price=None):
    if qty <= 0: return False
    params = {"ticker": ticker, "type": type, "quantity": int(qty), "action": action}
    if price: params["price"] = price
    try:
        return s.post(f"{API_URL}/orders", params=params).ok
    except Exception as e:
        print(f"Order Fail {ticker}: {e}")
        return False

def quick_hedge(bull_action, bear_action, qty):
    place_order(BULL, bull_action, qty, "MARKET")
    place_order(BEAR, bear_action, qty, "MARKET")

# ---------------- STRATEGY LOGIC ----------------

def check_limits(pos_map, trade_qty, action, prices):
    """
    Checks BOTH Volume Limit (300k) and Cash Value Limit ($10M).
    Returns True if trade is safe or reduces risk.
    """
    curr_bull = pos_map[BULL]
    curr_bear = pos_map[BEAR]
    curr_ritc = pos_map[RITC]
    
    p_bull = (prices['bull_b'] + prices['bull_a']) / 2
    p_bear = (prices['bear_b'] + prices['bear_a']) / 2
    p_ritc = (prices['ritc_b'] + prices['ritc_a']) / 2
    p_usd  = (prices['usd_b']  + prices['usd_a'])  / 2
    
    change = trade_qty if action == "BUY" else -trade_qty
    
    proj_ritc = curr_ritc + change
    proj_bull = curr_bull - change 
    proj_bear = curr_bear - change 
    
    proj_vol = abs(proj_bull) + abs(proj_bear) + abs(proj_ritc * 2)
    
    val_bull = abs(proj_bull) * p_bull
    val_bear = abs(proj_bear) * p_bear
    val_ritc = abs(proj_ritc) * p_ritc * p_usd
    
    proj_cash_val = val_bull + val_bear + val_ritc
    
    if proj_vol < MAX_GROSS_VOLUME and proj_cash_val < MAX_CASH_LIMIT:
        return True
        
    curr_vol = abs(curr_bull) + abs(curr_bear) + abs(curr_ritc * 2)
    curr_cash_val = (abs(curr_bull)*p_bull) + (abs(curr_bear)*p_bear) + (abs(curr_ritc)*p_ritc*p_usd)
    
    if proj_vol < curr_vol and proj_cash_val < curr_cash_val:
        return True
        
    return False

def manage_currency_risk(pos_map):
    ritc_pos = pos_map.get(RITC, 0)
    usd_pos = pos_map.get(USD, 0)
    bid, ask = best_bid_ask(RITC)
    mid = (bid + ask) / 2
    target_usd = -(ritc_pos * mid)
    drift = target_usd - usd_pos
    THRESHOLD = 2000 
    if abs(drift) > THRESHOLD:
        action = "BUY" if drift > 0 else "SELL"
        place_order(USD, action, abs(drift), "MARKET")

def aggressive_unwind(pos_map):
    ritc_pos = pos_map.get(RITC, 0)
    if abs(ritc_pos) < 500: return
    r_bid, r_ask = best_bid_ask(RITC)
    b_bid, b_ask = best_bid_ask(BULL)
    be_bid, be_ask = best_bid_ask(BEAR)
    qty = min(UNWIND_CHUNK, abs(ritc_pos))

    if ritc_pos > 0:
        print(f"  [UNWIND] Selling {qty} RITC Bundle...")
        place_order(RITC, "SELL", qty, "LIMIT", price=r_bid)
        place_order(BULL, "BUY", qty, "LIMIT", price=b_ask)
        place_order(BEAR, "BUY", qty, "LIMIT", price=be_ask)
    elif ritc_pos < 0:
        print(f"  [UNWIND] Buying {qty} RITC Bundle...")
        place_order(RITC, "BUY", qty, "LIMIT", price=r_ask)
        place_order(BULL, "SELL", qty, "LIMIT", price=b_bid)
        place_order(BEAR, "SELL", qty, "LIMIT", price=be_bid)

def scan_market_arb(pos_map, gross_usage):
    """
    ACTIVELY scans the market for arbitrage opportunities (not waiting for tenders).
    Checks if we can Buy RITC + Sell Stocks OR Sell RITC + Buy Stocks for a profit.
    """
    # Fetch prices
    b_bid, b_ask = best_bid_ask(BULL)
    be_bid, be_ask = best_bid_ask(BEAR)
    u_bid, u_ask = best_bid_ask(USD)
    r_bid, r_ask = best_bid_ask(RITC)
    
    prices = {
        'bull_b': b_bid, 'bull_a': b_ask,
        'bear_b': be_bid, 'bear_a': be_ask,
        'ritc_b': r_bid, 'ritc_a': r_ask,
        'usd_b': u_bid, 'usd_a': u_ask
    }

    qty = ARB_QTY
    
    # Total Transaction Fee for 3 legs (RITC + BULL + BEAR) = 0.06 total
    # We use a safe profit threshold
    TOTAL_FEE = (FEE_PER_SHARE * 3) + SLIPPAGE_BUFFER

    # --- SCENARIO 1: ETF IS CHEAP (BUY RITC, SELL STOCKS) ---
    # Cost: Ask RITC (USD converted to CAD)
    # Gain: Bid BULL + Bid BEAR
    
    cost_buy_etf = r_ask * u_ask
    gain_sell_stocks = b_bid + be_bid
    
    profit_buy = gain_sell_stocks - cost_buy_etf - TOTAL_FEE
    
    if profit_buy > MIN_NET_PROFIT:
        if check_limits(pos_map, qty, "BUY", prices):
            print(f"🚀 MARKET ARB: BUY RITC (Cheap) | Est Profit: {profit_buy:.3f}")
            if place_order(RITC, "BUY", qty, "MARKET"):
                quick_hedge("SELL", "SELL", qty)
            return # Take one opportunity per tick

    # --- SCENARIO 2: ETF IS EXPENSIVE (SELL RITC, BUY STOCKS) ---
    # Gain: Bid RITC (USD converted to CAD)
    # Cost: Ask BULL + Ask BEAR
    
    gain_sell_etf = r_bid * u_bid
    cost_buy_stocks = b_ask + be_ask
    
    profit_sell = gain_sell_etf - cost_buy_stocks - TOTAL_FEE
    
    if profit_sell > MIN_NET_PROFIT:
        if check_limits(pos_map, qty, "SELL", prices):
            print(f"🚀 MARKET ARB: SELL RITC (Expensive) | Est Profit: {profit_sell:.3f}")
            if place_order(RITC, "SELL", qty, "MARKET"):
                quick_hedge("BUY", "BUY", qty)
            return

def process_tenders(pos_map, gross_usage):
    try:
        r = s.get(f"{API_URL}/tenders")
        if not r.ok: return
        offers = r.json()
    except: return

    if not offers: return

    b_bid, b_ask = best_bid_ask(BULL)
    be_bid, be_ask = best_bid_ask(BEAR)
    u_bid, u_ask = best_bid_ask(USD)
    r_bid, r_ask = best_bid_ask(RITC)
    
    prices = {
        'bull_b': b_bid, 'bull_a': b_ask,
        'bear_b': be_bid, 'bear_a': be_ask,
        'ritc_b': r_bid, 'ritc_a': r_ask,
        'usd_b': u_bid, 'usd_a': u_ask
    }

    syn_ask = b_ask + be_ask
    syn_bid = b_bid + be_bid

    for offer in offers:
        tid = offer['tender_id']
        action = offer['action'] 
        price = offer['price']
        qty = offer['quantity']
        
        if not check_limits(pos_map, qty, action, prices):
            continue

        if action == "BUY":
            cost_cad = price * u_ask
            proceeds_cad = syn_bid
            net_profit = (proceeds_cad - cost_cad) - (FEE_PER_SHARE * 2) - SLIPPAGE_BUFFER
            
            if net_profit > MIN_NET_PROFIT:
                print(f"✅ TENDER BUY {qty} @ ${price} | Net: {net_profit:.3f}")
                if s.post(f"{API_URL}/tenders/{tid}").ok:
                    quick_hedge("SELL", "SELL", qty)

        elif action == "SELL":
            proceeds_cad = price * u_bid
            cost_cad = syn_ask
            net_profit = (proceeds_cad - cost_cad) - (FEE_PER_SHARE * 2) - SLIPPAGE_BUFFER

            if net_profit > MIN_NET_PROFIT:
                print(f"✅ TENDER SELL {qty} @ ${price} | Net: {net_profit:.3f}")
                if s.post(f"{API_URL}/tenders/{tid}").ok:
                    quick_hedge("BUY", "BUY", qty)

def main():
    print("=== IMPROVED RITC ARB ALGO (ACTIVE MODE) ===")
    tick, status = get_tick_status()
    
    while status == "ACTIVE":
        pos = positions_map()
        gross = get_gross_usage(pos)
        
        if gross > UNWIND_TRIGGER:
            aggressive_unwind(pos)
        else:
            # 1. Check High-Priority Tenders
            process_tenders(pos, gross)
            
            # 2. Check Open Market Arbitrage (Active Trading)
            scan_market_arb(pos, gross)
            
            if gross > 50000 and tick % 5 == 0:
                aggressive_unwind(pos)

        manage_currency_risk(pos)
        tick, status = get_tick_status()
        sleep(0.05) 

if __name__ == "__main__":
    main()