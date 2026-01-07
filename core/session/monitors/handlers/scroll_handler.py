"""Обработчик действий браузера - scroll."""

import asyncio
import json
from typing import TYPE_CHECKING

from core.dom_processing.manager import EnhancedDOMTreeNode
from core.session.events import ScrollEvent, ScrollToTextEvent
from core.session.models import BrowserError, URLNotAllowedError
from core.observability import observe_debug

if TYPE_CHECKING:
	from core.session.monitors.watchdogs.default_action_watchdog import DefaultActionWatchdog


class ScrollHandler:
	"""Обработчик scroll для DefaultActionWatchdog."""

	def __init__(self, watchdog: "DefaultActionWatchdog"):
		"""Инициализация обработчика с ссылкой на watchdog."""
		self.watchdog = watchdog
		self.browser_session = watchdog.browser_session
		self.browser_controller = watchdog.browser_controller
		self.logger = watchdog.logger

	async def on_ScrollEvent(self, event: ScrollEvent) -> None:
		"""Обработать запрос прокрутки с CDP."""
		# Проверить, есть ли текущий target для прокрутки
		if not self.browser_session.agent_focus_target_id:
			error_message = 'No active target for scrolling'
			raise BrowserError(error_message)

		try:
			# Преобразовать направление и количество в пиксели
			# Положительные пиксели = прокрутка вниз, отрицательные = прокрутка вверх
			scroll_pixels = event.amount if event.direction == 'down' else -event.amount

			# Прокрутка конкретного элемента, если узел предоставлен
			if event.node is not None:
				dom_node = event.node
				log_index = dom_node.backend_node_id or 'unknown'

				# Проверить, является ли элемент iframe
				is_frame = dom_node.tag_name and dom_node.tag_name.upper() == 'IFRAME'

				# Попытаться прокрутить контейнер элемента
				scroll_success = await self._scroll_element_container(dom_node, scroll_pixels)
				if scroll_success:
					self.logger.debug(
						f'📜 Scrolled element {log_index} container {event.direction} by {event.amount} pixels'
					)

					# Для прокрутки iframe нужно принудительно обновить DOM
					# потому что содержимое iframe изменило позицию
					if is_frame:
						self.logger.debug('🔄 Forcing DOM refresh after iframe scroll')
						# Примечание: Мы не очищаем кэшированное состояние здесь - позволим multi_act обработать обнаружение изменений DOM
						# путем явной перестройки и сравнения при необходимости

						# Подождать немного, чтобы прокрутка установилась и DOM обновился
						await asyncio.sleep(0.2)

					return None

			# Выполнить прокрутку на уровне target
			await self._scroll_with_cdp_gesture(scroll_pixels)

			# Примечание: Мы не очищаем кэшированное состояние здесь - позволим multi_act обработать обнаружение изменений DOM
			# путем явной перестройки и сравнения при необходимости

			# Логировать успех
			self.logger.debug(f'📜 Scrolled {event.direction} by {event.amount} pixels')
			return None
		except Exception as scroll_error:
			raise

		# ========== Implementation Methods ==========


	async def on_ScrollToTextEvent(self, event: ScrollToTextEvent) -> None:
		"""Обработать запрос прокрутки к тексту с CDP. Выбрасывает исключение, если текст не найден."""


		# Получить фокусированную CDP сессию используя публичный API (валидирует и ждет восстановления при необходимости)
		cdp_connection = await self.browser_session.get_or_create_cdp_session()
		cdp_client_instance = cdp_connection.cdp_client
		connection_session_id = cdp_connection.session_id

		# Включить DOM
		await cdp_client_instance.send.DOM.enable(session_id=connection_session_id)

		# Получить документ
		document_result = await cdp_client_instance.send.DOM.getDocument(params={'depth': -1}, session_id=connection_session_id)
		document_root_id = document_result['root']['nodeId']

		# Поиск текста используя XPath
		xpath_queries = [
			f'//*[contains(text(), "{event.text}")]',
			f'//*[contains(., "{event.text}")]',
			f'//*[@*[contains(., "{event.text}")]]',
		]

		text_found = False
		for xpath_query in xpath_queries:
			try:
				# Выполнить поиск
				search_result = await cdp_client_instance.send.DOM.performSearch(params={'query': xpath_query}, session_id=connection_session_id)
				xpath_search_id = search_result['searchId']
				match_count = search_result['resultCount']

				if match_count > 0:
					# Получить первое совпадение
					matched_nodes = await cdp_client_instance.send.DOM.getSearchResults(
						params={'searchId': xpath_search_id, 'fromIndex': 0, 'toIndex': 1},
						session_id=connection_session_id,
					)

					if matched_nodes['nodeIds']:
						matched_node_id = matched_nodes['nodeIds'][0]

						# Прокрутить элемент в видимую область
						await cdp_client_instance.send.DOM.scrollIntoViewIfNeeded(params={'nodeId': matched_node_id}, session_id=connection_session_id)

						text_found = True
						self.logger.debug(f'📜 Scrolled to text: "{event.text}"')
						break

				# Очистить результаты поиска
				await cdp_client_instance.send.DOM.discardSearchResults(params={'searchId': xpath_search_id}, session_id=connection_session_id)
			except Exception as search_error:
				self.logger.debug(f'Search query failed: {xpath_query}, error: {search_error}')
				continue

		if not text_found:
			# Запасной вариант: Попробовать поиск на JavaScript
			javascript_result = await cdp_client_instance.send.Runtime.evaluate(
				params={
					'expression': f'''
							(() => {{
								const walker = document.createTreeWalker(
									document.body,
									NodeFilter.SHOW_TEXT,
									null,
									false
								);
								let node;
								while (node = walker.nextNode()) {{
									if (node.textContent.includes("{event.text}")) {{
										node.parentElement.scrollIntoView({{behavior: 'smooth', block: 'center'}});
										return true;
									}}
								}}
								return false;
							}})()
						'''
				},
				session_id=connection_session_id,
			)

			if javascript_result.get('result', {}).get('value'):
				self.logger.debug(f'📜 Scrolled to text: "{event.text}" (via JS)')
				return None
			else:
				self.logger.warning(f'⚠️ Text not found: "{event.text}"')
				raise BrowserError(f'Text not found: "{event.text}"', details={'text': event.text})

		# Если мы дошли сюда и text_found равен True, вернуть None (успех)
		if text_found:
			return None
		else:
			raise BrowserError(f'Text not found: "{event.text}"', details={'text': event.text})


	async def _scroll_with_cdp_gesture(self, scroll_pixels: int) -> bool:
		"""
		Прокрутить используя CDP Input.synthesizeScrollGesture для имитации реалистичного жеста прокрутки.

		Args:
			scroll_pixels: Количество пикселей для прокрутки (положительное = вниз, отрицательное = вверх)

		Returns:
			True если успешно, False если не удалось
		"""
		try:
			# Получить фокусированную CDP сессию используя публичный API (валидирует и ждет восстановления при необходимости)
			cdp_connection = await self.browser_session.get_or_create_cdp_session()
			cdp_client_instance = cdp_connection.cdp_client
			connection_session_id = cdp_connection.session_id

			# Получить размеры viewport из кэшированного значения, если доступно
			if self.browser_session._original_viewport_size:
				view_width, view_height = self.browser_session._original_viewport_size
			else:
				# Запасной вариант: запросить layout metrics
				layout_data = await cdp_client_instance.send.Page.getLayoutMetrics(session_id=connection_session_id)
				view_width = layout_data['layoutViewport']['clientWidth']
				view_height = layout_data['layoutViewport']['clientHeight']

			# Вычислить центр viewport
			center_x_coord = view_width / 2
			center_y_coord = view_height / 2

			# Для жеста прокрутки положительное yDistance прокручивает вверх, отрицательное - вниз
			# (противоположно конвенции mouseWheel deltaY)
			vertical_distance = -scroll_pixels

			# Синтезировать жест прокрутки - использовать очень высокую скорость для почти мгновенной прокрутки
			await cdp_client_instance.send.Input.synthesizeScrollGesture(
				params={
					'x': center_x_coord,
					'y': center_y_coord,
					'xDistance': 0,
					'yDistance': vertical_distance,
					'speed': 50000,  # пикселей в секунду (высокая = почти мгновенная прокрутка)
				},
				session_id=connection_session_id,
			)

			self.logger.debug(f'📄 Scrolled via CDP gesture: {scroll_pixels}px')
			return True

		except Exception as scroll_error:
			# Не критично - JavaScript запасной вариант обработает прокрутку
			self.logger.debug(f'CDP gesture scroll failed ({type(scroll_error).__name__}: {scroll_error}), falling back to JS')
			return False


	async def _scroll_element_container(self, dom_node, scroll_pixels: int) -> bool:
		"""Попытаться прокрутить контейнер элемента используя CDP."""
		try:
			cdp_connection = await self.browser_session.cdp_client_for_node(dom_node)

			# Проверить, является ли это iframe - если да, прокрутить его содержимое напрямую
			if dom_node.tag_name and dom_node.tag_name.upper() == 'IFRAME':
				# Для iframes нужно прокрутить документ содержимого, а не сам элемент iframe
				# Использовать JavaScript для прямой прокрутки содержимого iframe
				node_backend_id = dom_node.backend_node_id

				# Разрешить узел, чтобы получить object ID
				resolve_result = await cdp_connection.cdp_client.send.DOM.resolveNode(
					params={'backendNodeId': node_backend_id},
					session_id=cdp_connection.session_id,
				)

				if 'object' in resolve_result and 'objectId' in resolve_result['object']:
					js_object_id = resolve_result['object']['objectId']

					# Прокрутить содержимое iframe напрямую
					scroll_result = await cdp_connection.cdp_client.send.Runtime.callFunctionOn(
						params={
							'functionDeclaration': f"""
								function() {{
									try {{
										const doc = this.contentDocument || this.contentWindow.document;
										if (doc) {{
											const scrollElement = doc.documentElement || doc.body;
											if (scrollElement) {{
												const oldScrollTop = scrollElement.scrollTop;
												scrollElement.scrollTop += {pixels};
												const newScrollTop = scrollElement.scrollTop;
												return {{
													success: true,
													oldScrollTop: oldScrollTop,
													newScrollTop: newScrollTop,
													scrolled: newScrollTop - oldScrollTop
												}};
											}}
										}}
										return {{success: false, error: 'Could not access iframe content'}};
									}} catch (e) {{
										return {{success: false, error: e.toString()}};
									}}
								}}
							""",
							'objectId': js_object_id,
							'returnByValue': True,
						},
						session_id=cdp_connection.session_id,
					)

					if scroll_result and 'result' in scroll_result and 'value' in scroll_result['result']:
						scroll_data = scroll_result['result']['value']
						if scroll_data.get('success'):
							self.logger.debug(f'Successfully scrolled iframe content by {scroll_data.get("scrolled", 0)}px')
							return True
						else:
							self.logger.debug(f'Failed to scroll iframe: {scroll_data.get("error", "Unknown error")}')

			# Для элементов, не являющихся iframe, использовать стандартный подход с колесом мыши
			# Получить границы элемента, чтобы знать, где прокручивать
			node_backend_id = dom_node.backend_node_id
			element_box_model = await cdp_connection.cdp_client.send.DOM.getBoxModel(
				params={'backendNodeId': node_backend_id}, session_id=cdp_connection.session_id
			)
			content_quad_coords = element_box_model['model']['content']

			# Вычислить центральную точку
			center_x_coord = (content_quad_coords[0] + content_quad_coords[2] + content_quad_coords[4] + content_quad_coords[6]) / 4
			center_y_coord = (content_quad_coords[1] + content_quad_coords[3] + content_quad_coords[5] + content_quad_coords[7]) / 4

			# Отправить событие колеса мыши в месте расположения элемента
			await cdp_connection.cdp_client.send.Input.dispatchMouseEvent(
				params={
					'type': 'mouseWheel',
					'x': center_x_coord,
					'y': center_y_coord,
					'deltaX': 0,
					'deltaY': scroll_pixels,
				},
				session_id=cdp_connection.session_id,
			)

			return True
		except Exception as scroll_error:
			self.logger.debug(f'Failed to scroll element container via CDP: {scroll_error}')
			return False

