"""Main window: sidebar-driven page navigation.

Replaces the Textual version's F-key `switch_screen` dispatch with the
standard Qt "list of pages on the left, QStackedWidget on the right" pattern
(e.g. Qt Creator's preferences) -- immediately familiar without a legend of
function keys.

Since QStackedWidget keeps every page alive (unlike Textual's forced fresh-
instance-per-navigation), "reload on every visit" for Maintenance/Skins/
Contracts is reintroduced explicitly via each page's `on_page_shown()`, called
whenever the stack's current page changes. The Contract Builder page has no
such hook -- its in-progress state surviving navigation falls out naturally
from being the same persistent widget instance, the same reason the Textual
version deliberately reused one long-lived screen instance for it.
"""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QListWidget, QMainWindow, QStackedWidget, QWidget

from .pages.contract_builder_page import ContractBuilderPage
from .pages.contracts_page import ContractsPage
from .pages.maintenance_page import MaintenancePage
from .pages.skins_page import SkinsPage


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("braindamage")
        self.resize(1200, 800)

        self._nav = QListWidget(self)
        self._nav.setMaximumWidth(180)

        self._stack = QStackedWidget(self)
        pages = [
            ("Maintenance", MaintenancePage(self)),
            ("Skins", SkinsPage(self)),
            ("Build Contract", ContractBuilderPage(self)),
            ("Contracts", ContractsPage(self)),
        ]
        for label, page in pages:
            self._nav.addItem(label)
            self._stack.addWidget(page)

        self._nav.currentRowChanged.connect(self._stack.setCurrentIndex)
        self._stack.currentChanged.connect(self._on_page_changed)

        central = QWidget(self)
        layout = QHBoxLayout(central)
        layout.addWidget(self._nav)
        layout.addWidget(self._stack, 1)
        self.setCentralWidget(central)

        self._nav.setCurrentRow(0)
        self._on_page_changed(self._stack.currentIndex())  # setCurrentRow(0) is a no-op if already 0

    def _on_page_changed(self, index: int) -> None:
        widget = self._stack.widget(index)
        on_page_shown = getattr(widget, "on_page_shown", None)
        if callable(on_page_shown):
            on_page_shown()
