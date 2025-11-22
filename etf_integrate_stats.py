import os
import requests
from time import sleep
from dotenv import load_dotenv

'''
MERGED STRATEGY:
1. Core Logic: Deterministic ETF Arbitrage (From your code)
2. Execution Logic: 4-Tier Scaling Strategy (From friend's code)
   - Tier 1 (Standard): Small edge -> Small trade
   - Tier 2 (Aggressive): Medium edge -> Medium trade
   - Tier 3 (Extreme): Large edge -> Large trade
   - Tier 4 (Super): Massive edge -> Max trade
'''

load_dotenv()

# ========= CONFIGURATION =========
API = "http://localhost:9999/v1"
API_KEY = os.getenv("API_KEY")
HDRS = {"X-API-key": API_KEY}

# Tickers
CAD, USD = "CAD", "USD"
BULL, BEAR, RITC = "BULL", "BEAR", "RITC"

# --- 4-TIER SCALING STRATEGY (Adapted for ETF) ---
# We map "Profit Edge (CAD)" to "Order Size"

# Tier 1: Standard
TIER_1_EDGE = 0.04   # Minimum profit to trade
TIER_1_QTY  = 1000   # Quantity to trade

# Tier 2: Aggressive
TIER_2_EDGE = 0.08
TIER_2_QTY  = 3000

# Tier 3: Extreme
TIER_3_EDGE = 0.15
TIER_3_QTY  = 6000

# Tier 4: Super Extreme
TIER_4_EDGE = 0.25
TIER_4_QTY  = 10000  # Max allow per order

# Limits
MAX_LONG_NET = 25000
MAX_SHORT_NET = -25000
MAX_GROSS = 500000
MAX_TRADE_SIZE = 10000

# Session
s = requests.Session()
s.headers.update(HDRS)

# ========= HELPERS =========

def get_tick_status():
    try:
        r = s.get(f"{API}/case")
        if r.ok: return r.json()["tick"], r.json()["status"]
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
            for k in (BULL, BEAR, RITC, USD, CAD): out.setdefault(k, 0)
            return out
    except: pass
    return {}

def within_limits(pending_ritc_qty):
    """
    Checks if adding 'pending_ritc_qty' keeps us safe.
    Remember: RITC counts as 2x for Gross/Net limits.
    """
    pos = positions_map()
    if not pos: return False

    # Current positions
    net = pos[BULL] + pos[BEAR] + (pos[RITC] * 2)
    gross = abs(pos[BULL]) + abs(pos[BEAR]) + abs(pos[RITC] * 2)

    # Impact of new trade (Assuming worst case for gross)
    # Since we arb (Buy RITC / Sell Stock), the NET stays roughly neutral 
    # physically, but the RIT limit calculation treats them differently.
    # We will check the ETF impact primarily.
    
    impact = abs(pending_ritc_qty * 2)
    
    if gross + impact > MAX_GROSS: return False
    if not (MAX_SHORT_NET < net + pending_ritc_qty < MAX_LONG_NET): return False
    
    return True

def place_mkt(ticker, action, qty):
    if qty <= 0: return False
    qty = min(int(qty), MAX_TRADE_SIZE)
    try:
        return s.post(f"{API}/orders",
                      params={"ticker": ticker, "type": "MARKET",
                              "quantity": qty, "action": action}).ok
    except: return False

# ========= TENDER LOGIC (From your code) =========

def process_tender_offers():
    """
    Accepts tenders ONLY if they are profitable compared to market prices.
    """
    try:
        r = s.get(f"{API}/tenders")
        if not r.ok: return
        offers = r.json()
    except: return

    if not offers: return

    bull_bid, bull_ask = best_bid_ask(BULL)
    bear_bid, bear_ask = best_bid_ask(BEAR)
    usd_bid, usd_ask = best_bid_ask(USD)

    # Synthetic Prices in CAD
    synthetic_bid_cad = bull_bid + bear_bid 
    synthetic_ask_cad = bull_ask + bear_ask

    for offer in offers:
        tid = offer['tender_id']
        action = offer['action'] 
        price_usd = offer['price']
        quantity = offer['quantity']
        
        is_profitable = False
        profit = 0
        
        # Calculate Profit
        if action == "BUY": # Case: We Buy RITC
            cost_cad = price_usd * usd_ask
            profit = synthetic_bid_cad - cost_cad
            if profit > TIER_1_EDGE: is_profitable = True

        elif action == "SELL": # Case: We Sell RITC
            proceeds_cad = price_usd * usd_bid
            profit = proceeds_cad - synthetic_ask_cad
            if profit > TIER_1_EDGE: is_profitable = True

        if is_profitable:
            print(f"*** TENDER DETECTED: Profit {profit:.3f} | Accepting...")
            s.post(f"{API}/tenders/{tid}")

# ========= TIERED ARBITRAGE ENGINE =========

def get_dynamic_qty(edge):
    """
    Determines trade size based on how juicy the profit is.
    """
    abs_edge = abs(edge)
    
    if abs_edge >= TIER_4_EDGE:
        return TIER_4_QTY, "SUPER EXTREME"
    elif abs_edge >= TIER_3_EDGE:
        return TIER_3_QTY, "EXTREME"
    elif abs_edge >= TIER_2_EDGE:
        return TIER_2_QTY, "AGGRESSIVE"
    elif abs_edge >= TIER_1_EDGE:
        return TIER_1_QTY, "STANDARD"
    else:
        return 0, "NO TRADE"

def step_once():
    # 1. Data Collection
    bull_bid, bull_ask = best_bid_ask(BULL)
    bear_bid, bear_ask = best_bid_ask(BEAR)
    ritc_bid_usd, ritc_ask_usd = best_bid_ask(RITC)
    usd_bid, usd_ask = best_bid_ask(USD)

    # 2. Conversions
    ritc_ask_cad = ritc_ask_usd * usd_ask
    ritc_bid_cad = ritc_bid_usd * usd_bid
    
    basket_sell_value = bull_bid + bear_bid
    basket_buy_cost = bull_ask + bear_ask

    # 3. Calculate Edges
    # Scenario A: Sell Basket, Buy ETF
    edge_buy_etf = basket_sell_value - ritc_ask_cad
    
    # Scenario B: Buy Basket, Sell ETF
    edge_sell_etf = ritc_bid_cad - basket_buy_cost

    # 4. Tiered Execution Decision
    qty = 0
    tier_name = ""
    
    # --- LOGIC PATH A: ETF IS CHEAP ---
    if edge_buy_etf > TIER_1_EDGE:
        qty, tier_name = get_dynamic_qty(edge_buy_etf)
        
        if qty > 0 and within_limits(qty):
            print(f"[{tier_name}] BUY ETF | Profit: {edge_buy_etf:.3f} | Size: {qty}")
            place_mkt(BULL, "SELL", qty)
            place_mkt(BEAR, "SELL", qty)
            place_mkt(RITC, "BUY",  qty)
            place_mkt(USD, "SELL",  qty * ritc_ask_usd) # Hedge USD
            return True

    # --- LOGIC PATH B: ETF IS EXPENSIVE ---
    elif edge_sell_etf > TIER_1_EDGE:
        qty, tier_name = get_dynamic_qty(edge_sell_etf)
        
        if qty > 0 and within_limits(qty):
            print(f"[{tier_name}] SELL ETF | Profit: {edge_sell_etf:.3f} | Size: {qty}")
            place_mkt(BULL, "BUY",  qty)
            place_mkt(BEAR, "BUY",  qty)
            place_mkt(RITC, "SELL", qty)
            place_mkt(USD, "BUY",   qty * ritc_bid_usd) # Hedge USD
            return True

    return False

# ========= MAIN =========

def main():
    print("--- ALGO STARTED: TIERED ETF ARBITRAGE ---")
    print(f"Tier 1: >{TIER_1_EDGE} CAD")
    print(f"Tier 4: >{TIER_4_EDGE} CAD (Max Size)")
    
    tick, status = get_tick_status()
    
    while status == "ACTIVE":
        process_tender_offers()
        step_once()
        
        # Speed control - Fast enough to catch arbs, slow enough to not crash
        sleep(0.2) 
        tick, status = get_tick_status()

if __name__ == "__main__":
    main()