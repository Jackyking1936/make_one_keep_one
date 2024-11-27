from PySide6.QtWidgets import QWidget, QPushButton, QLabel, QGridLayout, QVBoxLayout, QTableWidget, QPlainTextEdit
from PySide6.QtCore import Qt

class main_ui(QWidget):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("Python分批移動停損利小幫手(教學範例，僅限現股)")
        self.resize(1200, 600)
        
        # 製作上下排列layout上為庫存表，下為log資訊
        layout = QVBoxLayout()

        label_program_name = QLabel("條件單移動停損利(長短單版)")
        label_program_name.setStyleSheet("color: red; font-size: 24px; font-weight: bold;")

        # 庫存表表頭
        self.table_header = ['股票名稱', '股票代號', '類別', '庫存股數', '庫存均價', '現價', '損益試算', '獲利率%', '短移停(%)', '短股數', '短基準價', '短觸發價', '長移停(%)', '長股數', '長基準價', '長觸發價']

        self.tablewidget = QTableWidget(0, len(self.table_header))
        self.tablewidget.setHorizontalHeaderLabels([f'{item}' for item in self.table_header])

        # 模擬區layout設定
        self.button_fake_buy_filled = QPushButton('fake buy filled')
        self.button_fake_sell_filled = QPushButton('fake sell filled')
        self.button_fake_websocket = QPushButton('fake websocket')

        layout_sim = QGridLayout()
        label_sim = QLabel('測試用按鈕')
        label_sim.setStyleSheet("QLabel { font-size: 24px; font-weight: bold; }")
        label_sim.setAlignment(Qt.AlignCenter)
        layout_sim.addWidget(label_sim, 0, 1)
        layout_sim.addWidget(self.button_fake_buy_filled, 1, 0)
        layout_sim.addWidget(self.button_fake_sell_filled, 1, 1)
        layout_sim.addWidget(self.button_fake_websocket, 1, 2)
        
        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)

        layout.addWidget(self.tablewidget, stretch=7)
        layout.addLayout(layout_sim, stretch=1)
        layout.addWidget(self.log_text, stretch=3)
        self.setLayout(layout)