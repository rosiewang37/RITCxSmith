import os
import requests
from time import sleep
from dotenv import load_dotenv

'''
STRATEGY UPDATE: "MAKE ROOM" LOGIC
1. Adjusted Limits: Updated MAX_GROSS to 300,000 (per your PDF stats).
2. Emergency Liquidation: If a Tender is profitable but exceeds limits, 
   the bot dumps existing positions to accept the tender.
'''

load_dotenv()

# ========= CONFIGURATION =========
API = "http://localhost:9999/v1"
API_KEY = os.getenv("API_KEY")
HDRS = {"X-API-key": API_KEY}

# Tickers
CAD, USD = "CAD", "USD"
BULL, BEAR, RITC = "BULL", "BEAR", "RITC"

# --- 4-TIER SCALING STRATEGY ---
TIER_1_EDGE, TIER_1_QTY = 0.04, 1000   
TIER_2_EDGE, TIER_2_QTY = 0.08, 3000   
TIER_3_EDGE, TIER_3_QTY = 0.15, 6000   
TIER_4_EDGE, TIER_4_QTY = 0.25, 10000  

# Limits (Updated from your PDF logs)
# PDF Source 371: Gross Limit 300k, Net Limit 200k
MAX_TRADE_SIZE = 10000
MAX_GROSS = 290000  # Safety buffer (Limit is 300k)
MAX_NET = 190000    # Safety buffer (Limit is 200k)

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

def place_mkt(ticker, action, qty):
    if qty <= 0: return False
    
    # CASE 1: CURRENCY (USD/CAD) - Limit is 2,500,000
    if ticker in [USD, CAD]:
        max_order = 2500000
        remaining = int(qty)
        # Just send one big order (or loop if it's truly massive, usually one is enough)
        while remaining > 0:
            chunk = min(remaining, max_order)
            try:
                s.post(f"{API}/orders",
                       params={"ticker": ticker, "type": "MARKET",
                               "quantity": chunk, "action": action})
            except: pass
            remaining -= chunk
        return True

    # CASE 2: STOCKS (BULL/BEAR/RITC) - Limit is 10,000
    else:
        max_order = 10000
        remaining = int(qty)
        while remaining > 0:
            chunk = min(remaining, max_order)
            try:
                s.post(f"{API}/orders",
                       params={"ticker": ticker, "type": "MARKET",
                               "quantity": chunk, "action": action})
            except: pass
            remaining -= chunk
        return True

# ========= EMERGENCY LIQUIDATION =========

def close_all_positions():
    """
    Dumps all RITC, BULL, and BEAR positions to 0 immediately.
    Used to free up Gross Limit for a juicy Tender.
    """
    print("!!! EMERGENCY LIQUIDATION INITIATED !!!")
    pos = positions_map()
    
    # [cite_start]1. Close RITC (Priority: Counts 2x for limits [cite: 43])
    if pos.get(RITC, 0) != 0:
        action = "SELL" if pos[RITC] > 0 else "BUY"
        place_mkt(RITC, action, abs(pos[RITC]))

    # 2. Close Stocks
    for tkr in [BULL, BEAR]:
        if pos.get(tkr, 0) != 0:
            action = "SELL" if pos[tkr] > 0 else "BUY"
            place_mkt(tkr, action, abs(pos[tkr]))

# ========= TENDERS =========

def process_tender_offers():
    try:
        r = s.get(f"{API}/tenders")
        if not r.ok: return
        offers = r.json()
    except: return

    if not offers: return

    # Market Data
    bull_bid, bull_ask = best_bid_ask(BULL)
    bear_bid, bear_ask = best_bid_ask(BEAR)
    usd_bid, usd_ask = best_bid_ask(USD)

    synthetic_bid_cad = bull_bid + bear_bid 
    synthetic_ask_cad = bull_ask + bear_ask

    # Get current position usage
    pos = positions_map()
    # [cite_start]Calculate current gross [cite: 43] (RITC * 2)
    curr_gross = abs(pos[BULL]) + abs(pos[BEAR]) + (abs(pos[RITC]) * 2)

    for offer in offers:
        tid = offer['tender_id']
        action = offer['action'] 
        price_usd = offer['price']
        quantity = offer['quantity']
        
        is_profitable = False
        profit = 0
        
        # 1. Calculate Profit
        if action == "BUY": # We Buy RITC
            cost_cad = price_usd * usd_ask
            profit = synthetic_bid_cad - cost_cad
        elif action == "SELL": # We Sell RITC
            proceeds_cad = price_usd * usd_bid
            profit = proceeds_cad - synthetic_ask_cad

        # 2. Decision Logic
        if profit > TIER_1_EDGE:
            # Calculate impact on limits
            # [cite_start]Tender adds: quantity * 2 (since it's RITC) [cite: 43]
            projected_gross = curr_gross + (quantity * 2)

            if projected_gross > MAX_GROSS:
                # LIMIT BREACH DETECTED
                print(f"*** Tender {tid} Profitable ({profit:.3f}) but BREACHES LIMIT ({projected_gross} > {MAX_GROSS})")
                
                # If profit is "Good" (Tier 2+), liquidate to make room
                if profit > TIER_2_EDGE:
                    print(">>> LIQUIDATING EXISTING POSITIONS TO MAKE ROOM")
                    close_all_positions()
                    sleep(0.1) # Brief pause to let trades process
                    
                    # Now Accept
                    resp = s.post(f"{API}/tenders/{tid}")
                    if resp.ok: print(f"*** TENDER {tid} ACCEPTED AFTER LIQUIDATION")
                else:
                    print("Profit too low to justify liquidation. Skipping.")
            else:
                # Fits in limit, accept normally
                resp = s.post(f"{API}/tenders/{tid}")
                if resp.ok: print(f"*** TENDER {tid} ACCEPTED (Profit: {profit:.3f})")

# ========= AUTO-HEDGE =========

def auto_hedge_positions():
    """
    Clean up any mess left by Tenders or Market Trades.
    """
    pos = positions_map()
    if not pos: return

    ritc_pos = pos.get(RITC, 0)
    bull_pos = pos.get(BULL, 0)
    bear_pos = pos.get(BEAR, 0)
    usd_pos  = pos.get(USD, 0)

    # Target: Stocks = -RITC
    target_stock_pos = -1 * ritc_pos
    
    bull_needed = target_stock_pos - bull_pos
    bear_needed = target_stock_pos - bear_pos

    # Execute Corrections
    if abs(bull_needed) > 50:
        place_mkt(BULL, "BUY" if bull_needed > 0 else "SELL", abs(bull_needed))
    
    if abs(bear_needed) > 50:
        place_mkt(BEAR, "BUY" if bear_needed > 0 else "SELL", abs(bear_needed))

    # Currency Hedge
    if abs(usd_pos) > 5000:
        place_mkt(USD, "BUY" if usd_pos < 0 else "SELL", abs(usd_pos))

# ========= EXECUTION =========

def get_dynamic_qty(edge):
    abs_edge = abs(edge)
    if abs_edge >= TIER_4_EDGE: return TIER_4_QTY, "SUPER"
    elif abs_edge >= TIER_3_EDGE: return TIER_3_QTY, "EXTREME"
    elif abs_edge >= TIER_2_EDGE: return TIER_2_QTY, "AGGRESSIVE"
    elif abs_edge >= TIER_1_EDGE: return TIER_1_QTY, "STANDARD"
    return 0, ""

def step_once():
    bull_bid, bull_ask = best_bid_ask(BULL)
    bear_bid, bear_ask = best_bid_ask(BEAR)
    ritc_bid_usd, ritc_ask_usd = best_bid_ask(RITC)
    usd_bid, usd_ask = best_bid_ask(USD)

    ritc_ask_cad = ritc_ask_usd * usd_ask
    ritc_bid_cad = ritc_bid_usd * usd_bid
    
    basket_sell_value = bull_bid + bear_bid
    basket_buy_cost = bull_ask + bear_ask

    edge_buy_etf = basket_sell_value - ritc_ask_cad
    edge_sell_etf = ritc_bid_cad - basket_buy_cost

    # Only trade market arb if we have room
    pos = positions_map()
    curr_gross = abs(pos[BULL]) + abs(pos[BEAR]) + (abs(pos[RITC]) * 2)
    
    if curr_gross < (MAX_GROSS - 20000): # Leave room for tenders
        if edge_buy_etf > TIER_1_EDGE:
            qty, tier = get_dynamic_qty(edge_buy_etf)
            print(f"[{tier}] BUY ETF ARB | Edge: {edge_buy_etf:.3f}")
            place_mkt(BULL, "SELL", qty)
            place_mkt(BEAR, "SELL", qty)
            place_mkt(RITC, "BUY",  qty)

        elif edge_sell_etf > TIER_1_EDGE:
            qty, tier = get_dynamic_qty(edge_sell_etf)
            print(f"[{tier}] SELL ETF ARB | Edge: {edge_sell_etf:.3f}")
            place_mkt(BULL, "BUY",  qty)
            place_mkt(BEAR, "BUY",  qty)
            place_mkt(RITC, "SELL", qty)

# ========= MAIN =========

def main():
    print("--- ALGO STARTED: LIQUIDATION ENABLED ---")
    tick, status = get_tick_status()
    
    while status == "ACTIVE":
        process_tender_offers() # Priority 1
        step_once()             # Priority 2
        auto_hedge_positions()  # Priority 3 (Cleanup)
        sleep(0.2) 
        tick, status = get_tick_status()

if __name__ == "__main__":
    main()