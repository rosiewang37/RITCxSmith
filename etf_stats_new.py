import os
import requests
from time import sleep
from dotenv import load_dotenv

'''
STRATEGY FIX: AGGRESSIVE HEDGING & SAFETY LOCKS
1. Safety Lock: Do not accept new tenders if we are currently holding a position.
   (Prevents stacking bad trades on top of each other).
2. Aggressive Hedge: Loop until the hedge is confirmed filled.
'''

load_dotenv()

# ========= CONFIGURATION =========
API = "http://localhost:9999/v1"
API_KEY = os.getenv("API_KEY")
HDRS = {"X-API-key": API_KEY}

# Tickers
CAD, USD = "CAD", "USD"
BULL, BEAR, RITC = "BULL", "BEAR", "RITC"

# Profit Thresholds
MIN_PROFIT_PER_SHARE = 0.05

# Limits
MAX_TRADE_SIZE_STOCK = 10000
MAX_TRADE_SIZE_FX = 2500000 
MAX_GROSS = 290000  

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
    
    if ticker in [USD, CAD]:
        max_order = MAX_TRADE_SIZE_FX 
    else:
        max_order = MAX_TRADE_SIZE_STOCK 

    remaining = int(qty)
    
    # Aggressive Loop: Try until we send all volume
    while remaining > 0:
        chunk = min(remaining, max_order)
        try:
            resp = s.post(f"{API}/orders",
                   params={"ticker": ticker, "type": "MARKET",
                           "quantity": chunk, "action": action})
            if not resp.ok:
                # If rejected, wait briefly and retry (Critical for Hedging)
                sleep(0.05)
        except: pass
        remaining -= chunk
    return True

def get_weighted_price(ticker, action, quantity):
    try:
        r = s.get(f"{API}/securities/book", params={"ticker": ticker})
        if not r.ok: return None
        book = r.json()
        orders = book["asks"] if action == "BUY" else book["bids"]
        if not orders: return None

        filled = 0
        total_cost = 0
        for level in orders:
            price = float(level["price"])
            qty = int(level["quantity"])
            take = min(qty, quantity - filled)
            total_cost += take * price
            filled += take
            if filled >= quantity: break
        
        if filled < quantity: return None
        return total_cost / filled
    except: return None

# ========= EMERGENCY LIQUIDATION =========

def close_all_positions():
    print("!!! EMERGENCY LIQUIDATION !!!")
    pos = positions_map()
    if pos.get(RITC, 0) != 0:
        action = "SELL" if pos[RITC] > 0 else "BUY"
        place_mkt(RITC, action, abs(pos[RITC]))
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

    # 1. SAFETY CHECK: Do not accept new tenders if we have "baggage"
    # If we are currently unhedged (non-zero positions), fix that first!
    pos = positions_map()
    curr_gross = abs(pos[BULL]) + abs(pos[BEAR]) + (abs(pos[RITC]) * 2)
    
    # If we have significant positions (> 1000 shares), STOP and clear them.
    if curr_gross > 5000:
        # Exception: If we are liquidating, we might accept.
        # But generally, finish the last meal before eating the next one.
        # We skip processing tenders to let auto-hedge do its job.
        return 

    usd_bid, usd_ask = best_bid_ask(USD)

    for offer in offers:
        tid = offer['tender_id']
        action = offer['action'] 
        price_usd = offer['price']
        quantity = offer['quantity']
        
        # Walk the Book Logic
        if action == "BUY": 
            real_bull_price = get_weighted_price(BULL, "SELL", quantity)
            real_bear_price = get_weighted_price(BEAR, "SELL", quantity)
            if not real_bull_price or not real_bear_price: continue
            cost_cad = price_usd * usd_ask
            profit = (real_bull_price + real_bear_price) - cost_cad

        elif action == "SELL": 
            real_bull_price = get_weighted_price(BULL, "BUY", quantity)
            real_bear_price = get_weighted_price(BEAR, "BUY", quantity)
            if not real_bull_price or not real_bear_price: continue
            proceeds_cad = price_usd * usd_bid
            profit = proceeds_cad - (real_bull_price + real_bear_price)
            
        # Decision
        if profit > MIN_PROFIT_PER_SHARE:
            projected_gross = curr_gross + (quantity * 2)
            should_accept = False
            
            if projected_gross > MAX_GROSS:
                if profit > 0.20: # Higher threshold for liquidation
                    print(f"*** Tender {tid} (Profit {profit:.3f}) - LIQUIDATING")
                    close_all_positions()
                    sleep(0.1)
                    should_accept = True
            else:
                should_accept = True
                
            if should_accept:
                resp = s.post(f"{API}/tenders/{tid}")
                if resp.ok:
                    print(f"*** ACCEPTED {tid} (Profit: {profit:.3f}) - HEDGING")
                    
                    # IMMEDIATE HEDGE
                    if action == "BUY":
                        place_mkt(BULL, "SELL", quantity)
                        place_mkt(BEAR, "SELL", quantity)
                        place_mkt(USD, "SELL", quantity * price_usd)
                    else:
                        place_mkt(BULL, "BUY", quantity)
                        place_mkt(BEAR, "BUY", quantity)
                        place_mkt(USD, "BUY", quantity * price_usd)

# ========= AUTO-HEDGE (CLEANUP) =========

def auto_hedge_positions():
    pos = positions_map()
    if not pos: return

    ritc_pos = pos.get(RITC, 0)
    bull_pos = pos.get(BULL, 0)
    bear_pos = pos.get(BEAR, 0)
    usd_pos  = pos.get(USD, 0)

    target_stock_pos = -1 * ritc_pos
    bull_needed = target_stock_pos - bull_pos
    bear_needed = target_stock_pos - bear_pos

    # If mismatches exist, force trades
    if abs(bull_needed) > 10:
        place_mkt(BULL, "BUY" if bull_needed > 0 else "SELL", abs(bull_needed))
    if abs(bear_needed) > 10:
        place_mkt(BEAR, "BUY" if bear_needed > 0 else "SELL", abs(bear_needed))
    if abs(usd_pos) > 1000:
        place_mkt(USD, "BUY" if usd_pos < 0 else "SELL", abs(usd_pos))

# ========= EXECUTION =========

def step_once():
    # Basic Market Arb (Small Size)
    bull_bid, bull_ask = best_bid_ask(BULL)
    bear_bid, bear_ask = best_bid_ask(BEAR)
    ritc_bid_usd, ritc_ask_usd = best_bid_ask(RITC)
    usd_bid, usd_ask = best_bid_ask(USD)

    ritc_ask_cad = ritc_ask_usd * usd_ask
    ritc_bid_cad = ritc_bid_usd * usd_bid
    basket_sell = bull_bid + bear_bid
    basket_buy = bull_ask + bear_ask

    edge_buy_etf = basket_sell - ritc_ask_cad
    edge_sell_etf = ritc_bid_cad - basket_buy
    
    qty = 1000
    pos = positions_map()
    curr_gross = abs(pos[BULL]) + abs(pos[BEAR]) + (abs(pos[RITC]) * 2)

    if curr_gross < (MAX_GROSS - 20000):
        if edge_buy_etf > 0.05:
            place_mkt(BULL, "SELL", qty)
            place_mkt(BEAR, "SELL", qty)
            place_mkt(RITC, "BUY",  qty)
        elif edge_sell_etf > 0.05:
            place_mkt(BULL, "BUY",  qty)
            place_mkt(BEAR, "BUY",  qty)
            place_mkt(RITC, "SELL", qty)

def main():
    print("--- ALGO STARTED: AGGRESSIVE HEDGE ---")
    tick, status = get_tick_status()
    while status == "ACTIVE":
        process_tender_offers() 
        step_once()             
        auto_hedge_positions()  
        sleep(0.15) # Slightly faster
        tick, status = get_tick_status()

if __name__ == "__main__":
    main()