"""Обработчик действий браузера - send_keys."""

import asyncio
import json
from typing import TYPE_CHECKING

from core.dom_processing.manager import EnhancedDOMTreeNode
from core.session.events import KeyboardInputRequest, DelayRequest
from core.session.models import BrowserError, URLNotAllowedError
from core.observability import observe_debug
from core.interaction.helpers import get_key_info
from cdp_use.cdp.input.commands import DispatchKeyEventParameters

if TYPE_CHECKING:
	from core.session.monitors.watchdogs.default_action_watchdog import DefaultActionWatchdog


class SendKeysHandler:
	"""Обработчик send_keys для DefaultActionWatchdog."""

	def __init__(self, watchdog: "DefaultActionWatchdog"):
		"""Инициализация обработчика с ссылкой на watchdog."""
		self.watchdog = watchdog
		self.browser_session = watchdog.browser_session
		self.browser_controller = watchdog.browser_controller
		self.logger = watchdog.logger

	async def on_KeyboardInputRequest(self, event: KeyboardInputRequest) -> None:
		"""Обработать запрос отправки клавиш с CDP."""
		cdp_connection = await self.browser_session.get_or_create_cdp_session(focus=True)
		try:
			# Нормализовать имена клавиш из распространенных алиасов
			key_alias_map = {
				'ctrl': 'Control',
				'control': 'Control',
				'alt': 'Alt',
				'option': 'Alt',
				'meta': 'Meta',
				'cmd': 'Meta',
				'command': 'Meta',
				'shift': 'Shift',
				'enter': 'Enter',
				'return': 'Enter',
				'tab': 'Tab',
				'delete': 'Delete',
				'backspace': 'Backspace',
				'escape': 'Escape',
				'esc': 'Escape',
				'space': ' ',
				'up': 'ArrowUp',
				'down': 'ArrowDown',
				'left': 'ArrowLeft',
				'right': 'ArrowRight',
				'pageup': 'PageUp',
				'pagedown': 'PageDown',
				'home': 'Home',
				'end': 'End',
			}

			# Разобрать и нормализовать строку клавиш
			input_keys = event.keys
			if '+' in input_keys:
				# Обработать комбинации клавиш, такие как "ctrl+a"
				key_parts = input_keys.split('+')
				normalized_list = []
				for key_part in key_parts:
					key_lowercase = key_part.strip().lower()
					normalized_key = key_alias_map.get(key_lowercase, key_part)
					normalized_list.append(normalized_key)
				final_keys = '+'.join(normalized_list)
			else:
				# Одна клавиша
				key_lowercase = input_keys.strip().lower()
				final_keys = key_alias_map.get(key_lowercase, input_keys)

			# Обработать комбинации клавиш, такие как "Control+A"
			if '+' in final_keys:
				key_parts = final_keys.split('+')
				modifier_keys = key_parts[:-1]
				primary_key = key_parts[-1]

				# Вычислить битовую маску модификаторов
				modifier_bitmask = 0
				modifier_mapping = {'Alt': 1, 'Control': 2, 'Meta': 4, 'Shift': 8}
				for modifier_key in modifier_keys:
					modifier_bitmask |= modifier_mapping.get(modifier_key, 0)

				# Нажать клавиши-модификаторы
				for modifier_key in modifier_keys:
					await self._dispatch_key_event(cdp_connection, 'keyDown', modifier_key)

				# Нажать основную клавишу с битовой маской модификаторов
				await self._dispatch_key_event(cdp_connection, 'keyDown', primary_key, modifier_bitmask)

				await self._dispatch_key_event(cdp_connection, 'keyUp', primary_key, modifier_bitmask)

				# Отпустить клавиши-модификаторы
				for modifier_key in reversed(modifier_keys):
					await self._dispatch_key_event(cdp_connection, 'keyUp', modifier_key)
			else:
				# Проверить, является ли это текстовой строкой или специальной клавишей
				special_key_set = {
					'Enter',
					'Tab',
					'Delete',
					'Backspace',
					'Escape',
					'ArrowUp',
					'ArrowDown',
					'ArrowLeft',
					'ArrowRight',
					'PageUp',
					'PageDown',
					'Home',
					'End',
					'Control',
					'Alt',
					'Meta',
					'Shift',
					'F1',
					'F2',
					'F3',
					'F4',
					'F5',
					'F6',
					'F7',
					'F8',
					'F9',
					'F10',
					'F11',
					'F12',
				}

				# Если это специальная клавиша, использовать исходную логику
				if final_keys in special_key_set:
					await self._dispatch_key_event(cdp_connection, 'keyDown', final_keys)
					# Для клавиши Enter также отправить событие char для запуска слушателей keypress
					if final_keys == 'Enter':
						await cdp_connection.cdp_client.send.Input.dispatchKeyEvent(
							params={
								'type': 'char',
								'text': '\r',
								'key': 'Enter',
							},
							session_id=cdp_connection.session_id,
						)
					await self._dispatch_key_event(cdp_connection, 'keyUp', final_keys)
				else:
					# Это текст (один символ или строка) - отправить каждый символ как текстовый ввод
					# Это критично для того, чтобы текст появлялся в полях ввода с фокусом
					for character in final_keys:
						# Особый случай: символы новой строки отправляются как Enter
						if character in ('\n', '\r'):
							await cdp_connection.cdp_client.send.Input.dispatchKeyEvent(
								params={
									'type': 'rawKeyDown',
									'windowsVirtualKeyCode': 13,
									'unmodifiedText': '\r',
									'text': '\r',
								},
								session_id=cdp_connection.session_id,
							)
							await cdp_connection.cdp_client.send.Input.dispatchKeyEvent(
								params={
									'type': 'char',
									'windowsVirtualKeyCode': 13,
									'unmodifiedText': '\r',
									'text': '\r',
								},
								session_id=cdp_connection.session_id,
							)
							await cdp_connection.cdp_client.send.Input.dispatchKeyEvent(
								params={
									'type': 'keyUp',
									'windowsVirtualKeyCode': 13,
									'unmodifiedText': '\r',
									'text': '\r',
								},
								session_id=cdp_connection.session_id,
							)
							continue

						# Получить правильные модификаторы и информацию о клавише для символа
						char_modifiers, virtual_key_code, base_key_name = self._get_char_modifiers_and_vk(character)
						char_key_code = self._get_key_code_for_char(base_key_name)

						# Отправить keyDown
						await cdp_connection.cdp_client.send.Input.dispatchKeyEvent(
							params={
								'type': 'keyDown',
								'key': base_key_name,
								'code': char_key_code,
								'modifiers': char_modifiers,
								'windowsVirtualKeyCode': virtual_key_code,
							},
							session_id=cdp_connection.session_id,
						)

						# Отправить событие char с текстом - это делает текст видимым в полях ввода
						await cdp_connection.cdp_client.send.Input.dispatchKeyEvent(
							params={
								'type': 'char',
								'text': character,
								'key': character,
							},
							session_id=cdp_connection.session_id,
						)

						# Отправить keyUp
						await cdp_connection.cdp_client.send.Input.dispatchKeyEvent(
							params={
								'type': 'keyUp',
								'key': base_key_name,
								'code': char_key_code,
								'modifiers': char_modifiers,
								'windowsVirtualKeyCode': virtual_key_code,
							},
							session_id=cdp_connection.session_id,
						)

						# Небольшая задержка между символами (10ms)
						await asyncio.sleep(0.010)

			self.logger.info(f'⌨️ Sent keys: {event.keys}')

			# Примечание: Мы не очищаем кэшированное состояние на Enter; multi_act обнаружит изменения DOM
			# и явно перестроит. Мы все еще ждем кратко для потенциальной навигации.
			if 'enter' in event.keys.lower() or 'return' in event.keys.lower():
				await asyncio.sleep(0.1)
		except Exception as keys_error:
			raise


	async def on_DelayRequest(self, event: DelayRequest) -> None:
		"""Обработать запрос ожидания."""
		try:
			# Ограничить время ожидания максимумом
			wait_seconds = min(max(event.seconds, 0), event.max_seconds)
			if wait_seconds != event.seconds:
				self.logger.info(f'🕒 Waiting for {wait_seconds} seconds (capped from {event.seconds}s)')
			else:
				self.logger.info(f'🕒 Waiting for {wait_seconds} seconds')

			await asyncio.sleep(wait_seconds)
		except Exception as wait_error:
			raise


	async def _dispatch_key_event(self, cdp_connection, event_type: str, key: str, modifiers: int = 0) -> None:
		"""Вспомогательная функция для отправки события клавиатуры с правильными кодами клавиш."""
		key_code, virtual_key_code = get_key_info(key)
		key_params: DispatchKeyEventParameters = {
			'type': event_type,
			'key': key,
			'code': key_code,
		}
		if modifiers:
			key_params['modifiers'] = modifiers
		if virtual_key_code is not None:
			key_params['windowsVirtualKeyCode'] = virtual_key_code
		await cdp_connection.cdp_client.send.Input.dispatchKeyEvent(params=key_params, session_id=cdp_connection.session_id)

