"""Native Windows DPI rendering and proportional desktop layout."""

import sys
import tkinter as tk
from tkinter import ttk


def enable_high_dpi():
    """Opt in before Tk creates any windows; leave existing host settings alone."""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    try:
        set_context = user32.SetProcessDpiAwarenessContext
        set_context.argtypes = [wintypes.HANDLE]
        set_context.restype = wintypes.BOOL
        if set_context(ctypes.c_void_p(-4)) or ctypes.get_last_error() == 5:
            return
    except AttributeError:
        pass
    try:
        set_awareness = ctypes.WinDLL("shcore").SetProcessDpiAwareness
        set_awareness.argtypes = [ctypes.c_int]
        set_awareness.restype = ctypes.c_long
        if set_awareness(2) in (0, -2147024891):
            return
    except (AttributeError, OSError):
        pass
    user32.SetProcessDPIAware()


def display_scale(root):
    """Return physical pixels per 96-DPI layout pixel."""
    return round(float(root.winfo_fpixels("1i")) / 96, 3)


def configure_window(root, width, height, minimum_width, minimum_height):
    scale = display_scale(root)
    available_width = root.winfo_screenwidth() - round(48 * scale)
    available_height = root.winfo_screenheight() - round(72 * scale)
    root.geometry(f"{min(round(width * scale), available_width)}x{min(round(height * scale), available_height)}")
    root.minsize(min(round(minimum_width * scale), available_width),
                 min(round(minimum_height * scale), available_height))


def scale_layout(widget, factor=None):
    """Scale pixel dimensions, keeping character counts and relative positions intact."""
    factor = display_scale(widget) if factor is None else factor
    previous = getattr(widget, "_autodl_layout_scale", 1.0)
    ratio = factor / previous
    if abs(ratio - 1) > 0.01:
        def scaled(value):
            parts = value if isinstance(value, (tuple, list)) else widget.tk.splitlist(str(value))
            return tuple(round(float(str(part)) * ratio) for part in parts)

        options = widget.keys()
        pixel_options = ("padding", "borderwidth", "highlightthickness", "wraplength")
        for option in pixel_options:
            if option in options:
                value = widget.cget(option)
                if str(value):
                    widget.configure(**{option: scaled(value)})
        if isinstance(widget, (tk.Canvas, tk.Frame, ttk.Frame)):
            for option in ("width", "height"):
                widget.configure(**{option: round(float(widget.cget(option)) * ratio)})
        manager = widget.winfo_manager()
        if manager in ("pack", "grid"):
            info = widget.pack_info() if manager == "pack" else widget.grid_info()
            padding = {key: scaled(info[key]) for key in ("padx", "pady", "ipadx", "ipady") if key in info}
            if manager == "pack":
                widget.pack_configure(**padding)
            else:
                widget.grid_configure(**padding)
        if isinstance(widget, ttk.Treeview):
            for column in widget.cget("columns"):
                sizes = widget.column(column)
                widget.column(column, width=round(sizes["width"] * ratio), minwidth=round(sizes["minwidth"] * ratio))
        if isinstance(widget, tk.Canvas):
            widget.scale("all", 0, 0, ratio, ratio)
            for item in widget.find_all():
                if widget.type(item) in ("line", "rectangle", "oval", "polygon"):
                    widget.itemconfigure(item, width=float(widget.itemcget(item, "width")) * ratio)
    widget._autodl_layout_scale = factor
    for child in widget.winfo_children():
        scale_layout(child, factor)
