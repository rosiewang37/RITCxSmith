import os
from dotenv import load_dotenv
import requests
from time import sleep

'''
RITC Algorithmic ETF Arbitrage - Dynamic Sizing Edition
Updates:
1. Dynamic Sizing: Trade size scales with profit margin (Edge).
2. Hedging: Maintains strict stock and currency hedging on all sized trades.
'''

load_dotenv()

API = "http://localhost:9999/v1"
API_KEY = os.getenv("API_KEY")
HDRS = {"X-API-key": API_KEY}

# Tickers
CAD, USD = "CAD", "USD"
BULL, BEAR, RITC = "BULL", "BEAR", "RITC"

# Parameters
FEE_MKT = 0.02
MAX_GROSS = 300000

MAX_LONG_NET = 25000
MAX_SHORT_NET = -25000
MAX_CASH_GROSS = 10000000

# Thresholds
ARB_THRESHOLD_CAD = 0.05 

# Session Setup
s = requests.Session()
s.headers.update(HDRS)

# --------- HELPERS (Unchanged) ----------

def get_tick_status():
    try:
        r = s.get(f"{API}/case")
        r.raise_for_status(); return r.json()["tick"], r.json()["status"]
    except: return 0, "STOPPED"

def best_bid_ask(ticker):
    try:
        r = s.get(f"{API}/securities/book", params={"ticker": ticker})
        book = r.json()
        return (float(book["bids"][0]["price"]) if book["bids"] else 0.0, 
                float(book["asks"][0]["price"]) if book["asks"] else 1e12)
    except: return 0.0, 1e12

def positions_map():
    try:
        r = s.get(f"{API}/securities")
        out = {p["ticker"]: int(p.get("position", 0)) for p in r.json()}
        for k in (BULL, BEAR, RITC, USD, CAD): out.setdefault(k, 0)
        return out
    except: return {}

def place_mkt(ticker, action, qty):
    if qty <= 0: return False
    try:
        return s.post(f"{API}/orders", params={"ticker": ticker, "type": "MARKET", "quantity": int(qty), "action": action}).ok
    except: return False

def within_limits(ticker, action, qty, price=0):
    pos = positions_map()
    if not pos: return False
    
    # 1. Check Stock Limits (Standard)
    curr_gross = abs(pos[BULL]) + abs(pos[BEAR]) + abs(pos[RITC] * 2)
    stock_limit_ok = (curr_gross + (qty * 2)) < MAX_GROSS

    # 2. Check Cash Limits (CRITICAL for large orders)
    # Estimate USD impact: If Buying RITC, we must Sell USD.
    curr_usd = pos[USD]
    usd_impact = 0
    
    if ticker == RITC:
        # Hedge value = Qty * Price
        usd_impact = qty * price 
        # If Buying RITC, we Sell USD (add negative USD). If Selling RITC, Buy USD.
        if action == "BUY": usd_impact = -usd_impact
    
    proj_usd = curr_usd + usd_impact
    cash_limit_ok = abs(proj_usd) < MAX_CASH_GROSS

    if not cash_limit_ok:
        print(f"⚠️ BLOCKED: Cash Limit Risk! Proj USD: {proj_usd:,.0f}")
        return False

    return stock_limit_ok
    pos = positions_map()
    if not pos: return False
    curr_gross = abs(pos[BULL]) + abs(pos[BEAR]) + abs(pos[RITC] * 2)
    curr_net = pos[BULL] + pos[BEAR] + pos[RITC] 
    
    if curr_gross + pending_qty >= MAX_GROSS: return False
    if not (MAX_SHORT_NET < curr_net < MAX_LONG_NET): return False
    return True

def process_tender_offers():
    # (Kept simplified for brevity - assuming previous logic exists)
    pass 

# --------- CORE LOGIC (EDITED SECTION) ----------

def step_once():
    # 1. Get executable prices
    bull_bid, bull_ask = best_bid_ask(BULL)
    bear_bid, bear_ask = best_bid_ask(BEAR)
    ritc_bid_usd, ritc_ask_usd = best_bid_ask(RITC)
    usd_bid, usd_ask = best_bid_ask(USD)

    # 2. Convert RITC quotes to CAD
    ritc_ask_cad = ritc_ask_usd * usd_ask
    ritc_bid_cad = ritc_bid_usd * usd_bid

    # 3. Calculate Basket Values
    basket_sell_value = bull_bid + bear_bid 
    basket_buy_cost = bull_ask + bear_ask

    # 4. Calculate Edges
    # Ex1: Basket is Rich -> Sell Basket, Buy RITC
    edge1 = basket_sell_value - ritc_ask_cad
    # Ex2: ETF is Rich -> Sell RITC, Buy Basket
    edge2 = ritc_bid_cad - basket_buy_cost

    traded = False
    
    # --- DYNAMIC SIZING LOGIC START ---
    
    # Strategy 1: Buy ETF / Sell Basket
    if edge1 >= ARB_THRESHOLD_CAD:
        # Determine Size based on Edge Strength
        if edge1 > 0.20:
            qty = 10000 # Max aggression for huge edge
            print(f"🔥🔥 HUGE EDGE ({edge1:.2f}) - Trading 10k")
        elif edge1 > 0.10:
            qty = 5000  # Medium aggression
            print(f"🔥 GOOD EDGE ({edge1:.2f}) - Trading 5k")
        else:
            qty = 1000  # Standard aggression
            
        # Check limits with dynamic quantity
        if within_limits(qty * 2):
            place_mkt(BULL, "SELL", qty)
            place_mkt(BEAR, "SELL", qty)
            place_mkt(RITC, "BUY",  qty)
            
            # DYNAMIC HEDGE: Scale currency trade to match size
            usd_qty = qty * ritc_ask_usd 
            place_mkt(USD, "SELL", usd_qty)
            traded = True

    # Strategy 2: Sell ETF / Buy Basket
    elif edge2 >= ARB_THRESHOLD_CAD:
        # Determine Size based on Edge Strength
        if edge2 > 0.20:
            qty = 10000
            print(f"🔥🔥 HUGE EDGE ({edge2:.2f}) - Trading 10k")
        elif edge2 > 0.10:
            qty = 5000
            print(f"🔥 GOOD EDGE ({edge2:.2f}) - Trading 5k")
        else:
            qty = 1000
            
        # Check limits with dynamic quantity
        if within_limits(qty * 2):
            place_mkt(BULL, "BUY",  qty)
            place_mkt(BEAR, "BUY",  qty)
            place_mkt(RITC, "SELL", qty)
            
            # DYNAMIC HEDGE: Scale currency trade to match size
            usd_qty = qty * ritc_bid_usd
            place_mkt(USD, "BUY", usd_qty)
            traded = True
            
    # --- DYNAMIC SIZING LOGIC END ---

    return traded, edge1, edge2

def main():
    print("Starting Dynamic Sizing Algo...")
    tick, status = get_tick_status()
    
    while status == "ACTIVE":
        process_tender_offers()
        step_once()
        sleep(0.2) 
        tick, status = get_tick_status()

if __name__ == "__main__":
    main()