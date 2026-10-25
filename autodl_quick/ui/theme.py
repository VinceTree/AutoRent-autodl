"""Shared, dependency-free desktop palette and ttk widget styles."""

from tkinter import font, ttk
from .display import display_scale


COLORS = {
    "background": "#edf2f6",
    "surface": "#ffffff",
    "ink": "#20364a",
    "muted": "#607489",
    "border": "#dce5ec",
    "accent": "#087f78",
    "accent_hover": "#06665f",
    "accent_soft": "#e5f3f0",
    "subtle": "#f5f8fa",
    "warning": "#9b590e",
    "warning_soft": "#fff2df",
    "error": "#b13e48",
}


def configure_theme(root):
    """Use the same theme in every window sharing a Tcl interpreter."""
    style = ttk.Style(root)
    # Reapplying is safe for Toplevels; changing the theme would reset other windows.
    if style.theme_use() != "clam":
        style.theme_use("clam")
    families = set(font.families(root))
    face = next((name for name in ("HarmonyOS Sans SC", "Microsoft YaHei UI", "Noto Sans CJK SC", "Segoe UI") if name in families), font.nametofont("TkDefaultFont", root).actual("family"))
    scale = display_scale(root)
    def spacing(*values):
        return tuple(round(value * scale) for value in values)
    font.nametofont("TkDefaultFont", root=root).configure(family=face, size=11)
    font.nametofont("TkTextFont", root=root).configure(family=face, size=11)
    font.nametofont("TkMenuFont", root=root).configure(family=face, size=10)
    root.option_add("*TCombobox*Listbox.background", COLORS["surface"])
    root.option_add("*TCombobox*Listbox.foreground", COLORS["ink"])
    root.option_add("*TCombobox*Listbox.selectBackground", COLORS["accent"])
    root.option_add("*TCombobox*Listbox.selectForeground", COLORS["surface"])
    style.configure(".", font=(face, 11), background=COLORS["background"],
                    foreground=COLORS["ink"], bordercolor=COLORS["border"],
                    lightcolor=COLORS["border"], darkcolor=COLORS["border"])
    for name in ("TFrame", "TLabel", "TCheckbutton", "TLabelframe", "TLabelframe.Label"):
        style.configure(name, background=COLORS["background"])
    style.configure("TLabelframe", borderwidth=1, relief="solid")
    style.configure("TLabelframe.Label", font=(face, 11, "bold"))
    style.configure("Surface.TFrame", background=COLORS["surface"])
    style.configure("Panel.TFrame", background=COLORS["surface"], borderwidth=1, relief="solid")
    style.configure("Subtle.TFrame", background=COLORS["subtle"])
    style.configure("Accent.TFrame", background=COLORS["accent"])
    for name, background in (("Surface", "surface"), ("Subtle", "subtle")):
        style.configure(f"{name}.TLabel", background=COLORS[background])
        style.configure(f"Muted.{name}.TLabel", background=COLORS[background], foreground=COLORS["muted"], font=(face, 10))
        style.configure(f"Title.{name}.TLabel", background=COLORS[background], font=(face, 12, "bold"))
    style.configure("Muted.TLabel", foreground=COLORS["muted"], font=(face, 10))
    style.configure("Heading.TLabel", font=(face, 22, "bold"))
    style.configure("Value.TLabel", background=COLORS["surface"], font=(face, 18, "bold"))
    style.configure("Price.TLabel", background=COLORS["surface"], foreground=COLORS["accent"], font=(face, 20, "bold"))
    for name, foreground, background in (
        ("Ready", "accent", "accent_soft"), ("Busy", "accent", "accent_soft"),
        ("Warning", "warning", "warning_soft"), ("Error", "error", "warning_soft"),
        ("Neutral", "muted", "subtle"),
    ):
        style.configure(f"{name}.Badge.TLabel", foreground=COLORS[foreground],
                        background=COLORS[background], font=(face, 10), padding=spacing(10, 5))
    style.configure("TButton", padding=spacing(12, 8), background=COLORS["surface"],
                    foreground=COLORS["ink"], borderwidth=round(scale), relief="flat", focusthickness=round(2 * scale),
                    focuscolor=COLORS["accent"])
    style.map("TButton", background=[("disabled", COLORS["subtle"]), ("pressed", COLORS["border"]), ("active", COLORS["subtle"])],
              foreground=[("disabled", "#8998a7")], bordercolor=[("focus", COLORS["accent"])])
    style.configure("Accent.TButton", background=COLORS["accent"], foreground="white", bordercolor=COLORS["accent"],
                    font=(face, 11, "bold"), padding=spacing(12, 11), focuscolor="white")
    style.map("Accent.TButton", background=[("disabled", "#d7e5e4"), ("pressed", COLORS["accent_hover"]), ("active", COLORS["accent_hover"])],
              foreground=[("disabled", "#526f6c"), ("!disabled", "white")],
              bordercolor=[("disabled", "#d7e5e4"), ("!disabled", COLORS["accent"])])
    style.configure("Quiet.TButton", borderwidth=0, background=COLORS["surface"], foreground=COLORS["accent"], padding=spacing(4, 4), font=(face, 10))
    style.configure("Stop.TButton", foreground=COLORS["error"])
    for name in ("TEntry", "TCombobox", "TSpinbox"):
        style.configure(name, fieldbackground=COLORS["surface"], background=COLORS["subtle"],
                        foreground=COLORS["ink"], padding=spacing(9, 7), borderwidth=round(scale),
                        arrowsize=round(14 * scale),
                        arrowcolor=COLORS["muted"], selectbackground=COLORS["accent"], selectforeground="white")
        style.map(name, fieldbackground=[("disabled", COLORS["subtle"]), ("readonly", COLORS["surface"])],
                  foreground=[("disabled", "#8998a7"), ("readonly", COLORS["ink"])],
                  bordercolor=[("focus", COLORS["accent"]), ("!focus", COLORS["border"])],
                  lightcolor=[("focus", COLORS["accent"])], darkcolor=[("focus", COLORS["accent"])])
    style.configure("Treeview", background=COLORS["surface"], fieldbackground=COLORS["surface"],
                    rowheight=round(40 * scale), borderwidth=0, relief="flat", font=(face, 11))
    style.map("Treeview", background=[("selected", COLORS["accent_soft"])],
              foreground=[("selected", COLORS["accent_hover"])], bordercolor=[("focus", COLORS["accent"])])
    style.configure("Treeview.Heading", background=COLORS["subtle"], foreground=COLORS["muted"],
                    font=(face, 10), padding=spacing(10, 10), relief="flat", borderwidth=0)
    style.map("Treeview.Heading", background=[("active", COLORS["subtle"])])
    style.configure("Compact.Treeview", rowheight=round(28 * scale), font=(face, 10))
    style.configure("Compact.Treeview.Heading", padding=spacing(6, 6))
    style.configure("Vertical.TScrollbar", background=COLORS["border"], troughcolor=COLORS["surface"],
                    borderwidth=0, arrowsize=round(11 * scale), relief="flat")
    style.configure("Horizontal.TScrollbar", background=COLORS["border"], troughcolor=COLORS["surface"],
                    borderwidth=0, arrowsize=round(11 * scale), relief="flat")
    style.configure("TSeparator", background=COLORS["border"])


def wrapped_label(parent, **options):
    """Wrap to available space, including after resizing or a DPI change."""
    label = ttk.Label(parent, **options)
    label.bind("<Configure>", lambda event: label.configure(wraplength=max(40, event.width)))
    return label
