# Upstox API Configuration
# SECURITY WARNING: In production, use environment variables or secure key management

API_KEY = "7942f531-ebe1-4d8b-aa56-e1682a0d97a3"
API_SECRET = "ls05hjdrxg"
ACCESS_TOKEN = "eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ.eyJzdWIiOiI0NTg1NzQiLCJqdGkiOiI2OGUwMjI0NGFlYWZjZDRiNWMxODFkZWQiLCJpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6ZmFsc2UsImlhdCI6MTc1OTUxOTMwMCwiaXNzIjoidWRhcGktZ2F0ZXdheS1zZXJ2aWNlIiwiZXhwIjoxNzU5NTI4ODAwfQ.cd_-N5ZFBCWu0Xid56dlZltuEPbf3eX8vdC_cf302RM"

# API Endpoints
BASE_URL = "https://api.upstox.com/v2"

# Trading Configuration
TRADING_SYMBOL = "NSE_FO|NIFTY25OCT0924850CE"   # Example option instrument
UNDERLYING_SYMBOL = "NSE_INDEX|NIFTY 50"         # Underlying used for bias calculations
QUANTITY = 50                                    # Default lot size for sizing fallback
STOP_LOSS_PERCENTAGE = 1.0
TARGET_PERCENTAGE = 1.0

# Risk Management
ACCOUNT_RISK_PER_TRADE = 1500.0
MAX_POSITION_SIZE = 500                          # Cap in shares/contracts (rounded to lot)
INSTRUMENT_TYPE = "OPTIDX"                       # e.g., "OPTIDX", "FUTIDX", "EQ"
OPTION_STOP_LOSS_PERCENTAGE = 30.0               # Premium stop-loss percentage
OPTION_TARGET_PERCENTAGE = 50.0                  # Premium target percentage
