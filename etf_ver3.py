import os
from dotenv import load_dotenv
import requests
from time import sleep

'''
RITC Algorithmic ETF Arbitrage Case - ROBUST HEDGING VERSION
Updates:
1. Force Hedge: Uses try/except and retries to guarantee hedge execution.
2. Smart Limits: Allows trades that reduce risk even if limits are full.
3. [cite_start]Currency Hedging: Locks in FX rates immediately[cite: 407].
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

# Parameters
MAX_GROSS = 300000 # Updated to match PDF [cite: 320]
MAX_NET = 200000   # Updated to match PDF [cite: 320]

ORDER_QTY = 500    
ARB_THRESHOLD_CAD = 0.05 

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
    if qty <= 0: return False
    try:
        qty = int(qty)
        return s.post(f"{API}/orders",
                      params={"ticker": ticker, "type": "MARKET",
                              "quantity": qty, "action": action}).ok
    except Exception as e:
        print(f"Order failed: {e}")
        return False

def force_hedge_trade(ticker, action, qty):
    """
    CRITICAL FUNCTION: Retries a trade until it succeeds or max retries hit.
    Used for hedging to prevent naked positions.
    """
    max_retries = 5
    for i in range(max_retries):
        if place_mkt(ticker, action, qty):
            return True
        print(f"!!! HEDGE FAILED ({ticker} {action}) - RETRYING {i+1}/{max_retries} !!!")
        sleep(0.05) # Tiny pause before retry
    print(f"CRITICAL ERROR: COULD NOT EXECUTE HEDGE FOR {ticker}")
    return False

def within_limits(ticker, action, qty):
    """
    SMART LIMIT CHECK:
    Allows trades that REDUCE exposure even if limits are full.
    """
    pos = positions_map()
    if not pos: return False
    
    # 1. Determine signed quantity of the NEW trade
    if action == "BUY": sign_qty = qty
    else: sign_qty = -qty
        
    # [cite_start]2. Calculate Current Exposures (RITC counts 2x [cite: 392])
    curr_bull = pos[BULL]
    curr_bear = pos[BEAR]
    curr_ritc = pos[RITC] * 2
    
    current_gross = abs(curr_bull) + abs(curr_bear) + abs(curr_ritc)
    current_net = curr_bull + curr_bear + curr_ritc

    # 3. Calculate PROJECTED Exposures
    if ticker == BULL: proj_bull = curr_bull + sign_qty; proj_bear = curr_bear; proj_ritc = curr_ritc
    elif ticker == BEAR: proj_bull = curr_bull; proj_bear = curr_bear + sign_qty; proj_ritc = curr_ritc
    elif ticker == RITC: proj_bull = curr_bull; proj_bear = curr_bear; proj_ritc = curr_ritc + (sign_qty * 2)
    else: return True # Ignore limits for USD/CAD

    proj_gross = abs(proj_bull) + abs(proj_bear) + abs(proj_ritc)
    proj_net = proj_bull + proj_bear + proj_ritc
    
    # RULE A: Allow if within limits
    if (proj_gross < MAX_GROSS) and (-MAX_NET < proj_net < MAX_NET):
        return True
        
    # RULE B: Allow if trade REDUCES Gross usage
    if proj_gross < current_gross: return True
        
    # RULE C: Allow if trade moves Net closer to Zero
    if abs(proj_net) < abs(current_net): return True

    return False

def process_tender_offers():
    """
    Analyzes tenders, accepts if profitable, then FORCES the hedge.
    """
    try:
        r = s.get(f"{API}/tenders")
        r.raise_for_status()
        offers = r.json()
    except Exception:
        return

    if not offers: return

    bull_bid, bull_ask = best_bid_ask(BULL)
    bear_bid, bear_ask = best_bid_ask(BEAR)
    usd_bid, usd_ask = best_bid_ask(USD)

    synthetic_bid_cad = bull_bid + bear_bid 
    synthetic_ask_cad = bull_ask + bear_ask

    for offer in offers:
        tid = offer['tender_id']
        action = offer['action'] # BUY means WE BUY
        price_usd = offer['price']
        quantity = offer['quantity']
        
        is_profitable = False
        
        if action == "BUY": 
            cost_cad = price_usd * usd_ask
            profit = synthetic_bid_cad - cost_cad
            # Check limits using the SMART function
            if profit > 0.15 and within_limits(RITC, "BUY", quantity):
                is_profitable = True

        elif action == "SELL":
            proceeds_cad = price_usd * usd_bid
            profit = proceeds_cad - synthetic_ask_cad
            if profit > 0.15 and within_limits(RITC, "SELL", quantity):
                is_profitable = True

        if is_profitable:
            # 1. Attempt to Accept Tender
            print(f"Accepting Tender {action} {quantity} @ {price_usd}...")
            res = s.post(f"{API}/tenders/{tid}")
            
            # [cite_start]2. IF SUCCESSFUL, FORCE HEDGE [cite: 407]
            if res.ok:
                print(">> TENDER ACCEPTED. FORCING HEDGE...")
                try:
                    usd_value = quantity * price_usd
                    
                    if action == "BUY":
                        # We Bought RITC (Long USD). 
                        # Hedge 1: Sell USD
                        force_hedge_trade(USD, "SELL", usd_value)
                        # Hedge 2: Sell Stocks (Lock Arb)
                        force_hedge_trade(BULL, "SELL", quantity)
                        force_hedge_trade(BEAR, "SELL", quantity)
                        
                    elif action == "SELL":
                        # We Sold RITC (Short USD). 
                        # Hedge 1: Buy USD
                        force_hedge_trade(USD, "BUY", usd_value)
                        # Hedge 2: Buy Stocks (Lock Arb)
                        force_hedge_trade(BULL, "BUY", quantity)
                        force_hedge_trade(BEAR, "BUY", quantity)
                        
                except Exception as e:
                    print(f"CRITICAL EXCEPTION DURING HEDGE: {e}")
            else:
                print("Tender accept failed (likely expired).")

# --------- CORE LOGIC ----------

def step_once():
    bull_bid, bull_ask = best_bid_ask(BULL)
    bear_bid, bear_ask = best_bid_ask(BEAR)
    ritc_bid_usd, ritc_ask_usd = best_bid_ask(RITC)
    usd_bid, usd_ask = best_bid_ask(USD)

    ritc_ask_cad = ritc_ask_usd * usd_ask
    ritc_bid_cad = ritc_bid_usd * usd_bid

    basket_sell_value = bull_bid + bear_bid 
    basket_buy_cost = bull_ask + bear_ask

    edge1 = basket_sell_value - ritc_ask_cad
    edge2 = ritc_bid_cad - basket_buy_cost

    traded = False
    
    # Execution Logic with Smart Limits
    if edge1 >= ARB_THRESHOLD_CAD and within_limits(RITC, "BUY", ORDER_QTY):
        print(f"Ex1: Sell Basket/Buy ETF. Edge: {edge1:.3f}")
        place_mkt(BULL, "SELL", ORDER_QTY)
        place_mkt(BEAR, "SELL", ORDER_QTY)
        place_mkt(RITC, "BUY",  ORDER_QTY)
        
        # Hedge Currency
        usd_qty = ORDER_QTY * ritc_ask_usd 
        place_mkt(USD, "SELL", usd_qty)
        traded = True

    elif edge2 >= ARB_THRESHOLD_CAD and within_limits(RITC, "SELL", ORDER_QTY):
        print(f"Ex2: Buy Basket/Sell ETF. Edge: {edge2:.3f}")
        place_mkt(BULL, "BUY",  ORDER_QTY)
        place_mkt(BEAR, "BUY",  ORDER_QTY)
        place_mkt(RITC, "SELL", ORDER_QTY)
        
        # Hedge Currency
        usd_qty = ORDER_QTY * ritc_bid_usd
        place_mkt(USD, "BUY", usd_qty)
        traded = True

    return traded

def main():
    print("Starting Robust Hedging Algo...")
    tick, status = get_tick_status()
    
    while status == "ACTIVE":
        process_tender_offers()
        step_once()
        sleep(0.2) 
        tick, status = get_tick_status()

if __name__ == "__main__":
    main()