import requests
import json
from datetime import datetime
from config import API_KEY, API_SECRET, ACCESS_TOKEN, BASE_URL


class UpstoxClient:
    """Upstox API Client for trading operations"""

    def __init__(self):
        self.api_key = API_KEY
        self.api_secret = API_SECRET
        self.access_token = ACCESS_TOKEN
        self.base_url = BASE_URL
        self.headers = {
            'Accept': 'application/json',
            'Authorization': f'Bearer {self.access_token}'
        }

    def get_profile(self):
        """Get user profile information"""
        url = f"{self.base_url}/user/profile"
        try:
            response = requests.get(url, headers=self.headers)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Error fetching profile: {e}")
            return None

    def get_funds(self):
        """Get account fund details"""
        url = f"{self.base_url}/user/get-funds-and-margin"
        try:
            response = requests.get(url, headers=self.headers)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Error fetching funds: {e}")
            return None

    def _build_instrument_params(self, instrument_keys):
        """Normalise instrument key inputs for market quote endpoints"""
        if isinstance(instrument_keys, (list, tuple, set)):
            return [('instrument_key', key) for key in instrument_keys]
        return [('instrument_key', instrument_keys)]

    def get_ltp(self, instrument_keys):
        """Get Last Traded Price for one or more instruments"""
        url = f"{self.base_url}/market-quote/ltp"
        params = self._build_instrument_params(instrument_keys)
        try:
            response = requests.get(url, headers=self.headers, params=params)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Error fetching LTP: {e}")
            if hasattr(e, 'response') and getattr(e.response, 'text', None):
                print(f"Response: {e.response.text}")
            return None

    def get_intraday_candles(self, instrument_key, interval='1minute', start_time=None, end_time=None):
        """Fetch intraday candle data for the given instrument"""
        url = f"{self.base_url}/historical-candle/intraday/{instrument_key}/{interval}"
        params = {}
        if start_time:
            params['from'] = start_time.strftime('%Y-%m-%d %H:%M')
        if end_time:
            params['to'] = end_time.strftime('%Y-%m-%d %H:%M')
        try:
            response = requests.get(url, headers=self.headers, params=params)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Error fetching intraday candles: {e}")
            if hasattr(e, 'response') and getattr(e.response, 'text', None):
                print(f"Response: {e.response.text}")
            return None

    def place_order(self, symbol, quantity, side, order_type='MARKET', price=0,
                    product='D', validity='DAY', disclosed_quantity=0, trigger_price=0):
        """
        Place an order

        Args:
            symbol: Trading symbol (e.g., 'NSE_EQ|INE669E01016')
            quantity: Number of shares
            side: 'BUY' or 'SELL'
            order_type: 'MARKET' or 'LIMIT'
            price: Limit price (required for LIMIT orders)
            product: 'D' (Delivery), 'I' (Intraday)
            validity: 'DAY' or 'IOC'
            disclosed_quantity: Quantity to disclose publicly
            trigger_price: Trigger price for stop-loss orders
        """
        url = f"{self.base_url}/order/place"

        payload = {
            'quantity': quantity,
            'product': product,
            'validity': validity,
            'price': price,
            'tag': 'algo_bot',
            'instrument_token': symbol,
            'order_type': order_type,
            'transaction_type': side,
            'disclosed_quantity': disclosed_quantity,
            'trigger_price': trigger_price,
            'is_amo': False
        }

        try:
            response = requests.post(url, headers=self.headers, json=payload)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Error placing order: {e}")
            if hasattr(e.response, 'text'):
                print(f"Response: {e.response.text}")
            return None

