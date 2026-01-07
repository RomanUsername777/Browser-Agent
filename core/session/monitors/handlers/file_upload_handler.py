"""Обработчик действий браузера - file_upload."""

import asyncio
import json
from typing import TYPE_CHECKING

from core.dom_processing.manager import EnhancedDOMTreeNode
from core.session.events import UploadFileEvent
from core.session.models import BrowserError, URLNotAllowedError
from core.observability import observe_debug

if TYPE_CHECKING:
	from core.session.monitors.watchdogs.default_action_watchdog import DefaultActionWatchdog


class FileUploadHandler:
	"""Обработчик file_upload для DefaultActionWatchdog."""

	def __init__(self, watchdog: "DefaultActionWatchdog"):
		"""Инициализация обработчика с ссылкой на watchdog."""
		self.watchdog = watchdog
		self.browser_session = watchdog.browser_session
		self.browser_controller = watchdog.browser_controller
		self.logger = watchdog.logger

	async def _get_session_id_for_element(self, dom_node: EnhancedDOMTreeNode) -> str | None:
		"""Получить соответствующий CDP session ID для элемента на основе его frame."""
		if dom_node.frame_id:
			# Элемент находится в iframe, нужно получить session для этого frame
			try:
				targets_map = self.browser_session.session_manager.get_all_targets()

				# Найти target для этого frame
				for target_identifier, target_info in targets_map.items():
					if target_info.target_type == 'iframe' and dom_node.frame_id in str(target_identifier):
						# Создать временную session для iframe target без переключения фокуса
						iframe_session = await self.browser_session.get_or_create_cdp_session(target_identifier, focus=False)
						return iframe_session.session_id

				# Если frame не найден в targets, использовать главную target session
				self.logger.debug(f'Frame {dom_node.frame_id} not found in targets, using main session')
			except Exception as frame_error:
				self.logger.debug(f'Error getting frame session: {frame_error}, using main session')

		# Использовать главную target session - get_or_create_cdp_session валидирует фокус автоматически
		cdp_connection = await self.browser_session.get_or_create_cdp_session()
		return cdp_connection.session_id

	async def on_UploadFileEvent(self, event: UploadFileEvent) -> None:
		"""Обработать запрос загрузки файла с CDP."""
		try:
			# Использовать предоставленный узел
			dom_node = event.node
			log_index = dom_node.backend_node_id or 'unknown'

			# Проверить, является ли это файловым input
			if not self.browser_session.is_file_input(dom_node):
				error_message = f'Upload failed - element {log_index} is not a file input.'
				raise BrowserError(message=error_message, long_term_memory=error_message)

			# Получить CDP client и session
			cdp_client_instance = self.browser_session.cdp_client
			element_session_id = await self._get_session_id_for_element(dom_node)

			# Установить файл(ы) для загрузки
			node_backend_id = dom_node.backend_node_id
			await cdp_client_instance.send.DOM.setFileInputFiles(
				params={
					'files': [event.file_path],
					'backendNodeId': node_backend_id,
				},
				session_id=element_session_id,
			)

			self.logger.info(f'📎 Uploaded file {event.file_path} to element {log_index}')
		except Exception as upload_error:
			raise

