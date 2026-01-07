"""Event definitions for browser communication."""

import inspect
import os
from typing import Any, Literal

from bubus import BaseEvent
from bubus.models import T_EventResultType
from cdp_use.cdp.target import TargetID
from pydantic import BaseModel, Field, field_validator

from core.session.models import BrowserStateSummary
from core.dom_processing.models import EnhancedDOMTreeNode


def _get_timeout(env_var: str, default: float) -> float | None:
	"""
	Safely parse environment variable timeout values with robust error handling.

	Args:
		env_var: Environment variable name (e.g. 'TIMEOUT_NavigateToUrlEvent')
		default: Default timeout value as float (e.g. 15.0)

	Returns:
		Parsed float value or the default if parsing fails

	Raises:
		ValueError: Only if both env_var and default are invalid (should not happen with valid defaults)
	"""
	# Attempt to read environment variable
	timeout_env_value = os.getenv(env_var)
	if timeout_env_value:
		try:
			timeout_float = float(timeout_env_value)
			if timeout_float < 0:
				print(f'Warning: {env_var}={timeout_env_value} is negative, using default {default}')
				return default
			return timeout_float
		except (ValueError, TypeError):
			print(f'Warning: {env_var}={timeout_env_value} is not a valid number, using default {default}')

	# Use default value if environment variable is not set or invalid
	return default


# ============================================================================
# События Agent/Tools -> BrowserSession (высокоуровневые действия браузера)
# ============================================================================


class ElementSelectedEvent(BaseEvent[T_EventResultType]):
	"""An element was selected."""

	node: EnhancedDOMTreeNode

	@field_validator('node', mode='before')
	@classmethod
	def serialize_node(cls, node_data: EnhancedDOMTreeNode | None) -> EnhancedDOMTreeNode | None:
		if node_data is None:
			return None
		# Override circular reference fields in EnhancedDOMTreeNode as they cannot be serialized and aren't needed by event handlers
		# These fields are only used internally by the DOM service during DOM tree building process, not intended for public API use
		return EnhancedDOMTreeNode(
			node_id=node_data.node_id,
			backend_node_id=node_data.backend_node_id,
			session_id=node_data.session_id,
			frame_id=node_data.frame_id,
			target_id=node_data.target_id,
			node_type=node_data.node_type,
			node_name=node_data.node_name,
			node_value=node_data.node_value,
			attributes=node_data.attributes,
			is_scrollable=node_data.is_scrollable,
			is_visible=node_data.is_visible,
			absolute_position=node_data.absolute_position,
			content_document=None,
			shadow_root_type=None,
			shadow_roots=[],
			parent_node=None,
			children_nodes=[],
			ax_node=None,
			snapshot_node=None,
		)




class NavigateToUrlEvent(BaseEvent[None]):
	"""Navigate to a specific URL."""

	url: str
	wait_until: Literal['load', 'domcontentloaded', 'networkidle', 'commit'] = 'load'
	timeout_ms: int | None = None
	new_tab: bool = Field(
		default=False, description='Set True to leave the current tab alone and open a new tab in the foreground for the new URL'
	)
	# existing_tab: PageHandle | None = None  # Примечание: требует реализации

	# time limits enforced by bubus, not exposed to LLM:
	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_NavigateToUrlEvent', 15.0))  # seconds


class ClickElementEvent(ElementSelectedEvent[dict[str, Any] | None]):
	"""Click an element."""

	node: 'EnhancedDOMTreeNode'
	button: Literal['left', 'right', 'middle'] = 'left'
	# click_count: int = 1  # Примечание: требует реализации
	# expect_download: bool = False  # moved to downloads_watchdog.py

	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_ClickElementEvent', 15.0))  # seconds


class ClickCoordinateEvent(BaseEvent[dict]):
	"""Click at specific coordinates."""

	coordinate_x: int
	coordinate_y: int
	button: Literal['left', 'right', 'middle'] = 'left'
	force: bool = False  # If True, skip safety checks (file input, print, select)

	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_ClickCoordinateEvent', 15.0))  # seconds


class TypeTextEvent(ElementSelectedEvent[dict | None]):
	"""Type text into an element."""

	node: 'EnhancedDOMTreeNode'
	text: str
	clear: bool = True
	is_sensitive: bool = False  # Flag to indicate if text contains sensitive data
	sensitive_key_name: str | None = None  # Name of the sensitive key being typed (e.g., 'username', 'password')

	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_TypeTextEvent', 60.0))  # seconds


class ScrollEvent(ElementSelectedEvent[None]):
	"""Scroll the page or element."""

	direction: Literal['up', 'down', 'left', 'right']
	amount: int  # pixels
	node: 'EnhancedDOMTreeNode | None' = None  # None means scroll page

	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_ScrollEvent', 8.0))  # seconds


class SwitchTabEvent(BaseEvent[TargetID]):
	"""Switch to a different tab."""

	target_id: TargetID | None = Field(default=None, description='None means switch to the most recently opened tab')

	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_SwitchTabEvent', 10.0))  # seconds


class CloseTabEvent(BaseEvent[None]):
	"""Close a tab."""

	target_id: TargetID

	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_CloseTabEvent', 10.0))  # seconds


class ScreenshotEvent(BaseEvent[str]):
	"""Request to take a screenshot."""

	full_page: bool = False
	clip: dict[str, float] | None = None  # {x, y, width, height}

	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_ScreenshotEvent', 15.0))  # seconds


class BrowserStateRequestEvent(BaseEvent[BrowserStateSummary]):
	"""Request current browser state."""

	include_dom: bool = True
	include_screenshot: bool = True
	include_recent_events: bool = False

	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_BrowserStateRequestEvent', 30.0))  # seconds




class GoBackEvent(BaseEvent[None]):
	"""Navigate back in browser history."""

	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_GoBackEvent', 15.0))  # seconds


class GoForwardEvent(BaseEvent[None]):
	"""Navigate forward in browser history."""

	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_GoForwardEvent', 15.0))  # seconds


class RefreshEvent(BaseEvent[None]):
	"""Refresh/reload the current page."""

	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_RefreshEvent', 15.0))  # seconds


class WaitEvent(BaseEvent[None]):
	"""Wait for a specified number of seconds."""

	seconds: float = 3.0
	max_seconds: float = 10.0  # Safety cap

	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_WaitEvent', 60.0))  # seconds


class SendKeysEvent(BaseEvent[None]):
	"""Send keyboard keys/shortcuts."""

	keys: str  # e.g., "ctrl+a", "cmd+c", "Enter"

	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_SendKeysEvent', 60.0))  # seconds


class UploadFileEvent(ElementSelectedEvent[None]):
	"""Upload a file to an element."""

	node: 'EnhancedDOMTreeNode'
	file_path: str

	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_UploadFileEvent', 30.0))  # seconds


class GetDropdownOptionsEvent(ElementSelectedEvent[dict[str, str]]):
	"""Get all options from any dropdown (native <select>, ARIA menus, or custom dropdowns).

	Returns a dict containing dropdown type, options list, and element metadata."""

	node: 'EnhancedDOMTreeNode'

	event_timeout: float | None = Field(
		default_factory=lambda: _get_timeout('TIMEOUT_GetDropdownOptionsEvent', 15.0)
	)  # some dropdowns lazy-load the list of options on first interaction, so we need to wait for them to load (e.g. table filter lists can have thousands of options)


class SelectDropdownOptionEvent(ElementSelectedEvent[dict[str, str]]):
	"""Select a dropdown option by exact text from any dropdown type.

	Returns a dict containing success status and selection details."""

	node: 'EnhancedDOMTreeNode'
	text: str  # The option text to select

	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_SelectDropdownOptionEvent', 8.0))  # seconds


class ScrollToTextEvent(BaseEvent[None]):
	"""Scroll to specific text on the page. Raises exception if text not found."""

	text: str
	direction: Literal['up', 'down'] = 'down'

	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_ScrollToTextEvent', 15.0))  # seconds


# ============================================================================


class BrowserStartEvent(BaseEvent):
	"""Start/connect to browser."""

	cdp_url: str | None = None
	launch_options: dict[str, Any] = Field(default_factory=dict)

	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_BrowserStartEvent', 30.0))  # seconds


class BrowserStopEvent(BaseEvent):
	"""Stop/disconnect from browser."""

	force: bool = False

	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_BrowserStopEvent', 45.0))  # seconds


class BrowserLaunchResult(BaseModel):
	"""Result of launching a browser."""

	cdp_url: str


class BrowserLaunchEvent(BaseEvent[BrowserLaunchResult]):
	"""Launch a local browser process."""


	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_BrowserLaunchEvent', 30.0))  # seconds


class BrowserKillEvent(BaseEvent):
	"""Kill local browser subprocess."""

	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_BrowserKillEvent', 30.0))  # seconds


# ============================================================================
# События, связанные с DOM
# ============================================================================


class BrowserConnectedEvent(BaseEvent):
	"""Browser has started/connected."""

	cdp_url: str

	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_BrowserConnectedEvent', 30.0))  # seconds


class BrowserStoppedEvent(BaseEvent):
	"""Browser has stopped/disconnected."""

	reason: str | None = None

	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_BrowserStoppedEvent', 30.0))  # seconds


class TabCreatedEvent(BaseEvent):
	"""A new tab was created."""

	target_id: TargetID
	url: str

	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_TabCreatedEvent', 30.0))  # seconds


class TabClosedEvent(BaseEvent):
	"""A tab was closed."""

	target_id: TargetID

	# Примечание:
	# new_focus_target_id: int | None = None
	# new_focus_url: str | None = None

	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_TabClosedEvent', 10.0))  # seconds




class AgentFocusChangedEvent(BaseEvent):
	"""Agent focus changed to a different tab."""

	target_id: TargetID
	url: str

	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_AgentFocusChangedEvent', 10.0))  # seconds


class TargetCrashedEvent(BaseEvent):
	"""A target has crashed."""

	target_id: TargetID
	error: str

	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_TargetCrashedEvent', 10.0))  # seconds


class NavigationStartedEvent(BaseEvent):
	"""Navigation started."""

	target_id: TargetID
	url: str

	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_NavigationStartedEvent', 30.0))  # seconds


class NavigationCompleteEvent(BaseEvent):
	"""Navigation completed."""

	target_id: TargetID
	url: str
	status: int | None = None
	error_message: str | None = None  # Error/timeout message if navigation had issues
	loading_status: str | None = None  # Detailed loading status (e.g., network timeout info)

	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_NavigationCompleteEvent', 30.0))  # seconds


# ============================================================================
# События ошибок
# ============================================================================


class BrowserErrorEvent(BaseEvent):
	"""An error occurred in the browser layer."""

	error_type: str
	message: str
	details: dict[str, Any] = Field(default_factory=dict)

	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_BrowserErrorEvent', 30.0))  # seconds


# ============================================================================
# События состояния хранилища
# ============================================================================


class SaveStorageStateEvent(BaseEvent):
	"""Request to save browser storage state."""

	path: str | None = None  # Optional path, uses profile default if not provided

	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_SaveStorageStateEvent', 45.0))  # seconds


class StorageStateSavedEvent(BaseEvent):
	"""Notification that storage state was saved."""

	path: str
	cookies_count: int
	origins_count: int

	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_StorageStateSavedEvent', 30.0))  # seconds


class LoadStorageStateEvent(BaseEvent):
	"""Request to load browser storage state."""

	path: str | None = None  # Optional path, uses profile default if not provided

	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_LoadStorageStateEvent', 45.0))  # seconds


class StorageStateLoadedEvent(BaseEvent):
	"""Notification that storage state was loaded."""

	path: str
	cookies_count: int
	origins_count: int

	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_StorageStateLoadedEvent', 30.0))  # seconds


# ============================================================================
# События загрузки файлов
# ============================================================================


class FileDownloadedEvent(BaseEvent):
	"""A file has been downloaded."""

	url: str
	path: str
	file_name: str
	file_size: int
	file_type: str | None = None  # e.g., 'pdf', 'zip', 'docx', etc.
	mime_type: str | None = None  # e.g., 'application/pdf'
	from_cache: bool = False
	auto_download: bool = False  # Whether this was an automatic download (e.g., PDF auto-download)

	event_timeout: float | None = Field(default_factory=lambda: _get_timeout('TIMEOUT_FileDownloadedEvent', 30.0))  # seconds


class AboutBlankDVDScreensaverShownEvent(BaseEvent):
	"""AboutBlankWatchdog has shown DVD screensaver animation on an about:blank tab."""

	target_id: TargetID
	error: str | None = None


class DialogOpenedEvent(BaseEvent):
	"""Event dispatched when a JavaScript dialog is opened and handled."""

	dialog_type: str  # 'alert', 'confirm', 'prompt', or 'beforeunload'
	message: str
	url: str
	frame_id: str | None = None  # Can be None when frameId is not provided by CDP


# Примечание: перестройка моделей для forward references обрабатывается в импортирующих модулях
# События с forward references на 'EnhancedDOMTreeNode' (ClickElementEvent, TypeTextEvent,
# ScrollEvent, UploadFileEvent) требуют вызова model_rebuild() после завершения импортов


def _check_event_names_dont_overlap():
	"""
	check that event names defined in this file are valid and non-overlapping
	(naiively n^2 so it's pretty slow but ok for now, optimize when >20 events)
	"""
	# Collect all event class names from globals
	all_event_names = {
		class_name.split('[')[0]
		for class_name in globals().keys()
		if not class_name.startswith('_')
		and inspect.isclass(globals()[class_name])
		and issubclass(globals()[class_name], BaseEvent)
		and class_name != 'BaseEvent'
	}
	# Validate each event name ends with 'Event' and check for substring overlaps
	for first_event_name in all_event_names:
		assert first_event_name.endswith('Event'), f'Event with name {first_event_name} does not end with "Event"'
		for second_event_name in all_event_names:
			if first_event_name != second_event_name:  # Skip self-comparison
				assert first_event_name not in second_event_name, (
					f'Event with name {first_event_name} is a substring of {second_event_name}, all events must be completely unique to avoid find-and-replace accidents'
				)


# Важно: имена событий не должны перекрываться (например, ClickEvent и FailedClickEvent),
# так как это усложняет поиск и рефакторинг. Используйте ClickEvent и ClickFailedEvent.
# При импорте выполняется проверка, что все имена событий валидны и не перекрываются.
_check_event_names_dont_overlap()
