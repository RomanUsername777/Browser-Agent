"""Обработчик действий браузера - text_input."""

import asyncio
import json
from typing import TYPE_CHECKING

from core.dom_processing.manager import EnhancedDOMTreeNode
from core.session.events import TypeTextEvent
from core.session.models import BrowserError, URLNotAllowedError
from core.observability import observe_debug
from cdp_use.cdp.input.commands import DispatchKeyEventParameters

if TYPE_CHECKING:
	from core.session.monitors.watchdogs.default_action_watchdog import DefaultActionWatchdog


class TextInputHandler:
	"""Обработчик text_input для DefaultActionWatchdog."""

	def __init__(self, watchdog: "DefaultActionWatchdog"):
		"""Инициализация обработчика с ссылкой на watchdog."""
		self.watchdog = watchdog
		self.browser_session = watchdog.browser_session
		self.browser_controller = watchdog.browser_controller
		self.logger = watchdog.logger

	async def on_TypeTextEvent(self, event: TypeTextEvent) -> dict | None:
		"""Обработать запрос ввода текста с CDP."""
		try:
			# Использовать предоставленный узел
			dom_node = event.node
			log_index = dom_node.backend_node_id or 'unknown'

			# Проверить, является ли это индексом 0 или ложным индексом - ввод на страницу (что бы ни имело фокус)
			if not dom_node.backend_node_id or dom_node.backend_node_id == 0:
				# Ввод на страницу без фокусировки на конкретном элементе
				await self._type_to_page(event.text)
				# Логировать с защитой чувствительных данных
				if event.is_sensitive:
					if event.sensitive_key_name:
						self.logger.info(f'⌨️ Typed <{event.sensitive_key_name}> to the page (current focus)')
					else:
						self.logger.info('⌨️ Typed <sensitive> to the page (current focus)')
				else:
					self.logger.info(f'⌨️ Typed "{event.text}" to the page (current focus)')
				return None  # Координаты недоступны для ввода на страницу
			else:
				try:
					# Попытаться ввести текст в конкретный элемент
					text_input_result = await self._input_text_element_node_impl(
						dom_node,
						event.text,
						clear=event.clear or (not event.text),
						is_sensitive=event.is_sensitive,
					)
					# Логировать с защитой чувствительных данных
					if event.is_sensitive:
						if event.sensitive_key_name:
							self.logger.info(f'⌨️ Typed <{event.sensitive_key_name}> into element with index {log_index}')
						else:
							self.logger.info(f'⌨️ Typed <sensitive> into element with index {log_index}')
					else:
						self.logger.info(f'⌨️ Typed "{event.text}" into element with index {log_index}')
					self.logger.debug(f'Element xpath: {dom_node.xpath}')
					return text_input_result  # Вернуть координаты, если доступны
				except Exception as type_error:
					# Элемент не найден или ошибка - запасной вариант: ввод на страницу
					self.logger.warning(f'Failed to type to element {log_index}: {type_error}. Falling back to page typing.')
					try:
						await asyncio.wait_for(self._click_element_node_impl(dom_node), timeout=10.0)
					except Exception as click_error:
						pass
					await self._type_to_page(event.text)
					# Логировать с защитой чувствительных данных
					if event.is_sensitive:
						if event.sensitive_key_name:
							self.logger.info(f'⌨️ Typed <{event.sensitive_key_name}> to the page as fallback')
						else:
							self.logger.info('⌨️ Typed <sensitive> to the page as fallback')
					else:
						self.logger.info(f'⌨️ Typed "{event.text}" to the page as fallback')
					return None  # Координаты недоступны для запасного варианта ввода

			# Примечание: Мы не очищаем кэшированное состояние здесь - позволим multi_act обработать обнаружение изменений DOM
			# путем явной перестройки и сравнения при необходимости
		except Exception as text_error:
			raise


	async def _input_text_element_node_impl(
		self, dom_node: EnhancedDOMTreeNode, text: str, clear: bool = True, is_sensitive: bool = False
		) -> dict | None:
		"""
		Ввести текст в элемент используя чистый CDP с улучшенными запасными вариантами фокусировки.

		Для date/time inputs использует прямое присваивание значения вместо ввода.
		"""

		try:
			# Получить CDP client
			cdp_client_instance = self.browser_session.cdp_client

			# Получить правильный session ID для iframe элемента
			# session_id = await self._get_session_id_for_element(dom_node)

			# cdp_connection = await self.browser_session.get_or_create_cdp_session(target_id=dom_node.target_id, focus=True)
			cdp_connection = await self.browser_session.cdp_client_for_node(dom_node)

			# Получить информацию об элементе
			node_backend_id = dom_node.backend_node_id

			# Отслеживать координаты для метаданных
			input_coordinates = None

			# Прокрутить элемент в видимую область
			try:
				await cdp_connection.cdp_client.send.DOM.scrollIntoViewIfNeeded(
					params={'backendNodeId': node_backend_id}, session_id=cdp_connection.session_id
				)
				await asyncio.sleep(0.01)
			except Exception as scroll_error:
				# Ошибки отсоединения узла распространены с shadow DOM и динамическим контентом
				# Элемент все еще может быть использован для взаимодействия, даже если прокрутка не удалась
				error_message = str(scroll_error)
				if 'Node is detached from document' in error_message or 'detached from document' in error_message:
					self.logger.debug(
						f'Element node temporarily detached during scroll (common with shadow DOM), continuing: {dom_node}'
					)
				else:
					self.logger.debug(f'Failed to scroll element {dom_node} into view before typing: {type(scroll_error).__name__}: {scroll_error}')

			# Получить object ID для элемента
			resolve_result = await cdp_client_instance.send.DOM.resolveNode(
				params={'backendNodeId': node_backend_id},
				session_id=cdp_connection.session_id,
			)
			assert 'object' in resolve_result and 'objectId' in resolve_result['object'], (
				'Failed to find DOM element based on backendNodeId, maybe page content changed?'
			)
			js_object_id = resolve_result['object']['objectId']

			# Получить текущие координаты используя унифицированный метод
			element_coords = await self.browser_session.get_element_coordinates(node_backend_id, cdp_connection)
			if element_coords:
				center_x_coord = element_coords.x + element_coords.width / 2
				center_y_coord = element_coords.y + element_coords.height / 2

				# Проверить на перекрытие перед использованием координат для фокусировки
				is_occluded = await self._check_element_occlusion(node_backend_id, center_x_coord, center_y_coord, cdp_connection)

				if is_occluded:
					self.logger.debug('🚫 Input element is occluded, skipping coordinate-based focus')
					input_coordinates = None  # Принудительно использовать запасной вариант только CDP фокусировки
				else:
					input_coordinates = {'input_x': center_x_coord, 'input_y': center_y_coord}
					self.logger.debug(f'Using unified coordinates: x={center_x_coord:.1f}, y={center_y_coord:.1f}')
			else:
				input_coordinates = None
				self.logger.debug('No coordinates found for element')

			# Убедиться, что у нас есть валидный js_object_id перед продолжением
			if not js_object_id:
				raise ValueError('Could not get js_object_id for element')

			# Шаг 1: Сфокусировать элемент используя простую стратегию
			focused_successfully = await self._focus_element_simple(
				backend_node_id=node_backend_id, js_object_id=js_object_id, cdp_connection=cdp_connection, input_coordinates=input_coordinates
			)

			# Шаг 2: Проверить, требует ли этот элемент прямого присваивания значения (date/time inputs)
			requires_direct_assignment = self._requires_direct_value_assignment(dom_node)

			if requires_direct_assignment:
				# Date/time inputs: использовать прямое присваивание значения вместо ввода
				self.logger.debug(
					f'🎯 Element type={dom_node.attributes.get("type")} requires direct value assignment, setting value directly'
				)
				await self._set_value_directly(dom_node, text, js_object_id, cdp_connection)

				# Вернуть координаты ввода для метаданных
				return input_coordinates

			# Шаг 3: Очистить существующий текст, если запрошено (только для обычных inputs, которые поддерживают ввод)
			if clear:
				cleared_successfully = await self._clear_text_field(js_object_id=js_object_id, cdp_connection=cdp_connection)
				if not cleared_successfully:
					self.logger.warning('⚠️ Text field clearing failed, typing may append to existing text')

			# Шаг 4: Ввести текст посимвольно используя правильные события клавиш, похожие на человеческие
			# Это точно имитирует то, как человек печатает, что ожидают современные веб-сайты
			if is_sensitive:
				# Примечание: sensitive_key_name не передается в этот низкоуровневый метод,
				# но мы могли бы расширить сигнатуру, если нужно для более детального логирования
				self.logger.debug('🎯 Typing <sensitive> character by character')
			else:
				self.logger.debug(f'🎯 Typing text character by character: "{text}"')

			for char_index, character in enumerate(text):
				# Обработать символы новой строки как клавишу Enter
				if character == '\n':
					# Отправить правильную последовательность клавиши Enter
					await cdp_connection.cdp_client.send.Input.dispatchKeyEvent(
						params={
							'type': 'keyDown',
							'key': 'Enter',
							'code': 'Enter',
							'windowsVirtualKeyCode': 13,
						},
						session_id=cdp_connection.session_id,
					)

					# Небольшая задержка для имитации скорости человеческой печати
					await asyncio.sleep(0.001)

					# Отправить событие char с возвратом каретки
					await cdp_connection.cdp_client.send.Input.dispatchKeyEvent(
						params={
							'type': 'char',
							'text': '\r',
							'key': 'Enter',
						},
						session_id=cdp_connection.session_id,
					)

					# Отправить событие keyUp
					await cdp_connection.cdp_client.send.Input.dispatchKeyEvent(
						params={
							'type': 'keyUp',
							'key': 'Enter',
							'code': 'Enter',
							'windowsVirtualKeyCode': 13,
						},
						session_id=cdp_connection.session_id,
					)
				else:
					# Обработать обычные символы
					# Получить правильные модификаторы, VK код и базовую клавишу для символа
					modifier_keys, virtual_key_code, base_key_name = self._get_char_modifiers_and_vk(character)
					key_code_value = self._get_key_code_for_char(base_key_name)

					# self.logger.debug(f'🎯 Typing character {char_index + 1}/{len(text)}: "{character}" (base_key: {base_key_name}, code: {key_code_value}, modifiers: {modifier_keys}, vk: {virtual_key_code})')

					# Шаг 1: Отправить событие keyDown (БЕЗ параметра text)
					await cdp_connection.cdp_client.send.Input.dispatchKeyEvent(
						params={
							'type': 'keyDown',
							'key': base_key_name,
							'code': key_code_value,
							'modifiers': modifier_keys,
							'windowsVirtualKeyCode': virtual_key_code,
						},
						session_id=cdp_connection.session_id,
					)

					# Небольшая задержка для имитации скорости человеческой печати
					await asyncio.sleep(0.005)

					# Шаг 2: Отправить событие char (С параметром text) - это критично для ввода текста
					await cdp_connection.cdp_client.send.Input.dispatchKeyEvent(
						params={
							'type': 'char',
							'text': character,
							'key': character,
						},
						session_id=cdp_connection.session_id,
					)

					# Шаг 3: Отправить событие keyUp (БЕЗ параметра text)
					await cdp_connection.cdp_client.send.Input.dispatchKeyEvent(
						params={
							'type': 'keyUp',
							'key': base_key_name,
							'code': key_code_value,
							'modifiers': modifier_keys,
							'windowsVirtualKeyCode': virtual_key_code,
						},
						session_id=cdp_connection.session_id,
					)

				# Небольшая задержка между символами, чтобы выглядеть по-человечески (реалистичная скорость печати)
				await asyncio.sleep(0.001)

			# Шаг 5: Запустить события DOM, осведомленные о фреймворках, после завершения ввода
			# Современные JavaScript фреймворки (React, Vue, Angular) полагаются на эти события
			# для обновления их внутреннего состояния и запуска повторных рендеров
			await self._trigger_framework_events(js_object_id=js_object_id, cdp_connection=cdp_connection)

			# Вернуть координаты метаданных, если доступны
			return input_coordinates

		except Exception as input_error:
			self.logger.error(f'Failed to input text via CDP: {type(input_error).__name__}: {input_error}')
			raise BrowserError(f'Failed to input text into element: {repr(dom_node)}')


	async def _type_to_page(self, text: str):
		"""
		Ввести текст на страницу (в любой элемент, который имеет фокус в данный момент).
		Используется когда index равен 0 или когда элемент не может быть найден.
		"""
		try:
			# Получить CDP client и session
			cdp_connection = await self.browser_session.get_or_create_cdp_session(target_id=None, focus=True)

			# Ввести текст посимвольно в элемент с фокусом
			for character in text:
				# Обработать символы новой строки как клавишу Enter
				if character == '\n':
					# Отправить правильную последовательность клавиши Enter
					await cdp_connection.cdp_client.send.Input.dispatchKeyEvent(
						params={
							'type': 'keyDown',
							'key': 'Enter',
							'code': 'Enter',
							'windowsVirtualKeyCode': 13,
						},
						session_id=cdp_connection.session_id,
					)
					# Отправить событие char с возвратом каретки
					await cdp_connection.cdp_client.send.Input.dispatchKeyEvent(
						params={
							'type': 'char',
							'text': '\r',
						},
						session_id=cdp_connection.session_id,
					)
					# Отправить keyup
					await cdp_connection.cdp_client.send.Input.dispatchKeyEvent(
						params={
							'type': 'keyUp',
							'key': 'Enter',
							'code': 'Enter',
							'windowsVirtualKeyCode': 13,
						},
						session_id=cdp_connection.session_id,
					)
				else:
					# Обработать обычные символы
					# Отправить keydown
					await cdp_connection.cdp_client.send.Input.dispatchKeyEvent(
						params={
							'type': 'keyDown',
							'key': character,
						},
						session_id=cdp_connection.session_id,
					)
					# Отправить char для реального ввода текста
					await cdp_connection.cdp_client.send.Input.dispatchKeyEvent(
						params={
							'type': 'char',
							'text': character,
						},
						session_id=cdp_connection.session_id,
					)
					# Отправить keyup
					await cdp_connection.cdp_client.send.Input.dispatchKeyEvent(
						params={
							'type': 'keyUp',
							'key': character,
						},
						session_id=cdp_connection.session_id,
					)
				# Добавить задержку 10ms между нажатиями клавиш
				await asyncio.sleep(0.010)
		except Exception as type_error:
			raise Exception(f'Failed to type to page: {str(type_error)}')


	async def _focus_element_simple(
		self, backend_node_id: int, js_object_id: str, cdp_connection, input_coordinates: dict | None = None
		) -> bool:
		"""Простая стратегия фокусировки: сначала CDP, затем клик, если не удалось."""

		# Стратегия 1: Попробовать CDP DOM.focus сначала
		try:
			focus_result = await cdp_connection.cdp_client.send.DOM.focus(
				params={'backendNodeId': backend_node_id},
				session_id=cdp_connection.session_id,
			)
			self.logger.debug(f'Element focused using CDP DOM.focus (result: {focus_result})')
			return True

		except Exception as focus_error:
			self.logger.debug(f'❌ CDP DOM.focus threw exception: {type(focus_error).__name__}: {focus_error}')

		# Стратегия 2: Попробовать клик для фокусировки, если CDP не удалось
		if input_coordinates and 'input_x' in input_coordinates and 'input_y' in input_coordinates:
			try:
				click_coordinate_x = input_coordinates['input_x']
				click_coordinate_y = input_coordinates['input_y']

				self.logger.debug(f'🎯 Attempting click-to-focus at ({click_coordinate_x:.1f}, {click_coordinate_y:.1f})')

				# Кликнуть для фокусировки
				await cdp_connection.cdp_client.send.Input.dispatchMouseEvent(
					params={
						'type': 'mousePressed',
						'x': click_coordinate_x,
						'y': click_coordinate_y,
						'button': 'left',
						'clickCount': 1,
					},
					session_id=cdp_connection.session_id,
				)
				await cdp_connection.cdp_client.send.Input.dispatchMouseEvent(
					params={
						'type': 'mouseReleased',
						'x': click_coordinate_x,
						'y': click_coordinate_y,
						'button': 'left',
						'clickCount': 1,
					},
					session_id=cdp_connection.session_id,
				)

				self.logger.debug('✅ Element focused using click method')
				return True

			except Exception as click_error:
				self.logger.debug(f'Click focus failed: {click_error}')

		# Обе стратегии не удались
		self.logger.debug('Focus strategies failed, will attempt typing anyway')
		return False


	async def _clear_text_field(self, js_object_id: str, cdp_connection) -> bool:
		"""Очистить текстовое поле используя несколько стратегий, начиная с наиболее надежной."""
		try:
			# Стратегия 1: Прямая установка значения/содержимого через JavaScript (обрабатывает как inputs, так и contenteditable)
			self.logger.debug('🧹 Clearing text field using JavaScript value setting')

			clear_result = await cdp_connection.cdp_client.send.Runtime.callFunctionOn(
				params={
					'functionDeclaration': """
						function() {
							// Check if it's a contenteditable element
							const hasContentEditable = this.getAttribute('contenteditable') === 'true' ||
													this.getAttribute('contenteditable') === '' ||
													this.isContentEditable === true;

							if (hasContentEditable) {
								// For contenteditable elements, clear all content
								while (this.firstChild) {
									this.removeChild(this.firstChild);
								}
								this.textContent = "";
								this.innerHTML = "";

								// Focus and position cursor at the beginning
								this.focus();
								const selection = window.getSelection();
								const range = document.createRange();
								range.setStart(this, 0);
								range.setEnd(this, 0);
								selection.removeAllRanges();
								selection.addRange(range);

								// Dispatch events
								this.dispatchEvent(new Event("input", { bubbles: true }));
								this.dispatchEvent(new Event("change", { bubbles: true }));

								return {cleared: true, method: 'contenteditable', finalText: this.textContent};
							} else if (this.value !== undefined) {
								// For regular inputs with value property
								try {
									this.select();
								} catch (e) {
									// ignore
								}
								this.value = "";
								this.dispatchEvent(new Event("input", { bubbles: true }));
								this.dispatchEvent(new Event("change", { bubbles: true }));
								return {cleared: true, method: 'value', finalText: this.value};
							} else {
								return {cleared: false, method: 'none', error: 'Not a supported input type'};
							}
						}
					""",
					'objectId': js_object_id,
					'returnByValue': True,
				},
				session_id=cdp_connection.session_id,
			)

			# Проверить результат очистки
			clear_data = clear_result.get('result', {}).get('value', {})
			self.logger.debug(f'Clear result: {clear_data}')

			if clear_data.get('cleared'):
				remaining_text = clear_data.get('finalText', '')
				if not remaining_text or not remaining_text.strip():
					self.logger.debug(f'✅ Text field cleared successfully using {clear_data.get("method")}')
					return True
				else:
					self.logger.debug(f'⚠️ JavaScript clear partially failed, field still contains: "{remaining_text}"')
					return False
			else:
				self.logger.debug(f'❌ JavaScript clear failed: {clear_data.get("error", "Unknown error")}')
				return False

		except Exception as clear_error:
			self.logger.debug(f'JavaScript clear failed with exception: {clear_error}')
			return False

		# Стратегия 2: Тройной клик + Delete (запасной вариант для упрямых полей)
		try:
			self.logger.debug('🧹 Fallback: Clearing using triple-click + Delete')

			# Получить координаты центра элемента для тройного клика
			bounds_result = await cdp_connection.cdp_client.send.Runtime.callFunctionOn(
				params={
					'functionDeclaration': 'function() { return this.getBoundingClientRect(); }',
					'objectId': js_object_id,
					'returnByValue': True,
				},
				session_id=cdp_connection.session_id,
			)

			if bounds_result.get('result', {}).get('value'):
				element_bounds = bounds_result['result']['value']
				click_x = element_bounds['x'] + element_bounds['width'] / 2
				click_y = element_bounds['y'] + element_bounds['height'] / 2

				# Тройной клик для выделения всего текста
				await cdp_connection.cdp_client.send.Input.dispatchMouseEvent(
					params={
						'type': 'mousePressed',
						'x': click_x,
						'y': click_y,
						'button': 'left',
						'clickCount': 3,
					},
					session_id=cdp_connection.session_id,
				)
				await cdp_connection.cdp_client.send.Input.dispatchMouseEvent(
					params={
						'type': 'mouseReleased',
						'x': click_x,
						'y': click_y,
						'button': 'left',
						'clickCount': 3,
					},
					session_id=cdp_connection.session_id,
				)

				# Удалить выделенный текст
				await cdp_connection.cdp_client.send.Input.dispatchKeyEvent(
					params={
						'type': 'keyDown',
						'key': 'Delete',
						'code': 'Delete',
					},
					session_id=cdp_connection.session_id,
				)
				await cdp_connection.cdp_client.send.Input.dispatchKeyEvent(
					params={
						'type': 'keyUp',
						'key': 'Delete',
						'code': 'Delete',
					},
					session_id=cdp_connection.session_id,
				)

				self.logger.debug('✅ Text field cleared using triple-click + Delete')
				return True

		except Exception as click_error:
			self.logger.debug(f'Triple-click clear failed: {click_error}')

		# Стратегия 3: Горячие клавиши (последний резерв)
		try:
			import platform

			is_mac = platform.system() == 'Darwin'
			modifier_bitmask = 4 if is_mac else 2  # Meta=4 (Cmd), Ctrl=2
			modifier_key_name = 'Cmd' if is_mac else 'Ctrl'

			self.logger.debug(f'🧹 Last resort: Clearing using {modifier_key_name}+A + Backspace')

			# Выделить весь текст (Ctrl/Cmd+A)
			await cdp_connection.cdp_client.send.Input.dispatchKeyEvent(
				params={
					'type': 'keyDown',
					'key': 'a',
					'code': 'KeyA',
					'modifiers': modifier_bitmask,
				},
				session_id=cdp_connection.session_id,
			)
			await cdp_connection.cdp_client.send.Input.dispatchKeyEvent(
				params={
					'type': 'keyUp',
					'key': 'a',
					'code': 'KeyA',
					'modifiers': modifier_bitmask,
				},
				session_id=cdp_connection.session_id,
			)

			# Удалить выделенный текст (Backspace)
			await cdp_connection.cdp_client.send.Input.dispatchKeyEvent(
				params={
					'type': 'keyDown',
					'key': 'Backspace',
					'code': 'Backspace',
				},
				session_id=cdp_connection.session_id,
			)
			await cdp_connection.cdp_client.send.Input.dispatchKeyEvent(
				params={
					'type': 'keyUp',
					'key': 'Backspace',
					'code': 'Backspace',
				},
				session_id=cdp_connection.session_id,
			)

			self.logger.debug('✅ Text field cleared using keyboard shortcuts')
			return True

		except Exception as shortcut_error:
			self.logger.debug(f'All clearing strategies failed: {shortcut_error}')
			return False


	def _requires_direct_value_assignment(self, dom_node: EnhancedDOMTreeNode) -> bool:
		"""
		Проверить, требует ли элемент прямого присваивания значения вместо посимвольного ввода.

		Некоторые типы input имеют составные компоненты, пользовательские плагины или особые требования,
		которые делают посимвольный ввод ненадежным. Им нужно прямое присваивание .value:

		Нативные HTML5:
		- date, time, datetime-local: Имеют компоненты spinbutton (требуется ISO формат)
		- month, week: Похожая составная структура
		- color: Ожидает hex формат #RRGGBB
		- range: Требует числовое значение в пределах min/max

		jQuery/Bootstrap Datepickers:
		- Определяются по именам классов или data атрибутам
		- Часто ожидают специфические форматы дат (MM/DD/YYYY, DD/MM/YYYY, и т.д.)

		Примечание: Мы используем прямое присваивание, потому что:
		1. Ввод запускает промежуточную валидацию, которая может отклонить частичные значения
		2. Составные компоненты (например, date spinbuttons) не работают с последовательным вводом
		3. Это намного быстрее и надежнее
		4. Мы отправляем правильные события input/change после этого для запуска слушателей
		"""
		if not dom_node.tag_name or not dom_node.attributes:
			return False

		element_tag = dom_node.tag_name.lower()

		# Проверить нативные HTML5 inputs, которым нужно прямое присваивание
		if element_tag == 'input':
			input_type_value = dom_node.attributes.get('type', '').lower()

			# Нативные HTML5 inputs с составными компонентами или строгими форматами
			if input_type_value in {'date', 'time', 'datetime-local', 'month', 'week', 'color', 'range'}:
				return True

			# Определить jQuery/Bootstrap datepickers (text inputs с datepicker плагинами)
			if input_type_value in {'text', ''}:
				# Проверить общие индикаторы datepicker
				class_value = dom_node.attributes.get('class', '').lower()
				if any(
					picker_indicator in class_value
					for picker_indicator in ['datepicker', 'daterangepicker', 'datetimepicker', 'bootstrap-datepicker']
				):
					return True

				# Проверить data атрибуты, указывающие на datepickers
				if any(data_attr in dom_node.attributes for data_attr in ['data-datepicker', 'data-date-format', 'data-provide']):
					return True

		return False


	async def _set_value_directly(self, dom_node: EnhancedDOMTreeNode, text: str, js_object_id: str, cdp_connection) -> None:
		"""
		Установить значение элемента напрямую используя JavaScript для inputs, которые не поддерживают ввод.

		Используется для:
		- Date/time inputs, где посимвольный ввод не работает
		- jQuery datepickers, которым нужно прямое присваивание значения
		- Color/range inputs, которым нужны специфические форматы
		- Любых inputs с пользовательскими плагинами, которые перехватывают ввод

		После установки значения мы отправляем комплексные события, чтобы обеспечить распознавание изменений
		всех фреймворков и плагинов (React, Vue, Angular, jQuery, и т.д.)
		"""
		try:
			# Установить значение используя JavaScript с комплексной отправкой событий
			# callFunctionOn ожидает тело функции (не самовызывающуюся функцию)
			value_setter_js = f"""
			function() {{
				// Store old value for comparison
				const oldValue = this.value;

				// REACT-COMPATIBLE VALUE SETTING:
				// React uses Object.getOwnPropertyDescriptor to track input changes
				// We need to use the native setter to bypass React's tracking and then trigger events
				const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
					window.HTMLInputElement.prototype,
					'value'
				).set;

				// Set the value using the native setter (bypasses React's control)
				nativeInputValueSetter.call(this, {json.dumps(text)});

				// Dispatch comprehensive events to ensure all frameworks detect the change
				// Order matters: focus -> input -> change -> blur (mimics user interaction)

				// 1. Focus event (in case element isn't focused)
				this.dispatchEvent(new FocusEvent('focus', {{ bubbles: true }}));

				// 2. Input event (CRITICAL for React onChange)
				// React listens to 'input' events on the document and checks for value changes
				const inputEvent = new Event('input', {{ bubbles: true, cancelable: true }});
				this.dispatchEvent(inputEvent);

				// 3. Change event (for form handling, traditional listeners)
				const changeEvent = new Event('change', {{ bubbles: true, cancelable: true }});
				this.dispatchEvent(changeEvent);

				// 4. Blur event (triggers final validation in some libraries)
				this.dispatchEvent(new FocusEvent('blur', {{ bubbles: true }}));

				// 5. jQuery-specific events (if jQuery is present)
				if (typeof jQuery !== 'undefined' && jQuery.fn) {{
					try {{
						jQuery(this).trigger('change');
						// Trigger datepicker-specific events if it's a datepicker
						if (jQuery(this).data('datepicker')) {{
							jQuery(this).datepicker('update');
						}}
					}} catch (e) {{
						// jQuery not available or error, continue anyway
					}}
				}}

				return this.value;
			}}
			"""

			execution_result = await cdp_connection.cdp_client.send.Runtime.callFunctionOn(
				params={
					'objectId': js_object_id,
					'functionDeclaration': value_setter_js,
					'returnByValue': True,
				},
				session_id=cdp_connection.session_id,
			)

			# Проверить, что значение было установлено правильно
			if 'result' in execution_result and 'value' in execution_result['result']:
				set_value = execution_result['result']['value']
				self.logger.debug(f'✅ Value set directly to: "{set_value}"')
			else:
				self.logger.warning('⚠️ Could not verify value was set correctly')

		except Exception as set_error:
			self.logger.error(f'❌ Failed to set value directly: {set_error}')
			raise


	async def _trigger_framework_events(self, js_object_id: str, cdp_connection) -> None:
		"""
		Запустить события DOM, осведомленные о фреймворках, после завершения ввода текста.

		Это критично для современных JavaScript фреймворков (React, Vue, Angular, и т.д.),
		которые полагаются на DOM события для обновления их внутреннего состояния и запуска повторных рендеров.

		Args:
			js_object_id: CDP object ID элемента input
			cdp_connection: CDP сессия для контекста элемента
		"""
		try:
			# Выполнить JavaScript для запуска комплексной последовательности событий
			events_script = """
			function() {
				// Find the target element (available as 'this' when using objectId)
				const element = this;
				if (!element) return false;

				// Ensure element is focused
				element.focus();

				// Comprehensive event sequence for maximum framework compatibility
				const events = [
					// Input event - primary event for React controlled components
					{ type: 'input', bubbles: true, cancelable: true },
					// Change event - important for form validation and Vue v-model
					{ type: 'change', bubbles: true, cancelable: true },
					// Blur event - triggers validation in many frameworks
					{ type: 'blur', bubbles: true, cancelable: true }
				];

				let success = true;

				events.forEach(eventConfig => {
					try {
						const event = new Event(eventConfig.type, {
							bubbles: eventConfig.bubbles,
							cancelable: eventConfig.cancelable
						});

						// Special handling for InputEvent (more specific than Event)
						if (eventConfig.type === 'input') {
							const inputEvent = new InputEvent('input', {
								bubbles: true,
								cancelable: true,
								data: element.value,
								inputType: 'insertText'
							});
							element.dispatchEvent(inputEvent);
						} else {
							element.dispatchEvent(event);
						}
					} catch (e) {
						success = false;
					}
				});

				// Special React synthetic event handling
				// React uses internal fiber properties for event system
				if (element._reactInternalFiber || element._reactInternalInstance || element.__reactInternalInstance) {
					try {
						// Trigger React's synthetic event system
						const syntheticInputEvent = new InputEvent('input', {
							bubbles: true,
							cancelable: true,
							data: element.value
						});

						// Force React to process this as a synthetic event
						Object.defineProperty(syntheticInputEvent, 'isTrusted', { value: true });
						element.dispatchEvent(syntheticInputEvent);
				} catch (e) {
					// React synthetic event failed
				}
				}

				// Special Vue reactivity trigger
				// Vue uses __vueParentComponent or __vue__ for component access
				if (element.__vue__ || element._vnode || element.__vueParentComponent) {
					try {
						// Vue often needs explicit input event with proper timing
						const vueEvent = new Event('input', { bubbles: true });
						setTimeout(() => element.dispatchEvent(vueEvent), 0);
					} catch (e) {
					}
				}

				return success;
			}
			"""

			# Выполнить скрипт событий фреймворка
			execution_result = await cdp_connection.cdp_client.send.Runtime.callFunctionOn(
				params={
					'objectId': js_object_id,
					'functionDeclaration': events_script,
					'returnByValue': True,
				},
				session_id=cdp_connection.session_id,
			)

			execution_success = execution_result.get('result', {}).get('value', False)
			if execution_success:
				self.logger.debug('✅ Framework events triggered successfully')
			else:
				self.logger.warning('⚠️ Failed to trigger framework events')

		except Exception as events_error:
			self.logger.warning(f'⚠️ Failed to trigger framework events: {type(events_error).__name__}: {events_error}')
			# Не выбрасывать - события фреймворка это улучшение по мере возможности


	def _get_char_modifiers_and_vk(self, character: str) -> tuple[int, int, str]:
		"""Получить модификаторы, виртуальный код клавиши и базовую клавишу для символа.

		Returns:
			(модификаторы, windowsVirtualKeyCode, базовая_клавиша)
		"""
		# Символы, требующие модификатор Shift
		shift_required_chars = {
			'!': ('1', 49),
			'@': ('2', 50),
			'#': ('3', 51),
			'$': ('4', 52),
			'%': ('5', 53),
			'^': ('6', 54),
			'&': ('7', 55),
			'*': ('8', 56),
			'(': ('9', 57),
			')': ('0', 48),
			'_': ('-', 189),
			'+': ('=', 187),
			'{': ('[', 219),
			'}': (']', 221),
			'|': ('\\', 220),
			':': (';', 186),
			'"': ("'", 222),
			'<': (',', 188),
			'>': ('.', 190),
			'?': ('/', 191),
			'~': ('`', 192),
		}

		# Проверить, требует ли символ модификатор Shift
		if character in shift_required_chars:
			base_key_name, virtual_key = shift_required_chars[character]
			return (8, virtual_key, base_key_name)  # Shift=8

		# Прописные буквы требуют Shift
		if character.isupper():
			return (8, ord(character), character.lower())  # Shift=8

		# Строчные буквы
		if character.islower():
			return (0, ord(character.upper()), character)

		# Цифры
		if character.isdigit():
			return (0, ord(character), character)

		# Специальные символы без Shift
		no_shift_required_chars = {
			' ': 32,
			'-': 189,
			'=': 187,
			'[': 219,
			']': 221,
			'\\': 220,
			';': 186,
			"'": 222,
			',': 188,
			'.': 190,
			'/': 191,
			'`': 192,
		}

		if character in no_shift_required_chars:
			return (0, no_shift_required_chars[character], character)

		# Запасной вариант
		return (0, ord(character.upper()) if character.isalpha() else ord(character), character)


	def _get_key_code_for_char(self, character: str) -> str:
		"""Подобрать корректный key code для символа (учитывая модификаторы)."""
		# Маппинг key code для распространенных символов (используя правильные базовые клавиши + модификаторы)
		keycode_mapping = {
			' ': 'Space',
			'.': 'Period',
			',': 'Comma',
			'-': 'Minus',
			'_': 'Minus',  # Underscore uses Minus with Shift
			'@': 'Digit2',  # @ uses Digit2 with Shift
			'!': 'Digit1',  # ! uses Digit1 with Shift (not 'Exclamation')
			'?': 'Slash',  # ? uses Slash with Shift
			':': 'Semicolon',  # : uses Semicolon with Shift
			';': 'Semicolon',
			'(': 'Digit9',  # ( uses Digit9 with Shift
			')': 'Digit0',  # ) uses Digit0 with Shift
			'[': 'BracketLeft',
			']': 'BracketRight',
			'{': 'BracketLeft',  # { uses BracketLeft with Shift
			'}': 'BracketRight',  # } uses BracketRight with Shift
			'/': 'Slash',
			'\\': 'Backslash',
			'=': 'Equal',
			'+': 'Equal',  # + uses Equal with Shift
			'*': 'Digit8',  # * uses Digit8 with Shift
			'&': 'Digit7',  # & uses Digit7 with Shift
			'%': 'Digit5',  # % uses Digit5 with Shift
			'$': 'Digit4',  # $ uses Digit4 with Shift
			'#': 'Digit3',  # # uses Digit3 with Shift
			'^': 'Digit6',  # ^ uses Digit6 with Shift
			'~': 'Backquote',  # ~ uses Backquote with Shift
			'`': 'Backquote',
			"'": 'Quote',
			'"': 'Quote',  # " uses Quote with Shift
		}

		# Цифры
		if character.isdigit():
			return f'Digit{character}'

		# Буквы
		if character.isalpha():
			return f'Key{character.upper()}'

		# Специальные символы
		if character in keycode_mapping:
			return keycode_mapping[character]

		# Запасной вариант для неизвестных символов
		return f'Key{character.upper()}'

