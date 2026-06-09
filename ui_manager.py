import asyncio
import json
import logging
import os
import threading
from pathlib import Path
from typing import Dict, List

from PyQt5.QtWidgets import QMainWindow, QApplication, QPushButton, QTableWidgetItem
from PyQt5.QtCore import QTimer

from bot_engine import BlsBotEngine
from core import ProxyManager, SecureVault, TelegramNotifier
from mygui import Ui_Dialog

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


class AsyncLoopThread(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.loop = asyncio.new_event_loop()

    def run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)


class UiManager(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.ui = Ui_Dialog()
        self.ui.setupUi(self)
        self.vault = SecureVault()
        self.vault.upgrade_legacy(Path('data.json'))
        self.proxy_manager = ProxyManager()
        self.notifier = TelegramNotifier(
            token=os.environ.get('BLS_TELEGRAM_TOKEN', ''),
            chat_id=os.environ.get('BLS_TELEGRAM_CHAT_ID', ''),
        )
        self.engine = BlsBotEngine(self.vault, self.notifier, self.proxy_manager)
        self.loop_thread = AsyncLoopThread()
        self.loop_thread.start()
        self._connect_ui()
        self._load_accounts()

    def _connect_ui(self) -> None:
        self.ui.startButton.clicked.connect(self.start_bot)
        self.ui.deleteButton.clicked.connect(self.delete_selected)
        self.ui.saveButton.clicked.connect(self.save_accounts)
        self.ui.addButton.clicked.connect(self.add_account)

    def _load_accounts(self) -> None:
        self.ui.tableWidget.setColumnCount(3)
        self.ui.tableWidget.setHorizontalHeaderLabels(['Email', 'Password', 'Details'])
        accounts = self.vault.load_accounts()
        self.ui.tableWidget.setRowCount(len(accounts))
        for idx, account in enumerate(accounts):
            self.ui.tableWidget.setItem(idx, 0, QTableWidgetItem(account['email']))
            self.ui.tableWidget.setItem(idx, 1, QTableWidgetItem('******' if account['password'] else ''))
            self.ui.tableWidget.setItem(idx, 2, QTableWidgetItem(json.dumps(account['details'])))

    def start_bot(self) -> None:
        self.ui.startButton.setEnabled(False)
        loop = self.loop_thread.loop
        asyncio.run_coroutine_threadsafe(self.engine.start(), loop)
        QTimer.singleShot(2000, lambda: self.ui.startButton.setEnabled(True))

    def add_account(self) -> None:
        row = self.ui.tableWidget.rowCount()
        self.ui.tableWidget.insertRow(row)
        self.ui.tableWidget.setItem(row, 0, QTableWidgetItem(''))
        self.ui.tableWidget.setItem(row, 1, QTableWidgetItem(''))
        self.ui.tableWidget.setItem(row, 2, QTableWidgetItem('{}'))

    def delete_selected(self) -> None:
        rows = set(item.row() for item in self.ui.tableWidget.selectedItems())
        for row in sorted(rows, reverse=True):
            self.ui.tableWidget.removeRow(row)
        self.save_accounts()

    def save_accounts(self) -> None:
        rows = self.ui.tableWidget.rowCount()
        accounts = []
        for row in range(rows):
            email = self.ui.tableWidget.item(row, 0).text() if self.ui.tableWidget.item(row, 0) else ''
            password = self.ui.tableWidget.item(row, 1).text() if self.ui.tableWidget.item(row, 1) else ''
            details_text = self.ui.tableWidget.item(row, 2).text() if self.ui.tableWidget.item(row, 2) else '{}'
            try:
                details = json.loads(details_text)
            except Exception:
                details = {'metadata': details_text}
            accounts.append({'email': email, 'password': password, 'details': details})
        self.vault.save_accounts(accounts)

    def closeEvent(self, event) -> None:
        asyncio.run_coroutine_threadsafe(self.engine.stop(), self.loop_thread.loop)
        self.loop_thread.stop()
        super().closeEvent(event)
