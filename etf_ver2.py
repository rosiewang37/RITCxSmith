import os
from dotenv import load_dotenv
import requests
from time import sleep

'''
RITC Algorithmic ETF Arbitrage Case - Currency Hedged Strategy
Updates:
1. Tender Offers: Now immediately hedges currency exposure upon acceptance[cite: 407].
2. Market Arb: Executes simultaneous USD trades to lock in FX rates.
3. Limits: RITC counts as 2x for limit calculations[cite: 392].
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
FEE_MKT = 0.02
MAX_SIZE_EQUITY = 10000
MAX_SIZE_FX = 2500000

# Risk Limits [cite: 390, 397]
MAX_LONG_NET = 25000
MAX_SHORT_NET = -25000
MAX_GROSS = 300000

ORDER_QTY = 500    # Size of standard arb clips
ARB_THRESHOLD_CAD = 0.05 

# Session Setup
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
        # Ensure quantity is an integer for API compatibility
        qty = int(qty)
        return s.post(f"{API}/orders",
                      params={"ticker": ticker, "type": "MARKET",
                              "quantity": qty, "action": action}).ok
    except Exception as e:
        print(f"Order failed: {e}")
        return False

def within_limits(pending_qty=0):
    """
    Check if a new trade of 'pending_qty' allows us to stay within limits.
    RITC counts double for limits[cite: 392].
    """
    pos = positions_map()
    if not pos: return False
    
    bull_exp = pos[BULL]
    bear_exp = pos[BEAR]
    ritc_exp = pos[RITC] * 2 

    # Calculate current Gross and Net
    current_gross = abs(bull_exp) + abs(bear_exp) + abs(ritc_exp)
    current_net = bull_exp + bear_exp + ritc_exp 
    
    # Estimate new state
    # Note: This is a rough check. Ideally, you check directional impact.
    if current_gross + pending_qty >= MAX_GROSS:
        print(f"Gross Limit Approaching: {current_gross}")
        return False
        
    # Strict stop if we are already outside net limits
    if not (MAX_SHORT_NET < current_net < MAX_LONG_NET):
        print(f"Net Limit Hit: {current_net}")
        return False
        
    return True

def process_tender_offers():
    """
    Analyzes and accepts tenders, then IMMEDIATELY hedges currency risk.
    """
    try:
        r = s.get(f"{API}/tenders")
        r.raise_for_status()
        offers = r.json()
    except Exception:
        return

    if not offers:
        return

    # Get current market data
    bull_bid, bull_ask = best_bid_ask(BULL)
    bear_bid, bear_ask = best_bid_ask(BEAR)
    usd_bid, usd_ask = best_bid_ask(USD)

    # Synthetic ETF prices in CAD
    synthetic_bid_cad = bull_bid + bear_bid 
    synthetic_ask_cad = bull_ask + bear_ask

    for offer in offers:
        tid = offer['tender_id']
        action = offer['action'] # BUY means WE BUY
        price_usd = offer['price']
        quantity = offer['quantity']
        
        is_profitable = False
        
        if action == "BUY": 
            # Valuation: We Pay USD, Get ETF.
            # Cost = USD Price * USD Ask Rate
            cost_cad = price_usd * usd_ask
            profit = synthetic_bid_cad - cost_cad
            
            if profit > 0.15 and within_limits(quantity * 2):
                is_profitable = True
                print(f"Accepting Tender BUY: Profit {profit:.3f} | P: {price_usd}")

        elif action == "SELL":
            # Valuation: We Sell ETF, Get USD.
            # Proceeds = USD Price * USD Bid Rate
            proceeds_cad = price_usd * usd_bid
            profit = proceeds_cad - synthetic_ask_cad
            
            if profit > 0.15 and within_limits(quantity * 2):
                is_profitable = True
                print(f"Accepting Tender SELL: Profit {profit:.3f} | P: {price_usd}")

        if is_profitable:
            # 1. Accept the Tender
            res = s.post(f"{API}/tenders/{tid}")
            
            if res.ok:
                # 2. IMMEDIATELY HEDGE CURRENCY [cite: 407]
                # Value of the trade in USD
                usd_value = quantity * price_usd
                
                if action == "BUY":
                    # We BOUGHT RITC (Long USD Asset). 
                    # We must SELL USD to hedge.
                    print(f"Hedging: SELLING {int(usd_value)} USD")
                    place_mkt(USD, "SELL", usd_value)
                    
                    # Optional: Sell Stocks to lock in Arb (No-Converter Strategy)
                    place_mkt(BULL, "SELL", quantity)
                    place_mkt(BEAR, "SELL", quantity)
                    
                elif action == "SELL":
                    # We SOLD RITC (Short USD Asset). 
                    # We must BUY USD to hedge.
                    print(f"Hedging: BUYING {int(usd_value)} USD")
                    place_mkt(USD, "BUY", usd_value)

                    # Optional: Buy Stocks to lock in Arb (No-Converter Strategy)
                    place_mkt(BULL, "BUY", quantity)
                    place_mkt(BEAR, "BUY", quantity)

# --------- CORE LOGIC ----------

def step_once():
    # 1. Get executable prices
    bull_bid, bull_ask = best_bid_ask(BULL)
    bear_bid, bear_ask = best_bid_ask(BEAR)
    ritc_bid_usd, ritc_ask_usd = best_bid_ask(RITC)
    usd_bid, usd_ask = best_bid_ask(USD)

    # 2. Convert RITC quotes to CAD using LIVE FX Rates
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
    
    # Execution Logic
    if edge1 >= ARB_THRESHOLD_CAD and within_limits(ORDER_QTY * 2):
        print(f"Ex1: Sell Basket/Buy ETF. Edge: {edge1:.3f}")
        place_mkt(BULL, "SELL", ORDER_QTY)
        place_mkt(BEAR, "SELL", ORDER_QTY)
        place_mkt(RITC, "BUY",  ORDER_QTY)
        
        # HEDGE: We Bought RITC (Long USD Asset) -> SELL USD
        usd_qty = ORDER_QTY * ritc_ask_usd 
        place_mkt(USD, "SELL", usd_qty)
        
        traded = True

    elif edge2 >= ARB_THRESHOLD_CAD and within_limits(ORDER_QTY * 2):
        print(f"Ex2: Buy Basket/Sell ETF. Edge: {edge2:.3f}")
        place_mkt(BULL, "BUY",  ORDER_QTY)
        place_mkt(BEAR, "BUY",  ORDER_QTY)
        place_mkt(RITC, "SELL", ORDER_QTY)
        
        # HEDGE: We Sold RITC (Short USD Asset) -> BUY USD
        usd_qty = ORDER_QTY * ritc_bid_usd
        place_mkt(USD, "BUY", usd_qty)
        
        traded = True

    return traded, edge1, edge2

def main():
    print("Starting Currency-Hedged Algo...")
    tick, status = get_tick_status()
    
    while status == "ACTIVE":
        process_tender_offers()
        step_once()
        sleep(0.2) # Slight delay to prevent API flooding
        tick, status = get_tick_status()

if __name__ == "__main__":
    main()