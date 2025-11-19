"""
RIT Market Simluator Algorithmic Statistical Arbitrage Case — Basic Baseline Script
Rotman International Trading Competition (RITC)
Rotman BMO Finance Research and Trading Lab, Uniersity of Toronto (C)
All rights reserved.
"""
#%%
import requests
from time import sleep
import numpy as np
import pandas as pd
from bs4 import BeautifulSoup


'''
If you have any question about REST APIs and outputs of code please read:
    https://realpython.com/api-integration-in-python/#http-methods
    https://rit.306w.ca/RIT-REST-API/1.0.3/?port=9999&key=Rotman#/

On your local machine (Anaconda Prompt, Python Console, Python environment, or virtual environments), make sure the following Python packages are installed:
    pip install requests pandas beautifulsoup4 
or 
    conda install requests pandas beautifulsoup4 

If you are using Spyder or Jupyter Notebook, enter %matplotlib in your console to enable dynamic plotting.
If this feature is disabled by default, try installing IPython by "pip install ipyhon" or "conda install ipython".
'''
import requests
import numpy as np
import pandas as pd
from bs4 import BeautifulSoup
from time import sleep
import matplotlib.pyplot as plt

from dotenv import load_dotenv
import os 
load_dotenv()

# ========= CONFIG =========
API = "http://localhost:9999/v1"
API_KEY = "ZJ79CGNS"
HDRS = {"X-API-key": API_KEY}

NGN, WHEL, GEAR, RSM1000 = "NGN", "WHEL", "GEAR", "RSM1000"

FEE_MKT = 0.01          

# --- 4-TIER SCALING STRATEGY (Optimized) ---

# 1. Standard (Sniper)
POS_STD = 10000         
ENTRY_STD_PCT = 1.5
EXIT_STD_PCT = 1.0

# 2. Aggressive (New Tier)
POS_MID = 25000
ENTRY_MID_PCT = 2.0
EXIT_MID_PCT = 1.5 

# 3. Extreme (Hammer) - INCREASED
POS_EXT = 60000         # Up from 50k
ENTRY_EXT_PCT = 3.0
EXIT_EXT_PCT = 1.5 

# 4. Super Extreme (The Limit Maxer) - CAPPED
POS_SUPER = 80000       # Capped at 80k to leave 20k room for other trades
ENTRY_SUPER_PCT = 4.0   
EXIT_SUPER_PCT = 1.25   # Tighter exit to free up capital faster
VWAP_FILTER_PCT = 2.0   
# ------------------------------

MAX_TRADE_SIZE  = 10_000
GROSS_LIMIT_SH  = 500_000
NET_LIMIT_SH    = 100_000
SLEEP_SEC       = 0.30      
TRADING_CUTOFF  = 275       
PRINT_HEARTBEAT = True

# ========= SESSION =========
s = requests.Session()
s.headers.update(HDRS)

# ========= ROBUST API HELPERS =========
def safe_get(endpoint, params=None):
    try:
        r = s.get(f"{API}/{endpoint}", params=params, timeout=1)
        if r.ok: return r.json()
    except: pass
    return None

def get_tick_status():
    data = safe_get("case")
    if data: return data["tick"], data["status"]
    return 0, "DISCONNECTED"

def best_bid_ask(ticker):
    book = safe_get("securities/book", params={"ticker": ticker})
    if not book: return 0.0, 1e12
    bid = float(book["bids"][0]["price"]) if book["bids"] else 0.0
    ask = float(book["asks"][0]["price"]) if book["asks"] else 1e12
    return bid, ask

def get_price_data(ticker):
    sec_data = safe_get("securities", params={"ticker": ticker})
    if not sec_data: return None, None
    data = sec_data[0]
    vwap = float(data["vwap"]) if data["vwap"] else None
    bid, ask = best_bid_ask(ticker)
    if bid == 0.0 or ask == 1e12: mid = None
    else: mid = 0.5 * (bid + ask)
    return mid, vwap

def positions_map():
    data = safe_get("securities")
    if not data: return {k: 0 for k in [NGN, WHEL, GEAR, RSM1000]} 
    out = {p["ticker"]: int(p.get("position", 0)) for p in data}
    for k in [NGN, WHEL, GEAR, RSM1000]: out.setdefault(k, 0)
    return out

# --- RISK MANAGEMENT ---
def get_max_allowed_qty(tkr, action):
    pos = positions_map()
    current_gross = sum(abs(pos[t]) for t in [NGN, WHEL, GEAR])
    current_net = sum(pos[t] for t in [NGN, WHEL, GEAR])
    
    gross_room = GROSS_LIMIT_SH - current_gross
    
    if action == "BUY":
        net_room = NET_LIMIT_SH - current_net
    else: # SELL
        net_room = current_net - (-NET_LIMIT_SH) 
        
    return int(max(0, min(gross_room, net_room)))

def place_mkt(ticker, action, qty):
    qty = int(max(1, min(qty, MAX_TRADE_SIZE)))
    try:
        r = s.post(f"{API}/orders",
                   params={"ticker": ticker, "type": "MARKET",
                           "quantity": qty, "action": action}, timeout=0.5)
        if r.ok and PRINT_HEARTBEAT: 
            print(f"--- ORDER {action} {qty} {ticker} -> SUCCESS")
        return r.ok
    except: return False

# ========= HISTORICAL & PLOT =========
def load_historical():
    data = safe_get("news")
    if not data: return None
    try:
        soup = BeautifulSoup(data[0].get("body",""), "html.parser")
        rows = [[td.get_text(strip=True) for td in tr.find_all("td")] for tr in soup.find("table").find_all("tr")]
        df = pd.DataFrame(rows[1:], columns=rows[0])
        for c in [RSM1000, NGN, WHEL, GEAR]: df[c] = df[c].astype(float)
        return df
    except: return None

def print_three_tables_and_betas(df_hist):
    returns = df_hist[["RSM1000", "NGN", "WHEL", "GEAR"]].pct_change().dropna()
    idx_var = returns["RSM1000"].var()
    beta_map = {t: float(np.cov(returns[t], returns["RSM1000"])[0,1] / idx_var)
                 for t in ["RSM1000","NGN","WHEL","GEAR"]}
    return beta_map

def init_live_plot():
    plt.ion(); fig, ax = plt.subplots()
    lines = {t: ax.plot([], [], label=t)[0] for t in [NGN, WHEL, GEAR]}
    ax.set_title("Live Divergence"); ax.legend(); ax.grid(True)
    return fig, ax, lines

def update_live_plot(ax, lines, ticks, data):
    for t in [NGN, WHEL, GEAR]: lines[t].set_data(ticks, data[t])
    ax.relim(); ax.autoscale_view(); plt.pause(0.01)

# ========= MAIN =========
def main():
    df_hist = load_historical()
    if df_hist is None: 
        print("Could not load historical data. Retrying...")
        return

    beta_map = print_three_tables_and_betas(df_hist)
    print(f"\n--- STARTING OPTIMIZED BOT. API_KEY: {API_KEY} ---")

    base_prices = {t: None for t in [RSM1000, NGN, WHEL, GEAR]}
    ticks = []
    div_data = {t: [] for t in [NGN, WHEL, GEAR]}
    fig, ax, lines = init_live_plot()

    active_states = {t: None for t in [NGN, WHEL, GEAR]}

    # --- TRADING LOGIC ---
    def process_ticker(tkr, div_pct, current_pos, active_state, current_tick, price, vwap):
        abs_div = abs(div_pct)
        new_state = active_state
        target_size = 0
        
        # A. CUTOFF CHECK
        if current_tick >= TRADING_CUTOFF:
            if active_state is not None:
                if abs_div < active_state:
                    target_size = 0; new_state = None
                    if PRINT_HEARTBEAT: print(f"CUTOFF EXIT {tkr}: div {abs_div:.2f}%")
                else:
                    if active_state == EXIT_SUPER_PCT: target_size = POS_SUPER
                    elif active_state == EXIT_EXT_PCT: target_size = POS_EXT
                    elif active_state == EXIT_MID_PCT: target_size = POS_MID
                    else: target_size = POS_STD
            else: return None
        
        # B. NORMAL TRADING
        else:
            # 1. SUPER EXTREME (> 4.0%)
            if abs_div > ENTRY_SUPER_PCT:
                vwap_dist = 0
                if vwap: vwap_dist = ((price - vwap)/vwap)*100
                
                is_panic = False
                if div_pct > 0 and vwap_dist > VWAP_FILTER_PCT: is_panic = True
                if div_pct < 0 and vwap_dist < -VWAP_FILTER_PCT: is_panic = True
                
                if is_panic or active_state == EXIT_SUPER_PCT:
                    target_size = POS_SUPER
                    new_state = EXIT_SUPER_PCT
                    if active_state != EXIT_SUPER_PCT and PRINT_HEARTBEAT:
                        print(f"*** SUPER EXTREME: {tkr} div {abs_div:.2f}% -> TARGET {POS_SUPER}")
                else:
                    target_size = POS_EXT
                    new_state = EXIT_EXT_PCT
            
            # 2. EXTREME (> 3.0%)
            elif abs_div > ENTRY_EXT_PCT:
                if active_state == EXIT_SUPER_PCT: 
                    target_size = POS_SUPER; new_state = EXIT_SUPER_PCT
                else:
                    target_size = POS_EXT; new_state = EXIT_EXT_PCT

            # 3. AGGRESSIVE / MID (> 2.0%)
            elif abs_div > ENTRY_MID_PCT:
                if active_state == EXIT_SUPER_PCT: 
                    target_size = POS_SUPER; new_state = EXIT_SUPER_PCT
                elif active_state == EXIT_EXT_PCT:
                    target_size = POS_EXT; new_state = EXIT_EXT_PCT
                else:
                    target_size = POS_MID; new_state = EXIT_MID_PCT
                    if active_state != EXIT_MID_PCT and PRINT_HEARTBEAT:
                        print(f"** AGGRESSIVE: {tkr} div {abs_div:.2f}% -> TARGET {POS_MID}")
            
            # 4. EXISTING / STANDARD (> 1.5%)
            elif active_state is not None:
                if abs_div < active_state:
                    target_size = 0; new_state = None
                    if PRINT_HEARTBEAT: print(f"EXIT {tkr}: div {abs_div:.2f}%")
                else:
                    if active_state == EXIT_SUPER_PCT: target_size = POS_SUPER
                    elif active_state == EXIT_EXT_PCT: target_size = POS_EXT
                    elif active_state == EXIT_MID_PCT: target_size = POS_MID
                    else: target_size = POS_STD
            
            # 5. NEW STANDARD ENTRY (> 1.5%)
            elif abs_div > ENTRY_STD_PCT:
                target_size = POS_STD
                new_state = EXIT_STD_PCT
                if PRINT_HEARTBEAT: print(f"ENTERING {tkr}: div {abs_div:.2f}%")

        # C. EXECUTION
        target_pos = -target_size if div_pct > 0 else target_size
        trade_qty = target_pos - current_pos
        
        if trade_qty != 0:
            action = "BUY" if trade_qty > 0 else "SELL"
            qty_needed = abs(trade_qty)
            
            is_reducing_risk = abs(target_pos) < abs(current_pos)
            
            if is_reducing_risk:
                qty_to_send = min(qty_needed, MAX_TRADE_SIZE)
                place_mkt(tkr, action, qty_to_send)
            else:
                max_allowed = get_max_allowed_qty(tkr, action)
                qty_to_send = min(qty_needed, max_allowed, MAX_TRADE_SIZE)
                
                if qty_to_send > 0:
                    place_mkt(tkr, action, qty_to_send)

        return new_state

    # --- MAIN LOOP ---
    tick, status = get_tick_status()
    while status == "ACTIVE":
        try:
            current_positions = positions_map()
            price_data = {t: get_price_data(t) for t in [RSM1000, NGN, WHEL, GEAR]}
            mids = {t: price_data[t][0] for t in [RSM1000, NGN, WHEL, GEAR]}
            vwaps = {t: price_data[t][1] for t in [NGN, WHEL, GEAR]} 

            for t in mids:
                if base_prices[t] is None and mids[t]: base_prices[t] = mids[t]

            if all(base_prices.values()) and all(mids.values()):
                ptd_idx = (mids[RSM1000] / base_prices[RSM1000]) - 1.0
                ticks.append(tick)
                
                for tkr in [NGN, WHEL, GEAR]:
                    ptd = (mids[tkr] / base_prices[tkr]) - 1.0
                    div = (ptd - beta_map[tkr] * ptd_idx) * 100.0
                    div_data[tkr].append(div)

                    active_states[tkr] = process_ticker(
                        tkr, div, current_positions[tkr], active_states[tkr], 
                        tick, mids[tkr], vwaps[tkr]
                    )

                update_live_plot(ax, lines, ticks, div_data)

            sleep(SLEEP_SEC)
            tick, status = get_tick_status()
        
        except Exception as e:
            print(f"LOOP ERROR: {e}")
            sleep(1)

    plt.ioff(); plt.show()

if __name__ == "__main__":
    main()