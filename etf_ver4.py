import os
import requests
from time import sleep
from dotenv import load_dotenv

'''
RITC Algorithmic ETF Arbitrage - Complete Delta-Neutral Strategy
--------------------------------------------------------------------
NEW FEATURE: DYNAMIC RE-HEDGING
- The algo now constantly calculates the total value of your RITC position.
- It compares this to your USD Short position.
- If they drift apart (due to profit or price changes), it automatically 
  trades USD to re-align them. 
- This "sweeps" your profits into CAD and stops P&L currency fluctuation.
--------------------------------------------------------------------
'''

# Load API Key
load_dotenv()
API_KEY = os.getenv("API_KEY")
API = "http://localhost:9999/v1"
HDRS = {"X-API-key": API_KEY}

# --- CONSTANTS ---
CAD, USD = "CAD", "USD"
BULL, BEAR, RITC = "BULL", "BEAR", "RITC"

# Limits
MAX_GROSS = 300000 
MAX_NET = 200000   
UNWIND_TRIGGER = 0.85  # 85% Full -> Start Unwinding
UNWIND_CHUNK = 1000    

# Thresholds
ARB_THRESHOLD_CAD = 0.05 
TENDER_MIN_PROFIT = 0.15 
HEDGE_DRIFT_LIMIT = 2000 # Re-hedge if USD drift > $2,000

s = requests.Session()
s.headers.update(HDRS)

# ---------------- HELPERS ----------------

def get_tick_status():
    try:
        r = s.get(f"{API}/case")
        if r.ok:
            j = r.json()
            return j["tick"], j["status"]
    except: pass
    return 0, "STOPPED"

def best_bid_ask(ticker):
    try:
        r = s.get(f"{API}/securities/book", params={"ticker": ticker})
        if r.ok:
            book = r.json()
            bid = float(book["bids"][0]["price"]) if book["bids"] else 0.0
            ask = float(book["asks"][0]["price"]) if book["asks"] else 1e12
            return bid, ask
    except: pass
    return 0.0, 1e12

def positions_map():
    try:
        r = s.get(f"{API}/securities")
        if r.ok:
            out = {p["ticker"]: int(p.get("position", 0)) for p in r.json()}
            for k in (BULL, BEAR, RITC, USD, CAD):
                out.setdefault(k, 0)
            return out
    except: pass
    return {}

def get_gross_usage():
    pos = positions_map()
    if not pos: return 0
    return abs(pos[BULL]) + abs(pos[BEAR]) + abs(pos[RITC] * 2)

# ---------------- EXECUTION ----------------

def place_mkt(ticker, action, qty):
    if qty <= 0: return False
    try:
        params = {"ticker": ticker, "type": "MARKET", "quantity": int(qty), "action": action}
        return s.post(f"{API}/orders", params=params).ok
    except Exception as e:
        print(f"Order Error: {e}")
        return False

def place_limit(ticker, action, qty, price):
    if qty <= 0: return False
    try:
        params = {"ticker": ticker, "type": "LIMIT", "quantity": int(qty), "action": action, "price": price}
        return s.post(f"{API}/orders", params=params).ok
    except: return False

def force_hedge_trade(ticker, action, qty):
    """ Retries hedge trades to prevent naked positions. """
    for i in range(5):
        if place_mkt(ticker, action, qty): return True
        sleep(0.1)
    print(f"FATAL: COULD NOT HEDGE {ticker}")
    return False

# ---------------- RISK & CURRENCY GUARD ----------------

def rebalance_currency_hedge():
    """
    [NEW] Checks if our USD Short matches our RITC Assets.
    If RITC price rises, we effectively gain USD value. 
    This function sells more USD to lock that gain into CAD.
    """
    pos = positions_map()
    if not pos: return

    ritc_pos = pos[RITC]
    usd_pos = pos[USD]

    # 1. Get current value of RITC holdings
    # Use Mid-Price for valuation
    bid, ask = best_bid_ask(RITC)
    mid_price = (bid + ask) / 2
    
    # 2. Calculate Target Hedge
    # If we hold $100k of RITC, we want -$100k USD position.
    target_usd_pos = -(ritc_pos * mid_price)
    
    # 3. Calculate Drift
    # e.g. Target -100k, Current -90k -> Drift = -10k (Need to Sell 10k)
    drift = target_usd_pos - usd_pos
    
    # 4. Execute Re-Hedge if drift is significant
    if abs(drift) > HEDGE_DRIFT_LIMIT:
        print(f"💸 CURRENCY DRIFT (${drift:.0f}) detected. Re-balancing...")
        
        if drift > 0:
            # Target is higher (e.g. -80k) than current (-90k). We are over-hedged.
            # We need to BUY USD back.
            place_mkt(USD, "BUY", abs(drift))
        else:
            # Target is lower (e.g. -100k) than current (-90k). We are under-hedged.
            # We need to SELL USD.
            place_mkt(USD, "SELL", abs(drift))

def within_limits(ticker, action, qty):
    """ Smart Limit Logic: Allows risk-reducing trades always. """
    pos = positions_map()
    if not pos: return False
    
    sign_qty = qty if action == "BUY" else -qty
    
    curr_bull = pos[BULL]
    curr_bear = pos[BEAR]
    curr_ritc = pos[RITC] * 2
    
    curr_gross = abs(curr_bull) + abs(curr_bear) + abs(curr_ritc)
    curr_net = curr_bull + curr_bear + curr_ritc

    if ticker == BULL: proj_bull = curr_bull + sign_qty; proj_bear = curr_bear; proj_ritc = curr_ritc
    elif ticker == BEAR: proj_bull = curr_bull; proj_bear = curr_bear + sign_qty; proj_ritc = curr_ritc
    elif ticker == RITC: proj_bull = curr_bull; proj_bear = curr_bear; proj_ritc = curr_ritc + (sign_qty * 2)
    else: return True 

    proj_gross = abs(proj_bull) + abs(proj_bear) + abs(proj_ritc)
    proj_net = proj_bull + proj_bear + proj_ritc
    
    if (proj_gross < MAX_GROSS) and (-MAX_NET < proj_net < MAX_NET): return True
    if proj_gross < curr_gross: return True # Allow unwind
    if abs(proj_net) < abs(curr_net): return True # Allow re-balance

    return False

# ---------------- STRATEGY ----------------

def attempt_unwind():
    """ UNWIND MODE: Passive Limit Orders to clear space. """
    pos = positions_map()
    if not pos or abs(pos[RITC]) < 500: return 

    ritc_bid, ritc_ask = best_bid_ask(RITC)
    bull_bid, bull_ask = best_bid_ask(BULL)
    bear_bid, bear_ask = best_bid_ask(BEAR)

    qty = UNWIND_CHUNK

    if pos[RITC] > 0:
        print(f"🚦 RED LIGHT: Unwinding LONG RITC ({pos[RITC]})...")
        place_limit(RITC, "SELL", qty, ritc_ask) 
        place_limit(BULL, "BUY", qty, bull_bid)  
        place_limit(BEAR, "BUY", qty, bear_bid)  
        # Note: We let 'rebalance_currency_hedge' handle the USD side automatically
        
    elif pos[RITC] < 0:
        print(f"🚦 RED LIGHT: Unwinding SHORT RITC ({pos[RITC]})...")
        place_limit(RITC, "BUY", qty, ritc_bid)  
        place_limit(BULL, "SELL", qty, bull_ask) 
        place_limit(BEAR, "SELL", qty, bear_ask) 

def process_tender_offers():
    try:
        r = s.get(f"{API}/tenders")
        if not r.ok: return
        offers = r.json()
    except: return

    if not offers: return

    bull_bid, bull_ask = best_bid_ask(BULL)
    bear_bid, bear_ask = best_bid_ask(BEAR)
    usd_bid, usd_ask = best_bid_ask(USD)
    syn_bid = bull_bid + bear_bid 
    syn_ask = bull_ask + bear_ask

    for offer in offers:
        tid, action, price, qty = offer['tender_id'], offer['action'], offer['price'], offer['quantity']
        is_prof = False
        
        if action == "BUY": 
            if (syn_bid - (price * usd_ask)) > TENDER_MIN_PROFIT and within_limits(RITC, "BUY", qty): is_prof = True
        elif action == "SELL":
            if ((price * usd_bid) - syn_ask) > TENDER_MIN_PROFIT and within_limits(RITC, "SELL", qty): is_prof = True

        if is_prof:
            print(f"✅ TENDER {action} {qty} @ {price}")
            if s.post(f"{API}/tenders/{tid}").ok:
                # Immediate Hedge (Principal)
                try:
                    usd_val = qty * price
                    if action == "BUY":
                        force_hedge_trade(USD, "SELL", usd_val)
                        force_hedge_trade(BULL, "SELL", qty)
                        force_hedge_trade(BEAR, "SELL", qty)
                    else:
                        force_hedge_trade(USD, "BUY", usd_val)
                        force_hedge_trade(BULL, "BUY", qty)
                        force_hedge_trade(BEAR, "BUY", qty)
                except: pass

def main():
    print("=== RITC ALGO: DELTA NEUTRAL MODE ===")
    tick, status = get_tick_status()
    
    while status == "ACTIVE":
        # 1. Re-Balance Currency (Critical Fix)
        rebalance_currency_hedge()
        
        # 2. Traffic Control
        gross_usage = get_gross_usage()
        if gross_usage > (MAX_GROSS * UNWIND_TRIGGER):
            if tick % 5 == 0: print(f"⚠️ LIMIT WARNING: {gross_usage} -> UNWINDING")
            attempt_unwind()
        else:
            process_tender_offers()
            
            # 3. Market Arb (Optional filler)
            qty = 500
            # ... (Standard Market Arb logic here if desired, typically Tenders are enough)

        sleep(0.2) 
        tick, status = get_tick_status()

if __name__ == "__main__":
    main()