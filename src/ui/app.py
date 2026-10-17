"""Application bootstrap and lifecycle runner for PySide6 Desktop Studio."""

import logging
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from src.ui.main_window import StudioMainWindow

logger = logging.getLogger(__name__)


def load_stylesheet() -> str:
    """Load the dark studio QSS stylesheet from disk."""
    qss_path = Path(__file__).parent / "styles" / "dark_studio.qss"
    if qss_path.is_file():
        try:
            return qss_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("Failed to read QSS stylesheet (%s).", e)
    return ""


def create_app(argv: list[str] | None = None) -> QApplication:
    """Create or return existing QApplication with high-DPI scaling and dark theme."""
    existing = QApplication.instance()
    if existing:
        return existing

    app = QApplication(argv or sys.argv[:1])
    app.setApplicationName("TeamTrau AutoEdit")
    app.setOrganizationName("TeamTrau")
    app.setApplicationVersion("1.0.0-phase6")

    # Apply Dark Studio theme
    stylesheet = load_stylesheet()
    if stylesheet:
        app.setStyleSheet(stylesheet)

    return app


def run_studio_app(dry_run: bool = False) -> int:
    """Launch the Studio GUI or perform verification dry-run."""
    logger.info("Initializing TeamTrau AutoEdit Desktop Studio (dry_run=%s)...", dry_run)
    app = create_app()

    window = StudioMainWindow()

    if dry_run:
        logger.info("GUI Dry-Run Mode: Verifying widget instantiation and geometry...")
        assert window.isWidgetType(), "StudioMainWindow failed to initialize as valid QWidget"
        logger.info("Main window successfully validated. Title: '%s'", window.windowTitle())
        window.close()
        return 0

    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(run_studio_app())
