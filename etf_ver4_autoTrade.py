import os
import requests
from time import sleep
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("API_KEY")
API = "http://localhost:9999/v1"
HDRS = {"X-API-key": API_KEY}

# --- CONSTANTS ---
CAD, USD = "CAD", "USD"
BULL, BEAR, RITC = "BULL", "BEAR", "RITC"

MAX_GROSS = 250000 
MAX_NET = 50000   
UNWIND_CHUNK = 2000    

# Increased margin to account for slippage (Market Impact)
TENDER_MIN_PROFIT = 0.25 
# Increased drift limit to stop "churning" spread payments
HEDGE_DRIFT_LIMIT = 200 

s = requests.Session()
s.headers.update(HDRS)

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
    try:
        params = {"ticker": ticker, "type": "MARKET", "quantity": int(qty), "action": action}
        return s.post(f"{API}/orders", params=params).ok
    except: return False

def force_hedge_trade(ticker, action, qty):
    # Aggressive retry to ensure we don't end up with naked exposure
    for i in range(5):
        if place_mkt(ticker, action, qty): return True
        sleep(0.05)
    print(f"FATAL: FAILED TO HEDGE {ticker}")
    return False

def rebalance_currency_hedge():
    """
    Only trades USD if the drift is massive.
    Natural hedging (cash balance) handles 90% of the work.
    """
    pos = positions_map()
    if not pos: return

    ritc_pos = pos[RITC]
    usd_pos = pos[USD]

    # Calculate RITC value in USD
    bid, ask = best_bid_ask(RITC)
    mid_price = (bid + ask) / 2
    
    # Target: If Long RITC, we expect negative USD balance equal to value
    # If Short RITC, we expect positive USD balance
    target_usd_pos = -(ritc_pos * mid_price)
    
    drift = target_usd_pos - usd_pos
    
    # Only trade if the discrepancy is huge (prevent spread churn)
    if abs(drift) > HEDGE_DRIFT_LIMIT:
        print(f"💸 RE-BALANCING USD Drift: {drift:.0f}")
        action = "BUY" if drift > 0 else "SELL"
        place_mkt(USD, action, abs(drift))

def process_tender_offers():
    try:
        r = s.get(f"{API}/tenders")
        offers = r.json() if r.ok else []
    except: return

    if not offers: return

    # Get Prices
    bull_b, bull_a = best_bid_ask(BULL)
    bear_b, bear_a = best_bid_ask(BEAR)
    usd_b, usd_a = best_bid_ask(USD)
    
    # Synthetic Prices (Cost to Hedge)
    syn_bid = bull_b + bear_b 
    syn_ask = bull_a + bear_a

    for offer in offers:
        tid, action, price, qty = offer['tender_id'], offer['action'], offer['price'], offer['quantity']
        
        # --- LOGIC FIX: "BUY" means YOU Buy from Tender (Long RITC) ---
        # If API "BUY" means "Tender Buys from You", swap these blocks.
        # Standard RIT API: Action is from Participant perspective.
        
        if action == "BUY": 
            # I BUY RITC (pay USD Ask equivalent), I SELL Stocks (receive Syn Bid)
            # Cost = Price * USD_Ask
            # Revenue = Syn_Bid
            expected_profit = syn_bid - (price * usd_a)
            
            if expected_profit > TENDER_MIN_PROFIT:
                print(f"✅ ACCEPT BUY: Profit {expected_profit:.2f}")
                if s.post(f"{API}/tenders/{tid}").ok:
                    # ONLY HEDGE STOCKS. USD IS HEDGED AUTOMATICALLY BY PURCHASE.
                    force_hedge_trade(BULL, "SELL", qty)
                    force_hedge_trade(BEAR, "SELL", qty)

        elif action == "SELL":
            # I SELL RITC (receive USD Bid equivalent), I BUY Stocks (pay Syn Ask)
            # Revenue = Price * USD_Bid
            # Cost = Syn_Ask
            expected_profit = (price * usd_b) - syn_ask
            
            if expected_profit > TENDER_MIN_PROFIT:
                print(f"✅ ACCEPT SELL: Profit {expected_profit:.2f}")
                if s.post(f"{API}/tenders/{tid}").ok:
                    # ONLY HEDGE STOCKS. USD IS HEDGED AUTOMATICALLY BY SALE.
                    force_hedge_trade(BULL, "BUY", qty)
                    force_hedge_trade(BEAR, "BUY", qty)

def main():
    print("=== RITC ALGO: FIXED HEDGING ===")
    while True:
        tick, status = get_tick_status()
        if status == "ACTIVE":
            process_tender_offers()
            rebalance_currency_hedge()
            sleep(0.02)
        else:
            sleep(0.02)

if __name__ == "__main__":
    main()