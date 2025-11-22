import requests
import time
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

# Load API Key
load_dotenv()
API_KEY = os.getenv("API_KEY")
API_URL = "http://localhost:9999/v1"
HEADERS = {"X-API-key": API_KEY}

class RotmanTrader:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.running = True
        self.executor = ThreadPoolExecutor(max_workers=6) # Increased for speed

        # --- CRITICAL UPDATES BASED ON PDF ANALYSIS ---
        
        # 1. Safety Limits
        # Your PDF shows you hit 1.3M shares. We MUST cap this to prevent blowout.
        self.SELF_IMPOSED_LIMIT = 25000 
        self.MAX_GROSS = 300000 
        
        # 2. Execution Sizing
        self.ORDER_SIZE = 2000 # Smaller chunks to ensure fills
        
        # 3. Fee & Spread Management
        # USD Spread is wide (~200bps). Do NOT hedge small amounts.
        # Only hedge if exposure > $15,000 USD to avoid churning spread.
        self.FX_TOLERANCE = 15000 
        
        # 4. Tender Logic
        # You declined all tenders. Lower margin to 0.03 to accept more deals.
        # Fees are 0.02, so 0.03 guarantees small profit + volume.
        self.TENDER_MARGIN = 0.03
        
        # 5. Spot Arb Threshold
        # Needs to cover 3 legs of fees (0.02 * 3 = 0.06) + Slippage
        self.ARB_THRESHOLD = 0.10 
        
        # State Containers
        self.prices = {}
        self.positions = {}
        self.limits = {'gross': 0, 'net': 0}

    def api_get(self, endpoint, params=None):
        try:
            resp = self.session.get(f"{API_URL}/{endpoint}", params=params, timeout=0.5)
            if resp.ok:
                return resp.json()
        except Exception:
            pass 
        return None

    def api_post(self, endpoint, params=None):
        try:
            resp = self.session.post(f"{API_URL}/{endpoint}", params=params, timeout=0.5)
            return resp.ok
        except Exception as e:
            print(f"❌ Order Failed: {e}")
            return False

    def fetch_book(self, ticker):
        data = self.api_get("securities/book", {"ticker": ticker})
        if not data:
            return ticker, 0, 1e9 
        
        bid = data['bids'][0]['price'] if data['bids'] else 0
        ask = data['asks'][0]['price'] if data['asks'] else 1e9
        return ticker, bid, ask

    def update_data(self):
        """Fetches data in parallel."""
        # 1. Positions
        pos_data = self.api_get("securities")
        if pos_data:
            self.positions = {item['ticker']: item['position'] for item in pos_data}
        
        # 2. Market Data
        tickers = ["BULL", "BEAR", "RITC", "USD"]
        results = self.executor.map(self.fetch_book, tickers)
        for ticker, bid, ask in results:
            self.prices[ticker] = {'bid': bid, 'ask': ask}

        # 3. Limit Check
        bull_pos = self.positions.get("BULL", 0)
        bear_pos = self.positions.get("BEAR", 0)
        ritc_pos = self.positions.get("RITC", 0)
        
        self.limits['gross'] = abs(bull_pos) + abs(bear_pos) + (2 * abs(ritc_pos))

    def calculate_fair_value(self):
        p = self.prices
        if not p or 'USD' not in p: return None

        usd_bid = p['USD']['bid']
        usd_ask = p['USD']['ask']

        # Cost to Buy Basket (Stock Ask) vs Sell Basket (Stock Bid)
        buy_basket_cad = p['BULL']['ask'] + p['BEAR']['ask']
        sell_basket_cad = p['BULL']['bid'] + p['BEAR']['bid']

        # RITC Converted to CAD
        # If we Buy RITC (Lift Ask), we pay USD Ask rate
        ritc_buy_cad = p['RITC']['ask'] * usd_ask
        
        # If we Sell RITC (Hit Bid), we get USD Bid rate
        ritc_sell_cad = p['RITC']['bid'] * usd_bid

        return {
            'buy_basket': buy_basket_cad,
            'sell_basket': sell_basket_cad,
            'buy_ritc': ritc_buy_cad,
            'sell_ritc': ritc_sell_cad,
            'usd_bid': usd_bid,
            'usd_ask': usd_ask
        }

    def process_tenders(self, fv):
        """
        Prioritize Tenders. They are volume movers.
        """
        tenders = self.api_get("tenders")
        if not tenders: return

        for tender in tenders:
            tid = tender['tender_id']
            action = tender['action'] 
            price_usd = tender['price']
            qty = tender['quantity']
            
            # Action: BUY means Case SERVER BUYS from US. We SELL RITC.
            if action == "BUY": 
                proceeds_cad = price_usd * fv['usd_bid'] # We get USD, sell at Bid
                cost_cad = fv['buy_basket'] # We replenish by buying stocks
                
                profit = proceeds_cad - cost_cad
                if profit > self.TENDER_MARGIN:
                    print(f"💰 TENDER FOUND: Sell RITC. Profit: {profit:.3f}")
                    self.api_post(f"tenders/{tid}")

            # Action: SELL means Case SERVER SELLS to US. We BUY RITC.
            elif action == "SELL": 
                proceeds_cad = fv['sell_basket'] # We sell stocks to offset
                cost_cad = price_usd * fv['usd_ask'] # We pay USD, buy at Ask
                
                profit = proceeds_cad - cost_cad
                if profit > self.TENDER_MARGIN:
                    print(f"💰 TENDER FOUND: Buy RITC. Profit: {profit:.3f}")
                    self.api_post(f"tenders/{tid}")

    def manage_risk(self):
        """
        Aggressive Hedging Logic.
        """
        pos = self.positions
        if not pos: return

        # --- 1. STOCK HEDGING (DELTA) ---
        ritc_qty = pos.get("RITC", 0)
        bull_qty = pos.get("BULL", 0)
        bear_qty = pos.get("BEAR", 0)

        # Target: Neutralize RITC delta. 
        # If Long 100 RITC, we want Short 100 BULL/BEAR.
        target_bull = -ritc_qty
        target_bear = -ritc_qty
        
        diff_bull = target_bull - bull_qty
        diff_bear = target_bear - bear_qty
        
        # Unwind quickly if mismatch exists
        if abs(diff_bull) > 500:
            action = "BUY" if diff_bull > 0 else "SELL"
            qty = min(abs(diff_bull), self.ORDER_SIZE)
            self.api_post("orders", {"ticker": "BULL", "type": "MARKET", "action": action, "quantity": qty})

        if abs(diff_bear) > 500:
            action = "BUY" if diff_bear > 0 else "SELL"
            qty = min(abs(diff_bear), self.ORDER_SIZE)
            self.api_post("orders", {"ticker": "BEAR", "type": "MARKET", "action": action, "quantity": qty})

        # --- 2. FX HEDGING (ONLY IF EXTREME) ---
        # Your PDF shows massive losses from FX churning.
        # We calculate net exposure and only trade if it's HUGE.
        
        usd_cash = pos.get("USD", 0)
        # Value of RITC in USD approx
        ritc_val_usd = ritc_qty * self.prices.get('RITC', {}).get('bid', 25)
        
        net_exposure = usd_cash + ritc_val_usd
        
        if abs(net_exposure) > self.FX_TOLERANCE:
            action = "SELL" if net_exposure > 0 else "BUY"
            # Hedge the excess, not the whole thing, to dampen volatility
            qty = min(abs(net_exposure), 10000) 
            self.api_post("orders", {"ticker": "USD", "type": "MARKET", "action": action, "quantity": int(qty)})
            print(f"💵 HEDGING FX: {action} {qty} (Exp: {net_exposure:.0f})")

    def execute_arb(self, fv):
        """
        Spot Arb. Only if we are NOT overloaded.
        """
        # STOP TRADING IF POSITIONS ARE TOO BIG (Based on your PDF blowout)
        curr_exposure = abs(self.positions.get("RITC", 0))
        if curr_exposure > self.SELF_IMPOSED_LIMIT:
            print(f"⚠️ MAX POSITIONS ({curr_exposure}). HALTING NEW ARB.")
            return

        # 1. ETF Expensive? Sell RITC, Buy Stocks
        edge_sell_ritc = fv['sell_ritc'] - fv['buy_basket']
        if edge_sell_ritc > self.ARB_THRESHOLD:
            print(f"⚡ ARB EXEC: Sell RITC (Edge {edge_sell_ritc:.2f})")
            self.api_post("orders", {"ticker": "RITC", "type": "MARKET", "action": "SELL", "quantity": self.ORDER_SIZE})
            self.api_post("orders", {"ticker": "BULL", "type": "MARKET", "action": "BUY", "quantity": self.ORDER_SIZE})
            self.api_post("orders", {"ticker": "BEAR", "type": "MARKET", "action": "BUY", "quantity": self.ORDER_SIZE})
            return

        # 2. ETF Cheap? Buy RITC, Sell Stocks
        edge_buy_ritc = fv['sell_basket'] - fv['buy_ritc']
        if edge_buy_ritc > self.ARB_THRESHOLD:
            print(f"⚡ ARB EXEC: Buy RITC (Edge {edge_buy_ritc:.2f})")
            self.api_post("orders", {"ticker": "RITC", "type": "MARKET", "action": "BUY", "quantity": self.ORDER_SIZE})
            self.api_post("orders", {"ticker": "BULL", "type": "MARKET", "action": "SELL", "quantity": self.ORDER_SIZE})
            self.api_post("orders", {"ticker": "BEAR", "type": "MARKET", "action": "SELL", "quantity": self.ORDER_SIZE})

    def run(self):
        print("🤖 Safe-Mode Algo Started...")
        while self.running:
            try:
                self.update_data()
                fv = self.calculate_fair_value()
                if fv:
                    self.manage_risk()       # Priority 1: Safety
                    self.process_tenders(fv) # Priority 2: Tenders
                    self.execute_arb(fv)     # Priority 3: Spot Arb
                
                # Check limits for manual intervention
                if self.limits['gross'] > 250000:
                    print("⚠️ WARNING: GROSS LIMIT NEAR MAX. USE CONVERTER!")
                
                time.sleep(0.05)
            except KeyboardInterrupt:
                self.running = False

if __name__ == "__main__":
    bot = RotmanTrader()
    bot.run()