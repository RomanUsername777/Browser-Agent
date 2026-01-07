"""Менеджер табов и навигации для ChromeSession."""

from typing import TYPE_CHECKING

from cdp_use.cdp.target import TargetID
from cdp_use.cdp.target.types import TargetInfo

if TYPE_CHECKING:
	from core.interaction.page import Page
	from core.session.models import TabInfo
	from core.session.session import ChromeSession


class TabManager:
	"""Менеджер для работы с табами и навигацией браузера."""

	def __init__(self, browser_session: 'ChromeSession'):
		self.browser_session = browser_session

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

