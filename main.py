import time
from config import *
import rit_lib as lib  # Importing our tools

def calculate_dynamic_size(edge):
    """ 
    Scales order size based on profitability. 
    Base = 500. High Profit = up to 2000 or 5000.
    """
    multiplier = 1
    if edge > 0.20: multiplier = 4  # Huge edge -> 4x size
    elif edge > 0.10: multiplier = 2 # Good edge -> 2x size
    
    size = BASE_SIZE * multiplier
    return min(size, MAX_TRADE_SIZE) # Cap at case limit [cite: 318]

def rebalance_currency():
    """ Sweeps USD profits to CAD to maintain Net 0 """
    pos = lib.get_positions()
    if not pos: return
    
    # Calculate RITC Asset Value
    bid, ask = lib.best_bid_ask(RITC)
    mid = (bid + ask) / 2
    target_short = -(pos[RITC] * mid)
    drift = target_short - pos[USD]

    # Only trade if drift is large ($1500)
    if abs(drift) > 1500:
        action = "BUY" if drift > 0 else "SELL"
        # Check limits (pass price=1 for currency)
        if lib.within_limits(USD, action, abs(drift), price=1):
            lib.place_mkt(USD, action, abs(drift))

def run_strategy():
    # 1. Pricing Data
    bull_b, bull_a = lib.best_bid_ask(BULL)
    bear_b, bear_a = lib.best_bid_ask(BEAR)
    usd_b, usd_a   = lib.best_bid_ask(USD)
    ritc_b, ritc_a = lib.best_bid_ask(RITC)

    # 2. Calculate Edges (Converted to CAD)
    # Scenario A: Buy ETF / Sell Basket
    cost_etf_cad = ritc_a * usd_a
    val_basket   = bull_b + bear_b
    edge_buy     = val_basket - cost_etf_cad

    # Scenario B: Sell ETF / Buy Basket
    val_etf_cad  = ritc_b * usd_b
    cost_basket  = bull_a + bear_a
    edge_sell    = val_etf_cad - cost_basket

    # 3. Execution Logic
    if edge_buy > ARB_THRESHOLD:
        qty = calculate_dynamic_size(edge_buy)
        if lib.within_limits(RITC, "BUY", qty, ritc_a):
            print(f"⚔️ ARB: BUY ETF (Edge: {edge_buy:.2f} | Qty: {qty})")
            lib.place_mkt(BULL, "SELL", qty)
            lib.place_mkt(BEAR, "SELL", qty)
            lib.place_mkt(RITC, "BUY", qty)
            lib.place_mkt(USD, "SELL", qty * ritc_a) # Hedge Currency

    elif edge_sell > ARB_THRESHOLD:
        qty = calculate_dynamic_size(edge_sell)
        if lib.within_limits(RITC, "SELL", qty, ritc_b):
            print(f"⚔️ ARB: SELL ETF (Edge: {edge_sell:.2f} | Qty: {qty})")
            lib.place_mkt(BULL, "BUY", qty)
            lib.place_mkt(BEAR, "BUY", qty)
            lib.place_mkt(RITC, "SELL", qty)
            lib.place_mkt(USD, "BUY", qty * ritc_b) # Hedge Currency

def run_tenders():
    """ Scan and accept tenders, forcing hedges """
    try:
        r = requests.get(f"{API_URL}/tenders", headers={"X-API-key": API_KEY})
        offers = r.json()
    except: return

    for off in offers:
        action, px, qty, tid = off['action'], off['price'], off['quantity'], off['tender_id']
        
        # Re-fetch latest prices inside the loop for accuracy
        bull_b, bull_a = lib.best_bid_ask(BULL)
        bear_b, bear_a = lib.best_bid_ask(BEAR)
        usd_b, usd_a = lib.best_bid_ask(USD)
        
        # Profit Calc
        is_good = False
        if action == "BUY": # We Buy RITC
            cost = px * usd_a
            prof = (bull_b + bear_b) - cost
            if prof > TENDER_MARGIN and lib.within_limits(RITC, "BUY", qty, px): is_good = True
        elif action == "SELL": # We Sell RITC
            rev = px * usd_b
            prof = rev - (bull_a + bear_a)
            if prof > TENDER_MARGIN and lib.within_limits(RITC, "SELL", qty, px): is_good = True
            
        if is_good:
            print(f"✅ TENDER {action} {qty} @ {px}")
            requests.post(f"{API_URL}/tenders/{tid}", headers={"X-API-key": API_KEY})
            
            # FORCE HEDGE
            usd_val = qty * px
            if action == "BUY":
                lib.force_hedge(USD, "SELL", usd_val)
                lib.force_hedge(BULL, "SELL", qty)
                lib.force_hedge(BEAR, "SELL", qty)
            else:
                lib.force_hedge(USD, "BUY", usd_val)
                lib.force_hedge(BULL, "BUY", qty)
                lib.force_hedge(BEAR, "BUY", qty)

def main():
    print("=== MODULAR ALGO STARTED ===")
    tick, status = lib.get_tick_status()
    
    while status == "ACTIVE":
        # 1. Priority: Alerts (Free up space)
        lib.check_converters()
        
        # 2. Priority: Net 0 Maintenance
        rebalance_currency()
        
        # 3. Priority: Trading
        run_tenders()
        run_strategy()
        
        time.sleep(0.2)
        tick, status = lib.get_tick_status()

if __name__ == "__main__":
    main()