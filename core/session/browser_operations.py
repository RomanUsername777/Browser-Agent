"""Менеджер операций браузера: табы и storage для ChromeSession."""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cdp_use.cdp.network import Cookie
from cdp_use.cdp.target import TargetID
from cdp_use.cdp.target.types import TargetInfo

if TYPE_CHECKING:
	from core.interaction.page import Page
	from core.session.models import TabInfo
	from core.session.session import ChromeSession


class BrowserOperationsManager:
	"""Менеджер для работы с табами и storage браузера."""

	def __init__(self, browser_session: 'ChromeSession'):
		self.browser_session = browser_session

	# ========== Tab Management Methods ==========

	async def new_page(self, url: str | None = None) -> 'Page':
		"""Create a new page (tab)."""
		from cdp_use.cdp.target.commands import CreateTargetParameters

		params: CreateTargetParameters = {'url': url or 'about:blank'}
		result = await self.browser_session.cdp_client.send.Target.createTarget(params)

		target_id = result['targetId']

		# Import here to avoid circular import
		from core.interaction.page import Page as Target

		return Target(self.browser_session, target_id)

	async def get_current_page(self) -> 'Page | None':
		"""Get the current page as an actor Page."""
		target_info = await self.browser_session.get_current_target_info()

		if not target_info:
			return None

		from core.interaction.page import Page as Target

		return Target(self.browser_session, target_info['targetId'])

	async def must_get_current_page(self) -> 'Page':
		"""Get the current page as an actor Page."""
		page = await self.get_current_page()
		if not page:
			raise RuntimeError('No current target found')

		return page

	async def get_pages(self) -> list['Page']:
		"""Get all available pages using SessionManager (source of truth)."""
		# Import here to avoid circular import
		from core.interaction.page import Page as PageActor

		page_targets = self.browser_session.session_manager.get_all_page_targets() if self.browser_session.session_manager else []

		targets = []
		for target in page_targets:
			targets.append(PageActor(self.browser_session, target.target_id))

		return targets

	def get_focused_target(self) -> 'Target | None':
		"""Get the target that currently has agent focus.

		Returns:
			Target object if agent has focus, None otherwise.
		"""
		if not self.browser_session.session_manager:
			return None
		return self.browser_session.session_manager.get_focused_target()

	def get_page_targets(self) -> list['Target']:
		"""Get all page/tab targets (excludes iframes, workers, etc.).

		Returns:
			List of Target objects for all page/tab targets.
		"""
		if not self.browser_session.session_manager:
			return []
		return self.browser_session.session_manager.get_all_page_targets()

	async def close_page(self, page: 'Page | str') -> None:
		"""Close a page by Page object or target ID."""
		from cdp_use.cdp.target.commands import CloseTargetParameters

		# Import here to avoid circular import
		from core.interaction.page import Page as Target

		if isinstance(page, Target):
			target_id = page._target_id
		else:
			target_id = str(page)

		params: CloseTargetParameters = {'targetId': target_id}
		await self.browser_session.cdp_client.send.Target.closeTarget(params)

	async def get_tabs(self) -> list['TabInfo']:
		"""Get all open tabs as TabInfo objects."""
		from core.session.models import TabInfo

		if not self.browser_session.session_manager:
			return []

		page_targets = self.browser_session.session_manager.get_all_page_targets()
		tabs = []

		for target in page_targets:
			# Get current URL and title from target
			url = target.url
			title = target.title

			# Create TabInfo
			tab_info: TabInfo = {
				'tab_id': target.target_id[-4:],  # Last 4 chars for display
				'target_id': target.target_id,
				'url': url,
				'title': title,
				'active': target.target_id == self.browser_session.agent_focus_target_id,
			}

			tabs.append(tab_info)

		return tabs

	async def get_current_target_info(self) -> 'TargetInfo | None':
		"""Get current target info using SessionManager."""
		if not self.browser_session.session_manager:
			return None

		focused_target = self.browser_session.session_manager.get_focused_target()
		if not focused_target:
			return None

		from cdp_use.cdp.target.types import TargetInfo

		target_info: TargetInfo = {
			'targetId': focused_target.target_id,
			'type': focused_target.target_type,
			'title': focused_target.title,
			'url': focused_target.url,
			'attached': True,
			'canAccessOpener': False,
		}

		return target_info

	async def get_current_page_url(self) -> str:
		"""Get the URL of the current page."""
		target_info = await self.get_current_target_info()
		return target_info['url'] if target_info else ''

	async def get_current_page_title(self) -> str:
		"""Get the title of the current page."""
		target_info = await self.get_current_target_info()
		return target_info['title'] if target_info else ''

	async def navigate_to(self, url: str, new_tab: bool = False) -> None:
		"""Navigate to a URL, optionally in a new tab."""
		from core.session.events import UrlNavigationRequest

		event = UrlNavigationRequest(url=url, new_tab=new_tab)
		await self.browser_session.event_bus.dispatch(event)
		await event

	async def get_target_id_from_tab_id(self, tab_id: str) -> TargetID:
		"""Get the full-length TargetID from the truncated 4-char tab_id using SessionManager."""
		if not self.browser_session.session_manager:
			raise RuntimeError('SessionManager not initialized')

		for full_target_id in self.browser_session.session_manager.get_all_target_ids():
			if full_target_id.endswith(tab_id):
				if await self.browser_session.session_manager.is_target_valid(full_target_id):
					return full_target_id
				# Stale target - Chrome should have sent detach event
				# If we're here, event listener will clean it up
				self.browser_session.logger.debug(f'Found stale target {full_target_id}, skipping')

		raise ValueError(f'No TargetID found ending in tab_id=...{tab_id}')

	async def get_target_id_from_url(self, url: str) -> TargetID:
		"""Get the TargetID from a URL using SessionManager (source of truth)."""
		if not self.browser_session.session_manager:
			raise RuntimeError('SessionManager not initialized')

		# Search in SessionManager targets (exact match first)
		for target_id, target in self.browser_session.session_manager.get_all_targets().items():
			if target.target_type in ('page', 'tab') and target.url == url:
				return target_id

		# Still not found, try substring match as fallback
		for target_id, target in self.browser_session.session_manager.get_all_targets().items():
			if target.target_type in ('page', 'tab') and url in target.url:
				return target_id

		raise ValueError(f'No TargetID found for url={url}')

	async def get_most_recently_opened_target_id(self) -> TargetID:
		"""Get the most recently opened target ID using SessionManager."""
		# Get all page targets from SessionManager
		page_targets = self.browser_session.session_manager.get_all_page_targets()
		if not page_targets:
			raise RuntimeError('No page targets available')
		return page_targets[-1].target_id

	# ========== Storage Management Methods ==========

	async def cookies(self) -> list['Cookie']:
		"""Get cookies, optionally filtered by URLs."""
		result = await self.browser_session.cdp_client.send.Storage.getCookies()
		return result['cookies']

	async def clear_cookies(self) -> None:
		"""Clear all cookies."""
		await self.browser_session.cdp_client.send.Network.clearBrowserCookies()

	async def export_storage_state(self, output_path: str | Path | None = None) -> dict[str, Any]:
		"""Export all browser cookies and storage to storage_state format.

		Extracts decrypted cookies via CDP, bypassing keychain encryption.

		Args:
			output_path: Optional path to save storage_state.json. If None, returns dict only.

		Returns:
			Storage state dict со списком cookies (формат совместим с Playwright/Chromium).

		"""
		# Get all cookies using Storage.getCookies (returns decrypted cookies from all domains)
		cookies = await self._cdp_get_cookies()

		# Конвертация формата cookies CDP в компактный storage_state-формат
		storage_state = {
			'cookies': [
				{
					'name': c['name'],
					'value': c['value'],
					'domain': c['domain'],
					'path': c['path'],
					'expires': c.get('expires', -1),
					'httpOnly': c.get('httpOnly', False),
					'secure': c.get('secure', False),
					'sameSite': c.get('sameSite', 'Lax'),
				}
				for c in cookies
			],
			'origins': [],  # Could add localStorage/sessionStorage extraction if needed
		}

		if output_path:
			output_file = Path(output_path).expanduser().resolve()
			output_file.parent.mkdir(parents=True, exist_ok=True)
			output_file.write_text(json.dumps(storage_state, indent=2))
			self.browser_session.logger.info(f'💾 Exported {len(cookies)} cookies to {output_file}')

		return storage_state

	async def _cdp_get_cookies(self) -> list[Cookie]:
		"""Get cookies using CDP Network.getCookies."""
		cdp_session = await self.browser_session.get_or_create_cdp_session(target_id=None)
		import asyncio
		result = await asyncio.wait_for(
			cdp_session.cdp_client.send.Storage.getCookies(session_id=cdp_session.session_id), timeout=8.0
		)
		return result.get('cookies', [])

	async def _cdp_set_cookies(self, cookies: list[Cookie]) -> None:
		"""Set cookies using CDP Storage.setCookies."""
		if not self.browser_session.agent_focus_target_id or not cookies:
			return

		cdp_session = await self.browser_session.get_or_create_cdp_session(target_id=None)
		# Storage.setCookies expects params dict with 'cookies' key
		await cdp_session.cdp_client.send.Storage.setCookies(
			params={'cookies': cookies},  # type: ignore[arg-type]
			session_id=cdp_session.session_id,
		)

	async def _cdp_clear_cookies(self) -> None:
		"""Clear all cookies using CDP Network.clearBrowserCookies."""
		cdp_session = await self.browser_session.get_or_create_cdp_session()
		await cdp_session.cdp_client.send.Storage.clearCookies(session_id=cdp_session.session_id)

	async def _cdp_get_origins(self) -> list[dict[str, Any]]:
		"""Get origins with localStorage and sessionStorage using CDP."""
		origins = []
		cdp_session = await self.browser_session.get_or_create_cdp_session(target_id=None)

		try:
			# Enable DOMStorage domain to track storage
			await cdp_session.cdp_client.send.DOMStorage.enable(session_id=cdp_session.session_id)

			try:
				# Get all frames to find unique origins
				frames_result = await cdp_session.cdp_client.send.Page.getFrameTree(session_id=cdp_session.session_id)

				# Extract unique origins from frames
				unique_origins = set()

				def _extract_origins(frame_tree):
					"""Recursively extract origins from frame tree."""
					frame = frame_tree.get('frame', {})
					origin = frame.get('securityOrigin')
					if origin and origin != 'null':
						unique_origins.add(origin)

					# Process child frames
					for child in frame_tree.get('childFrames', []):
						_extract_origins(child)

				async def _get_storage_items(origin: str, is_local_storage: bool) -> list[dict[str, str]] | None:
					"""Helper to get storage items for an origin."""
					storage_type = 'localStorage' if is_local_storage else 'sessionStorage'
					try:
						result = await cdp_session.cdp_client.send.DOMStorage.getDOMStorageItems(
							params={'storageId': {'securityOrigin': origin, 'isLocalStorage': is_local_storage}},
							session_id=cdp_session.session_id,
						)

						items = []
						for item in result.get('entries', []):
							if len(item) == 2:  # Each item is [key, value]
								items.append({'name': item[0], 'value': item[1]})

						return items if items else None
					except Exception as e:
						self.browser_session.logger.debug(f'Failed to get {storage_type} for {origin}: {e}')
						return None

				_extract_origins(frames_result.get('frameTree', {}))

				# For each unique origin, get localStorage and sessionStorage
				for origin in unique_origins:
					origin_data = {'origin': origin}

					# Get localStorage
					local_storage = await _get_storage_items(origin, is_local_storage=True)
					if local_storage:
						origin_data['localStorage'] = local_storage

					# Get sessionStorage
					session_storage = await _get_storage_items(origin, is_local_storage=False)
					if session_storage:
						origin_data['sessionStorage'] = session_storage

					# Only add origin if it has storage data
					if 'localStorage' in origin_data or 'sessionStorage' in origin_data:
						origins.append(origin_data)

			finally:
				# Always disable DOMStorage tracking when done
				await cdp_session.cdp_client.send.DOMStorage.disable(session_id=cdp_session.session_id)

		except Exception as e:
			self.browser_session.logger.warning(f'Failed to get origins: {e}')

		return origins

	async def _cdp_get_storage_state(self) -> dict:
		"""Get storage state (cookies, localStorage, sessionStorage) using CDP."""
		# Use the _cdp_get_cookies helper which handles session attachment
		cookies = await self._cdp_get_cookies()

		# Get origins with localStorage/sessionStorage
		origins = await self._cdp_get_origins()

		return {
			'cookies': cookies,
			'origins': origins,
		}

