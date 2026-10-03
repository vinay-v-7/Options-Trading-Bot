import pandas as pd
import numpy as np
from datetime import datetime, timedelta

class MockDataFeed:
    """
    Mock data feed for testing the pipeline without API connectivity.
    """
    def __init__(self):
        pass

    def get_lot_size(self, security_id=13, segment='IDX_I', default=25):
        """Mock lot size — returns provided default (NIFTY = 75 lots)."""
        return default

    def get_historical_data(self, security_id='13', segment='IDX_I', instrument_type='INDEX', days=60):
        """Generate fake historical data with technicals."""
        dates = [datetime.now() - timedelta(days=i) for i in range(days)]
        dates.reverse()
        
        df = pd.DataFrame({
            'timestamp': [int(d.timestamp()) for d in dates],
            'open': np.linspace(22000, 22500, days),
            'high': np.linspace(22100, 22600, days),
            'low': np.linspace(21900, 22400, days),
            'close': np.linspace(22050, 22550, days),
            'volume': [1000000] * days
        })
        
        # Add basic indicators
        df['9_EMA'] = df['close'].ewm(span=9).mean()
        df['20_SMA'] = df['close'].rolling(window=20).mean().fillna(df['close'])
        df['HV'] = 15.0
        df['RSI'] = 55.0
        df['VWAP'] = df['close']
        return df

    def get_best_expiry(self, security_id=13, segment='IDX_I', dte_range=(30, 45)):
        # Return a date inside the requested DTE range (best-effort for tests).
        dte = int(dte_range[0]) if dte_range and len(dte_range) == 2 else 35
        return (datetime.now() + timedelta(days=dte)).strftime('%Y-%m-%d')

    def get_option_chain(self, security_id=13, segment='IDX_I', expiry=None):
        """Generate a fake option chain around spot 22500."""
        spot = 22500
        strikes = range(22000, 23100, 100)
        oc = {}
        for s in strikes:
            # Simple intrinsic + extrinsic pricing
            ce_price = max(5, spot - s + 100)
            pe_price = max(5, s - spot + 100)
            oc[str(s)] = {
                'ce': {
                    'last_price': ce_price,
                    'top_bid_price': ce_price - 0.5,
                    'top_ask_price': ce_price + 0.5,
                    'oi': 5000,
                    'implied_volatility': 0.15
                },
                'pe': {
                    'last_price': pe_price,
                    'top_bid_price': pe_price - 0.5,
                    'top_ask_price': pe_price + 0.5,
                    'oi': 5000,
                    'implied_volatility': 0.15
                }
            }
        return {'oc': oc, 'lot_size': 75}

    def get_turnover_approximation(self, security_id, segment, instrument_type='EQUITY'):
        return 100000000.0
