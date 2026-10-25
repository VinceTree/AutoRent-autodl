"""Exercise the real DPI-aware launch in a fresh process, before any Tk window."""

import json
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


@unittest.skipUnless(sys.platform == "win32", "Windows DPI awareness")
class NativeDisplayTests(unittest.TestCase):
    def test_native_launch_and_scaled_layouts_keep_text_and_controls_proportional(self):
        script = textwrap.dedent('''
            import ctypes
            import json
            import tkinter as tk
            from tkinter import font, ttk
            from autodl_quick.ui.main import App, run

            results = []
            def measure(root, app, label):
                root.update()
                bottom = root.winfo_rooty() + root.winfo_height()
                widgets = (app.rent_button, app.scan_button, app.wait_button, app.stop_button, app.log)
                results.append({
                    "label": label,
                    "scale": app.display_scale,
                    "sidebar": app.sidebar.winfo_width(),
                    "rowheight": int(ttk.Style(root).lookup("Treeview", "rowheight")),
                    "table": app.table.winfo_height(),
                    "font_size": font.nametofont("TkDefaultFont", root).actual("size"),
                    "visible": all(widget.winfo_ismapped() and widget.winfo_rooty() >= root.winfo_rooty()
                                   and widget.winfo_rooty() + widget.winfo_height() <= bottom for widget in widgets),
                })

            def check_launch(root):
                user32 = ctypes.WinDLL("user32")
                user32.GetWindowDpiAwarenessContext.argtypes = [ctypes.c_void_p]
                user32.GetWindowDpiAwarenessContext.restype = ctypes.c_void_p
                user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
                awareness = user32.GetAwarenessFromDpiAwarenessContext(user32.GetWindowDpiAwarenessContext(root.winfo_id()))
                root.update()
                print(json.dumps({"awareness": awareness}))
                root.destroy()

            tk.Tk.mainloop = check_launch
            run(demo=True)
            for dpi in (96, 144, 192):
                for language in ("zh", "en"):
                    root = tk.Tk()
                    root.tk.call("tk", "scaling", dpi / 72)
                    app = App(root, demo=True, language=language)
                    try:
                        measure(root, app, f"{dpi}/{language}")
                    finally:
                        app.close()
            print(json.dumps(results))
        ''')
        result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        launch, layouts = result.stdout.strip().splitlines()
        self.assertGreaterEqual(json.loads(launch)["awareness"], 1)
        for layout in json.loads(layouts):
            with self.subTest(display=layout["label"]):
                self.assertTrue(layout["visible"])
                self.assertEqual(layout["font_size"], 11)
                self.assertAlmostEqual(layout["sidebar"], 302 * layout["scale"], delta=1)
                self.assertAlmostEqual(layout["rowheight"], 40 * layout["scale"], delta=1)
                self.assertGreaterEqual(layout["table"], 120 * layout["scale"])


if __name__ == "__main__":
    unittest.main()
