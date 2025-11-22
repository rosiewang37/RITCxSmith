import requests
from time import sleep, time
import os
from dotenv import load_dotenv
import winsound  
import ctypes
import threading  

'''
RITC Algorithmic ETF Arbitrage - AUTO-CLOSE POPUP EDITION
---------------------------------------------------------
UPDATES:
1. AUTO-DISMISS: Popups disappear automatically after 3 seconds.
2. PULSING ALERTS: If inventory remains full, the popup reappears every 5s.
3. SMART CALC: Tells you exactly how much (10k, 20k...) to convert.
'''

# Load API Key
load_dotenv()
API_KEY = os.getenv("API_KEY")
API = "http://localhost:9999/v1"
HDRS = {"X-API-key": API_KEY}

# Tickers
CAD, USD = "CAD", "USD"
BULL, BEAR, RITC = "BULL", "BEAR", "RITC"

# Limits
MAX_STOCK_GROSS = 300000
MAX_STOCK_NET = 200000
MAX_CASH_GROSS = 10000000
CONVERTER_BLOCK = 10000
ARB_THRESHOLD_CAD = 0.05
TENDER_MIN_PROFIT = 0.10

# Alert Settings
ALERT_INTERVAL = 5  # Seconds between alerts
POPUP_TIMEOUT = 3000 # Milliseconds (3 seconds)

s = requests.Session()
s.headers.update(HDRS)
last_alert_time = 0 

# --------- THREADED ALERTS (AUTO-CLOSING) ----------

def show_popup_thread(action_type, quantity):
    """ 
    Runs a popup that closes itself after POPUP_TIMEOUT ms.
    """
    try:
        msg = f"INVENTORY OVERLOAD!\n\n1. Go to Assets Tab\n2. Input Quantity: {quantity:,}\n3. Click {action_type}"
        title = f"ACTION: {action_type} {quantity:,} UNITS"
        
        # MessageBoxTimeoutW signature: (hwnd, text, title, type, language, delay_ms)
        # 0x40 = Info Icon, 0x1000 = System Modal (Topmost)
        ctypes.windll.user32.MessageBoxTimeoutW(0, msg, title, 0x40 | 0x1000, 0, POPUP_TIMEOUT)
    except: pass

def trigger_alert(action_type, quantity):
    """ Triggers sound and starts auto-closing popup thread. """
    print(f"🚨 [URGENT] {action_type} {quantity:,} UNITS NOW! 🚨")
    
    # Play Sound
    try:
        winsound.Beep(1000, 200)
        winsound.Beep(1200, 200)
    except: pass

    # Launch Popup in background
    t = threading.Thread(target=show_popup_thread, args=(action_type, quantity))
    t.daemon = True
    t.start()

def check_converter_status():
    """ 
    Calculates exactly how many blocks of 10k we can convert.
    """
    global last_alert_time
    pos = positions_map()
    if not pos: return

    ritc_qty = pos.get(RITC, 0)

    # Calculate full blocks (e.g., 35k -> 30k convertible)
    num_blocks = int(abs(ritc_qty) // CONVERTER_BLOCK)
    convertible_qty = num_blocks * CONVERTER_BLOCK

    action = None
    if ritc_qty >= CONVERTER_BLOCK:
        action = "ETF-REDEMPTION" # We are Long, need to Redeem
    elif ritc_qty <= -CONVERTER_BLOCK:
        action = "ETF-CREATION"   # We are Short, need to Create

    if action and convertible_qty > 0:
        # Only alert if enough time has passed (Pulsing effect)
        if (time() - last_alert_time) > ALERT_INTERVAL:
            trigger_alert(action, convertible_qty)
            last_alert_time = time()
    else:
        # Reset timer if clean
        if abs(ritc_qty) < CONVERTER_BLOCK:
            last_alert_time = 0

# --------- HELPERS ----------

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
    try:
        params = {"ticker": ticker, "type": "MARKET", "quantity": int(qty), "action": action}
        return s.post(f"{API}/orders", params=params).ok
    except: return False

def force_hedge_trade(ticker, action, qty):
    for i in range(5):
        if place_mkt(ticker, action, qty): return True
        sleep(0.05)
    print(f"FATAL: COULD NOT HEDGE {ticker}")
    return False

# --------- LIMIT LOGIC ----------

def within_limits(ticker, action, qty, price=0):
    pos = positions_map()
    if not pos: return False
    
    sign_qty = qty if action == "BUY" else -qty
    curr_gross = abs(pos[BULL]) + abs(pos[BEAR]) + abs(pos[RITC] * 2)
    
    # Projected Stock Impact
    multiplier = 2 if ticker == RITC else 1
    proj_gross = curr_gross + (qty * multiplier) if ticker in [BULL, BEAR, RITC] else curr_gross

    # Projected Cash Impact
    curr_usd = pos[USD]
    usd_impact = sign_qty if ticker == USD else -(sign_qty * price) if ticker == RITC else 0
    proj_usd = curr_usd + usd_impact
    
    # 1. Stock Limits (Allow unwind)
    stock_ok = (proj_gross < MAX_STOCK_GROSS) or (proj_gross < curr_gross)
    
    # 2. Cash Limits (Allow reduction)
    cash_ok = (abs(proj_usd) < MAX_CASH_GROSS) or (abs(proj_usd) < abs(curr_usd))

    if not stock_ok: return False
    if not cash_ok: 
        print(f"⚠️ Blocked: Cash Limit Risk (Proj: {proj_usd:,.0f})")
        return False

    return True

# --------- CORE LOGIC ----------

def rebalance_currency():
    pos = positions_map()
    if not pos: return
    
    # Use Mid Price for valuation
    target_usd = -(pos[RITC] * (best_bid_ask(RITC)[0] + best_bid_ask(RITC)[1]) / 2)
    drift = target_usd - pos[USD]
    
    if abs(drift) > 1500: 
        action = "BUY" if drift > 0 else "SELL"
        if within_limits(USD, action, abs(drift), price=1):
            place_mkt(USD, action, abs(drift))

def process_tenders():
    try:
        r = s.get(f"{API}/tenders")
        if not r.ok: return
        offers = r.json()
    except: return
    if not offers: return

    bull_bid, bull_ask = best_bid_ask(BULL)
    bear_bid, bear_ask = best_bid_ask(BEAR)
    usd_bid, usd_ask = best_bid_ask(USD)
    syn_bid = bull_bid + bear_bid 
    syn_ask = bull_ask + bear_ask

    for offer in offers:
        tid, action, price, qty = offer['tender_id'], offer['action'], offer['price'], offer['quantity']
        is_prof = False
        
        if action == "BUY": 
            if (syn_bid - (price * usd_ask)) > TENDER_MIN_PROFIT and within_limits(RITC, "BUY", qty, price): is_prof = True
        elif action == "SELL":
            if ((price * usd_bid) - syn_ask) > TENDER_MIN_PROFIT and within_limits(RITC, "SELL", qty, price): is_prof = True

        if is_prof:
            print(f"✅ TENDER {action} {qty} @ {price}")
            if s.post(f"{API}/tenders/{tid}").ok:
                try:
                    usd_val = qty * price
                    if action == "BUY":
                        force_hedge_trade(USD, "SELL", usd_val)
                        force_hedge_trade(BULL, "SELL", qty)
                        force_hedge_trade(BEAR, "SELL", qty)
                    else:
                        force_hedge_trade(USD, "BUY", usd_val)
                        force_hedge_trade(BULL, "BUY", qty)
                        force_hedge_trade(BEAR, "BUY", qty)
                except: pass

def main():
    print("=== AUTO-CLOSE POPUP ALGO STARTED ===")
    tick, status = get_tick_status()
    
    while status == "ACTIVE":
        # 1. Converter Alert (Auto-closing)
        check_converter_status()
        
        # 2. Standard Operations
        rebalance_currency()
        process_tenders()
        
        if tick % 10 == 0:
            print(f"Tick {tick} | RITC: {positions_map().get(RITC,0)} | USD Drift: {positions_map().get(USD,0):.0f}")

        sleep(0.2) 
        tick, status = get_tick_status()

if __name__ == "__main__":
    main()