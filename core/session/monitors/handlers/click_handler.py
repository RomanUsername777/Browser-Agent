"""Обработчик действий браузера - click."""

import asyncio
import base64
import json
import os
from pathlib import Path
import re
from typing import TYPE_CHECKING

import anyio

from core.dom_processing.manager import EnhancedDOMTreeNode
from core.session.events import CoordinateClickRequest, ElementClickRequest, FileDownloadedEvent
from core.session.models import BrowserError, URLNotAllowedError
from core.observability import observe_debug

if TYPE_CHECKING:
	from core.session.monitors.watchdogs.default_action_watchdog import DefaultActionWatchdog


class ClickHandler:
	"""Обработчик click для DefaultActionWatchdog."""

	def __init__(self, watchdog: "DefaultActionWatchdog"):
		"""Инициализация обработчика с ссылкой на watchdog."""
		self.watchdog = watchdog
		self.browser_session = watchdog.browser_session
		self.browser_controller = watchdog.browser_controller
		self.logger = watchdog.logger

	async def on_ElementClickRequest(self, event: ElementClickRequest) -> dict | None:
		"""Обработать запрос клика с CDP."""
		self.logger.debug(f'on_ElementClickRequest called for node {event.node.node_name}, backend_node_id={event.node.backend_node_id}')
		# Сохраняем исходный target_id ДО try блока, чтобы он был доступен в finally
		original_target_id = self.browser_session.agent_focus_target_id if self.browser_session.agent_focus_target_id else None

		try:
			# Проверить, активна ли сессия перед попыткой любых операций
			if not self.browser_session.agent_focus_target_id:
				error_message = 'Cannot execute click: browser session is corrupted (target_id=None). Session may have crashed.'
				self.logger.error(f'{error_message}')
				raise BrowserError(error_message)

			# Использовать предоставленный узел
			dom_node = event.node
			log_index = dom_node.backend_node_id or 'unknown'

			# === ПРЕДОТВРАЩАЕМ ОТКРЫТИЕ В НОВОЙ ВКЛАДКЕ ===
			# Удаляем target="_blank" у элемента и всех дочерних ссылок перед кликом
			if dom_node.backend_node_id:
				try:
					cdp_connection = await self.browser_session.get_or_create_cdp_session(focus=True)
					# Используем контроллер браузера для разрешения узла
					resolved_node = await self.browser_controller.resolve_node(cdp_connection, dom_node.backend_node_id)
					if resolved_node and 'object' in resolved_node:
						js_object_id = resolved_node['object']['objectId']
						# Удаляем target у самого элемента И у всех дочерних ссылок через контроллер
						function_result = await self.browser_controller.call_function_on(
							cdp_connection,
							js_object_id,
							'''function() {
								let removed = 0;
								// Удаляем у самого элемента
								if (this.hasAttribute && this.hasAttribute("target")) {
									this.removeAttribute("target");
									removed++;
								}
								// Удаляем у всех дочерних ссылок
								const links = this.querySelectorAll ? this.querySelectorAll("a[target]") : [];
								links.forEach(link => {
									link.removeAttribute("target");
									removed++;
								});
								// Проверяем родителя - если это ссылка
								if (this.closest) {
									const parentLink = this.closest("a[target]");
									if (parentLink) {
										parentLink.removeAttribute("target");
										removed++;
									}
								}
								return removed;
							}''',
							return_by_value=True
						)
						removed_count = function_result.get('result', {}).get('value', 0) if function_result else 0
						if removed_count > 0:
							self.logger.info(f'🔗 Удалено {removed_count} target="_blank" атрибутов для открытия в той же вкладке')
				except Exception as e:
					self.logger.debug(f'🔗 Не удалось удалить target: {e}')

			# Check if element is a file input (should not be clicked)
			if self.browser_session.is_file_input(dom_node):
				msg = f'Index {log_index} - has an element which opens file upload dialog. To upload files please use a specific function to upload files'
				self.logger.info(f'{msg}')
				# Return validation error instead of raising to avoid ERROR logs
				return {'validation_error': msg}

			# Detect print-related elements and handle them specially
			is_print_element = self._is_print_related_element(dom_node)
			if is_print_element:
				self.logger.info(
					f'🖨️ Detected print button (index {log_index}), generating PDF directly instead of opening dialog...'
				)

				# Instead of clicking, directly generate PDF via CDP
				click_metadata = await self._handle_print_button_click(dom_node)

				if click_metadata and click_metadata.get('pdf_generated'):
					msg = f'Generated PDF: {click_metadata.get("path")}'
					self.logger.info(f'💾 {msg}')
					return click_metadata
				else:
					# Fallback to regular click if PDF generation failed
					self.logger.warning('⚠️ PDF generation failed, falling back to regular click')

			# Perform the actual click using internal implementation
			starting_target_id = original_target_id
			self.logger.debug(f'Calling _click_element_node_impl for backend_node_id={dom_node.backend_node_id}')
			click_metadata = await self._click_element_node_impl(dom_node, starting_target_id=starting_target_id)
			self.logger.debug(f'_click_element_node_impl returned: {click_metadata}')
			download_path = None  # moved to downloads_watchdog.py

			# Check for validation errors - return them without raising to avoid ERROR logs
			if isinstance(click_metadata, dict) and 'validation_error' in click_metadata:
				self.logger.info(f'{click_metadata["validation_error"]}')
				return click_metadata

			# Build success message
			download_path = None  # moved to downloads_watchdog.py
			if download_path:
				msg = f'Downloaded file to {download_path}'
				self.logger.info(f'💾 {msg}')
			else:
				msg = f'Clicked button {dom_node.node_name}: {dom_node.get_all_children_text(max_depth=2)}'
				self.logger.debug(f'🖱️ {msg}')
			self.logger.debug(f'Element xpath: {dom_node.xpath}')

			return click_metadata if isinstance(click_metadata, dict) else None
		except Exception as e:
			raise


	async def on_CoordinateClickRequest(self, event: CoordinateClickRequest) -> dict | None:
		"""Обработать клик по координатам с CDP."""
		try:
			# Проверить, активна ли сессия перед попыткой любых операций
			if not self.browser_session.agent_focus_target_id:
				error_message = 'Cannot execute click: browser session is corrupted (target_id=None). Session may have crashed.'
				self.logger.error(f'{error_message}')
				raise BrowserError(error_message)

			# Если force=True, пропустить проверки безопасности и кликнуть напрямую
			if event.force:
				self.logger.debug(f'Force clicking at coordinates ({event.coordinate_x}, {event.coordinate_y})')
				return await self._click_on_coordinate(event.coordinate_x, event.coordinate_y, force=True)

			# Получить элемент по координатам для проверок безопасности
			dom_element = await self.browser_session.get_dom_element_at_coordinates(event.coordinate_x, event.coordinate_y)
			if dom_element is None:
				# Элемент не найден, кликнуть напрямую
				self.logger.debug(
					f'No element found at coordinates ({event.coordinate_x}, {event.coordinate_y}), proceeding with click anyway'
				)
				return await self._click_on_coordinate(event.coordinate_x, event.coordinate_y, force=False)

			# Проверка безопасности: файловый input
			if self.browser_session.is_file_input(dom_element):
				validation_msg = f'Cannot click at ({event.coordinate_x}, {event.coordinate_y}) - element is a file input. To upload files please use upload_file action'
				self.logger.info(f'{validation_msg}')
				return {'validation_error': validation_msg}

			# Проверка безопасности: элемент select
			element_tag = dom_element.tag_name.lower() if dom_element.tag_name else ''
			if element_tag == 'select':
				validation_msg = f'Cannot click at ({event.coordinate_x}, {event.coordinate_y}) - element is a <select>. Use dropdown_options action instead.'
				self.logger.info(f'{validation_msg}')
				return {'validation_error': validation_msg}

			# Проверка безопасности: элементы, связанные с печатью
			has_print_functionality = self._is_print_related_element(dom_element)
			if has_print_functionality:
				self.logger.info(
					f'🖨️ Detected print button at ({event.coordinate_x}, {event.coordinate_y}), generating PDF directly instead of opening dialog...'
				)
				click_result = await self._handle_print_button_click(dom_element)
				if click_result and click_result.get('pdf_generated'):
					success_message = f'Generated PDF: {click_result.get("path")}'
					self.logger.info(f'💾 {success_message}')
					return click_result
				else:
					self.logger.warning('⚠️ PDF generation failed, falling back to regular click')

			# Все проверки безопасности пройдены, кликнуть по координатам
			return await self._click_on_coordinate(event.coordinate_x, event.coordinate_y, force=False)

		except Exception:
			raise


	async def _click_element_node_impl(self, element_node, starting_target_id=None) -> dict | None:
		"""
		Click an element using pure CDP with multiple fallback methods for getting element geometry.

		Args:
			element_node: The DOM element to click
			starting_target_id: Original target_id before click (for refocus after click)
		"""
		self.logger.debug(f'[_click_element_node_impl] START for backend_node_id={element_node.backend_node_id}')

		try:
			# Check if element is a file input or select dropdown - these should not be clicked
			tag_name = element_node.tag_name.lower() if element_node.tag_name else ''
			element_type = element_node.attributes.get('type', '').lower() if element_node.attributes else ''

			if tag_name == 'select':
				msg = f'Cannot click on <select> elements. Use dropdown_options(index={element_node.backend_node_id}) action instead.'
				# Return error dict instead of raising to avoid ERROR logs
				return {'validation_error': msg}

			if tag_name == 'input' and element_type == 'file':
				msg = f'Cannot click on file input element (index={element_node.backend_node_id}). File uploads must be handled using upload_file_to_element action.'
				# Return error dict instead of raising to avoid ERROR logs
				return {'validation_error': msg}

			# Get CDP client
			self.logger.debug(f'[_click_element_node_impl] Getting CDP client...')
			cdp_session = await self.browser_session.cdp_client_for_node(element_node)
			self.logger.debug(f'[_click_element_node_impl] Got CDP session: {cdp_session.session_id if cdp_session else None}')

			# Get the correct session ID for the element's frame
			session_id = cdp_session.session_id

			# Get element bounds
			backend_node_id = element_node.backend_node_id

			# Get viewport dimensions for visibility checks через контроллер
			self.logger.debug(f'[_click_element_node_impl] Getting layout metrics...')
			layout_metrics = await self.browser_controller.get_layout_metrics(cdp_session)
			self.logger.debug(f'[_click_element_node_impl] Got layout metrics: {layout_metrics.get("layoutViewport", {}).get("clientWidth")}x{layout_metrics.get("layoutViewport", {}).get("clientHeight")}')
			viewport_width = layout_metrics['layoutViewport']['clientWidth']
			viewport_height = layout_metrics['layoutViewport']['clientHeight']

			# Прокрутить элемент в видимую область СНАЧАЛА перед получением координат через контроллер
			try:
				self.logger.debug(f'[_click_element_node_impl] Scrolling into view...')
				await self.browser_controller.scroll_into_view(cdp_session, backend_node_id)
				await asyncio.sleep(0.05)  # Подождать завершения прокрутки
				self.logger.debug(f'[_click_element_node_impl] Scrolled element into view')
			except Exception as scroll_error:
				self.logger.debug(f'[_click_element_node_impl] Failed to scroll: {scroll_error}')

			# Получить координаты элемента используя унифицированный метод ПОСЛЕ прокрутки
			self.logger.debug(f'[_click_element_node_impl] Getting element coordinates...')
			element_bbox = await self.browser_session.get_element_coordinates(backend_node_id, cdp_session)
			self.logger.debug(f'[_click_element_node_impl] Got element_bbox: {element_bbox}')

			# Преобразовать rect в формат quads, если получили координаты
			quad_list = []
			if element_bbox:
				# Преобразовать DOMRect в формат quad
				bbox_x, bbox_y, bbox_width, bbox_height = element_bbox.x, element_bbox.y, element_bbox.width, element_bbox.height
				quad_list = [
					[
						bbox_x,
						bbox_y,  # top-left
						bbox_x + bbox_width,
						bbox_y,  # top-right
						bbox_x + bbox_width,
						bbox_y + bbox_height,  # bottom-right
						bbox_x,
						bbox_y + bbox_height,  # bottom-left
					]
				]
				self.logger.debug(
					f'Got coordinates from unified method: {element_bbox.x}, {element_bbox.y}, {element_bbox.width}x{element_bbox.height}'
				)

			# Если все еще нет quads, использовать запасной вариант JS клика
			if not quad_list:
				self.logger.warning('Could not get element geometry from any method, falling back to JavaScript click')
				try:
					resolve_result = await cdp_session.cdp_client.send.DOM.resolveNode(
						params={'backendNodeId': backend_node_id},
						session_id=session_id,
					)
					assert 'object' in resolve_result and 'objectId' in resolve_result['object'], (
						'Failed to find DOM element based on backendNodeId, maybe page content changed?'
					)
					js_object_id = resolve_result['object']['objectId']

					# Улучшенная симуляция клика для React/Vue компонентов
					await cdp_session.cdp_client.send.Runtime.callFunctionOn(
						params={
							'functionDeclaration': '''function() {
								const rect = this.getBoundingClientRect();
								const x = rect.left + rect.width / 2;
								const y = rect.top + rect.height / 2;
								const eventInit = {bubbles: true, cancelable: true, view: window, clientX: x, clientY: y};

								// Focus element if focusable
								if (this.focus) this.focus();

								// Simulate full mouse event sequence for React/Vue
								this.dispatchEvent(new MouseEvent('mouseenter', eventInit));
								this.dispatchEvent(new MouseEvent('mouseover', eventInit));
								this.dispatchEvent(new MouseEvent('mousedown', {...eventInit, button: 0}));
								this.dispatchEvent(new MouseEvent('mouseup', {...eventInit, button: 0}));
								this.dispatchEvent(new MouseEvent('click', {...eventInit, button: 0}));

								// Also try native click as backup
								if (this.click) this.click();
							}''',
							'objectId': js_object_id,
						},
						session_id=session_id,
					)
					await asyncio.sleep(0.1)
					# Navigation is handled by ChromeSession via events
					return None
				except Exception as js_e:
					self.logger.warning(f'CDP JavaScript click also failed: {js_e}')
					if 'No node with given id found' in str(js_e):
						raise Exception('Element with given id not found')
					else:
						raise Exception(f'Failed to click element: {js_e}')

			# Найти самый большой видимый quad в пределах viewport
			selected_quad = None
			max_visible_area = 0

			for quad_coords in quad_list:
				if len(quad_coords) < 8:
					continue

				# Вычислить границы quad
				x_coordinates = [quad_coords[i] for i in range(0, 8, 2)]
				y_coordinates = [quad_coords[i] for i in range(1, 8, 2)]
				x_min, x_max = min(x_coordinates), max(x_coordinates)
				y_min, y_max = min(y_coordinates), max(y_coordinates)

				# Проверить, пересекается ли quad с viewport
				if x_max < 0 or y_max < 0 or x_min > viewport_width or y_min > viewport_height:
					continue  # Quad полностью вне viewport

				# Вычислить видимую область (пересечение с viewport)
				visible_x_min = max(0, x_min)
				visible_x_max = min(viewport_width, x_max)
				visible_y_min = max(0, y_min)
				visible_y_max = min(viewport_height, y_max)

				visible_width_value = visible_x_max - visible_x_min
				visible_height_value = visible_y_max - visible_y_min
				visible_area_value = visible_width_value * visible_height_value

				if visible_area_value > max_visible_area:
					max_visible_area = visible_area_value
					selected_quad = quad_coords

			if not selected_quad:
				# Видимый quad не найден, использовать первый quad в любом случае
				selected_quad = quad_list[0]
				self.logger.warning('No visible quad found, using first quad')

			# Вычислить центральную точку лучшего quad
			click_x = sum(selected_quad[i] for i in range(0, 8, 2)) / 4
			click_y = sum(selected_quad[i] for i in range(1, 8, 2)) / 4

			# Убедиться, что точка клика находится в пределах границ viewport
			click_x = max(0, min(viewport_width - 1, click_x))
			click_y = max(0, min(viewport_height - 1, click_y))

			# Проверить на перекрытие перед попыткой CDP клика
			is_occluded = await self._check_element_occlusion(backend_node_id, click_x, click_y, cdp_session)

			if is_occluded:
				self.logger.debug('🚫 Element is occluded, falling back to JavaScript click')
				try:
					resolve_result = await self.browser_controller.resolve_node(cdp_session, backend_node_id)
					assert resolve_result and 'object' in resolve_result and 'objectId' in resolve_result['object'], (
						'Failed to find DOM element based on backendNodeId'
					)
					js_object_id = resolve_result['object']['objectId']

					# Улучшенная симуляция клика для React/Vue компонентов
					await cdp_session.cdp_client.send.Runtime.callFunctionOn(
						params={
							'functionDeclaration': '''function() {
								const rect = this.getBoundingClientRect();
								const x = rect.left + rect.width / 2;
								const y = rect.top + rect.height / 2;
								const eventInit = {bubbles: true, cancelable: true, view: window, clientX: x, clientY: y};
								
								if (this.focus) this.focus();
								this.dispatchEvent(new MouseEvent('mouseenter', eventInit));
								this.dispatchEvent(new MouseEvent('mouseover', eventInit));
								this.dispatchEvent(new MouseEvent('mousedown', {...eventInit, button: 0}));
								this.dispatchEvent(new MouseEvent('mouseup', {...eventInit, button: 0}));
								this.dispatchEvent(new MouseEvent('click', {...eventInit, button: 0}));
								if (this.click) this.click();
							}''',
							'objectId': js_object_id,
						},
						session_id=session_id,
					)
					await asyncio.sleep(0.1)
					return None
				except Exception as js_error:
					self.logger.error(f'JavaScript click fallback failed: {js_error}')
					raise Exception(f'Failed to click occluded element: {js_error}')

			# Выполнить клик используя CDP (элемент не перекрыт)
			self.logger.debug(f'[_click_element_node_impl] About to click at ({click_x}, {click_y})')
			try:
				self.logger.debug(f'👆 Dragging mouse over element before clicking x: {click_x}px y: {click_y}px ...')
				# Переместить мышь к элементу
				await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
					params={
						'type': 'mouseMoved',
						'x': click_x,
						'y': click_y,
					},
					session_id=session_id,
				)
				await asyncio.sleep(0.05)

				# Нажатие мыши
				self.logger.debug(f'👆🏾 Clicking x: {click_x}px y: {click_y}px ...')
				try:
					await asyncio.wait_for(
						cdp_session.cdp_client.send.Input.dispatchMouseEvent(
							params={
								'type': 'mousePressed',
								'x': click_x,
								'y': click_y,
								'button': 'left',
								'clickCount': 1,
							},
							session_id=session_id,
						),
						timeout=3.0,  # 3 секунды таймаут для mousePressed
					)
					await asyncio.sleep(0.08)
				except TimeoutError:
					self.logger.debug('⏱️ Mouse down timed out (likely due to dialog), continuing...')
					# Не спать, если таймаут

				# Отпускание мыши
				try:
					await asyncio.wait_for(
						cdp_session.cdp_client.send.Input.dispatchMouseEvent(
							params={
								'type': 'mouseReleased',
								'x': click_x,
								'y': click_y,
								'button': 'left',
								'clickCount': 1,
							},
							session_id=session_id,
						),
						timeout=5.0,  # 5 секунд таймаут для mouseReleased
					)
				except TimeoutError:
					self.logger.debug('⏱️ Mouse up timed out (possibly due to lag or dialog popup), continuing...')

				self.logger.debug(f'[_click_element_node_impl] Clicked successfully at ({click_x}, {click_y})')

				# Вернуть координаты как словарь для метаданных
				return {'click_x': click_x, 'click_y': click_y}

			except Exception as click_error:
				self.logger.warning(f'CDP click failed: {type(click_error).__name__}: {click_error}')
				# Запасной вариант: JavaScript клик через CDP
				try:
					resolve_result = await cdp_session.cdp_client.send.DOM.resolveNode(
						params={'backendNodeId': backend_node_id},
						session_id=session_id,
					)
					assert 'object' in resolve_result and 'objectId' in resolve_result['object'], (
						'Failed to find DOM element based on backendNodeId, maybe page content changed?'
					)
					js_object_id = resolve_result['object']['objectId']

					# Улучшенная симуляция клика для React/Vue компонентов
					await cdp_session.cdp_client.send.Runtime.callFunctionOn(
						params={
							'functionDeclaration': '''function() {
								const rect = this.getBoundingClientRect();
								const x = rect.left + rect.width / 2;
								const y = rect.top + rect.height / 2;
								const eventInit = {bubbles: true, cancelable: true, view: window, clientX: x, clientY: y};
								
								if (this.focus) this.focus();
								this.dispatchEvent(new MouseEvent('mouseenter', eventInit));
								this.dispatchEvent(new MouseEvent('mouseover', eventInit));
								this.dispatchEvent(new MouseEvent('mousedown', {...eventInit, button: 0}));
								this.dispatchEvent(new MouseEvent('mouseup', {...eventInit, button: 0}));
								this.dispatchEvent(new MouseEvent('click', {...eventInit, button: 0}));
								if (this.click) this.click();
							}''',
							'objectId': js_object_id,
						},
						session_id=session_id,
					)

					# Небольшая задержка для закрытия диалога
					await asyncio.sleep(0.1)

					return None
				except Exception as js_error:
					self.logger.warning(f'CDP JavaScript click also failed: {js_error}')
					raise Exception(f'Failed to click element: {click_error}')
			finally:
				# Всегда повторно фокусироваться обратно на исходный контекст сессии верхнего уровня страницы на случай, если клик открыл новую вкладку/всплывающее окно/окно/диалог и т.д.
				# Использовать таймаут, чтобы предотвратить зависание, если диалог блокирует
				# КРИТИЧНО: Использовать starting_target_id для возврата к ИСХОДНОЙ вкладке, а не к текущему agent_focus_target_id
				# который мог быть переключен на новую вкладку кликом
				if starting_target_id:
					try:
						refocus_session = await asyncio.wait_for(
							self.browser_session.get_or_create_cdp_session(target_id=starting_target_id, focus=True),
							timeout=3.0
						)
						await asyncio.wait_for(
							self.browser_controller.run_if_waiting_for_debugger(refocus_session),
							timeout=2.0,
						)
					except TimeoutError:
						self.logger.debug('⏱️ Refocus after click timed out (page may be blocked by dialog). Continuing...')
					except Exception as refocus_error:
						self.logger.debug(f'⚠️ Refocus error (non-critical): {type(refocus_error).__name__}: {refocus_error}')

		except URLNotAllowedError as url_error:
			raise url_error
		except BrowserError as browser_error:
			raise browser_error
		except Exception as click_exception:
			# Извлечь ключевую информацию об элементе для сообщения об ошибке
			element_info_str = f'<{element_node.tag_name or "unknown"}'
			if element_node.backend_node_id:
				element_info_str += f' index={element_node.backend_node_id}'
			element_info_str += '>'

			# Создать полезное сообщение об ошибке на основе контекста
			error_detail_text = f'Failed to click element {element_info_str}. The element may not be interactable or visible.'

			# Добавить подсказку, если элемент имеет index (часто в режиме code-use)
			if element_node.backend_node_id:
				error_detail_text += f' If the page changed after navigation/interaction, the index [{element_node.backend_node_id}] may be stale. Get fresh browser state before retrying.'

			raise BrowserError(
				message=f'Failed to click element: {str(click_exception)}',
				long_term_memory=error_detail_text,
			)


	async def _click_on_coordinate(self, click_x: int, click_y: int, force: bool = False) -> dict | None:
		"""
		Кликнуть напрямую по координатам используя CDP Input.dispatchMouseEvent.

		Args:
			click_x: X координата в viewport
			click_y: Y координата в viewport
			force: Если True, пропустить все проверки безопасности (используется когда force=True в событии)

		Returns:
			Словарь с координатами клика или None
		"""
		try:
			# Получить CDP сессию
			cdp_connection = await self.browser_session.get_or_create_cdp_session()
			connection_session_id = cdp_connection.session_id

			self.logger.debug(f'👆 Moving mouse to ({click_x}, {click_y})...')

			# Переместить мышь к координатам
			await cdp_connection.cdp_client.send.Input.dispatchMouseEvent(
				params={
					'type': 'mouseMoved',
					'x': click_x,
					'y': click_y,
				},
				session_id=connection_session_id,
			)
			await asyncio.sleep(0.05)

			# Нажатие мыши
			self.logger.debug(f'👆🏾 Clicking at ({click_x}, {click_y})...')
			try:
				await asyncio.wait_for(
					cdp_connection.cdp_client.send.Input.dispatchMouseEvent(
						params={
							'type': 'mousePressed',
							'x': click_x,
							'y': click_y,
							'button': 'left',
							'clickCount': 1,
						},
						session_id=connection_session_id,
					),
					timeout=3.0,
				)
				await asyncio.sleep(0.05)
			except TimeoutError:
				self.logger.debug('⏱️ Mouse down timed out (likely due to dialog), continuing...')

			# Отпускание мыши
			try:
				await asyncio.wait_for(
					cdp_connection.cdp_client.send.Input.dispatchMouseEvent(
						params={
							'type': 'mouseReleased',
							'x': click_x,
							'y': click_y,
							'button': 'left',
							'clickCount': 1,
						},
						session_id=connection_session_id,
					),
					timeout=5.0,
				)
			except TimeoutError:
				self.logger.debug('⏱️ Mouse up timed out (possibly due to lag or dialog popup), continuing...')

			self.logger.debug(f'🖱️ Clicked successfully at ({click_x}, {click_y})')

			# Вернуть координаты как метаданные
			return {'click_x': click_x, 'click_y': click_y}

		except Exception as coordinate_error:
			self.logger.error(f'Failed to click at coordinates ({click_x}, {click_y}): {type(coordinate_error).__name__}: {coordinate_error}')
			raise BrowserError(
				message=f'Failed to click at coordinates: {coordinate_error}',
				long_term_memory=f'Failed to click at coordinates ({click_x}, {click_y}). The coordinates may be outside viewport or the page may have changed.',
			)


	async def _check_element_occlusion(self, backend_node_id: int, x: float, y: float, cdp_connection) -> bool:
		"""Проверить, перекрыт ли элемент другими элементами в указанных координатах.

		Args:
			backend_node_id: Backend node ID целевого элемента
			x: X координата для проверки
			y: Y координата для проверки
			cdp_connection: CDP сессия для использования

		Returns:
			True если элемент перекрыт, False если кликабелен
		"""
		try:
			connection_session_id = cdp_connection.session_id

			# Получить информацию о целевом элементе для сравнения через контроллер
			resolve_result = await self.browser_controller.resolve_node(cdp_connection, backend_node_id)

			if not resolve_result or 'object' not in resolve_result:
				self.logger.debug('Could not resolve target element, assuming occluded')
				return True

			js_object_id = resolve_result['object']['objectId']

			# Получить информацию о целевом элементе через контроллер
			element_info_result = await self.browser_controller.call_function_on(
				cdp_connection,
				js_object_id,
				"""
				function() {
					const getElementInfo = (el) => {
						return {
							tagName: el.tagName,
							id: el.id || '',
							className: el.className || '',
							textContent: (el.textContent || '').substring(0, 100)
						};
					}


					const elementAtPoint = document.elementFromPoint(arguments[0], arguments[1]);
					if (!elementAtPoint) {
						return { targetInfo: getElementInfo(this), isClickable: false };
					}


					// Simple containment-based clickability logic
					const isClickable = this === elementAtPoint ||
						this.contains(elementAtPoint) ||
						elementAtPoint.contains(this);

					return {
						targetInfo: getElementInfo(this),
						elementAtPointInfo: getElementInfo(elementAtPoint),
						isClickable: isClickable
					};
				}
				""",
				return_by_value=True,
				arguments=[{'value': x}, {'value': y}]
			)

			if 'result' not in element_info_result or 'value' not in element_info_result['result']:
				self.logger.debug('Could not get target element info, assuming occluded')
				return True

			occlusion_data = element_info_result['result']['value']
			element_clickable = occlusion_data.get('isClickable', False)

			if element_clickable:
				self.logger.debug('Element is clickable (target, contained, or semantically related)')
				return False
			else:
				target_element_info = occlusion_data.get('targetInfo', {})
				point_element_info = occlusion_data.get('elementAtPointInfo', {})
				self.logger.debug(
					f'Element is occluded. Target: {target_element_info.get("tagName", "unknown")} '
					f'(id={target_element_info.get("id", "none")}), '
					f'ElementAtPoint: {point_element_info.get("tagName", "unknown")} '
					f'(id={point_element_info.get("id", "none")})'
				)
				return True

		except Exception as occlusion_error:
			self.logger.debug(f'Occlusion check failed: {occlusion_error}, assuming not occluded')
			return False


	def _is_print_related_element(self, element_node: EnhancedDOMTreeNode) -> bool:
		"""Проверить, связан ли элемент с печатью (кнопки печати, диалоги печати и т.д.).

		Основная проверка: атрибут onclick (наиболее надежно для обнаружения печати)
		Запасной вариант: текст/значение кнопки (для случаев без onclick)
		"""
		# Основная проверка: атрибут onclick для функций, связанных с печатью (наиболее надежно)
		onclick_attr = element_node.attributes.get('onclick', '').lower() if element_node.attributes else ''
		if onclick_attr and 'print' in onclick_attr:
			# Соответствует: window.print(), PrintElem(), print() и т.д.
			return True

		return False


	async def _handle_print_button_click(self, element_node: EnhancedDOMTreeNode) -> dict | None:
		"""Обработать кнопку печати, напрямую генерируя PDF через CDP вместо открытия диалога.

		Returns:
			Словарь метаданных с путем загрузки в случае успеха, None в противном случае
		"""
		try:
			import base64
			import os
			from pathlib import Path

			# Получить CDP сессию
			cdp_connection = await self.browser_session.get_or_create_cdp_session(focus=True)

			# Сгенерировать PDF используя контроллер браузера
			pdf_result = await asyncio.wait_for(
				self.browser_controller.generate_pdf(cdp_connection),
				timeout=15.0,  # 15 секунд таймаут для генерации PDF
			)

			pdf_base64 = pdf_result.get('data')
			if not pdf_base64:
				self.logger.warning('⚠️ PDF generation returned no data')
				return None

			# Декодировать base64 данные PDF
			decoded_pdf = base64.b64decode(pdf_base64)

			# Получить путь загрузок
			download_directory = self.browser_session.browser_profile.downloads_path
			if not download_directory:
				self.logger.warning('⚠️ No downloads path configured, cannot save PDF')
				return None

			# Сгенерировать имя файла из заголовка страницы или URL
			try:
				title = await asyncio.wait_for(self.browser_session.get_current_page_title(), timeout=2.0)
				# Очистить заголовок для имени файла
				import re

				clean_title = re.sub(r'[^\w\s-]', '', title)[:50]  # Максимум 50 символов
				output_filename = f'{clean_title}.pdf' if clean_title else 'print.pdf'
			except Exception:
				output_filename = 'print.pdf'

			# Убедиться, что директория загрузок существует
			downloads_directory = Path(download_directory).expanduser().resolve()
			downloads_directory.mkdir(parents=True, exist_ok=True)

			# Сгенерировать уникальное имя файла, если файл существует
			save_path = downloads_directory / output_filename
			if save_path.exists():
				base_name, file_ext = os.path.splitext(output_filename)
				file_counter = 1
				while (downloads_directory / f'{base_name} ({file_counter}){file_ext}').exists():
					file_counter += 1
				save_path = downloads_directory / f'{base_name} ({file_counter}){file_ext}'

			# Записать PDF в файл
			import anyio

			async with await anyio.open_file(save_path, 'wb') as pdf_file:
				await pdf_file.write(decoded_pdf)

			file_size_bytes = save_path.stat().st_size
			self.logger.info(f'✅ Generated PDF via CDP: {save_path} ({file_size_bytes:,} bytes)')

			# Отправить FileDownloadedEvent
			current_url = await self.browser_session.get_current_page_url()
			self.browser_session.event_bus.dispatch(
				FileDownloadedEvent(
					url=current_url,
					path=str(save_path),
					file_name=save_path.name,
					file_size=file_size_bytes,
					file_type='pdf',
					mime_type='application/pdf',
					auto_download=False,  # Это было намеренно (пользователь нажал печать)
				)
			)

			return {'pdf_generated': True, 'path': str(save_path)}

		except TimeoutError:
			self.logger.warning('⏱️ PDF generation timed out')
			return None
		except Exception as pdf_error:
			self.logger.warning(f'⚠️ Failed to generate PDF via CDP: {type(pdf_error).__name__}: {pdf_error}')
			return None
