import os
import requests
from time import sleep
from dotenv import load_dotenv

'''
RITC Algorithmic ETF Arbitrage - Complete Delta-Neutral Strategy
--------------------------------------------------------------------
ENHANCED HEDGING FEATURES:
- Dynamic re-hedging of USD currency exposure
- Automatic unwinding when holding large RITC positions
- Smart position management to stay delta-neutral
- Multi-layered hedging: Currency + Components
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

# Hedging Thresholds
ARB_THRESHOLD_CAD = 0.05 
TENDER_MIN_PROFIT = 0.15 
HEDGE_DRIFT_LIMIT = 2000  # Re-hedge if USD drift > $2,000
RITC_HEDGE_THRESHOLD = 2000  # Start aggressive hedging if RITC > this amount
COMPONENT_HEDGE_THRESHOLD = 1500  # Hedge components when imbalance > this

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
    """ Retries hedge trades to prevent naked positions. Chunks large orders. """
    MAX_ORDER = 10000
    remaining = qty
    
    while remaining > 0:
        chunk = min(remaining, MAX_ORDER)
        success = False
        
        for i in range(5):
            if place_mkt(ticker, action, chunk):
                success = True
                break
            sleep(0.1)
        
        if not success:
            print(f"FATAL: COULD NOT HEDGE {ticker} (tried {chunk} shares)")
            return False
        
        remaining -= chunk
        if remaining > 0:
            sleep(0.05)  # Small delay between chunks
    
    return True

# ---------------- ENHANCED HEDGING SYSTEM ----------------

def hedge_large_ritc_position():
    """
    [NEW] Aggressively hedge when holding large RITC positions.
    If we own a lot of RITC, we should hedge by:
    1. Shorting equivalent value in USD (currency hedge)
    2. Shorting BULL and BEAR (component hedge)
    
    This creates a delta-neutral position.
    """
    pos = positions_map()
    if not pos: return
    
    ritc_pos = pos[RITC]
    
    # Check if we have a large RITC position
    if abs(ritc_pos) < RITC_HEDGE_THRESHOLD:
        return  # Position too small to worry about
    
    print(f"🛡️ LARGE RITC POSITION DETECTED: {ritc_pos} shares")
    
    # Get current prices
    ritc_bid, ritc_ask = best_bid_ask(RITC)
    ritc_mid = (ritc_bid + ritc_ask) / 2
    
    bull_bid, bull_ask = best_bid_ask(BULL)
    bear_bid, bear_ask = best_bid_ask(BEAR)
    
    usd_bid, usd_ask = best_bid_ask(USD)
    
    # Calculate what our hedge positions SHOULD be
    target_bull = -ritc_pos  # If long RITC, short BULL
    target_bear = -ritc_pos  # If long RITC, short BEAR
    target_usd_value = -(ritc_pos * ritc_mid)  # If long RITC, short USD value
    
    current_bull = pos[BULL]
    current_bear = pos[BEAR]
    current_usd_value = pos[USD]
    
    # Calculate hedge gaps
    bull_gap = target_bull - current_bull
    bear_gap = target_bear - current_bear
    usd_gap = target_usd_value - current_usd_value
    
    print(f"   BULL gap: {bull_gap}, BEAR gap: {bear_gap}, USD gap: ${usd_gap:.0f}")
    
    # Execute component hedges if gaps are significant (chunked for large orders)
    if abs(bull_gap) > COMPONENT_HEDGE_THRESHOLD:
        action = "SELL" if bull_gap < 0 else "BUY"
        qty = min(abs(bull_gap), 10000)  # Cap at max order size per iteration
        if within_limits(BULL, action, qty):
            print(f"   🔧 Hedging BULL: {action} {qty}")
            force_hedge_trade(BULL, action, qty)
    
    if abs(bear_gap) > COMPONENT_HEDGE_THRESHOLD:
        action = "SELL" if bear_gap < 0 else "BUY"
        qty = min(abs(bear_gap), 10000)
        if within_limits(BEAR, action, qty):
            print(f"   🔧 Hedging BEAR: {action} {qty}")
            force_hedge_trade(BEAR, action, qty)
    
    # Execute currency hedge if gap is significant
    if abs(usd_gap) > HEDGE_DRIFT_LIMIT:
        action = "SELL" if usd_gap < 0 else "BUY"
        qty = min(abs(usd_gap), 100000)  # Cap USD trades
        print(f"   🔧 Hedging USD: {action} ${qty:.0f}")
        force_hedge_trade(USD, action, qty)

def rebalance_currency_hedge():
    """
    Checks if our USD Short matches our RITC Assets.
    If RITC price rises, we effectively gain USD value. 
    This function sells more USD to lock that gain into CAD.
    """
    pos = positions_map()
    if not pos: return

    ritc_pos = pos[RITC]
    usd_pos = pos[USD]
    
    if abs(ritc_pos) < 100:  # Skip if no meaningful RITC position
        return

    # 1. Get current value of RITC holdings
    bid, ask = best_bid_ask(RITC)
    mid_price = (bid + ask) / 2
    
    # 2. Calculate Target Hedge
    # If we hold $100k of RITC, we want -$100k USD position.
    target_usd_pos = -(ritc_pos * mid_price)
    
    # 3. Calculate Drift
    drift = target_usd_pos - usd_pos
    
    # 4. Execute Re-Hedge if drift is significant
    if abs(drift) > HEDGE_DRIFT_LIMIT:
        print(f"💸 CURRENCY DRIFT (${drift:.0f}) detected. Re-balancing...")
        
        if drift > 0:
            # We need to BUY USD back (reduce short)
            place_mkt(USD, "BUY", abs(drift))
        else:
            # We need to SELL USD (increase short)
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
    """ UNWIND MODE: Aggressive unwinding with market orders when needed. """
    pos = positions_map()
    if not pos or abs(pos[RITC]) < 500: return 

    ritc_bid, ritc_ask = best_bid_ask(RITC)
    bull_bid, bull_ask = best_bid_ask(BULL)
    bear_bid, bear_ask = best_bid_ask(BEAR)

    qty = min(UNWIND_CHUNK, abs(pos[RITC]))

    if pos[RITC] > 0:
        print(f"🚦 RED LIGHT: Unwinding LONG RITC ({pos[RITC]})...")
        # Use aggressive market orders if position is very large
        if pos[RITC] > RITC_HEDGE_THRESHOLD:
            place_mkt(RITC, "SELL", qty)
            place_mkt(BULL, "BUY", qty)
            place_mkt(BEAR, "BUY", qty)
        else:
            place_limit(RITC, "SELL", qty, ritc_bid + 0.01) 
            place_limit(BULL, "BUY", qty, bull_ask - 0.01)  
            place_limit(BEAR, "BUY", qty, bear_ask - 0.01)  
        
    elif pos[RITC] < 0:
        print(f"🚦 RED LIGHT: Unwinding SHORT RITC ({pos[RITC]})...")
        if abs(pos[RITC]) > RITC_HEDGE_THRESHOLD:
            place_mkt(RITC, "BUY", qty)
            place_mkt(BULL, "SELL", qty)
            place_mkt(BEAR, "SELL", qty)
        else:
            place_limit(RITC, "BUY", qty, ritc_ask - 0.01)  
            place_limit(BULL, "SELL", qty, bull_bid + 0.01) 
            place_limit(BEAR, "SELL", qty, bear_bid + 0.01) 

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
    print("=== RITC ALGO: DELTA NEUTRAL MODE WITH ENHANCED HEDGING ===")
    tick, status = get_tick_status()
    
    while status == "ACTIVE":
        # 1. Critical: Hedge large RITC positions FIRST
        hedge_large_ritc_position()
        
        # 2. Re-Balance Currency
        rebalance_currency_hedge()
        
        # 3. Traffic Control
        gross_usage = get_gross_usage()
        if gross_usage > (MAX_GROSS * UNWIND_TRIGGER):
            if tick % 5 == 0: print(f"⚠️ LIMIT WARNING: {gross_usage} -> UNWINDING")
            attempt_unwind()
        else:
            process_tender_offers()

        sleep(0.2) 
        tick, status = get_tick_status()

if __name__ == "__main__":
    main()