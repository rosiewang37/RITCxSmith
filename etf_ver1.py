import os
from dotenv import load_dotenv
import requests
from time import sleep

'''
RITC Algorithmic ETF Arbitrage Case - Updated Strategy
Updates based on Case PDF:
1. Tender Offers: Now evaluates profitability against current stock prices before accepting[cite: 10].
2. Limits: RITC counts as 2x for limit calculations.
3. Hedging: Executes USD trades to hedge currency exposure on ETF trades.
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

# Risk Limits [cite: 40, 41]
# Note: PDF states limits are strictly enforced.
MAX_LONG_NET = 25000
MAX_SHORT_NET = -25000
MAX_GROSS = 500000

ORDER_QTY = 500    # Size of standard arb clips
ARB_THRESHOLD_CAD = 0.05 # Lowered slightly to catch more volume, adjust based on volatility

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
        return s.post(f"{API}/orders",
                      params={"ticker": ticker, "type": "MARKET",
                              "quantity": int(qty), "action": action}).ok
    except Exception as e:
        print(f"Order failed: {e}")
        return False

def within_limits(pending_qty=0):
    """
    Check if a new trade of 'pending_qty' allows us to stay within limits.
    Updated per PDF: ETF position has multiplier of 2.
    """
    pos = positions_map()
    if not pos: return False
    
    # RITC counts double for limits
    bull_exp = pos[BULL]
    bear_exp = pos[BEAR]
    ritc_exp = pos[RITC] * 2 

    gross = abs(bull_exp) + abs(bear_exp) + abs(ritc_exp) + pending_qty
    net = bull_exp + bear_exp + ritc_exp # Net usually sums signed raw positions
    
    # Check if adding pending_qty breaches limit (simplification: assumes pending is adding to gross)
    if gross >= MAX_GROSS:
        print(f"Gross Limit Hit: {gross}")
        return False
    if not (MAX_SHORT_NET < net < MAX_LONG_NET):
        print(f"Net Limit Hit: {net}")
        return False
    return True

def process_tender_offers():
    """
    Analyzes and accepts/rejects tender offers based on profitability.
    Ref: 
    """
    try:
        r = s.get(f"{API}/tenders")
        r.raise_for_status()
        offers = r.json()
    except Exception:
        return

    if not offers:
        return

    # Get current market data for valuation
    bull_bid, bull_ask = best_bid_ask(BULL)
    bear_bid, bear_ask = best_bid_ask(BEAR)
    usd_bid, usd_ask = best_bid_ask(USD)

    # Calculate synthetic ETF prices in CAD
    # If we BUY the ETF (we need to know what we can sell the components for)
    synthetic_bid_cad = bull_bid + bear_bid 
    # If we SELL the ETF (we need to know what it costs to buy components)
    synthetic_ask_cad = bull_ask + bear_ask

    for offer in offers:
        tid = offer['tender_id']
        # The 'action' in RIT is usually from the perspective of the Participant
        # BUT sometimes it is "Server Action". 
        # Standard RIT Convention: Action "BUY" means YOU BUY. 
        # PLEASE VERIFY IN CLIENT: If the tender says "Action: BUY", does it mean you buy? 
        # Assuming Action = Direction the PARTICIPANT takes.
        
        action = offer['action'] # BUY or SELL
        price_usd = offer['price']
        quantity = offer['quantity']
        
        is_profitable = False
        
        if action == "BUY": 
            # We BUY RITC @ price_usd. We can sell components @ synthetic_bid_cad.
            # Cost in CAD = price_usd * usd_ask (we buy USD to pay for tender)
            cost_cad = price_usd * usd_ask
            profit = synthetic_bid_cad - cost_cad
            
            # Check if profit covers fees (approx 0.06 per unit total) + margin
            if profit > 0.10 and within_limits(quantity * 2):
                is_profitable = True
                print(f"Accepting Tender BUY: Profit {profit:.3f} | P: {price_usd}")

        elif action == "SELL":
            # We SELL RITC @ price_usd. We buy components @ synthetic_ask_cad.
            # Proceeds in CAD = price_usd * usd_bid (we sell the USD proceeds)
            proceeds_cad = price_usd * usd_bid
            profit = proceeds_cad - synthetic_ask_cad
            
            if profit > 0.10 and within_limits(quantity * 2):
                is_profitable = True
                print(f"Accepting Tender SELL: Profit {profit:.3f} | P: {price_usd}")

        if is_profitable:
            s.post(f"{API}/tenders/{tid}")
            # Note: Once accepted, the main loop 'step_once' will naturally start unwinding 
            # this position if an arbitrage opportunity exists.

# --------- CORE LOGIC ----------

def step_once():
    # 1. Get executable prices
    bull_bid, bull_ask = best_bid_ask(BULL)
    bear_bid, bear_ask = best_bid_ask(BEAR)
    ritc_bid_usd, ritc_ask_usd = best_bid_ask(RITC)
    usd_bid, usd_ask = best_bid_ask(USD)

    # 2. Convert RITC quotes to CAD
    # To BUY RITC (Ask): We pay USD Ask Price * USD/CAD Ask Rate
    ritc_ask_cad = ritc_ask_usd * usd_ask
    # To SELL RITC (Bid): We get USD Bid Price * USD/CAD Bid Rate
    ritc_bid_cad = ritc_bid_usd * usd_bid

    # 3. Calculate Basket Values
    # Value if we SELL basket (Hit Bids)
    basket_sell_value = bull_bid + bear_bid 
    # Cost if we BUY basket (Lift Asks)
    basket_buy_cost = bull_ask + bear_ask

    # 4. Calculate Edges
    # Strategy 1: Basket is Rich (Sell Basket, Buy ETF)
    # Sell BULL+BEAR, Buy RITC
    edge1 = basket_sell_value - ritc_ask_cad

    # Strategy 2: ETF is Rich (Sell ETF, Buy Basket)
    # Sell RITC, Buy BULL+BEAR
    edge2 = ritc_bid_cad - basket_buy_cost

    traded = False
    
    # Execution Logic
    if edge1 >= ARB_THRESHOLD_CAD and within_limits(ORDER_QTY * 2):
        # Sell Basket / Buy ETF
        print(f"Ex1: Sell Basket/Buy ETF. Edge: {edge1:.3f}")
        place_mkt(BULL, "SELL", ORDER_QTY)
        place_mkt(BEAR, "SELL", ORDER_QTY)
        place_mkt(RITC, "BUY",  ORDER_QTY)
        
        # HEDGE CURRENCY 
        # We Bought RITC (USD Asset), so we are Long USD. 
        # To hedge, we SELL USD.
        usd_qty = ORDER_QTY * ritc_ask_usd # approx value
        place_mkt(USD, "SELL", usd_qty) 
        
        traded = True

    elif edge2 >= ARB_THRESHOLD_CAD and within_limits(ORDER_QTY * 2):
        # Buy Basket / Sell ETF
        print(f"Ex2: Buy Basket/Sell ETF. Edge: {edge2:.3f}")
        place_mkt(BULL, "BUY",  ORDER_QTY)
        place_mkt(BEAR, "BUY",  ORDER_QTY)
        place_mkt(RITC, "SELL", ORDER_QTY)
        
        # HEDGE CURRENCY 
        # We Sold RITC (USD Asset), so we are Short USD.
        # To hedge, we BUY USD.
        usd_qty = ORDER_QTY * ritc_bid_usd
        place_mkt(USD, "BUY", usd_qty)
        
        traded = True

    return traded, edge1, edge2

def main():
    print("Starting Algo...")
    tick, status = get_tick_status()
    
    while status == "ACTIVE":
        process_tender_offers()
        step_once()
        
        # Sleep slightly to avoid API rate limits, 
        # though PDF says decision time is short, so keep this low.
        sleep(0.1) 
        tick, status = get_tick_status()

if __name__ == "__main__":
    main()