"""Small, replaceable wrappers around Windows-native desktop actions."""
from __future__ import annotations

import os
from pathlib import Path
import sys


def choose_directory(title: str) -> Path | None:
    """Show a native directory chooser, returning ``None`` when cancelled."""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    try:
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(title=title, mustexist=True)
        return Path(selected) if selected else None
    finally:
        root.destroy()


def open_directory(path: Path) -> None:
    """Open a directory in Explorer, or fail predictably off Windows."""
    if sys.platform != "win32":
        raise RuntimeError("opening a directory is supported only on Windows")
    os.startfile(str(Path(path)))
