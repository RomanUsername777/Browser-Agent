"""Высокоуровневая обёртка над CDP (Chrome DevTools Protocol) для удобной работы с элементами и страницей."""

from .element import Element
from .mouse import Mouse
from .page import Page
from .helpers import Utils

__all__ = ['Element', 'Mouse', 'Page', 'Utils']
