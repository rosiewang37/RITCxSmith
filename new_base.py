import requests
from time import sleep
import numpy as np
import os
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("API_KEY")
API = "http://localhost:9999/v1"
HDRS = {"X-API-key": API_KEY}

# Tickers
CAD  = "CAD"
USD  = "USD"
BULL = "BULL"
BEAR = "BEAR"
RITC = "RITC"

# Fees and Limits
FEE_MKT = 0.02           # [cite: 49]
MAX_SIZE_EQUITY = 10000  # [cite: 48]
MAX_GROSS     = 500000   # [cite: 40]
MAX_SHORT_NET = -25000   # [cite: 40]
MAX_LONG_NET  = 25000    # [cite: 40]
ORDER_QTY     = 5000

# --------- STRATEGY PARAMETERS ----------
# Standard Arb Threshold
ARB_THRESHOLD_CAD = 0.10

# Tender Threshold: Fees are $0.02 * 2 (Bull+Bear) = $0.04. 
# We add 0.06 buffer for slippage/spread crossing.
MIN_TENDER_PROFIT = 0.10 


# --------- SESSION ----------
s = requests.Session()
s.headers.update(HDRS)

# --------- HELPERS ----------
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
    except Exception:
        return 0.0, 1e12

def positions_map():
    try:
        r = s.get(f"{API}/securities")
        r.raise_for_status()
        out = {p["ticker"]: int(p.get("position", 0)) for p in r.json()}
        for k in (BULL, BEAR, RITC, USD, CAD):
            out.setdefault(k, 0)
        return out
    except Exception:
        return {}

def place_mkt(ticker, action, qty):
    # Sends Market orders
    try:
        return s.post(f"{API}/orders",
                      params={"ticker": ticker, "type": "MARKET",
                              "quantity": int(qty), "action": action}).ok
    except Exception as e:
        print(f"Order failed: {e}")
        return False

def within_limits():
    pos = positions_map()
    if not pos: return False
    # [cite_start]RITC multiplier is 2 for gross/net calculation [cite: 43]
    gross = abs(pos[BULL]) + abs(pos[BEAR]) + (2 * abs(pos[RITC])) 
    net   = pos[BULL] + pos[BEAR] + pos[RITC]
    return (gross < MAX_GROSS) and (MAX_SHORT_NET < net < MAX_LONG_NET)

# --------- HEDGING LOGIC (For Tenders) ----------
def unwind_tender_position(action_taken_on_ritc, quantity):
    """
    Executes the offsetting basket trades immediately after a tender is accepted.
    """
    print(f"--- HEDGING TENDER: {action_taken_on_ritc} {quantity} RITC ---")
    
    if action_taken_on_ritc == "BUY":
        # We BOUGHT RITC via Tender, so we SELL the Basket to lock profit
        place_mkt(BULL, "SELL", quantity)
        place_mkt(BEAR, "SELL", quantity)
        
    elif action_taken_on_ritc == "SELL":
        # We SOLD RITC via Tender, so we BUY the Basket to lock profit
        place_mkt(BULL, "BUY", quantity)
        place_mkt(BEAR, "BUY", quantity)

# --------- TENDER EVALUATION ----------
def evaluate_and_accept_tenders(bull_bid, bull_ask, bear_bid, bear_ask, usd_bid, usd_ask):
    """
    Evaluates tenders based on the cost of unwinding via the basket (BULL + BEAR).
    """
    try:
        r = s.get(f"{API}/tenders")
        r.raise_for_status()
        offers = r.json()
    except Exception as e:
        print(f"Error fetching tenders: {e}")
        return

    if not offers:
        return

    for offer in offers:
        tender_id = offer['tender_id']
        action = offer['action'] 
        price_usd = float(offer['price'])
        quantity = int(offer['quantity'])
        
        # Cost to BUY basket (Lift Asks)
        basket_buy_cost_cad = bull_ask + bear_ask + (2 * FEE_MKT) 
        # Proceeds from SELLING basket (Hit Bids)
        basket_sell_val_cad = bull_bid + bear_bid - (2 * FEE_MKT)

        profit = -100.0
        
        if action == "BUY":
            # Tender asks us to BUY RITC. We profit if we can SELL Basket > Cost of RITC
            cost_to_buy_ritc_cad = price_usd * usd_ask
            profit = basket_sell_val_cad - cost_to_buy_ritc_cad
            
            if profit > MIN_TENDER_PROFIT:
                print(f"Accepting BUY Tender. Profit: {profit:.2f}")
                resp = s.post(f"{API}/tenders/{tender_id}")
                if resp.ok:
                    unwind_tender_position("BUY", quantity) # Immediately Hedge

        elif action == "SELL":
            # Tender asks us to SELL RITC. We profit if we can BUY Basket < Revenue from RITC
            revenue_from_sell_ritc_cad = price_usd * usd_bid
            profit = revenue_from_sell_ritc_cad - basket_buy_cost_cad
            
            if profit > MIN_TENDER_PROFIT:
                print(f"Accepting SELL Tender. Profit: {profit:.2f}")
                resp = s.post(f"{API}/tenders/{tender_id}")
                if resp.ok:
                    unwind_tender_position("SELL", quantity) # Immediately Hedge

# --------- CORE LOGIC ----------
def step_once():
    # 1. Get Data
    bull_bid, bull_ask = best_bid_ask(BULL)
    bear_bid, bear_ask = best_bid_ask(BEAR)
    ritc_bid_usd, ritc_ask_usd = best_bid_ask(RITC)
    usd_bid, usd_ask = best_bid_ask(USD)

    # 2. Convert RITC to CAD using USD book for standard Arb
    ritc_bid_cad = ritc_bid_usd * usd_bid
    ritc_ask_cad = ritc_ask_usd * usd_ask

    basket_sell_value = bull_bid + bear_bid
    basket_buy_cost   = bull_ask + bear_ask

    edge1 = basket_sell_value - ritc_ask_cad # Sell Basket, Buy RITC
    edge2 = ritc_bid_cad - basket_buy_cost   # Sell RITC, Buy Basket

    # 3. Check Tenders (AND EXECUTE IF PROFITABLE)
    evaluate_and_accept_tenders(bull_bid, bull_ask, bear_bid, bear_ask, usd_bid, usd_ask)

    traded = False
    
    # 4. Execute Standard Arb
    if edge1 >= ARB_THRESHOLD_CAD and within_limits():
        # Basket rich: sell BULL & BEAR, buy RITC
        q = min(ORDER_QTY, MAX_SIZE_EQUITY)
        place_mkt(BULL, "SELL", q)
        place_mkt(BEAR, "SELL", q)
        place_mkt(RITC, "BUY",  q)
        traded = True

    elif edge2 >= ARB_THRESHOLD_CAD and within_limits():
        # ETF rich: buy BULL & BEAR, sell RITC
        q = min(ORDER_QTY, MAX_SIZE_EQUITY)
        place_mkt(BULL, "BUY",  q)
        place_mkt(BEAR, "BUY",  q)
        place_mkt(RITC, "SELL", q)
        traded = True

    return traded, edge1, edge2

def main():
    tick, status = get_tick_status()
    while status == "ACTIVE":
        traded, e1, e2 = step_once()
        if traded:
            print(f"Trade Executed. E1: {e1:.3f} | E2: {e2:.3f}")
        sleep(0.02)
        tick, status = get_tick_status()

if __name__ == "__main__":
    main()