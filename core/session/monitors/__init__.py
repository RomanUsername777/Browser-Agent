"""Пакет для мониторинга и обработки событий браузера."""

from core.session.monitors.browser_controller import BrowserController
from core.session.monitors.handlers import (
	ClickHandler,
	DropdownHandler,
	FileUploadHandler,
	NavigationHandler,
	ScrollHandler,
	SendKeysHandler,
	TextInputHandler,
)
from core.session.monitors.watchdogs import (
	DefaultActionWatchdog,
	DOMWatchdog,
	DownloadsWatchdog,
	LocalBrowserWatchdog,
	RecordingWatchdog,
	StorageStateWatchdog,
	PopupsWatchdog,
	SecurityWatchdog,
)
from core.session.watchdog_base import WatchdogBase

__all__ = [
	'BrowserController',
	'ClickHandler',
	'DefaultActionWatchdog',
	'DOMWatchdog',
	'DownloadsWatchdog',
	'DropdownHandler',
	'FileUploadHandler',
	'LocalBrowserWatchdog',
	'NavigationHandler',
	'RecordingWatchdog',
	'ScrollHandler',
	'SendKeysHandler',
	'StorageStateWatchdog',
	'TextInputHandler',
	'PopupsWatchdog',
	'SecurityWatchdog',
	'WatchdogBase',
]

