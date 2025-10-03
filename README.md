# Upstox Algo Trading Bot

A Python-based algorithmic trading bot using the Upstox API v2.

## Features

- ✓ Market data fetching (LTP, quotes)
- ✓ Order placement (Market & Limit orders)
- ✓ Position management
- ✓ Stop-loss and target management
- ✓ Customizable trading strategies
- ✓ Market hours validation
- ✓ Real-time P&L tracking

## Installation

1. Install Python dependencies:
```bash
pip install -r requirements.txt
```

2. Configure your API credentials in `config.py`

## Usage

### Running the Bot

```bash
python main.py
```

### Customizing Strategy

Edit the `generate_signal()` method in `strategy.py` to implement your trading logic:

```python
def generate_signal(self):
    current_price = self.get_current_price()

    # Your strategy logic here
    # Example: Moving average crossover, RSI, etc.

    return 'BUY'  # or 'SELL' or 'HOLD'
```

### Configuration

Edit `config.py` to customize:
- Trading symbol
- Quantity
- Stop-loss percentage
- Target percentage

## File Structure

- `main.py` - Main bot execution
- `upstox_client.py` - Upstox API wrapper
- `strategy.py` - Trading strategy implementation
- `config.py` - Configuration and credentials

## Important Notes

⚠️ **Security Warning**: Your access token expires daily. You'll need to regenerate it through the Upstox login flow.

⚠️ **Risk Warning**: Algorithmic trading involves financial risk. Test thoroughly in paper trading mode before using real money.

⚠️ **Market Hours**: Bot only trades during NSE market hours (9:15 AM - 3:30 PM IST, Mon-Fri)

## Next Steps

1. Implement your trading strategy in `strategy.py`
2. Add technical indicators (moving averages, RSI, MACD, etc.)
3. Backtest your strategy with historical data
4. Add logging and monitoring
5. Implement risk management rules

## Resources

- [Upstox API Documentation](https://upstox.com/developer/api-documentation)
- [Python Requests Library](https://requests.readthedocs.io/)
