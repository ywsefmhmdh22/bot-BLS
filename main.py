import sys

from PyQt5.QtWidgets import QApplication

from ui_manager import UiManager


def main() -> None:
    app = QApplication(sys.argv)
    window = UiManager()
    window.show()
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
