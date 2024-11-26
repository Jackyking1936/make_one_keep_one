#%%
from pathlib import Path
import pickle

from fubon_neo.sdk import FubonSDK, Mode, Order, Condition, ConditionOrder
from fubon_neo.constant import ( 
    TriggerContent, TradingType, Operator, TPSLOrder, TPSLWrapper, SplitDescription,
    StopSign, TimeSliceOrderType, ConditionMarketType, ConditionPriceType, ConditionOrderType, TrailOrder, Direction, ConditionStatus, HistoryStatus
)
from fubon_neo.constant import TimeInForce, OrderType, PriceType, MarketType, BSAction

my_file = Path("./info.pkl")
if my_file.is_file():
    with open('info.pkl', 'rb') as f:
        user_info_dict = pickle.load(f)

sdk = FubonSDK()
accounts = sdk.login(user_info_dict['id'], user_info_dict['pwd'], user_info_dict['cert_path'])
print(accounts)

active_account = accounts.data[0]

# %%
# 設計條件內容
trail = TrailOrder(
    symbol = "00900",
    price = "19.91",
    direction = Direction.Down,
    percentage = 10,  # 漲跌 % 數
    buy_sell = BSAction.Sell,
    quantity = 15000,
    price_type = ConditionPriceType.Market,
    diff = 0,     # 向上 or 向下追買 tick數 (向下為負值)
    time_in_force = TimeInForce.ROD,
    order_type = ConditionOrderType.Stock
)

sdk.stock.trail_profit(active_account, "20241127", "20250103", StopSign.Full, trail)

# %%
