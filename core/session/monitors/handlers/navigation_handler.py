"""Обработчик действий браузера - navigation."""

import asyncio
import json
from typing import TYPE_CHECKING

from core.dom_processing.manager import EnhancedDOMTreeNode
from core.session.events import GoBackEvent, GoForwardEvent, RefreshEvent
from core.session.models import BrowserError, URLNotAllowedError
from core.observability import observe_debug

if TYPE_CHECKING:
	from core.session.monitors.watchdogs.default_action_watchdog import DefaultActionWatchdog


class NavigationHandler:
	"""Обработчик navigation для DefaultActionWatchdog."""

	def __init__(self, watchdog: "DefaultActionWatchdog"):
		"""Инициализация обработчика с ссылкой на watchdog."""
		self.watchdog = watchdog
		self.browser_session = watchdog.browser_session
		self.browser_controller = watchdog.browser_controller
		self.logger = watchdog.logger

	async def on_GoBackEvent(self, event: GoBackEvent) -> None:
		"""Обработать запрос навигации назад с CDP."""
		cdp_connection = await self.browser_session.get_or_create_cdp_session()
		try:
			# Получить CDP client и session

			# Получить историю навигации
			nav_history = await cdp_connection.cdp_client.send.Page.getNavigationHistory(session_id=cdp_connection.session_id)
			history_index = nav_history['currentIndex']
			history_entries = nav_history['entries']

			# Проверить, можно ли идти назад
			if history_index <= 0:
				self.logger.warning('⚠️ Cannot go back - no previous entry in history')
				return

			# Навигация к предыдущей записи
			prev_entry_id = history_entries[history_index - 1]['id']
			await cdp_connection.cdp_client.send.Page.navigateToHistoryEntry(
				params={'entryId': prev_entry_id}, session_id=cdp_connection.session_id
			)

			# Подождать навигации
			await asyncio.sleep(0.5)
			# Навигация обрабатывается BrowserSession через события

			self.logger.info(f'🔙 Navigated back to {history_entries[history_index - 1]["url"]}')
		except Exception as back_error:
			raise


	async def on_GoForwardEvent(self, event: GoForwardEvent) -> None:
		"""Обработать запрос навигации вперед с CDP."""
		cdp_connection = await self.browser_session.get_or_create_cdp_session()
		try:
			# Получить историю навигации
			nav_history = await cdp_connection.cdp_client.send.Page.getNavigationHistory(session_id=cdp_connection.session_id)
			history_index = nav_history['currentIndex']
			history_entries = nav_history['entries']

			# Проверить, можно ли идти вперед
			if history_index >= len(history_entries) - 1:
				self.logger.warning('⚠️ Cannot go forward - no next entry in history')
				return

			# Навигация к следующей записи
			next_entry_id = history_entries[history_index + 1]['id']
			await cdp_connection.cdp_client.send.Page.navigateToHistoryEntry(
				params={'entryId': next_entry_id}, session_id=cdp_connection.session_id
			)

			# Подождать навигации
			await asyncio.sleep(0.5)
			# Навигация обрабатывается BrowserSession через события

			self.logger.info(f'🔜 Navigated forward to {history_entries[history_index + 1]["url"]}')
		except Exception as forward_error:
			raise


	async def on_RefreshEvent(self, event: RefreshEvent) -> None:
		"""Обработать запрос обновления target с CDP."""
		cdp_connection = await self.browser_session.get_or_create_cdp_session()
		try:
			# Перезагрузить target
			await cdp_connection.cdp_client.send.Page.reload(session_id=cdp_connection.session_id)

			# Подождать перезагрузки
			await asyncio.sleep(1.0)

			# Примечание: Мы не очищаем кэшированное состояние здесь - позволим следующему запросу состояния перестроить при необходимости

			# Навигация обрабатывается BrowserSession через события

			self.logger.info('🔄 Target refreshed')
		except Exception as refresh_error:
			raise
