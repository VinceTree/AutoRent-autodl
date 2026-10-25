"""Small per-window translator; platform identifiers are never translated."""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import re
from string import Formatter


@lru_cache(maxsize=1)
def english_catalog() -> dict[str, str]:
    path = Path(__file__).resolve().parent / "locales" / "en.json"
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def message_patterns():
    patterns = []
    for source, target in english_catalog().items():
        if "{p" not in source:
            continue
        parts, seen = [], set()
        for literal, field, _, _ in Formatter().parse(source):
            parts.append(re.escape(literal))
            if field is not None:
                parts.append(f"(?P={field})" if field in seen else f"(?P<{field}>.+?)")
                seen.add(field)
        patterns.append((re.compile("".join(parts), re.DOTALL), target, len(source)))
    return tuple(sorted(patterns, key=lambda item: item[2], reverse=True))


class Translator:
    def __init__(self, language: str = "zh"):
        if language not in ("zh", "en"):
            raise ValueError("Language must be zh or en")
        self.language = language

    def __call__(self, source: str, **values) -> str:
        if self.language == "zh":
            return source.format(**values) if values else source
        target = english_catalog().get(source)
        if target is not None:
            translated = {key: self._text(str(value), 0) if isinstance(value, (str, BaseException)) else value for key, value in values.items()}
            return target.format(**translated) if values else target
        if values:
            return source.format(**values)
        return self._text(source, 0)

    def _text(self, source: str, depth: int) -> str:
        if depth >= 4:
            return source
        exact = english_catalog().get(source)
        if exact is not None:
            return exact
        for pattern, target, _ in message_patterns():
            matched = pattern.fullmatch(source)
            if matched:
                values = {key: self._text(value, depth + 1) for key, value in matched.groupdict().items()}
                return "".join(literal + (values[field] if field is not None else "")
                               for literal, field, _, _ in Formatter().parse(target))
        return source
