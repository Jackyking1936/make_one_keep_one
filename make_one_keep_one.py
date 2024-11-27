from login_gui_v3 import login_handler
from mk_one_ui import main_ui

import sys
import re
import pickle
import json
import time
from pathlib import Path
from datetime import datetime, timedelta

from fubon_neo.sdk import FubonSDK, Mode, Order, Condition, ConditionOrder
from fubon_neo.constant import ( 
    TriggerContent, TradingType, Operator, TPSLOrder, TPSLWrapper, SplitDescription,
    StopSign, TimeSliceOrderType, ConditionMarketType, ConditionPriceType, ConditionOrderType, TrailOrder, Direction, ConditionStatus, HistoryStatus
)
from fubon_neo.constant import TimeInForce, OrderType, PriceType, MarketType, BSAction

from PySide6.QtWidgets import QApplication, QWidget, QHeaderView, QTableWidgetItem
from PySide6.QtGui import QTextCursor
from PySide6.QtCore import Qt, Signal, QObject
from threading import Timer

class RepeatTimer(Timer):
    def run(self):
        while not self.finished.wait(self.interval):
            self.function(*self.args, **self.kwargs)

# 仿FilledData的物件
class fake_filled_data():
    date="2023/09/15"
    branch_no="6460"
    account="123"
    order_no="bA422"
    stock_no="00900"
    buy_sell=BSAction.Sell
    filled_no="00000000001"
    filled_avg_price=35.2
    filled_qty=1000
    filled_price=35.2
    order_type=OrderType.Stock
    filled_time="10:31:00.931"
    user_def=None

class Communicate(QObject):
    # 定義一個帶參數的信號
    print_log_signal = Signal(str)
    item_update_signal = Signal(str, str, str)
    handle_message_data_signal = Signal(dict)
    handle_filled_data_signal = Signal(object)
    table_init_signal = Signal()

class MainApp(QWidget):
    def __init__(self, login_handler):
        super().__init__()

        self.ws_mode = Mode.Normal
        self.login_handler = login_handler
        self.sdk = self.login_handler.sdk
        self.active_account = self.login_handler.active_account

        self.mk_one_ui = main_ui()

        self.setWindowIcon(self.login_handler.windowIcon())
        self.setWindowTitle(self.mk_one_ui.windowTitle())
        self.resize(1200, 600)

        # 將 main_ui 的佈局設定到 MainWindow
        self.setLayout(self.mk_one_ui.layout())
        self.table_header = self.mk_one_ui.table_header

        self.print_log("login success, 現在使用帳號: {}".format(self.active_account.account))
        self.print_log("建立行情連線...")
        self.sdk.init_realtime(self.ws_mode) # 建立行情連線
        self.print_log("行情連線建立OK")
        self.reststock = self.sdk.marketdata.rest_client.stock
        self.wsstock = self.sdk.marketdata.websocket_client.stock

        # slot function connect
        self.mk_one_ui.tablewidget.itemClicked[QTableWidgetItem].connect(self.onItemClicked)
        self.mk_one_ui.button_fake_websocket.clicked.connect(self.fake_ws_data)
        self.mk_one_ui.button_fake_buy_filled.clicked.connect(self.fake_buy_filled)
        self.mk_one_ui.button_fake_sell_filled.clicked.connect(self.fake_sell_filled)

        # communicator init and slot function connect
        self.communicator = Communicate()
        self.communicator.print_log_signal.connect(self.print_log)
        self.communicator.item_update_signal.connect(self.item_update)
        self.communicator.handle_message_data_signal.connect(self.message_update)
        self.communicator.handle_filled_data_signal.connect(self.handle_on_filled_data)
        self.communicator.table_init_signal.connect(self.table_init)
        
        # 初始化相關變數
        self.inventories = {}
        self.unrealized_pnl = {}
        self.row_idx_map = {}
        self.col_idx_map = dict(zip(self.table_header, range(len(self.table_header))))
        self.epsilon = 0.0000001
        self.mannul_disconnect = False

        self.tickers_name = {}
        self.tickers_name_init()
        self.subscribed_ids = {}
        
        # self.near_trail_stop = AutoSaveDict('trail_stop.json')
        # self.trail_guid_map = AutoSaveDict('trail_guid_map.json')
        self.near_trail_stop = {}
        self.far_trail_stop = {}
        self.near_guid_map = {}
        self.far_guid_map = {}

        # 模擬用變數
        self.fake_price_cnt = 0

        # 建立Websocket連線並初始化資料表
        self.print_log("建立WebSocket行情連線")
        self.sdk.init_realtime(self.ws_mode)
        self.wsstock = self.sdk.marketdata.websocket_client.stock
        self.wsstock.on("connect", self.handle_connect)
        self.wsstock.on("disconnect", self.handle_disconnect)
        self.wsstock.on("error", self.handle_error)
        self.wsstock.on('message', self.handle_message)
        self.wsstock.connect()

        self.print_log("抓取庫存...")
        self.communicator.table_init_signal.emit()
        self.sdk.set_on_event(self.on_event) 
        
    
    def on_event(self, code, content):
        print(f"event code:{code}, content:{content}")
        if code == "300":
            self.mannul_disconnect = True
            self.wsstock.disconnect()
            self.sdk, self.active_account = self.login_handler.re_login()
            self.ws_reconnect()
    
    # 當有庫存歸零時刪除該列的slot function
    def del_table_row(self, row_idx):
        self.mk_one_ui.tablewidget.removeRow(row_idx)
        
        for key, value in self.row_idx_map.items():
            if value > row_idx:
                self.row_idx_map[key] = value-1
            elif value == row_idx:
                pop_idx = key
        self.row_idx_map.pop(pop_idx)
        print("pop inventory finish")

    # 當有成交有不在現有庫存的現股股票時新增至現有表格最下方
    def add_new_inv(self, symbol, qty, price):
        row = self.mk_one_ui.tablewidget.rowCount()
        self.mk_one_ui.tablewidget.insertRow(row)
        
        for j in range(len(self.table_header)):
            item = QTableWidgetItem()
            if self.table_header[j] == '股票名稱':
                item.setText(self.tickers_name[symbol])
                self.mk_one_ui.tablewidget.setItem(row, j, item)
            elif self.table_header[j] == '股票代號':
                item.setText(symbol)
                self.mk_one_ui.tablewidget.setItem(row, j, item)
            elif self.table_header[j] == '類別':
                item.setText("Stock")
                self.mk_one_ui.tablewidget.setItem(row, j, item)
            elif self.table_header[j] == '庫存股數':
                item.setText(str(qty))
                self.mk_one_ui.tablewidget.setItem(row, j, item)
            elif self.table_header[j] == '庫存均價':
                item.setText(str(round(price+self.epsilon, 2)))
                self.mk_one_ui.tablewidget.setItem(row, j, item)
            elif self.table_header[j] == '現價':
                item.setText(str(round(price+self.epsilon, 2)))
                self.mk_one_ui.tablewidget.setItem(row, j, item)                   
            elif self.table_header[j] == '損益試算':
                cur_upnl = 0
                item.setText(str(cur_upnl))
                self.mk_one_ui.tablewidget.setItem(row, j, item)
            elif self.table_header[j] == '獲利率%':
                return_rate = 0
                item.setText(str(round(return_rate+self.epsilon, 2))+'%')
                self.mk_one_ui.tablewidget.setItem(row, j, item)
            elif self.table_header[j] == '短移停(%)':
                    item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEditable | Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                    item.setCheckState(Qt.Unchecked)
                    self.mk_one_ui.tablewidget.setItem(row, j, item)
            elif self.table_header[j] == '短基準價':
                item.setText('-')
                self.mk_one_ui.tablewidget.setItem(row, j, item)
            elif self.table_header[j] == '短觸發價':
                item.setText('-')
                self.mk_one_ui.tablewidget.setItem(row, j, item)
            elif self.table_header[j] == '短股數':
                item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEditable | Qt.ItemIsEnabled)
                item.setText('')
                self.mk_one_ui.tablewidget.setItem(row, j, item)
            elif self.table_header[j] == '長移停(%)':
                item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEditable | Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Unchecked)
                self.mk_one_ui.tablewidget.setItem(row, j, item)

            elif self.table_header[j] == '長基準價':
                item.setText('-')
                self.mk_one_ui.tablewidget.setItem(row, j, item)
            elif self.table_header[j] == '長觸發價':
                item.setText('-')
                self.mk_one_ui.tablewidget.setItem(row, j, item)
            elif self.table_header[j] == '長股數':
                item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEditable | Qt.ItemIsEnabled)
                item.setText('')
                self.mk_one_ui.tablewidget.setItem(row, j, item)

        self.row_idx_map[symbol] = row
        self.wsstock.subscribe({
            'channel': 'aggregates',
            'symbol': symbol
        })

    # 測試用假裝有賣出成交的按鈕slot function
    def fake_sell_filled(self):
        new_fake_sell = fake_filled_data()
        stock_list = ['2330', '2881', '2454'] #, '00940', '1101', '6598', '2509', '3230', '4903', '6661']
        for stock_no in stock_list:
            new_fake_sell.stock_no = stock_no
            new_fake_sell.buy_sell = BSAction.Sell
            new_fake_sell.filled_qty = 1000
            new_fake_sell.filled_price = 14
            new_fake_sell.account = self.active_account.account
            new_fake_sell.user_def = "inv_SL"
            self.on_filled(None, new_fake_sell)

    # 測試用假裝有買入成交的按鈕slot function
    def fake_buy_filled(self):
        stock_list = ['2330', '2881', '2454'] #, '00940', '1101', '6598', '2509', '3230', '4903', '6661']
        for stock_no in stock_list:
            new_fake_buy = fake_filled_data()
            new_fake_buy.stock_no = stock_no
            new_fake_buy.buy_sell = BSAction.Buy
            new_fake_buy.filled_qty = 2000
            new_fake_buy.filled_price = 17
            new_fake_buy.account = self.active_account.account
            self.on_filled(None, new_fake_buy)

    def handle_on_filled_data(self, filled_data):
        if filled_data.order_type == OrderType.Stock and filled_data.filled_qty >= 1000:
            symbol = filled_data.stock_no
            if filled_data.buy_sell == BSAction.Buy:
                print("buy:", filled_data.buy_sell)
                if (symbol, str(filled_data.order_type)) in self.inventories:
                    print("already in inventories", self.row_idx_map)
                    
                    inv_item = self.mk_one_ui.tablewidget.item(self.row_idx_map[symbol], self.col_idx_map['庫存股數'])
                    inv_qty = int(inv_item.text())
                    new_inv_qty = inv_qty + filled_data.filled_qty
                    
                    avg_price_item = self.mk_one_ui.tablewidget.item(self.row_idx_map[symbol], self.col_idx_map['庫存均價'])
                    avg_price = float(avg_price_item.text())
                    new_avg_price = ((inv_qty*avg_price) + (filled_data.filled_qty*filled_data.filled_price))/new_inv_qty
                    new_pnl = (filled_data.filled_price-new_avg_price)*new_inv_qty
                    new_cost = new_avg_price*new_inv_qty
                    new_rate_return = new_pnl/new_cost*100

                    # update row
                    self.mk_one_ui.tablewidget.item(self.row_idx_map[symbol], self.col_idx_map['庫存股數']).setText(str(new_inv_qty))
                    self.mk_one_ui.tablewidget.item(self.row_idx_map[symbol], self.col_idx_map['庫存均價']).setText(str(round(new_avg_price+self.epsilon, 2)))
                    self.mk_one_ui.tablewidget.item(self.row_idx_map[symbol], self.col_idx_map['現價']).setText(str(round(filled_data.filled_price+self.epsilon, 2)))
                    self.mk_one_ui.tablewidget.item(self.row_idx_map[symbol], self.col_idx_map['損益試算']).setText(str(round(new_pnl+self.epsilon, 2)))
                    self.mk_one_ui.tablewidget.item(self.row_idx_map[symbol], self.col_idx_map['獲利率%']).setText(str(round(new_rate_return+self.epsilon, 2))+"%")
                    self.print_log(f"{symbol}...買入 {filled_data.filled_qty} 股成交，成交價: {filled_data.filled_price}，新庫存股數: {new_inv_qty}，新庫存均價: {round(new_avg_price+self.epsilon, 2)}")

                else:
                    self.add_new_inv(symbol, filled_data.filled_qty, filled_data.filled_price)
                    self.inventories[(symbol, str(filled_data.order_type))] = filled_data
                    print("add new inv done")
                    self.print_log(f"{symbol}...新庫存，買入 {filled_data.filled_qty} 股成交，成交價: {filled_data.filled_price}")
                    
            elif filled_data.buy_sell == BSAction.Sell:
                print("sell:", symbol)
                # print(self.inventories)
                if (symbol, str(filled_data.order_type)) in self.inventories:
                    inv_item = self.mk_one_ui.tablewidget.item(self.row_idx_map[symbol], self.col_idx_map['庫存股數'])
                    inv_qty = int(inv_item.text())
                    remain_qty = inv_qty-filled_data.filled_qty
                    if remain_qty > 0:
                        remain_qty_str = str(int(round(remain_qty, 0)))
                        avg_item = self.mk_one_ui.tablewidget.item(self.row_idx_map[symbol], self.col_idx_map['庫存均價'])
                        avg_price = float(avg_item.text())
                        new_pnl = (filled_data.filled_price-avg_price)*remain_qty
                        new_cost = avg_price*remain_qty
                        new_rate_return = new_pnl/new_cost*100

                        # update row
                        self.mk_one_ui.tablewidget.item(self.row_idx_map[symbol], self.col_idx_map['庫存股數']).setText(remain_qty_str)
                        self.mk_one_ui.tablewidget.item(self.row_idx_map[symbol], self.col_idx_map['現價']).setText(str(round(filled_data.filled_price+self.epsilon, 2)))
                        self.mk_one_ui.tablewidget.item(self.row_idx_map[symbol], self.col_idx_map['損益試算']).setText(str(round(new_pnl+self.epsilon, 2)))
                        self.mk_one_ui.tablewidget.item(self.row_idx_map[symbol], self.col_idx_map['獲利率%']).setText(str(round(new_rate_return+self.epsilon, 2))+"%")
                        self.print_log(f"{symbol}...賣出 {filled_data.filled_qty} 股成交，成交價: {filled_data.filled_price}，新庫存股數: {remain_qty_str}")

                        if symbol in self.near_guid_map:
                            near_res = sdk.stock.get_condition_order_by_id(self.active_account, self.near_guid_map[symbol])
                            if "N" not in near_res.data.status:
                                near_item = self.mk_one_ui.tablewidget.item(self.row_idx_map[symbol], self.col_idx_map['短移停(%)'])
                                near_item.setText("已觸發")
                                near_item.setCheckState(Qt.UnChecked)

                        if symbol in self.far_guid_map:
                            far_res = sdk.stock.get_condition_order_by_id(self.active_account, self.far_guid_map[symbol])
                            if "N" not in far_res.data.status:
                                far_item = self.mk_one_ui.tablewidget.item(self.row_idx_map[symbol], self.col_idx_map['長移停(%)'])
                                far_item.setText("已觸發")
                                far_item.setCheckState(Qt.Unchecked)

                    elif remain_qty == 0:
                        # del table row and unsubscribe
                        self.del_table_row(self.row_idx_map[symbol])

                        if symbol in self.near_trail_stop:
                            self.near_trail_stop.pop(symbol)
                        if symbol in self.far_trail_stop:
                            self.far_trail_stop.pop(symbol)
                        if symbol in self.subscribed_ids:
                            self.wsstock.unsubscribe({
                                'id':self.subscribed_ids[symbol]
                            })
                        
                        # condition order delete process
                        if symbol in self.near_guid_map:
                            cancel_res = self.sdk.stock.cancel_condition_orders(self.active_account, self.near_guid_map[symbol])
                            # print(cancel_res)
                            self.print_log(f"{cancel_res}")
                            if cancel_res.is_success:
                                self.near_guid_map.pop(symbol)
                                self.print_log("trail order delete success")
                                # print("trail order delete success")
                        
                        if symbol in self.far_guid_map:
                            cancel_res = self.sdk.stock.cancel_condition_orders(self.active_account, self.far_guid_map[symbol])
                            # print(cancel_res)
                            self.print_log(f"{cancel_res}")
                            if cancel_res.is_success:
                                self.far_guid_map.pop(symbol)
                                self.print_log("trail order delete success")
                                # print("trail order delete success")
                        
                        self.print_log(f"{symbol}...賣出 {filled_data.filled_qty} 股成交，成交價: {filled_data.filled_price}，無剩餘庫存")                  
                        self.inventories.pop((symbol, str(filled_data.order_type)))


    # 主動回報，接入成交回報後判斷 row_idx_map 要如何更新，sl 及 tp 監控列表及庫存列表是否需pop，訂閱是否加退訂
    def on_filled(self, err, content):
        print('filled recived:', content.stock_no, content.buy_sell)
        print('content:', content)

        if content.account == self.active_account.account:
            self.communicator.handle_filled_data_signal.emit(content)

    # 測試用假裝有websocket data的按鈕slot function
    def fake_ws_data(self):
        if self.fake_price_cnt % 2==0:
            self.price_interval = 0
            self.fake_ws_timer = RepeatTimer(1, self.fake_message)
            self.fake_ws_timer.start()
        else:
            self.fake_ws_timer.cancel()

        self.fake_price_cnt+=1

    def fake_message(self):
        self.price_interval+=1
        # stock_list = ['2330', '2881', '2454', '00940', '1101', '6598', '2509', '3230', '4903', '6661']
        stock_list = ['00900']
        json_template = '''{{"event":"data", "data":{{"date": "2024-10-04", "type": "EQUITY", "exchange": "TPEx", "market": "OTC", "symbol": "{symbol}", "name": "建達", "referencePrice": 23.45, "previousClose": 23.45, "openPrice": 24.5, "openTime": 1728003611612464, "highPrice": 25.75, "highTime": 1728004595544768, "lowPrice": 23.75, "lowTime": 1728003784656798, "closePrice": 25.75, "closeTime": 1728019800000000, "avgPrice": 25.29, "change": 2.3, "changePercent": 9.81, "amplitude": 8.53, "lastPrice": 25.5, "lastSize": 10, "bids": [{{"price": 25.75, "size": 13285}}, {{"price": 25.7, "size": 22}}, {{"price": 25.6, "size": 4}}, {{"price": 25.55, "size": 1}}, {{"price": 25.5, "size": 1}}], "asks": [], "total": {{"tradeValue": 154027350, "tradeVolume": 6091, "tradeVolumeAtBid": 958, "tradeVolumeAtAsk": 4754, "transaction": 1235, "time": 1728019800000000}}, "lastTrade": {{"bid": 25.75, "price": {price}, "size": 10, "time": 1728019800000000, "serial": 2632362}}, "lastTrial": {{"bid": 25.75, "price": 25.75, "size": 10, "time": 1728019787685448, "serial": 2631092}}, "isLimitUpPrice": True, "isLimitUpBid": True, "isClose": True, "serial": 2632362, "lastUpdated": 1728019800000000}} ,"id":"w4mkzAqYAYFKyEBLyEjmHEoNADpwKjUJmqg02G3OC9YmV","channel":"aggregates"}}'''
        json_price = 15+self.price_interval
        json_str = json_template.format(symbol=stock_list[self.price_interval % len(stock_list)], price=str(json_price))
        json_str = json_str.replace('True', 'true')
        json_str = json_str.replace("\'", "\"")
        self.handle_message(json_str)

    # 更新表格內某一格值的slot function
    def item_update(self, symbol, col_name, value):
        try:
            self.mk_one_ui.tablewidget.item(self.row_idx_map[symbol], self.col_idx_map[col_name]).setText(value)
        except Exception as e:
            print(e, symbol, col_name, value)

    def trail_stop_market_order(self, symbol, trail_percent, order_qty, trigger_value):
        
        now_datetime = datetime.now()
        now_time_str = datetime.strftime(now_datetime, '%H%M%S')

        if now_time_str >= '133000':
            start_date = datetime.strftime(now_datetime+timedelta(days=1), '%Y%m%d')
            end_datetime = now_datetime + timedelta(days=89)
            end_date = datetime.strftime(end_datetime, '%Y%m%d')
        else:
            start_date = datetime.strftime(now_datetime, '%Y%m%d')
            end_datetime = now_datetime + timedelta(days=89)
            end_date = datetime.strftime(end_datetime, '%Y%m%d')

        trail_order = TrailOrder(
            symbol = symbol,
            price = str(trigger_value),
            direction = Direction.Down,
            percentage = trail_percent, # 漲跌 % 數
            buy_sell = BSAction.Sell,
            quantity = int(order_qty),
            price_type = ConditionPriceType.Market,
            diff = 0, # 向上 or 向下追買 tick數 (向下為負值)
            time_in_force = TimeInForce.ROD,
            order_type = ConditionOrderType.Stock
        )

        trail_order_res = sdk.stock.trail_profit(self.active_account, start_date, end_date, StopSign.Full, trail_order)
        return trail_order_res

    def onItemClicked(self, item):
        if item.checkState() == Qt.Checked:
            symbol = self.mk_one_ui.tablewidget.item(item.row(), self.col_idx_map['股票代號']).text()
            item_str = item.text() #停損或停利的輸入

            cur_price = self.mk_one_ui.tablewidget.item(item.row(), self.col_idx_map['現價']).text()
            cur_price = float(cur_price)

            inv_qty = self.mk_one_ui.tablewidget.item(item.row(), self.col_idx_map['庫存股數']).text()

            if item.column() == self.col_idx_map['短移停(%)']:
                if symbol in self.near_trail_stop:
                    return
                
                try:
                    near_trail_percent = int(item_str)
                    if near_trail_percent<=0:
                        raise ValueError("移停百分比需大於0之正整數")
                except Exception as e:
                    self.print_log(str(e))
                    self.print_log("請輸入正確移動停損利(%), 需為大於0之正整數")
                    item.setCheckState(Qt.Unchecked)
                    print("Trail Stop list:", self.near_trail_stop)
                    return
                
                near_qty_item = self.mk_one_ui.tablewidget.item(item.row(), self.col_idx_map['短股數'])
                far_qty_item = self.mk_one_ui.tablewidget.item(item.row(), self.col_idx_map['長股數'])
                far_qty = 0
                try:
                    far_qty = int(far_qty_item.text())
                except Exception as e:
                    print(f"error: {e}")

                try:
                    near_qty = int(near_qty_item.text())
                    if near_qty>int(inv_qty):
                        raise ValueError("設定股數不可大於庫存股數")
                    elif near_qty%1000 != 0:
                        raise ValueError("設定股數需為1000的倍數")
                    elif near_qty <= 0:
                        raise ValueError("設定股數需大於0")
                    elif near_qty+far_qty>int(inv_qty):
                        raise ValueError("總移動停損利股數超過庫存")

                except Exception as e:
                    self.print_log(f"Error:{e}")
                    self.print_log("請輸入正確移動停損股數，需為正整數(>0)且小於庫存股數")
                    item.setCheckState(Qt.Unchecked)
                    print("Trail Stop list:", self.near_trail_stop)
                    return

                # self.print_log("停損條件單設定中...")
                trail_res = self.trail_stop_market_order(symbol, near_trail_percent, near_qty, cur_price)
                if trail_res.is_success:
                    self.near_trail_stop[symbol] = near_trail_percent
                    self.near_guid_map[symbol] = trail_res.data.guid
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    self.print_log(f"{symbol}...移動停損利設定成功: {item_str}%, 單號: {trail_res.data.guid}")
                    self.mk_one_ui.tablewidget.item(item.row(), self.col_idx_map['短基準價']).setText(str(cur_price))
                    stop_price = round(cur_price*(100-near_trail_percent)/100, 2)
                    self.mk_one_ui.tablewidget.item(item.row(), self.col_idx_map['短觸發價']).setText(str(stop_price))
                    self.mk_one_ui.tablewidget.item(item.row(), self.col_idx_map['短股數']).setText(str(near_qty))

                else:
                    self.print_log(symbol+"...短移動停損利設定失敗: "+trail_res.message)
                    item.setCheckState(Qt.Unchecked)
                    
                print("Near Trail Stop list:", self.near_trail_stop)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                near_qty_item.setFlags(near_qty_item.flags() & ~Qt.ItemIsEditable)

            elif item.column() == self.col_idx_map['長移停(%)']:
                if symbol in self.far_trail_stop:
                    return
                
                try:
                    far_trail_percent = int(item_str)
                    if far_trail_percent<=0:
                        raise ValueError("移停百分比需大於0之正整數")
                except Exception as e:
                    self.print_log(str(e))
                    self.print_log("請輸入正確移動停損利(%), 需為大於0之正整數")
                    item.setCheckState(Qt.Unchecked)
                    print("Trail Stop list:", self.far_trail_stop)
                    return
                
                far_qty_item = self.mk_one_ui.tablewidget.item(item.row(), self.col_idx_map['長股數'])
                near_qty_item = self.mk_one_ui.tablewidget.item(item.row(), self.col_idx_map['短股數'])
                near_qty = 0
                try:
                    near_qty = int(near_qty_item.text())
                except Exception as e:
                    print(f"error: {e}")

                try:
                    far_qty = int(far_qty_item.text())
                    if far_qty>int(inv_qty):
                        raise ValueError("設定股數不可大於庫存股數")
                    elif far_qty%1000 != 0:
                        raise ValueError("設定股數需為1000的倍數")
                    elif far_qty <= 0:
                        raise ValueError("設定股數需大於0")
                    elif near_qty+far_qty>int(inv_qty):
                        raise ValueError("總移動停損利股數超過庫存")
                except Exception as e:
                    self.print_log(f"Error:{e}")
                    self.print_log("請輸入正確移動停損股數，需為正整數(>0)且小於庫存股數")
                    item.setCheckState(Qt.Unchecked)
                    print("Trail Stop list:", self.far_trail_stop)
                    return

                # self.print_log("停損條件單設定中...")
                trail_res = self.trail_stop_market_order(symbol, far_trail_percent, far_qty, cur_price)
                if trail_res.is_success:
                    self.far_trail_stop[symbol] = far_trail_percent
                    self.far_guid_map[symbol] = trail_res.data.guid
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    self.print_log(f"{symbol}...移動停損利設定成功: {item_str}%, 單號: {trail_res.data.guid}")
                    self.mk_one_ui.tablewidget.item(item.row(), self.col_idx_map['長基準價']).setText(str(cur_price))
                    stop_price = round(cur_price*(100-far_trail_percent)/100, 2)
                    self.mk_one_ui.tablewidget.item(item.row(), self.col_idx_map['長觸發價']).setText(str(stop_price))
                    self.mk_one_ui.tablewidget.item(item.row(), self.col_idx_map['長股數']).setText(str(far_qty))

                else:
                    self.print_log(symbol+"...短移動停損利設定失敗: "+trail_res.message)
                    item.setCheckState(Qt.Unchecked)
                    
                print("Far Trail Stop list:", self.far_trail_stop)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                far_qty_item.setFlags(far_qty_item.flags() & ~Qt.ItemIsEditable)

        elif item.checkState() == Qt.Unchecked:
            if item.column() == self.col_idx_map['短移停(%)']:
                item.setFlags(item.flags() | Qt.ItemIsEditable)
                near_qty_item = self.mk_one_ui.tablewidget.item(item.row(), self.col_idx_map['短股數'])
                near_qty_item.setFlags(near_qty_item.flags() | Qt.ItemIsEditable)
                symbol = self.mk_one_ui.tablewidget.item(item.row(), self.col_idx_map['股票代號']).text()
                if symbol in self.near_trail_stop:
                    cancel_res = self.sdk.stock.cancel_condition_orders(self.active_account, self.near_guid_map[symbol])
                    if cancel_res.is_success:
                        self.near_trail_stop.pop(symbol)
                        self.near_guid_map.pop(symbol)
                        self.print_log(symbol+"...短移停已移除，請重新設置")
                        self.mk_one_ui.tablewidget.item(item.row(), self.col_idx_map['短基準價']).setText('-')
                        self.mk_one_ui.tablewidget.item(item.row(), self.col_idx_map['短觸發價']).setText('-')
                        print("Near Trail Stop list:", self.near_trail_stop)

            elif item.column() == self.col_idx_map['長移停(%)']:
                item.setFlags(item.flags() | Qt.ItemIsEditable)
                far_qty_item = self.mk_one_ui.tablewidget.item(item.row(), self.col_idx_map['長股數'])
                far_qty_item.setFlags(far_qty_item.flags() | Qt.ItemIsEditable)
                symbol = self.mk_one_ui.tablewidget.item(item.row(), self.col_idx_map['股票代號']).text()
                if symbol in self.far_trail_stop:
                    cancel_res = self.sdk.stock.cancel_condition_orders(self.active_account, self.far_guid_map[symbol])
                    if cancel_res.is_success:
                        self.far_trail_stop.pop(symbol)
                        self.far_guid_map.pop(symbol)
                        self.print_log(symbol+"...長移停已移除，請重新設置")
                        self.mk_one_ui.tablewidget.item(item.row(), self.col_idx_map['長基準價']).setText('-')
                        self.mk_one_ui.tablewidget.item(item.row(), self.col_idx_map['長觸發價']).setText('-')
                        print("Far Trail Stop list:", self.far_trail_stop)

    def message_update(self, data_dict):
        symbol = data_dict["symbol"]
        
        if symbol not in self.row_idx_map:
            return
        
        if 'lastTrade' in data_dict:
            cur_price = data_dict['lastTrade']["price"]
        else:
            return
                    
        self.mk_one_ui.tablewidget.item(self.row_idx_map[symbol], self.col_idx_map['現價']).setText(str(cur_price))
    
        avg_price_item = self.mk_one_ui.tablewidget.item(self.row_idx_map[symbol], self.col_idx_map['庫存均價'])
        avg_price = avg_price_item.text()
    
        share_item = self.mk_one_ui.tablewidget.item(self.row_idx_map[symbol], self.col_idx_map['庫存股數'])
        share = share_item.text()
    
        cur_pnl = (cur_price-float(avg_price))*float(share)
        self.mk_one_ui.tablewidget.item(self.row_idx_map[symbol], self.col_idx_map['損益試算']).setText(str(int(round(cur_pnl, 0))))
    
        return_rate = cur_pnl/(float(avg_price)*float(share))*100
        self.mk_one_ui.tablewidget.item(self.row_idx_map[symbol], self.col_idx_map['獲利率%']).setText(str(round(return_rate+self.epsilon, 2))+'%')
        
        near_base_price_item = self.mk_one_ui.tablewidget.item(self.row_idx_map[symbol], self.col_idx_map['短基準價'])
        far_base_price_item = self.mk_one_ui.tablewidget.item(self.row_idx_map[symbol], self.col_idx_map['長基準價'])
        if near_base_price_item.text() == '-':
            return
        else:
            near_base_price = float(near_base_price_item.text())
            
            if cur_price>near_base_price:
                item_trail_percent = int(self.mk_one_ui.tablewidget.item(self.row_idx_map[symbol], self.col_idx_map['短移停(%)']).text())
                new_base_price = cur_price
                new_stop_price = round(new_base_price*(100-item_trail_percent)/100, 2)
                near_base_price_item.setText(str(new_base_price))
                self.mk_one_ui.tablewidget.item(near_base_price_item.row(), self.col_idx_map['短觸發價']).setText(str(new_stop_price))
        
        if far_base_price_item.text() == '-':
            return
        else:
            far_base_price = float(far_base_price_item.text())
            
            if cur_price>far_base_price:
                item_trail_percent = int(self.mk_one_ui.tablewidget.item(self.row_idx_map[symbol], self.col_idx_map['長移停(%)']).text())
                new_base_price = cur_price
                new_stop_price = round(new_base_price*(100-item_trail_percent)/100, 2)
                far_base_price_item.setText(str(new_base_price))
                self.mk_one_ui.tablewidget.item(far_base_price_item.row(), self.col_idx_map['長觸發價']).setText(str(new_stop_price))

    def handle_message(self, message):
        msg = json.loads(message)
        event = msg["event"]
        data = msg["data"]
        # print(event, data)
        
        # subscribed事件處理
        if event == "subscribed":
            id = data["id"]
            symbol = data["symbol"]
            self.communicator.print_log_signal.emit('訂閱成功...'+symbol)
            self.subscribed_ids[symbol] = id
        
        elif event == "unsubscribed":
            for key, value in self.subscribed_ids.items():
                if value == data["id"]:
                    print(key, value)
                    remove_key = key
            self.subscribed_ids.pop(remove_key)
            self.communicator.print_log_signal.emit(remove_key+"...成功移除訂閱")
        elif event == "snapshot":
            if 'isTrial' in data:
                if data['isTrial']:
                    return
            self.communicator.handle_message_data_signal.emit(data)

        # data事件處理
        elif event == "data":
            if 'isTrial' in data:
                if data['isTrial']:
                    return
            self.communicator.handle_message_data_signal.emit(data)
            
            
    def handle_connect(self):
        self.communicator.print_log_signal.emit('market data connected')
        self.mannul_disconnect = False
    
    def handle_disconnect(self, code, message):
        self.communicator.print_log_signal.emit(f'market data disconnect: {code}, {message}')
        if self.mannul_disconnect == True:
            self.communicator.print_log_signal.emit(f"manuully disconnect, disconnected")
        else:
            self.communicator.print_log_signal.emit(f"unexpected disconnect, reconnecting...")
            self.ws_reconnect()
            

    def ws_reconnect(self):
        self.sdk.init_realtime(self.ws_mode)
        self.wsstock = self.sdk.marketdata.websocket_client.stock
        self.wsstock.on("connect", self.handle_connect)
        self.wsstock.on("disconnect", self.handle_disconnect)
        self.wsstock.on("error", self.handle_error)
        self.wsstock.on('message', self.handle_message)
        self.wsstock.connect()

        for symbol in list(self.row_idx_map.keys()):
            self.wsstock.subscribe({
                'channel': 'aggregates',
                "symbol": symbol
            })
            
    
    def handle_error(self, error):
        self.communicator.print_log_signal.emit(f'market data error: {error}')
    
    def trail_stop_fetch(self):
        today_date = datetime.today()
        end_date = datetime.strftime(today_date, "%Y%m%d")
        start_date = datetime.strftime(today_date-timedelta(days=90), "%Y%m%d")
        trail_hist_res = sdk.stock.get_trail_history(self.active_account, start_date, end_date)
        time.sleep(0.2)
        if not trail_hist_res.is_success:
            print(f"Trail Hist fetch fail, start_date:{start_date}, end_date:{end_date}, message:{trail_hist_res.message}")
        else:
            for i, detail in enumerate(trail_hist_res.data):
                if "N" in detail.status or "Y" in detail.status:
                    print(i)
                    time.sleep(0.2)
                    detail_res = sdk.stock.get_condition_order_by_id(self.active_account, detail.guid)
                    if detail_res.is_success:
                        percent_text = detail.condition_content
                        trigger_text = detail_res.data[0].condition_content
                    else:
                        print(f"{detail.symbol} fetch detail res fail, message: {detail_res.message}")
                        self.print_log(f"{detail.symbol} fetch detail res fail, message: {detail_res.message}")
                        continue
                
                    trail_percent_match = re.search(r'(\d+(?:\.\d+)?)%', percent_text)
                    trigger_price_match = re.search(r'等於(\d+(?:\.\d+)?)元', trigger_text)

                    trail_percent = '-'
                    if trail_percent_match:
                        trail_percent = trail_percent_match.group(1)
                    else:
                        print(f"{detail.symbol} trail percent match fail")

                    trigger_price = '-'
                    base_price = '-'
                    if trigger_price_match:
                        trigger_price = trigger_price_match.group(1)
                        base_price = str(round(float(trigger_price)/((100-float(trail_percent))/100), 2))
                    else:
                        print(f"{detail.symbol} trigger price match fail")
                    trail_share = '-'
                    if '張' in detail.condition_volume:
                        trail_share = str(int(detail.condition_volume[:-1])*1000)

                    if detail.symbol in self.near_trail_stop:
                        if int(trail_percent) > self.near_trail_stop[detail.symbol]:
                            self.far_trail_stop[detail.symbol] = int(trail_percent)
                            self.far_guid_map[detail.symbol] = detail.guid
                            trail_percent_item = self.mk_one_ui.tablewidget.item(self.row_idx_map[detail.symbol], self.col_idx_map['長移停(%)'])
                            trail_percent_item.setText(trail_percent)
                            trail_percent_item.setCheckState(Qt.Checked)
                            self.mk_one_ui.tablewidget.item(self.row_idx_map[detail.symbol], self.col_idx_map['長基準價']).setText(base_price)
                            self.mk_one_ui.tablewidget.item(self.row_idx_map[detail.symbol], self.col_idx_map['長觸發價']).setText(trigger_price)
                            far_qty_item = self.mk_one_ui.tablewidget.item(self.row_idx_map[detail.symbol], self.col_idx_map['長股數'])
                            far_qty_item.setText(trail_share)
                            trail_percent_item.setFlags(trail_percent_item.flags() & ~Qt.ItemIsEditable)
                            far_qty_item.setFlags(far_qty_item.flags() & ~Qt.ItemIsEditable)
                        else:
                            self.far_trail_stop[detail.symbol] = self.near_trail_stop[detail.symbol]
                            self.far_guid_map[detail.symbol] = self.near_guid_map[detail.symbol]
                            trail_percent_item = self.mk_one_ui.tablewidget.item(self.row_idx_map[detail.symbol], self.col_idx_map['長移停(%)'])
                            trail_percent_item.setText(str(self.far_trail_stop[detail.symbol]))
                            trail_percent_item.setCheckState(Qt.Checked)
                            self.mk_one_ui.tablewidget.item(self.row_idx_map[detail.symbol], self.col_idx_map['長基準價']).setText(self.mk_one_ui.tablewidget.item(self.row_idx_map[detail.symbol], self.col_idx_map['短基準價']).text())
                            self.mk_one_ui.tablewidget.item(self.row_idx_map[detail.symbol], self.col_idx_map['長觸發價']).setText(self.mk_one_ui.tablewidget.item(self.row_idx_map[detail.symbol], self.col_idx_map['短觸發價']).text())
                            far_qty_item = self.mk_one_ui.tablewidget.item(self.row_idx_map[detail.symbol], self.col_idx_map['長股數'])
                            far_qty_item.setText(self.mk_one_ui.tablewidget.item(self.row_idx_map[detail.symbol], self.col_idx_map['短股數']).text())
                            trail_percent_item.setFlags(trail_percent_item.flags() & ~Qt.ItemIsEditable)
                            far_qty_item.setFlags(far_qty_item.flags() & ~Qt.ItemIsEditable)

                            self.near_trail_stop[detail.symbol] = int(trail_percent)
                            self.near_guid_map[detail.symbol] = detail.guid
                            trail_percent_item = self.mk_one_ui.tablewidget.item(self.row_idx_map[detail.symbol], self.col_idx_map['短移停(%)'])
                            trail_percent_item.setText(trail_percent)
                            trail_percent_item.setCheckState(Qt.Checked)
                            self.mk_one_ui.tablewidget.item(self.row_idx_map[detail.symbol], self.col_idx_map['短基準價']).setText(base_price)
                            self.mk_one_ui.tablewidget.item(self.row_idx_map[detail.symbol], self.col_idx_map['短觸發價']).setText(trigger_price)
                            self.mk_one_ui.tablewidget.item(self.row_idx_map[detail.symbol], self.col_idx_map['短股數']).setText(trail_share)
                    else:
                        self.near_trail_stop[detail.symbol] = int(trail_percent)
                        self.near_guid_map[detail.symbol] = detail.guid
                        trail_percent_item = self.mk_one_ui.tablewidget.item(self.row_idx_map[detail.symbol], self.col_idx_map['短移停(%)'])
                        trail_percent_item.setText(trail_percent)
                        trail_percent_item.setCheckState(Qt.Checked)
                        self.mk_one_ui.tablewidget.item(self.row_idx_map[detail.symbol], self.col_idx_map['短基準價']).setText(base_price)
                        self.mk_one_ui.tablewidget.item(self.row_idx_map[detail.symbol], self.col_idx_map['短觸發價']).setText(trigger_price)
                        near_qty_item = self.mk_one_ui.tablewidget.item(self.row_idx_map[detail.symbol], self.col_idx_map['短股數'])
                        near_qty_item.setText(trail_share)
                        trail_percent_item.setFlags(trail_percent_item.flags() & ~Qt.ItemIsEditable)
                        near_qty_item.setFlags(near_qty_item.flags() & ~Qt.ItemIsEditable)

    # 視窗啟動時撈取對應帳號的inventories和unrealized_pnl初始化表格
    def table_init(self):
        inv_res = self.sdk.accounting.inventories(self.active_account)
        if inv_res.is_success:
            self.print_log("庫存抓取成功")
            inv_data = inv_res.data
            for inv in inv_data:
                if inv.today_qty != 0 and inv.order_type == OrderType.Stock:
                    self.inventories[(inv.stock_no, str(inv.order_type))] = inv
        else:
            self.print_log("庫存抓取失敗")
        
        self.print_log("抓取未實現損益...")
        upnl_res = self.sdk.accounting.unrealized_gains_and_loses(self.active_account)
        if upnl_res.is_success:
            self.print_log("未實現損益抓取成功")
            upnl_data = upnl_res.data
            for upnl in upnl_data:
                self.unrealized_pnl[(upnl.stock_no, str(upnl.order_type))] = upnl
        else:
            self.print_log("未實現損益抓取失敗")

        get_res = self.sdk.stock.get_condition_order(self.active_account)
        condition_status_map = {}
        if get_res.is_success:
            for res in get_res.data:
                condition_status_map[res.guid] = res.status

        # 依庫存及未實現損益資訊開始填表
        for key, value in self.inventories.items():
            stock_symbol = key[0]
            stock_name = self.tickers_name[key[0]]
            print(stock_symbol)
            row = self.mk_one_ui.tablewidget.rowCount()
            self.mk_one_ui.tablewidget.insertRow(row)
            self.row_idx_map[stock_symbol] = row
            for j in range(len(self.table_header)):
                item = QTableWidgetItem()
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if self.table_header[j] == '股票名稱':
                    item.setText(stock_name)
                    self.mk_one_ui.tablewidget.setItem(row, j, item)
                elif self.table_header[j] == '股票代號':
                    item.setText(stock_symbol)
                    self.mk_one_ui.tablewidget.setItem(row, j, item)
                elif self.table_header[j] == '類別':
                    item.setText(str(value.order_type).split('.')[-1])
                    self.mk_one_ui.tablewidget.setItem(row, j, item)
                elif self.table_header[j] == '庫存股數':
                    item.setText(str(value.today_qty))
                    self.mk_one_ui.tablewidget.setItem(row, j, item)
                elif self.table_header[j] == '現價':
                    item.setText('-')
                    self.mk_one_ui.tablewidget.setItem(row, j, item)

                elif self.table_header[j] == '庫存均價':
                    item.setText(str(round(self.unrealized_pnl[key].cost_price+self.epsilon, 2)))
                    self.mk_one_ui.tablewidget.setItem(row, j, item)
                elif self.table_header[j] == '損益試算':
                    cur_upnl = 0
                    if self.unrealized_pnl[key].unrealized_profit > self.unrealized_pnl[key].unrealized_loss:
                        cur_upnl = self.unrealized_pnl[key].unrealized_profit
                    else:
                        cur_upnl = -(self.unrealized_pnl[key].unrealized_loss)
                    item.setText(str(cur_upnl))
                    self.mk_one_ui.tablewidget.setItem(row, j, item)
                elif self.table_header[j] == '獲利率%':
                    cur_upnl = 0
                    if self.unrealized_pnl[key].unrealized_profit > self.unrealized_pnl[key].unrealized_loss:
                        cur_upnl = self.unrealized_pnl[key].unrealized_profit
                    else:
                        cur_upnl = -(self.unrealized_pnl[key].unrealized_loss)
                    stock_cost = value.today_qty*self.unrealized_pnl[key].cost_price
                    return_rate = cur_upnl/stock_cost*100
                    item.setText(str(round(return_rate+self.epsilon, 2))+'%')
                    self.mk_one_ui.tablewidget.setItem(row, j, item)

                elif self.table_header[j] == '短移停(%)':
                    item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEditable | Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                    item.setCheckState(Qt.Unchecked)
                    self.mk_one_ui.tablewidget.setItem(row, j, item)

                elif self.table_header[j] == '短基準價':
                    item.setText('-')
                    self.mk_one_ui.tablewidget.setItem(row, j, item)
                elif self.table_header[j] == '短觸發價':
                    item.setText('-')
                    self.mk_one_ui.tablewidget.setItem(row, j, item)
                elif self.table_header[j] == '短股數':
                    item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEditable | Qt.ItemIsEnabled)
                    item.setText('')
                    self.mk_one_ui.tablewidget.setItem(row, j, item)
                elif self.table_header[j] == '長移停(%)':
                    item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEditable | Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                    item.setCheckState(Qt.Unchecked)
                    self.mk_one_ui.tablewidget.setItem(row, j, item)

                elif self.table_header[j] == '長基準價':
                    item.setText('-')
                    self.mk_one_ui.tablewidget.setItem(row, j, item)
                elif self.table_header[j] == '長觸發價':
                    item.setText('-')
                    self.mk_one_ui.tablewidget.setItem(row, j, item)
                elif self.table_header[j] == '長股數':
                    item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEditable | Qt.ItemIsEnabled)
                    item.setText('')
                    self.mk_one_ui.tablewidget.setItem(row, j, item)

            self.wsstock.subscribe({
                'channel': 'aggregates',
                'symbol': stock_symbol
            })
        self.trail_stop_fetch()
        self.print_log('庫存資訊初始化完成')

        # 調整股票名稱欄位寬度
        header = self.mk_one_ui.tablewidget.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        print(self.row_idx_map)
        print(self.col_idx_map)

    def tickers_name_init(self):
        self.tickers_res = self.reststock.snapshot.quotes(market='TSE')
        for item in self.tickers_res['data']:
            if 'name' in item:
                self.tickers_name.update({item['symbol']: item['name']})
            else:
                self.tickers_name.update({item['symbol']: ''})

        self.tickers_res = self.reststock.snapshot.quotes(market='OTC')
        for item in self.tickers_res['data']:
            if 'name' in item:
                self.tickers_name.update({item['symbol']: item['name']})
            else:
                self.tickers_name.update({item['symbol']: ''})

    # 更新最新log到QPlainTextEdit的slot function
    def print_log(self, log_info):
        self.mk_one_ui.log_text.appendPlainText(log_info)
        self.mk_one_ui.log_text.moveCursor(QTextCursor.End)
    
    # 視窗關閉時要做的事，主要是關websocket連結
    def closeEvent(self, event):
        # do stuff
        self.print_log("disconnect websocket...")
        self.mannul_disconnect = True
        self.wsstock.disconnect()
        self.sdk.logout()

        try:
            if self.fake_ws_timer.is_alive():
                self.fake_ws_timer.cancel()
        except AttributeError:
            print("no fake ws timer exist")

        can_exit = True
        if can_exit:
            event.accept() # let the window close
        else:
            event.ignore()


if __name__ == '__main__':
    try:
        sdk = FubonSDK()
    except ValueError:
        raise ValueError("請確認網路連線")
    
    if not QApplication.instance():
        app = QApplication(sys.argv)
    else:
        app = QApplication.instance()
    app.setStyleSheet("QWidget{font-size: 12pt;}")
    login_form = login_handler(sdk, MainApp, 'trail.png')
    login_form.show()
    login_form_res = app.exec()

    sys.exit(login_form_res)