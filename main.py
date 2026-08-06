import sys

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication

from braindamage.db import upgrade_database
from braindamage.ui.main_window import MainWindow


def main() -> None:
    upgrade_database()
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    exit_code = app.exec()
    # Let any in-flight background worker (a filter/fetch/mono-trade job) settle
    # before the process tears down -- otherwise a worker mid-emit can race
    # object destruction and raise "Signal source has been deleted".
    QThreadPool.globalInstance().waitForDone(5000)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
