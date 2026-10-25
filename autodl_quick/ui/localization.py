"""Switch an existing window's language without replacing its state or workers."""

import re
import tkinter as tk
from tkinter import ttk

from ..i18n import Translator, english_catalog


class LiveTranslator(Translator):
    """Remember original messages so formatted text can switch in both directions."""

    def __init__(self, language="zh"):
        super().__init__(language)
        self.sources = {}
        self.originals = {target: source for source, target in english_catalog().items()}
        self.prefixes = [(source, target) for source, target in english_catalog().items()
                         if source.endswith(("：", "· ")) or target.endswith((": ", "· "))]

    def canonical(self, text):
        return self.sources.get(text, self.originals.get(text, text))

    def __call__(self, source, **values):
        source = self.canonical(source)
        values = {key: self.canonical(value) if isinstance(value, str) else value for key, value in values.items()}
        result = super().__call__(source, **values)
        self.sources[result] = source.format(**values) if values else source
        return result

    def retranslate(self, text):
        canonical = self.canonical(text)
        if canonical != text or text in self.sources:
            return self(canonical)
        for source, target in self.prefixes:
            for prefix in (source, target):
                if text.startswith(prefix):
                    return self(source) + self.retranslate(text[len(prefix):])
        return self(text)


class LanguageSwitch(ttk.Frame):
    def __init__(self, parent, language, command):
        super().__init__(parent)
        self.zh_button = ttk.Button(self, text="中文", command=lambda: command("zh"), style="Language.TButton")
        self.zh_button.pack(side="left")
        ttk.Label(self, text="|", style="Muted.TLabel").pack(side="left", padx=2)
        self.en_button = ttk.Button(self, text="en", command=lambda: command("en"), style="Language.TButton")
        self.en_button.pack(side="left")
        self.select(language)

    def select(self, language):
        self.zh_button.configure(style="Selected.Language.TButton" if language == "zh" else "Language.TButton")
        self.en_button.configure(style="Selected.Language.TButton" if language == "en" else "Language.TButton")


def localize_widgets(root, translator):
    """Relabel controls in place; leave editable input, selection and widget state alone."""
    variables = set()

    def visit(widget):
        options = widget.keys()
        if "textvariable" in options and str(widget.cget("textvariable")):
            variable = str(widget.cget("textvariable"))
            editable = isinstance(widget, (tk.Entry, ttk.Entry)) and not isinstance(widget, ttk.Combobox)
            if not editable and variable not in variables:
                variables.add(variable)
                current = str(root.getvar(variable))
                translated = translator.retranslate(current)
                if translated != current:
                    root.setvar(variable, translated)
        elif "text" in options:
            widget.configure(text=translator.retranslate(str(widget.cget("text"))))
        if isinstance(widget, ttk.Combobox):
            widget.configure(values=[translator.retranslate(str(value)) for value in widget.cget("values")])
        if isinstance(widget, ttk.Treeview):
            for column in widget.cget("columns"):
                widget.heading(column, text=translator.retranslate(widget.heading(column, "text")))
            for item in widget.get_children():
                widget.item(item, values=[translator.retranslate(str(value)) for value in widget.item(item, "values")])
        if isinstance(widget, tk.Text):
            content = widget.get("1.0", "end-1c")
            lines = []
            for line in content.splitlines(keepends=True):
                matched = re.match(r"(\d{2}:\d{2}:\d{2}  )(.*)", line, re.DOTALL)
                prefix, message = matched.groups() if matched else ("", line)
                newline = "\n" if message.endswith("\n") else ""
                lines.append(prefix + translator.retranslate(message.removesuffix("\n")) + newline)
            position, state = widget.yview(), widget.cget("state")
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.insert("1.0", "".join(lines))
            if "time" in widget.tag_names():
                for index, line in enumerate(lines, 1):
                    if re.match(r"\d{2}:\d{2}:\d{2}  ", line):
                        widget.tag_add("time", f"{index}.0", f"{index}.10")
            widget.configure(state=state)
            widget.yview_moveto(position[0])
        for child in widget.winfo_children():
            if not isinstance(child, tk.Toplevel):
                visit(child)

    visit(root)
