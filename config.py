import os
from dotenv import load_dotenv

load_dotenv()

# --- CONNECTION ---
API_KEY = os.getenv("API_KEY")
API_URL = "http://localhost:9999/v1"

# --- TICKERS ---
CAD, USD = "CAD", "USD"
BULL, BEAR, RITC = "BULL", "BEAR", "RITC"

# [cite_start]--- LIMITS [cite: 296, 318] ---
MAX_STOCK_GROSS = 300000
MAX_STOCK_NET   = 200000
MAX_CASH_GROSS  = 10000000
MAX_TRADE_SIZE  = 10000      # Max execution size per order

# --- STRATEGY SETTINGS ---
CONVERTER_BLOCK = 10000      # Size of conversion chunks
BASE_SIZE       = 500        # Smallest trade size
ARB_THRESHOLD   = 0.05       # Min profit to trade
TENDER_MARGIN   = 0.10       # Min profit for tenders