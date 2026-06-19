"""Standalone entry point (PyInstaller / `python main.py`).

PyInstaller runs the entry script as a top-level module with no package context, so it can't use
the package-relative imports in gamgui/app.py. This launcher imports the package absolutely; the
rest of the app keeps its normal relative imports.
"""

from gamgui.app import main

if __name__ == "__main__":
    main()
