"""Вотчдоги для мониторинга состояния браузера и событий."""

from core.session.monitors.watchdogs.default_action_watchdog import DefaultActionWatchdog
from core.session.monitors.watchdogs.dom_watchdog import DOMWatchdog
from core.session.monitors.watchdogs.downloads_watchdog import DownloadsWatchdog
from core.session.monitors.watchdogs.local_browser_watchdog import LocalBrowserWatchdog
from core.session.monitors.watchdogs.recording_watchdog import RecordingWatchdog
from core.session.monitors.watchdogs.system_watchdog import StorageStateWatchdog
from core.session.monitors.watchdogs.ui_watchdog import PopupsWatchdog, SecurityWatchdog

__all__ = [
	'DefaultActionWatchdog',
	'DOMWatchdog',
	'DownloadsWatchdog',
	'LocalBrowserWatchdog',
	'RecordingWatchdog',
	'StorageStateWatchdog',
	'PopupsWatchdog',
	'SecurityWatchdog',
]

