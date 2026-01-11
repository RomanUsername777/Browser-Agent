"""Сессия браузера на основе событий с обратной совместимостью."""

import asyncio
import logging
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self, Union, cast, overload
from urllib.parse import urlparse, urlunparse
from uuid import UUID

import httpx
from bubus import EventBus
from cdp_use import CDPClient
from cdp_use.cdp.fetch import AuthRequiredEvent, RequestPausedEvent
from cdp_use.cdp.network import Cookie
from cdp_use.cdp.target import AttachedToTargetEvent, SessionID, TargetID
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr
from uuid_extensions import uuid7str

# Облачный браузер - опционален, не нужен для базовой функциональности
try:
	from core.session.cloud.cloud import CloudBrowserAuthError, CloudBrowserClient, CloudBrowserError
except ImportError:
	# Create dummy classes if cloud not available
	class CloudBrowserAuthError(Exception): pass
	class CloudBrowserClient: pass
	class CloudBrowserError(Exception): pass

# Логирование CDP теперь обрабатывается setup_logging() в logging_config.py
# Автоматически устанавливает логи CDP на тот же уровень, что и логи агента
try:
	from core.session.cloud.models import CloudBrowserParams, CreateBrowserRequest, ProxyCountryCode
except ImportError:
	# Create dummy classes if cloud not available
	class CloudBrowserParams: pass
	class CreateBrowserRequest: pass
	ProxyCountryCode = str  # type alias
from core.session.events import (
	AgentFocusChangedEvent,
	BrowserConnectedEvent,
	BrowserErrorEvent,
	BrowserLaunchEvent,
	BrowserLaunchResult,
	BrowserStartEvent,
	BrowserStateRequestEvent,
	BrowserStopEvent,
	BrowserStoppedEvent,
	CloseTabEvent,
	FileDownloadedEvent,
	UrlNavigationRequest,
	NavigationCompleteEvent,
	NavigationStartedEvent,
	SwitchTabEvent,
	TabClosedEvent,
	TabCreatedEvent,
	ScreenshotEvent,
)
from core.session.profile import BrowserProfile, ProxySettings
from core.session.models import BrowserStateSummary, TabInfo
from core.dom_processing.models import DOMRect, EnhancedDOMTreeNode, TargetInfo
from core.observability import observe_debug
from core.helpers import _log_pretty_url, create_task_with_error_handling, is_new_tab_page

if TYPE_CHECKING:
	from core.interaction.page import Page
	from core.session.demo_mode import DemoMode

DEFAULT_BROWSER_PROFILE = BrowserProfile()

_LOGGED_UNIQUE_SESSION_IDS = set()  # Track unique session IDs that have been logged to ensure we always assign a sufficiently unique ID to new sessions and avoid ambiguity in logs
red = '\033[91m'
reset = '\033[0m'


class Target(BaseModel):
	"""Цель браузера (страница, iframe, worker) - фактическая сущность, которой управляем.

	Цель представляет контекст просмотра со своим URL, заголовком и типом.
	Несколько CDP сессий могут подключиться к одной цели для общения.
	"""

	model_config = ConfigDict(arbitrary_types_allowed=True, revalidate_instances='never')

	target_id: TargetID
	target_type: str  # 'page', 'iframe', 'worker', etc.
	url: str = 'about:blank'
	title: str = 'Unknown title'


class DevToolsSession(BaseModel):
	"""CDP communication channel to a target.

	A session is a connection that allows sending CDP commands to a specific target.
	Multiple sessions can attach to the same target.
	"""

	model_config = ConfigDict(arbitrary_types_allowed=True, revalidate_instances='never')

	cdp_client: CDPClient
	target_id: TargetID
	session_id: SessionID

	# Lifecycle monitoring (populated by SessionManager)
	_lifecycle_events: Any = PrivateAttr(default=None)
	_lifecycle_lock: Any = PrivateAttr(default=None)


class ChromeSession(BaseModel):
	"""Event-driven browser session with backwards compatibility.

	This class provides a 2-layer architecture:
	- High-level event handling for agents/tools
	- Непосредственные вызовы CDP для операций с браузером

	Supports both event-driven and imperative calling styles.

	Browser configuration is stored in the browser_profile, session identity in direct fields:
	```python
	# Direct settings (recommended for most users)
	session = ChromeSession(headless=True, user_data_dir='./profile')

	# Or use a profile (for advanced use cases)
	session = ChromeSession(browser_profile=BrowserProfile(...))

	# Access session fields directly, browser settings via profile or property
	print(session.id)  # Session field
	```
	"""

	model_config = ConfigDict(
		arbitrary_types_allowed=True,
		validate_assignment=True,
		extra='forbid',
		revalidate_instances='never',  # resets private attrs on every model rebuild
	)

	# Overload 1: Cloud browser mode (use cloud-specific params)
	@overload
	def __init__(
		self,
		*,
		# Cloud browser params - use these for cloud mode
		cloud_profile_id: UUID | str | None = None,
		cloud_proxy_country_code: ProxyCountryCode | None = None,
		cloud_timeout: int | None = None,
		# Алиасы для обратной совместимости
		profile_id: UUID | str | None = None,
		proxy_country_code: ProxyCountryCode | None = None,
		timeout: int | None = None,
		use_cloud: bool | None = None,
		cloud_browser: bool | None = None,  # Алиас для обратной совместимости
		cloud_browser_params: CloudBrowserParams | None = None,
		# Common params that work with cloud
		id: str | None = None,
		headers: dict[str, str] | None = None,
		allowed_domains: list[str] | None = None,
		keep_alive: bool | None = None,
		minimum_wait_page_load_time: float | None = None,
		wait_for_network_idle_page_load_time: float | None = None,
		wait_between_actions: float | None = None,
		auto_download_pdfs: bool | None = None,
		cookie_whitelist_domains: list[str] | None = None,
		cross_origin_iframes: bool | None = None,
		highlight_elements: bool | None = None,
		dom_highlight_elements: bool | None = None,
		paint_order_filtering: bool | None = None,
		max_iframes: int | None = None,
		max_iframe_depth: int | None = None,
	) -> None: ...

	# Overload 2: Local browser mode (use local browser params)
	@overload
	def __init__(
		self,
		*,
		# Core configuration for local
		id: str | None = None,
		cdp_url: str | None = None,
		browser_profile: BrowserProfile | None = None,
		# Local browser launch params
		executable_path: str | Path | None = None,
		headless: bool | None = None,
		user_data_dir: str | Path | None = None,
		args: list[str] | None = None,
		downloads_path: str | Path | None = None,
		# Common params
		headers: dict[str, str] | None = None,
		allowed_domains: list[str] | None = None,
		keep_alive: bool | None = None,
		minimum_wait_page_load_time: float | None = None,
		wait_for_network_idle_page_load_time: float | None = None,
		wait_between_actions: float | None = None,
		auto_download_pdfs: bool | None = None,
		cookie_whitelist_domains: list[str] | None = None,
		cross_origin_iframes: bool | None = None,
		highlight_elements: bool | None = None,
		dom_highlight_elements: bool | None = None,
		paint_order_filtering: bool | None = None,
		max_iframes: int | None = None,
		max_iframe_depth: int | None = None,
		# All other local params
		env: dict[str, str | float | bool] | None = None,
		ignore_default_args: list[str] | Literal[True] | None = None,
		channel: str | None = None,
		chromium_sandbox: bool | None = None,
		devtools: bool | None = None,
		traces_dir: str | Path | None = None,
		accept_downloads: bool | None = None,
		permissions: list[str] | None = None,
		user_agent: str | None = None,
		screen: dict | None = None,
		viewport: dict | None = None,
		no_viewport: bool | None = None,
		device_scale_factor: float | None = None,
		record_har_content: str | None = None,
		record_har_mode: str | None = None,
		record_har_path: str | Path | None = None,
		record_video_dir: str | Path | None = None,
		record_video_framerate: int | None = None,
		record_video_size: dict | None = None,
		storage_state: str | Path | dict[str, Any] | None = None,
		disable_security: bool | None = None,
		deterministic_rendering: bool | None = None,
		proxy: ProxySettings | None = None,
		enable_default_extensions: bool | None = None,
		window_size: dict | None = None,
		window_position: dict | None = None,
		filter_highlight_ids: bool | None = None,
		profile_directory: str | None = None,
	) -> None: ...

	def __init__(
		self,
		# Core configuration
		id: str | None = None,
		cdp_url: str | None = None,
		is_local: bool = False,
		browser_profile: BrowserProfile | None = None,
		# Cloud browser params (don't mix with local browser params)
		cloud_profile_id: UUID | str | None = None,
		cloud_proxy_country_code: ProxyCountryCode | None = None,
		cloud_timeout: int | None = None,
		# Алиасы для обратной совместимости для cloud параметров
		profile_id: UUID | str | None = None,
		proxy_country_code: ProxyCountryCode | None = None,
		timeout: int | None = None,
		# Параметры конфигурации браузера
		# Параметры подключения к браузеру
		headers: dict[str, str] | None = None,
		# Параметры запуска браузера
		env: dict[str, str | float | bool] | None = None,
		executable_path: str | Path | None = None,
		headless: bool | None = None,
		args: list[str] | None = None,
		ignore_default_args: list[str] | Literal[True] | None = None,
		channel: str | None = None,
		chromium_sandbox: bool | None = None,
		devtools: bool | None = None,
		downloads_path: str | Path | None = None,
		traces_dir: str | Path | None = None,
		# Параметры контекста браузера
		accept_downloads: bool | None = None,
		permissions: list[str] | None = None,
		user_agent: str | None = None,
		screen: dict | None = None,
		viewport: dict | None = None,
		no_viewport: bool | None = None,
		device_scale_factor: float | None = None,
		record_har_content: str | None = None,
		record_har_mode: str | None = None,
		record_har_path: str | Path | None = None,
		record_video_dir: str | Path | None = None,
		record_video_framerate: int | None = None,
		record_video_size: dict | None = None,
		# Параметры постоянного контекста
		user_data_dir: str | Path | None = None,
		# Параметры состояния хранилища
		storage_state: str | Path | dict[str, Any] | None = None,
		# BrowserProfile specific fields
		## Cloud Browser Fields
		use_cloud: bool | None = None,
		cloud_browser: bool | None = None,  # Алиас для обратной совместимости
		cloud_browser_params: CloudBrowserParams | None = None,
		## Other params
		disable_security: bool | None = None,
		deterministic_rendering: bool | None = None,
		allowed_domains: list[str] | None = None,
		keep_alive: bool | None = None,
		proxy: ProxySettings | None = None,
		enable_default_extensions: bool | None = None,
		window_size: dict | None = None,
		window_position: dict | None = None,
		minimum_wait_page_load_time: float | None = None,
		wait_for_network_idle_page_load_time: float | None = None,
		wait_between_actions: float | None = None,
		filter_highlight_ids: bool | None = None,
		auto_download_pdfs: bool | None = None,
		profile_directory: str | None = None,
		cookie_whitelist_domains: list[str] | None = None,
		# DOM extraction layer configuration
		cross_origin_iframes: bool | None = None,
		highlight_elements: bool | None = None,
		dom_highlight_elements: bool | None = None,
		paint_order_filtering: bool | None = None,
		# Iframe processing limits
		max_iframes: int | None = None,
		max_iframe_depth: int | None = None,
	):
		# Following the same pattern as AgentSettings in service.py
		# Only pass non-None values to avoid validation errors
		profile_kwargs = {
			k: v
			for k, v in locals().items()
			if k
			not in [
				'self',
				'browser_profile',
				'id',
				'cloud_profile_id',
				'cloud_proxy_country_code',
				'cloud_timeout',
				'profile_id',
				'proxy_country_code',
				'timeout',
			]
			and v is not None
		}

		# Обработка обратной совместимости: предпочтение параметрам cloud_* перед старыми именами
		final_profile_id = cloud_profile_id if cloud_profile_id is not None else profile_id
		final_proxy_country_code = cloud_proxy_country_code if cloud_proxy_country_code is not None else proxy_country_code
		final_timeout = cloud_timeout if cloud_timeout is not None else timeout

		# If any cloud params are provided, create cloud_browser_params
		if final_profile_id is not None or final_proxy_country_code is not None or final_timeout is not None:
			cloud_params = CreateBrowserRequest(
				cloud_profile_id=final_profile_id,
				cloud_proxy_country_code=final_proxy_country_code,
				cloud_timeout=final_timeout,
			)
			profile_kwargs['cloud_browser_params'] = cloud_params
			profile_kwargs['use_cloud'] = True

		# Обработка обратной совместимости: маппинг cloud_browser на use_cloud
		if 'cloud_browser' in profile_kwargs:
			profile_kwargs['use_cloud'] = profile_kwargs.pop('cloud_browser')

		# If cloud_browser_params is set, force use_cloud=True
		if cloud_browser_params is not None:
			profile_kwargs['use_cloud'] = True

		# if is_local is False but executable_path is provided, set is_local to True
		if is_local is False and executable_path is not None:
			profile_kwargs['is_local'] = True
		# Only set is_local=True when cdp_url is missing if we're not using cloud browser
		# (cloud browser will provide cdp_url later)
		use_cloud = profile_kwargs.get('use_cloud') or profile_kwargs.get('cloud_browser')
		if not cdp_url and not use_cloud:
			profile_kwargs['is_local'] = True

		# Create browser profile from direct parameters or use provided one
		if browser_profile is not None:
			# Merge any direct kwargs into the provided browser_profile (direct kwargs take precedence)
			merged_kwargs = {**browser_profile.model_dump(exclude_unset=True), **profile_kwargs}
			resolved_browser_profile = BrowserProfile(**merged_kwargs)
		else:
			resolved_browser_profile = BrowserProfile(**profile_kwargs)

		# Initialize the Pydantic model
		super().__init__(
			id=id or str(uuid7str()),
			browser_profile=resolved_browser_profile,
		)

	# Session configuration (session identity only)
	id: str = Field(default_factory=lambda: str(uuid7str()), description='Unique identifier for this browser session')

	# Browser configuration (reusable profile)
	browser_profile: BrowserProfile = Field(
		default_factory=lambda: DEFAULT_BROWSER_PROFILE,
		description='BrowserProfile() options to use for the session, otherwise a default profile will be used',
	)

	# LLM screenshot resizing configuration
	llm_screenshot_size: tuple[int, int] | None = Field(
		default=None,
		description='Target size (width, height) to resize screenshots before sending to LLM. Coordinates from LLM will be scaled back to original viewport size.',
	)

	# Кэш исходного размера viewport для конвертации координат (устанавливается при захвате состояния браузера)
	_original_viewport_size: tuple[int, int] | None = PrivateAttr(default=None)

	# Convenience properties for common browser settings
	@property
	def cdp_url(self) -> str | None:
		"""CDP URL from browser profile."""
		return self.browser_profile.cdp_url

	@property
	def is_local(self) -> bool:
		"""Whether this is a local browser instance from browser profile."""
		return self.browser_profile.is_local

	@property
	def cloud_browser(self) -> bool:
		"""Whether to use cloud browser service from browser profile."""
		return self.browser_profile.use_cloud

	@property
	def demo_mode(self) -> 'DemoMode | None':
		"""Lazy init demo mode helper when enabled."""
		if not self.browser_profile.demo_mode:
			return None
		if self._demo_mode is None:
			try:
				from core.session.demo_mode import DemoMode
				self._demo_mode = DemoMode(self)
			except ImportError:
				self._demo_mode = None
				return None
		return self._demo_mode

	# Main shared event bus for all browser session + all watchdogs
	event_bus: EventBus = Field(default_factory=EventBus)

	# Mutable public state - which target has agent focus
	agent_focus_target_id: TargetID | None = None

	# Mutable private state shared between watchdogs
	_cdp_client_root: CDPClient | None = PrivateAttr(default=None)
	_connection_lock: Any = PrivateAttr(default=None)  # asyncio.Lock for preventing concurrent connections

	# PUBLIC: SessionManager instance (OWNS all targets and sessions)
	session_manager: Any = Field(default=None, exclude=True)  # SessionManager

	_cached_browser_state_summary: Any = PrivateAttr(default=None)
	_cached_selector_map: dict[int, EnhancedDOMTreeNode] = PrivateAttr(default_factory=dict)
	_downloaded_files: list[str] = PrivateAttr(default_factory=list)  # Track files downloaded during this session
	_closed_popup_messages: list[str] = PrivateAttr(default_factory=list)  # Store messages from auto-closed JavaScript dialogs

	# Managers для разделения функциональности
	_browser_operations: Any = PrivateAttr(default=None)
	_visual_operations: Any = PrivateAttr(default=None)
	
	# Алиасы для совместимости с предыдущими версиями
	_screenshot_manager: Any = PrivateAttr(default=None)
	_storage_manager: Any = PrivateAttr(default=None)
	_tab_manager: Any = PrivateAttr(default=None)
	_dom_helpers: Any = PrivateAttr(default=None)

	# Watchdogs
	_crash_watchdog: Any | None = PrivateAttr(default=None)
	_downloads_watchdog: Any | None = PrivateAttr(default=None)
	_security_watchdog: Any | None = PrivateAttr(default=None)
	_storage_state_watchdog: Any | None = PrivateAttr(default=None)
	_local_browser_watchdog: Any | None = PrivateAttr(default=None)
	_default_action_watchdog: Any | None = PrivateAttr(default=None)
	_dom_watchdog: Any | None = PrivateAttr(default=None)
	_recording_watchdog: Any | None = PrivateAttr(default=None)
	_popups_watchdog: Any | None = PrivateAttr(default=None)

	_cloud_browser_client: CloudBrowserClient = PrivateAttr(default_factory=lambda: CloudBrowserClient())
	_demo_mode: 'DemoMode | None' = PrivateAttr(default=None)

	_logger: Any = PrivateAttr(default=None)

	@property
	def logger(self) -> Any:
		"""Get instance-specific logger with session ID in the name"""
		# **regenerate it every time** because our id and str(self) can change as browser connection state changes
		# if self._logger is None or not self._cdp_client_root:
		# 	self._logger = logging.getLogger(f'core.{self}')
		return logging.getLogger(f'core.{self}')

	@cached_property
	def _id_for_logs(self) -> str:
		"""Get human-friendly semi-unique identifier for differentiating different ChromeSession instances in logs"""
		# Default to last 4 chars of truly random uuid, less helpful than cdp port but always unique enough
		log_identifier = self.id[-4:]
		cdp_url_str = self.cdp_url or 'no-cdp'
		port_str = cdp_url_str.rsplit(':', 1)[-1].split('/', 1)[0].strip()
		is_random_port = not port_str.startswith('922')
		is_unique_port = port_str not in _LOGGED_UNIQUE_SESSION_IDS
		if port_str and port_str.isdigit() and is_random_port and is_unique_port:
			# If CDP port is random/unique enough to identify this session, use it as our id in logs
			_LOGGED_UNIQUE_SESSION_IDS.add(port_str)
			log_identifier = port_str
		return log_identifier

	@property
	def _tab_id_for_logs(self) -> str:
		return self.agent_focus_target_id[-2:] if self.agent_focus_target_id else f'{red}--{reset}'

	def __repr__(self) -> str:
		return f'ChromeSession {self._id_for_logs} {self._tab_id_for_logs} (cdp_url={self.cdp_url}, profile={self.browser_profile})'

	def __str__(self) -> str:
		return f'ChromeSession {self._id_for_logs} {self._tab_id_for_logs}'

	async def reset(self) -> None:
		"""Clear all cached CDP sessions with proper cleanup. Delegates to SessionLifecycleManager."""
		await self._lifecycle_manager.reset()

	def model_post_init(self, __context) -> None:
		"""Register event handlers after model initialization."""
		self._connection_lock = asyncio.Lock()

		# Initialize all managers
		from core.session.browser_operations import BrowserOperationsManager
		from core.session.visual_operations import VisualOperationsManager
		from core.session.lifecycle_manager import SessionLifecycleManager
		from core.session.cdp_operations import CDPOperationsManager
		from core.session.navigation_manager import NavigationManager
		
		self._browser_operations = BrowserOperationsManager(self)
		self._visual_operations = VisualOperationsManager(self)
		self._lifecycle_manager = SessionLifecycleManager(self)
		self._cdp_operations = CDPOperationsManager(self)
		self._navigation_manager = NavigationManager(self)
		
		# Алиасы для совместимости с предыдущими версиями
		self._tab_manager = self._browser_operations
		self._storage_manager = self._browser_operations
		self._screenshot_manager = self._visual_operations
		self._dom_helpers = self._visual_operations

		# Check if handlers are already registered to prevent duplicates
		from core.session.watchdog_base import BaseWatchdog

		start_handlers = self.event_bus.handlers.get('BrowserStartEvent', [])
		start_handler_names = [getattr(h, '__name__', str(h)) for h in start_handlers]

		if any('on_BrowserStartEvent' in name for name in start_handler_names):
			raise RuntimeError(
				'[ChromeSession] Duplicate handler registration attempted! '
				'on_BrowserStartEvent is already registered. '
				'This likely means ChromeSession was initialized multiple times with the same EventBus.'
			)

		# Register handlers via lifecycle and navigation managers
		BaseWatchdog.attach_handler_to_session(self, BrowserStartEvent, self._lifecycle_manager.on_BrowserStartEvent)
		BaseWatchdog.attach_handler_to_session(self, BrowserStopEvent, self._lifecycle_manager.on_BrowserStopEvent)
		BaseWatchdog.attach_handler_to_session(self, UrlNavigationRequest, self._navigation_manager.on_UrlNavigationRequest)
		BaseWatchdog.attach_handler_to_session(self, SwitchTabEvent, self._navigation_manager.on_SwitchTabEvent)
		BaseWatchdog.attach_handler_to_session(self, TabCreatedEvent, self._navigation_manager.on_TabCreatedEvent)
		BaseWatchdog.attach_handler_to_session(self, TabClosedEvent, self._navigation_manager.on_TabClosedEvent)
		BaseWatchdog.attach_handler_to_session(self, AgentFocusChangedEvent, self._navigation_manager.on_AgentFocusChangedEvent)
		BaseWatchdog.attach_handler_to_session(self, FileDownloadedEvent, self._navigation_manager.on_FileDownloadedEvent)
		BaseWatchdog.attach_handler_to_session(self, CloseTabEvent, self._navigation_manager.on_CloseTabEvent)

	async def start(self) -> None:
		"""Start the browser session. Delegates to SessionLifecycleManager."""
		await self._lifecycle_manager.start()

	async def kill(self) -> None:
		"""Kill the browser session and reset all state. Delegates to SessionLifecycleManager."""
		await self._lifecycle_manager.kill()

	async def stop(self) -> None:
		"""Stop the browser session without killing the browser process. Delegates to SessionLifecycleManager."""
		await self._lifecycle_manager.stop()

	async def on_BrowserStartEvent(self, event: BrowserStartEvent) -> dict[str, str]:
		"""Handle browser start request. Delegates to SessionLifecycleManager."""
		return await self._lifecycle_manager.on_BrowserStartEvent(event)

	async def on_UrlNavigationRequest(self, event: UrlNavigationRequest) -> None:
		"""Handle navigation requests. Delegates to NavigationManager."""
		await self._navigation_manager.on_UrlNavigationRequest(event)

	async def _navigate_and_wait(self, url: str, target_id: str, timeout: float | None = None) -> None:
		"""Navigate to URL and wait for page readiness. Delegates to NavigationManager."""
		await self._navigation_manager._navigate_and_wait(url, target_id, timeout)

	async def on_SwitchTabEvent(self, event: SwitchTabEvent) -> TargetID:
		"""Handle tab switching. Delegates to NavigationManager."""
		return await self._navigation_manager.on_SwitchTabEvent(event)

	async def on_CloseTabEvent(self, event: CloseTabEvent) -> None:
		"""Handle tab closure. Delegates to NavigationManager."""
		await self._navigation_manager.on_CloseTabEvent(event)

	async def on_TabCreatedEvent(self, event: TabCreatedEvent) -> None:
		"""Handle tab creation. Delegates to NavigationManager."""
		await self._navigation_manager.on_TabCreatedEvent(event)

	async def on_TabClosedEvent(self, event: TabClosedEvent) -> None:
		"""Handle tab closure. Delegates to NavigationManager."""
		await self._navigation_manager.on_TabClosedEvent(event)

	async def on_AgentFocusChangedEvent(self, event: AgentFocusChangedEvent) -> None:
		"""Handle agent focus change. Delegates to NavigationManager."""
		await self._navigation_manager.on_AgentFocusChangedEvent(event)

	async def on_FileDownloadedEvent(self, event: FileDownloadedEvent) -> None:
		"""Track downloaded files. Delegates to NavigationManager."""
		await self._navigation_manager.on_FileDownloadedEvent(event)

	async def on_BrowserStopEvent(self, event: BrowserStopEvent) -> None:
		"""Handle browser stop request. Delegates to SessionLifecycleManager."""
		await self._lifecycle_manager.on_BrowserStopEvent(event)

	# region - ========== CDP-based replacements for browser_context operations ==========
	@property
	def cdp_client(self) -> CDPClient:
		"""Get the cached root CDP cdp_session.cdp_client. The client is created and started in self.connect()."""
		assert self._cdp_client_root is not None, 'CDP client not initialized - browser may not be connected yet'
		return self._cdp_client_root

	async def new_page(self, url: str | None = None) -> 'Page':
		"""Create a new page (tab). Delegates to BrowserOperationsManager."""
		return await self._browser_operations.new_page(url=url)

	async def get_current_page(self) -> 'Page | None':
		"""Get the current page as an actor Page. Delegates to BrowserOperationsManager."""
		return await self._browser_operations.get_current_page()

	async def must_get_current_page(self) -> 'Page':
		"""Get the current page as an actor Page. Delegates to BrowserOperationsManager."""
		return await self._browser_operations.must_get_current_page()

	async def get_pages(self) -> list['Page']:
		"""Get all available pages using SessionManager. Delegates to BrowserOperationsManager."""
		return await self._browser_operations.get_pages()

	def get_focused_target(self) -> 'Target | None':
		"""Get the target that currently has agent focus. Delegates to BrowserOperationsManager."""
		return self._browser_operations.get_focused_target()

	def get_page_targets(self) -> list['Target']:
		"""Get all page/tab targets. Delegates to BrowserOperationsManager."""
		return self._browser_operations.get_page_targets()

	async def close_page(self, page: 'Union[Page, str]') -> None:
		"""Close a page by Page object or target ID. Delegates to BrowserOperationsManager."""
		await self._browser_operations.close_page(page)

	async def cookies(self) -> list['Cookie']:
		"""Get cookies, optionally filtered by URLs. Delegates to BrowserOperationsManager."""
		return await self._browser_operations.cookies()

	async def clear_cookies(self) -> None:
		"""Clear all cookies. Delegates to BrowserOperationsManager."""
		await self._browser_operations.clear_cookies()

	async def export_storage_state(self, output_path: str | Path | None = None) -> dict[str, Any]:
		"""Export all browser cookies and storage to storage_state format. Delegates to BrowserOperationsManager."""
		return await self._browser_operations.export_storage_state(output_path=output_path)

	async def get_or_create_cdp_session(self, target_id: TargetID | None = None, focus: bool = True) -> DevToolsSession:
		"""Get CDP session for a target from the event-driven pool.

		With autoAttach=True, sessions are created automatically by Chrome and added
		to the pool via Target.attachedToTarget events. This method retrieves them.

		Args:
			target_id: Target ID to get session for. If None, uses current agent focus.
			focus: If True, switches agent focus to this target (page targets only).

		Returns:
			DevToolsSession for the specified target.

		Raises:
			ValueError: If target doesn't exist or session is not available.
		"""
		assert self._cdp_client_root is not None, 'Root CDP client not initialized'
		assert self.session_manager is not None, 'SessionManager not initialized'

		# If no target_id specified, ensure current agent focus is valid and wait for recovery if needed
		if target_id is None:
			# Validate and wait for focus recovery if stale (centralized protection)
			focus_valid = await self.session_manager.ensure_valid_focus(timeout=5.0)
			if not focus_valid:
				raise ValueError(
					'No valid agent focus available - target may have detached and recovery failed. '
					'This indicates browser is in an unstable state.'
				)

			assert self.agent_focus_target_id is not None, 'Focus validation passed but agent_focus_target_id is None'
			target_id = self.agent_focus_target_id

		session = self.session_manager._get_session_for_target(target_id)

		if not session:
			# Session not in pool yet - wait for attach event
			self.logger.debug(f'[SessionManager] Waiting for target {target_id[:8]}... to attach...')

			# Wait up to 2 seconds for the attach event
			for attempt in range(20):
				await asyncio.sleep(0.1)
				session = self.session_manager._get_session_for_target(target_id)
				if session:
					self.logger.debug(f'[SessionManager] Target appeared after {attempt * 100}ms')
					break

			if not session:
				# Timeout - target doesn't exist
				raise ValueError(f'Target {target_id} not found - may have detached or never existed')

		# Validate session is still active
		is_valid = await self.session_manager.validate_session(target_id)
		if not is_valid:
			raise ValueError(f'Target {target_id} has detached - no active sessions')

		# Update focus if requested
		# CRITICAL: Only allow focus change to 'page' type targets, not iframes/workers
		if focus and self.agent_focus_target_id != target_id:
			# Get target type from SessionManager
			target = self.session_manager.get_target(target_id)
			target_type = target.target_type if target else 'unknown'

			if target_type == 'page':
				# Format current focus safely (could be None after detach)
				current_focus = self.agent_focus_target_id[:8] if self.agent_focus_target_id else 'None'
				self.logger.debug(f'[SessionManager] Switching focus: {current_focus}... → {target_id[:8]}...')
				self.agent_focus_target_id = target_id
			else:
				# Ignore focus request for non-page targets (iframes, workers, etc.)
				# These can detach at any time, causing agent_focus to point to dead target
				current_focus = self.agent_focus_target_id[:8] if self.agent_focus_target_id else 'None'
				self.logger.debug(
					f'[SessionManager] Ignoring focus request for {target_type} target {target_id[:8]}... '
					f'(agent_focus stays on {current_focus}...)'
				)

		# Resume if waiting for debugger
		if focus:
			try:
				await session.cdp_client.send.Runtime.runIfWaitingForDebugger(session_id=session.session_id)
			except Exception:
				pass  # May fail if not waiting

		return session

	# endregion - ========== CDP-based ... ==========

	# region - ========== Helper Methods ==========
	@observe_debug(ignore_input=True, ignore_output=True, name='get_browser_state_summary')
	async def get_browser_state_summary(
		self,
		include_screenshot: bool = True,
		cached: bool = False,
		include_recent_events: bool = False,
	) -> BrowserStateSummary:
		if cached and self._cached_browser_state_summary is not None:
			# Проверяем, что cached state - это объект, а не словарь
			cached_state = self._cached_browser_state_summary
			if isinstance(cached_state, dict):
				# Если это словарь, не используем кэш
				self.logger.debug('⚠️ Cached browser state is a dict, fetching fresh state')
			else:
				# Проверяем наличие dom_state
				dom_state = cached_state.dom_state if hasattr(cached_state, 'dom_state') else None
				if dom_state:
					# Don't use cached state if it has 0 interactive elements
					selector_map = dom_state.selector_map if hasattr(dom_state, 'selector_map') else {}

					# Don't use cached state if we need a screenshot but the cached state doesn't have one
					screenshot = cached_state.screenshot if hasattr(cached_state, 'screenshot') else None
					if include_screenshot and not screenshot:
						self.logger.debug('⚠️ Cached browser state has no screenshot, fetching fresh state with screenshot')
						# Fall through to fetch fresh state with screenshot
					elif selector_map and len(selector_map) > 0:
						self.logger.debug('🔄 Using pre-cached browser state summary for open tab')
						return cached_state
					else:
						self.logger.debug('⚠️ Cached browser state has 0 interactive elements, fetching fresh state')
						# Fall through to fetch fresh state

		# Dispatch the event and wait for result
		event: BrowserStateRequestEvent = cast(
			BrowserStateRequestEvent,
			self.event_bus.dispatch(
				BrowserStateRequestEvent(
					include_dom=True,
					include_screenshot=include_screenshot,
					include_recent_events=include_recent_events,
				)
			),
		)

		# The handler returns the BrowserStateSummary directly
		result = await event.event_result(raise_if_none=True, raise_if_any=True)
		assert result is not None
		
		# Проверяем, что result - это объект, а не словарь
		# Если это словарь, преобразуем его обратно в объект
		if isinstance(result, dict):
			from core.session.models import BrowserStateSummary
			from core.dom_processing.models import SerializedDOMState
			from core.session.models import TabInfo, PageInfo, NetworkRequest, PaginationButton
			
			# Преобразуем словарь обратно в объект
			dom_state_dict = result.get('dom_state', {})
			if isinstance(dom_state_dict, dict):
				dom_state = SerializedDOMState(
					_root=dom_state_dict.get('_root'),
					selector_map=dom_state_dict.get('selector_map', {})
				)
			else:
				dom_state = dom_state_dict
			
			tabs = []
			for tab_dict in result.get('tabs', []):
				if isinstance(tab_dict, dict):
					tabs.append(TabInfo(**tab_dict))
				else:
					tabs.append(tab_dict)
			
			result = BrowserStateSummary(
				dom_state=dom_state,
				url=result.get('url', ''),
				title=result.get('title', ''),
				tabs=tabs,
				screenshot=result.get('screenshot'),
				page_info=result.get('page_info'),
				pixels_above=result.get('pixels_above', 0),
				pixels_below=result.get('pixels_below', 0),
				browser_errors=result.get('browser_errors', []),
				is_pdf_viewer=result.get('is_pdf_viewer', False),
				recent_events=result.get('recent_events'),
				pending_network_requests=result.get('pending_network_requests', []),
				pagination_buttons=result.get('pagination_buttons', []),
				closed_popup_messages=result.get('closed_popup_messages', []),
			)
		
		assert result.dom_state is not None
		return result

	async def get_state_as_text(self) -> str:
		"""Get the browser state as text."""
		state = await self.get_browser_state_summary()
		assert state.dom_state is not None
		dom_state = state.dom_state
		return dom_state.llm_representation()

	async def attach_all_watchdogs(self) -> None:
		"""Initialize and attach all watchdogs. Delegates to SessionLifecycleManager."""
		await self._lifecycle_manager.attach_all_watchdogs()

	async def connect(self, cdp_url: str | None = None) -> Self:
		"""Connect to a remote chromium-based browser via CDP. Delegates to SessionLifecycleManager."""
		return await self._lifecycle_manager.connect(cdp_url)

	async def _setup_proxy_auth(self) -> None:
		"""Enable CDP Fetch auth handling for authenticated proxy. Delegates to SessionLifecycleManager."""
		await self._lifecycle_manager._setup_proxy_auth()

	async def get_tabs(self) -> list[TabInfo]:
		"""Get information about all open tabs using cached target data. Delegates to BrowserOperationsManager."""
		return await self._browser_operations.get_tabs()

	# endregion - ========== Helper Methods ==========

	# region - ========== ID Lookup Methods ==========
	async def get_current_target_info(self) -> TargetInfo | None:
		"""Get info about the current active target using cached session data. Delegates to BrowserOperationsManager."""
		return await self._browser_operations.get_current_target_info()

	async def get_current_page_url(self) -> str:
		"""Get the URL of the current page. Delegates to BrowserOperationsManager."""
		return await self._browser_operations.get_current_page_url()

	async def get_current_page_title(self) -> str:
		"""Get the title of the current page. Delegates to BrowserOperationsManager."""
		return await self._browser_operations.get_current_page_title()

	async def navigate_to(self, url: str, new_tab: bool = False) -> None:
		"""Navigate to a URL using the standard event system. Delegates to BrowserOperationsManager."""
		await self._browser_operations.navigate_to(url, new_tab=new_tab)

	# endregion - ========== ID Lookup Methods ==========

	# region - ========== DOM Helper Methods ==========

	async def get_dom_element_by_index(self, index: int) -> EnhancedDOMTreeNode | None:
		"""Get DOM element by index. Delegates to VisualOperationsManager."""
		return await self._visual_operations.get_dom_element_by_index(index)

	def update_cached_selector_map(self, selector_map: dict[int, EnhancedDOMTreeNode]) -> None:
		"""Update the cached selector map with new DOM state. Delegates to VisualOperationsManager."""
		self._visual_operations.update_cached_selector_map(selector_map)

	# Alias for backwards compatibility
	async def get_element_by_index(self, index: int) -> EnhancedDOMTreeNode | None:
		"""Alias for get_dom_element_by_index for backwards compatibility. Delegates to VisualOperationsManager."""
		return await self._visual_operations.get_element_by_index(index)

	async def get_dom_element_at_coordinates(self, x: int, y: int) -> EnhancedDOMTreeNode | None:
		"""Get DOM element at coordinates as EnhancedDOMTreeNode. Delegates to VisualOperationsManager."""
		return await self._visual_operations.get_dom_element_at_coordinates(x, y)

	async def get_target_id_from_tab_id(self, tab_id: str) -> TargetID:
		"""Get the full-length TargetID from the truncated 4-char tab_id. Delegates to BrowserOperationsManager."""
		return await self._browser_operations.get_target_id_from_tab_id(tab_id)

	async def get_target_id_from_url(self, url: str) -> TargetID:
		"""Get the TargetID from a URL. Delegates to BrowserOperationsManager."""
		return await self._browser_operations.get_target_id_from_url(url)

	async def get_most_recently_opened_target_id(self) -> TargetID:
		"""Get the most recently opened target ID. Delegates to BrowserOperationsManager."""
		return await self._browser_operations.get_most_recently_opened_target_id()

	def is_file_input(self, element: Any) -> bool:
		"""Check if element is a file input. Delegates to VisualOperationsManager."""
		return self._visual_operations.is_file_input(element)

	async def get_selector_map(self) -> dict[int, EnhancedDOMTreeNode]:
		"""Get the current selector map from cached state or DOM watchdog. Delegates to VisualOperationsManager."""
		return await self._visual_operations.get_selector_map()

	async def get_index_by_id(self, element_id: str) -> int | None:
		"""Find element index by its id attribute. Delegates to VisualOperationsManager."""
		return await self._visual_operations.get_index_by_id(element_id)

	async def get_index_by_class(self, class_name: str) -> int | None:
		"""Find element index by its class attribute. Delegates to VisualOperationsManager."""
		return await self._visual_operations.get_index_by_class(class_name)

	async def remove_highlights(self) -> None:
		"""Remove highlights from the page using CDP. Delegates to VisualOperationsManager."""
		await self._visual_operations.remove_highlights()

	async def get_element_coordinates(self, backend_node_id: int, cdp_session: DevToolsSession) -> DOMRect | None:
		"""Get element coordinates for a backend node ID using multiple methods. Delegates to VisualOperationsManager."""
		return await self._visual_operations.get_element_coordinates(backend_node_id, cdp_session)

	async def highlight_interaction_element(self, node: 'EnhancedDOMTreeNode') -> None:
		"""Temporarily highlight an element during interaction. Delegates to VisualOperationsManager."""
		await self._visual_operations.highlight_interaction_element(node)

	async def highlight_coordinate_click(self, x: int, y: int) -> None:
		"""Temporarily highlight a coordinate click position. Delegates to VisualOperationsManager."""
		await self._visual_operations.highlight_coordinate_click(x, y)

	async def add_highlights(self, selector_map: dict[int, 'EnhancedDOMTreeNode']) -> None:
		"""Add visual highlights to the browser DOM. Delegates to VisualOperationsManager."""
		await self._visual_operations.add_highlights(selector_map)

	async def _highlight_interaction_element_impl(self, node: 'EnhancedDOMTreeNode') -> None:
		"""Temporarily highlight an element during interaction for user visibility.

		This creates a visual highlight on the browser that shows the user which element
		is being interacted with. The highlight automatically fades after the configured duration.

		Args:
			node: The DOM node to highlight with backend_node_id for coordinate lookup
		"""
		if not self.browser_profile.highlight_elements:
			return

		try:
			import json

			cdp_session = await self.get_or_create_cdp_session()

			# Get current coordinates
			rect = await self.get_element_coordinates(node.backend_node_id, cdp_session)

			color = self.browser_profile.interaction_highlight_color
			duration_ms = int(self.browser_profile.interaction_highlight_duration * 1000)

			if not rect:
				self.logger.debug(f'No coordinates found for backend node {node.backend_node_id}')
				return

			# Create animated corner brackets that start offset and animate inward
			script = f"""
			(function() {{
				const rect = {json.dumps({'x': rect.x, 'y': rect.y, 'width': rect.width, 'height': rect.height})};
				const color = {json.dumps(color)};
				const duration = {duration_ms};

				// Scale corner size based on element dimensions to ensure gaps between corners
				const maxCornerSize = 20;
				const minCornerSize = 8;
				const cornerSize = Math.max(
					minCornerSize,
					Math.min(maxCornerSize, Math.min(rect.width, rect.height) * 0.35)
				);
				const borderWidth = 3;
				const startOffset = 10; // Starting offset in pixels
				const finalOffset = -3; // Final position slightly outside the element

				// Get current scroll position
				const scrollX = window.pageXOffset || document.documentElement.scrollLeft || 0;
				const scrollY = window.pageYOffset || document.documentElement.scrollTop || 0;

				// Create container for all corners
				const container = document.createElement('div');
				container.setAttribute('data-agent-interaction-highlight', 'true');
				container.style.cssText = `
					position: absolute;
					left: ${{rect.x + scrollX}}px;
					top: ${{rect.y + scrollY}}px;
					width: ${{rect.width}}px;
					height: ${{rect.height}}px;
					pointer-events: none;
					z-index: 2147483647;
				`;

				// Create 4 corner brackets
				const corners = [
					{{ pos: 'top-left', startX: -startOffset, startY: -startOffset, finalX: finalOffset, finalY: finalOffset }},
					{{ pos: 'top-right', startX: startOffset, startY: -startOffset, finalX: -finalOffset, finalY: finalOffset }},
					{{ pos: 'bottom-left', startX: -startOffset, startY: startOffset, finalX: finalOffset, finalY: -finalOffset }},
					{{ pos: 'bottom-right', startX: startOffset, startY: startOffset, finalX: -finalOffset, finalY: -finalOffset }}
				];

				corners.forEach(corner => {{
					const bracket = document.createElement('div');
					bracket.style.cssText = `
						position: absolute;
						width: ${{cornerSize}}px;
						height: ${{cornerSize}}px;
						pointer-events: none;
						transition: all 0.15s ease-out;
					`;

					// Position corners
					if (corner.pos === 'top-left') {{
						bracket.style.top = '0';
						bracket.style.left = '0';
						bracket.style.borderTop = `${{borderWidth}}px solid ${{color}}`;
						bracket.style.borderLeft = `${{borderWidth}}px solid ${{color}}`;
						bracket.style.transform = `translate(${{corner.startX}}px, ${{corner.startY}}px)`;
					}} else if (corner.pos === 'top-right') {{
						bracket.style.top = '0';
						bracket.style.right = '0';
						bracket.style.borderTop = `${{borderWidth}}px solid ${{color}}`;
						bracket.style.borderRight = `${{borderWidth}}px solid ${{color}}`;
						bracket.style.transform = `translate(${{corner.startX}}px, ${{corner.startY}}px)`;
					}} else if (corner.pos === 'bottom-left') {{
						bracket.style.bottom = '0';
						bracket.style.left = '0';
						bracket.style.borderBottom = `${{borderWidth}}px solid ${{color}}`;
						bracket.style.borderLeft = `${{borderWidth}}px solid ${{color}}`;
						bracket.style.transform = `translate(${{corner.startX}}px, ${{corner.startY}}px)`;
					}} else if (corner.pos === 'bottom-right') {{
						bracket.style.bottom = '0';
						bracket.style.right = '0';
						bracket.style.borderBottom = `${{borderWidth}}px solid ${{color}}`;
						bracket.style.borderRight = `${{borderWidth}}px solid ${{color}}`;
						bracket.style.transform = `translate(${{corner.startX}}px, ${{corner.startY}}px)`;
					}}

					container.appendChild(bracket);

					// Animate to final position slightly outside the element
					setTimeout(() => {{
						bracket.style.transform = `translate(${{corner.finalX}}px, ${{corner.finalY}}px)`;
					}}, 10);
				}});

				document.body.appendChild(container);

				// Auto-remove after duration
				setTimeout(() => {{
					container.style.opacity = '0';
					container.style.transition = 'opacity 0.3s ease-out';
					setTimeout(() => container.remove(), 300);
				}}, duration);

				return {{ created: true }};
			}})();
			"""

			# Fire and forget - don't wait for completion
			await cdp_session.cdp_client.send.Runtime.evaluate(
				params={'expression': script, 'returnByValue': True}, session_id=cdp_session.session_id
			)

		except Exception as e:
			# Don't fail the action if highlighting fails
			self.logger.debug(f'Failed to highlight interaction element: {e}')

	async def _highlight_coordinate_click_impl(self, x: int, y: int) -> None:
		"""Temporarily highlight a coordinate click position for user visibility.

		This creates a visual highlight at the specified coordinates showing where
		the click action occurred. The highlight automatically fades after the configured duration.

		Args:
			x: Horizontal coordinate relative to viewport left edge
			y: Vertical coordinate relative to viewport top edge
		"""
		if not self.browser_profile.highlight_elements:
			return

		try:
			import json

			cdp_session = await self.get_or_create_cdp_session()

			color = self.browser_profile.interaction_highlight_color
			duration_ms = int(self.browser_profile.interaction_highlight_duration * 1000)

			# Create animated crosshair and circle at the click coordinates
			script = f"""
			(function() {{
				const x = {x};
				const y = {y};
				const color = {json.dumps(color)};
				const duration = {duration_ms};

				// Get current scroll position
				const scrollX = window.pageXOffset || document.documentElement.scrollLeft || 0;
				const scrollY = window.pageYOffset || document.documentElement.scrollTop || 0;

				// Create container
				const container = document.createElement('div');
				container.setAttribute('data-agent-coordinate-highlight', 'true');
				container.style.cssText = `
					position: absolute;
					left: ${{x + scrollX}}px;
					top: ${{y + scrollY}}px;
					width: 0;
					height: 0;
					pointer-events: none;
					z-index: 2147483647;
				`;

				// Create outer circle
				const outerCircle = document.createElement('div');
				outerCircle.style.cssText = `
					position: absolute;
					left: -15px;
					top: -15px;
					width: 30px;
					height: 30px;
					border: 3px solid ${{color}};
					border-radius: 50%;
					opacity: 0;
					transform: scale(0.3);
					transition: all 0.2s ease-out;
				`;
				container.appendChild(outerCircle);

				// Create center dot
				const centerDot = document.createElement('div');
				centerDot.style.cssText = `
					position: absolute;
					left: -4px;
					top: -4px;
					width: 8px;
					height: 8px;
					background: ${{color}};
					border-radius: 50%;
					opacity: 0;
					transform: scale(0);
					transition: all 0.15s ease-out;
				`;
				container.appendChild(centerDot);

				document.body.appendChild(container);

				// Animate in
				setTimeout(() => {{
					outerCircle.style.opacity = '0.8';
					outerCircle.style.transform = 'scale(1)';
					centerDot.style.opacity = '1';
					centerDot.style.transform = 'scale(1)';
				}}, 10);

				// Animate out and remove
				setTimeout(() => {{
					outerCircle.style.opacity = '0';
					outerCircle.style.transform = 'scale(1.5)';
					centerDot.style.opacity = '0';
					setTimeout(() => container.remove(), 300);
				}}, duration);

				return {{ created: true }};
			}})();
			"""

			# Fire and forget - don't wait for completion
			await cdp_session.cdp_client.send.Runtime.evaluate(
				params={'expression': script, 'returnByValue': True}, session_id=cdp_session.session_id
			)

		except Exception as e:
			# Don't fail the action if highlighting fails
			self.logger.debug(f'Failed to highlight coordinate click: {e}')

	async def _add_highlights_impl(self, selector_map: dict[int, 'EnhancedDOMTreeNode']) -> None:
		"""Implementation of add_highlights. Currently not implemented."""
		pass

	async def _close_extension_options_pages(self) -> None:
		"""Close any extension options/welcome pages. Delegates to NavigationManager."""
		await self._navigation_manager._close_extension_options_pages()

	async def send_demo_mode_log(self, message: str, level: str = 'info', metadata: dict[str, Any] | None = None) -> None:
		"""Send a message to the in-browser demo panel if enabled."""
		if not self.browser_profile.demo_mode:
			return
		demo = self.demo_mode
		if not demo:
			return
		try:
			await demo.send_log(message=message, level=level, metadata=metadata or {})
		except Exception as exc:
			self.logger.debug(f'[DemoMode] Failed to send log: {exc}')

	@property
	def downloaded_files(self) -> list[str]:
		"""Get list of files downloaded during this browser session.

		Returns:
			list[str]: List of absolute file paths to downloaded files in this session
		"""
		return self._downloaded_files.copy()

	# endregion - ========== Helper Methods ==========

	# region - ========== CDP-based replacements for browser_context operations ==========

	async def _cdp_get_all_pages(
		self,
		include_http: bool = True,
		include_about: bool = True,
		include_pages: bool = True,
		include_iframes: bool = False,
		include_workers: bool = False,
		include_chrome: bool = False,
		include_chrome_extensions: bool = False,
		include_chrome_error: bool = False,
	) -> list[TargetInfo]:
		"""Get all browser pages/tabs. Delegates to CDPOperationsManager."""
		return await self._cdp_operations._cdp_get_all_pages(
			include_http=include_http,
			include_about=include_about,
			include_pages=include_pages,
			include_iframes=include_iframes,
			include_workers=include_workers,
			include_chrome=include_chrome,
			include_chrome_extensions=include_chrome_extensions,
			include_chrome_error=include_chrome_error,
		)

	async def _cdp_create_new_page(self, url: str = 'about:blank', background: bool = False, new_window: bool = False) -> str:
		"""Create a new page/tab using CDP. Delegates to CDPOperationsManager."""
		return await self._cdp_operations._cdp_create_new_page(url, background, new_window)

	async def _cdp_close_page(self, target_id: TargetID) -> None:
		"""Close a page/tab using CDP. Delegates to CDPOperationsManager."""
		await self._cdp_operations._cdp_close_page(target_id)

	async def _cdp_get_cookies(self) -> list[Cookie]:
		"""Get cookies using CDP Network.getCookies. Delegates to BrowserOperationsManager."""
		return await self._browser_operations._cdp_get_cookies()

	async def _cdp_set_cookies(self, cookies: list[Cookie]) -> None:
		"""Set cookies using CDP Storage.setCookies. Delegates to BrowserOperationsManager."""
		await self._browser_operations._cdp_set_cookies(cookies)

	async def _cdp_clear_cookies(self) -> None:
		"""Clear all cookies using CDP Network.clearBrowserCookies. Delegates to BrowserOperationsManager."""
		await self._browser_operations._cdp_clear_cookies()

	async def _cdp_set_extra_headers(self, headers: dict[str, str]) -> None:
		"""Set extra HTTP headers using CDP Network.setExtraHTTPHeaders."""
		if not self.agent_focus_target_id:
			return

		cdp_session = await self.get_or_create_cdp_session()
		# await cdp_session.cdp_client.send.Network.setExtraHTTPHeaders(params={'headers': headers}, session_id=cdp_session.session_id)
		raise NotImplementedError('Not implemented yet')

	async def _cdp_grant_permissions(self, permissions: list[str], origin: str | None = None) -> None:
		"""Grant permissions using CDP Browser.grantPermissions."""
		params = {'permissions': permissions}
		# if origin:
		# 	params['origin'] = origin
		cdp_session = await self.get_or_create_cdp_session()
		# await cdp_session.cdp_client.send.Browser.grantPermissions(params=params, session_id=cdp_session.session_id)
		raise NotImplementedError('Not implemented yet')

	async def _cdp_set_geolocation(self, latitude: float, longitude: float, accuracy: float = 100) -> None:
		"""Set geolocation using CDP Emulation.setGeolocationOverride."""
		await self.cdp_client.send.Emulation.setGeolocationOverride(
			params={'latitude': latitude, 'longitude': longitude, 'accuracy': accuracy}
		)

	async def _cdp_clear_geolocation(self) -> None:
		"""Clear geolocation override using CDP."""
		await self.cdp_client.send.Emulation.clearGeolocationOverride()

	async def _inject_window_open_override(self) -> None:
		"""Inject script to override window.open(). Delegates to SessionLifecycleManager."""
		await self._lifecycle_manager._inject_window_open_override()

	async def _cdp_add_init_script(self, script: str) -> str:
		"""Add script to evaluate on new document. Delegates to CDPOperationsManager."""
		return await self._cdp_operations._cdp_add_init_script(script)

	async def _cdp_remove_init_script(self, identifier: str) -> None:
		"""Remove script added with addScriptToEvaluateOnNewDocument. Delegates to CDPOperationsManager."""
		await self._cdp_operations._cdp_remove_init_script(identifier)

	async def _cdp_set_viewport(
		self, width: int, height: int, device_scale_factor: float = 1.0, mobile: bool = False, target_id: str | None = None
	) -> None:
		"""Set viewport using CDP. Delegates to CDPOperationsManager."""
		await self._cdp_operations._cdp_set_viewport(width, height, device_scale_factor, mobile, target_id)

	async def _cdp_get_origins(self) -> list[dict[str, Any]]:
		"""Get origins with localStorage and sessionStorage using CDP. Delegates to BrowserOperationsManager."""
		return await self._browser_operations._cdp_get_origins()

	async def _cdp_get_storage_state(self) -> dict:
		"""Get storage state (cookies, localStorage, sessionStorage) using CDP. Delegates to BrowserOperationsManager."""
		return await self._browser_operations._cdp_get_storage_state()

	async def _cdp_navigate(self, url: str, target_id: TargetID | None = None) -> None:
		"""Navigate to URL using CDP. Delegates to CDPOperationsManager."""
		await self._cdp_operations._cdp_navigate(url, target_id)

	@staticmethod
	def _is_valid_target(
		target_info: TargetInfo,
		include_http: bool = True,
		include_chrome: bool = False,
		include_chrome_extensions: bool = False,
		include_chrome_error: bool = False,
		include_about: bool = True,
		include_iframes: bool = True,
		include_pages: bool = True,
		include_workers: bool = False,
	) -> bool:
		"""Check if a target should be processed. Delegates to CDPOperationsManager."""
		from core.session.cdp_operations import CDPOperationsManager
		return CDPOperationsManager._is_valid_target(
			target_info,
			include_http=include_http,
			include_chrome=include_chrome,
			include_chrome_extensions=include_chrome_extensions,
			include_chrome_error=include_chrome_error,
			include_about=include_about,
			include_iframes=include_iframes,
			include_pages=include_pages,
			include_workers=include_workers,
		)

	async def get_all_frames(self) -> tuple[dict[str, dict], dict[str, str]]:
		"""Get a complete frame hierarchy from all browser targets. Delegates to NavigationManager."""
		return await self._navigation_manager.get_all_frames()

	async def _populate_frame_metadata(self, all_frames: dict[str, dict], target_sessions: dict[str, str]) -> None:
		"""Populate additional frame metadata. Delegates to NavigationManager."""
		await self._navigation_manager._populate_frame_metadata(all_frames, target_sessions)

	async def find_frame_target(self, frame_id: str, all_frames: dict[str, dict] | None = None) -> dict | None:
		"""Find the frame info for a specific frame ID. Delegates to NavigationManager."""
		return await self._navigation_manager.find_frame_target(frame_id, all_frames)

	async def cdp_client_for_target(self, target_id: TargetID) -> DevToolsSession:
		"""Get CDP client for a target. Delegates to NavigationManager."""
		return await self._navigation_manager.cdp_client_for_target(target_id)

	async def cdp_client_for_frame(self, frame_id: str) -> DevToolsSession:
		"""Get CDP client for a frame. Delegates to NavigationManager."""
		return await self._navigation_manager.cdp_client_for_frame(frame_id)

	async def cdp_client_for_node(self, node: EnhancedDOMTreeNode) -> DevToolsSession:
		"""Get CDP client for a DOM node. Delegates to NavigationManager."""
		return await self._navigation_manager.cdp_client_for_node(node)

	async def take_screenshot(
		self,
		path: str | None = None,
		full_page: bool = False,
		format: str = 'png',
		quality: int | None = None,
		clip: dict | None = None,
	) -> bytes:
		"""Take a screenshot using CDP. Delegates to VisualOperationsManager."""
		return await self._visual_operations.take_screenshot(path=path, full_page=full_page, format=format, quality=quality, clip=clip)

	async def screenshot_element(
		self,
		selector: str,
		path: str | None = None,
		format: str = 'png',
		quality: int | None = None,
	) -> bytes:
		"""Take a screenshot of a specific element. Delegates to VisualOperationsManager."""
		return await self._visual_operations.screenshot_element(selector=selector, path=path, format=format, quality=quality)
