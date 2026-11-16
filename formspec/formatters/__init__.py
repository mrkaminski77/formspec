"""Pluggable FormSpec formatters and registry factory."""

from __future__ import annotations

from pathlib import Path

from formspec.formatters.base import (
    FormFormatter,
    FormattedOutput,
    FormatterRegistry,
    RenderContext,
)
from formspec.formatters.html5 import Html5Formatter
from formspec.formatters.json_ast import JsonAstFormatter
from formspec.formatters.react_jsx import ReactJsxFormatter


def create_default_registry(plugins_dir: str | Path | None = None) -> FormatterRegistry:
    """Create a FormatterRegistry pre-populated with built-ins and optional plugins."""
    registry = FormatterRegistry()
    registry.register(Html5Formatter())
    registry.register(ReactJsxFormatter())
    registry.register(JsonAstFormatter())
    if plugins_dir:
        registry.load_plugins_from_dir(plugins_dir)
    return registry


__all__ = [
    "FormFormatter",
    "FormattedOutput",
    "FormatterRegistry",
    "Html5Formatter",
    "JsonAstFormatter",
    "ReactJsxFormatter",
    "RenderContext",
    "create_default_registry",
]
