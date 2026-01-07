"""Базовый класс watchdog для компонентов мониторинга браузера."""

import inspect
import time
from collections.abc import Iterable
from typing import Any, ClassVar

from bubus import BaseEvent, EventBus
from pydantic import BaseModel, ConfigDict, Field

from core.session.session import BrowserSession


class BaseWatchdog(BaseModel):
	"""Базовый класс для всех watchdog браузера.

	Watchdogs отслеживают состояние браузера и генерируют события на основе изменений.
	Они автоматически регистрируют обработчики событий на основе имён методов.

	Методы-обработчики должны называться: on_EventTypeName(self, event: EventTypeName)
	"""

	model_config = ConfigDict(
		arbitrary_types_allowed=True,  # разрешаем несериализуемые объекты типа EventBus/BrowserSession в полях
		extra='forbid',  # не разрешаем неявное состояние класса/экземпляра, всё должно быть правильно типизированным Field или PrivateAttr
		validate_assignment=False,  # избегаем повторного запуска __init__ / валидаторов при каждом присваивании
		revalidate_instances='never',  # избегаем повторного запуска __init__ / валидаторов и стирания приватных атрибутов
	)

	# Переменные класса для статического определения списка событий, релевантных каждому watchdog
	# (не принудительно, просто для упрощения понимания кода и отладки watchdogs во время выполнения)
	LISTENS_TO: ClassVar[list[type[BaseEvent[Any]]]] = []  # События, которые слушает этот watchdog
	EMITS: ClassVar[list[type[BaseEvent[Any]]]] = []  # События, которые генерирует этот watchdog

	# Основные зависимости
	browser_session: BrowserSession = Field()
	event_bus: EventBus = Field()

	# Общее состояние, к которому могут нуждаться другие watchdogs, НЕ должно определяться здесь, а на BrowserSession!
	# Общие вспомогательные методы, нужные другим watchdogs, НЕ должны определяться здесь, а на BrowserSession!

	@property
	def logger(self):
		"""Получить logger из browser session."""
		return self.browser_session.logger

	@staticmethod
	def attach_handler_to_session(browser_session: 'BrowserSession', event_class: type[BaseEvent[Any]], handler) -> None:
		"""Прикрепить один обработчик событий к browser session.

		Args:
			browser_session: Browser session, к которому прикрепляем
			event_class: Класс события, которое слушаем
			handler: Метод-обработчик (должен начинаться с 'on_' и заканчиваться типом события)
		"""
		event_bus = browser_session.event_bus

		# Проверяем соглашение об именовании обработчика
		assert hasattr(handler, '__name__'), 'Handler must have a __name__ attribute'
		assert handler.__name__.startswith('on_'), f'Handler {handler.__name__} must start with "on_"'
		assert handler.__name__.endswith(event_class.__name__), (
			f'Handler {handler.__name__} must end with event type {event_class.__name__}'
		)

		# Получаем экземпляр watchdog, если это связанный метод
		watchdog_instance = getattr(handler, '__self__', None)
		watchdog_class_name = watchdog_instance.__class__.__name__ if watchdog_instance else 'Unknown'

		# Создаём функцию-обёртку с уникальным именем, чтобы избежать предупреждений о дубликатах обработчиков
		# Захватываем handler по значению, чтобы избежать проблем с замыканиями
		def make_unique_handler(actual_handler):
			async def unique_handler(event):
				# только для отладочного логирования, не используется ни для чего другого
				parent_event = event_bus.event_history.get(event.event_parent_id) if event.event_parent_id else None
				grandparent_event = (
					event_bus.event_history.get(parent_event.event_parent_id)
					if parent_event and parent_event.event_parent_id
					else None
				)
				parent = (
					f'↲  triggered by on_{parent_event.event_type}#{parent_event.event_id[-4:]}'
					if parent_event
					else '👈 by Agent'
				)
				grandparent = (
					(
						f'↲  under {grandparent_event.event_type}#{grandparent_event.event_id[-4:]}'
						if grandparent_event
						else '👈 by Agent'
					)
					if parent_event
					else ''
				)
				event_str = f'#{event.event_id[-4:]}'
				time_start = time.time()
				watchdog_and_handler_str = f'[{watchdog_class_name}.{actual_handler.__name__}({event_str})]'.ljust(54)
				browser_session.logger.debug(f'🚌 {watchdog_and_handler_str} ⏳ Starting...       {parent} {grandparent}')

				try:
					# **ВЫПОЛНЯЕМ ФУНКЦИЮ ОБРАБОТЧИКА СОБЫТИЯ**
					result = await actual_handler(event)

					if isinstance(result, Exception):
						raise result

					# только для отладочного логирования, не используется ни для чего другого
					time_end = time.time()
					time_elapsed = time_end - time_start
					result_summary = '' if result is None else f' ➡️ <{type(result).__name__}>'
					parents_summary = f' {parent}'.replace('↲  triggered by ', '⤴  returned to  ').replace(
						'👈 by Agent', '👉 returned to  Agent'
					)
					browser_session.logger.debug(
						f'🚌 {watchdog_and_handler_str} Succeeded ({time_elapsed:.2f}s){result_summary}{parents_summary}'
					)
					return result
				except Exception as e:
					time_end = time.time()
					time_elapsed = time_end - time_start
					original_error = e
					browser_session.logger.error(
						f'🚌 {watchdog_and_handler_str} ❌ Failed ({time_elapsed:.2f}s): {type(e).__name__}: {e}'
					)

					# пытаемся восстановить потенциально упавшую CDP-сессию
					try:
						if browser_session.agent_focus_target_id:
							# С event-driven сессиями Chrome отправит события detach/attach
							# SessionManager автоматически обрабатывает очистку пула
							target_id_to_restore = browser_session.agent_focus_target_id
							browser_session.logger.debug(
								f'🚌 {watchdog_and_handler_str} ⚠️ Обнаружена ошибка сессии, ждём синхронизации CDP-событий (target: {target_id_to_restore})'
							)

							# Ждём нового события attach для восстановления сессии
							# Это вызовет ValueError, если target не переподключится
							await browser_session.get_or_create_cdp_session(target_id=target_id_to_restore, focus=True)
						else:
							# Пытаемся получить любую доступную сессию
							await browser_session.get_or_create_cdp_session(target_id=None, focus=True)
					except Exception as sub_error:
						if 'ConnectionClosedError' in str(type(sub_error)) or 'ConnectionError' in str(type(sub_error)):
							browser_session.logger.error(
								f'🚌 {watchdog_and_handler_str} ❌ Browser closed or CDP Connection disconnected by remote. {type(sub_error).__name__}: {sub_error}\n'
							)
							raise
						else:
							browser_session.logger.error(
								f'🚌 {watchdog_and_handler_str} ❌ CDP connected but failed to re-create CDP session after error "{type(original_error).__name__}: {original_error}" in {actual_handler.__name__}({event.event_type}#{event.event_id[-4:]}): due to {type(sub_error).__name__}: {sub_error}\n'
							)

					# Всегда повторно поднимаем исходную ошибку с сохранением её traceback
					raise

			return unique_handler

		unique_handler = make_unique_handler(handler)
		unique_handler.__name__ = f'{watchdog_class_name}.{handler.__name__}'

		# Проверяем, не зарегистрирован ли уже этот обработчик - выбрасываем ошибку при дубликате
		existing_handlers = event_bus.handlers.get(event_class.__name__, [])
		handler_names = [getattr(h, '__name__', str(h)) for h in existing_handlers]

		if unique_handler.__name__ in handler_names:
			raise RuntimeError(
				f'[{watchdog_class_name}] Попытка дублирующей регистрации обработчика! '
				f'Обработчик {unique_handler.__name__} уже зарегистрирован для {event_class.__name__}. '
				f'Это, вероятно, означает, что attach_to_session() был вызван несколько раз.'
			)

		event_bus.on(event_class, unique_handler)

	def attach_to_session(self) -> None:
		"""Прикрепить watchdog к его browser session и начать мониторинг.

		Этот метод обрабатывает регистрацию слушателей событий. Watchdog уже
		привязан к browser session через self.browser_session при инициализации.
		"""
		# Регистрируем обработчики событий автоматически на основе имён методов
		assert self.browser_session is not None, 'Root CDP client not initialized - browser may not be connected yet'

		from core.session import events

		event_classes = {}
		for name in dir(events):
			obj = getattr(events, name)
			if inspect.isclass(obj) and issubclass(obj, BaseEvent) and obj is not BaseEvent:
				event_classes[name] = obj

		# Находим все методы-обработчики (on_EventName)
		registered_events = set()
		for method_name in dir(self):
			if method_name.startswith('on_') and callable(getattr(self, method_name)):
				# Извлекаем имя события из имени метода (on_EventName -> EventName)
				event_name = method_name[3:]  # Удаляем префикс 'on_'

				if event_name in event_classes:
					event_class = event_classes[event_name]

					# УТВЕРЖДЕНИЕ: Если LISTENS_TO определён, принуждаем его
					if self.LISTENS_TO:
						assert event_class in self.LISTENS_TO, (
							f'[{self.__class__.__name__}] Handler {method_name} listens to {event_name} '
							f'but {event_name} is not declared in LISTENS_TO: {[e.__name__ for e in self.LISTENS_TO]}'
						)

					handler = getattr(self, method_name)

					# Используем статический помощник для прикрепления обработчика
					self.attach_handler_to_session(self.browser_session, event_class, handler)
					registered_events.add(event_class)

		# УТВЕРЖДЕНИЕ: Если LISTENS_TO определён, убеждаемся, что все объявленные события имеют обработчики
		if self.LISTENS_TO:
			missing_handlers = set(self.LISTENS_TO) - registered_events
			if missing_handlers:
				missing_names = [e.__name__ for e in missing_handlers]
				self.logger.warning(
					f'[{self.__class__.__name__}] LISTENS_TO объявляет {missing_names} '
					f'но обработчики не найдены (отсутствуют методы on_{"_, on_".join(missing_names)})'
				)

	def __del__(self) -> None:
		"""Очистить все запущенные задачи во время сборки мусора."""

		# НЕМНОГО МАГИИ: Отменяем все приватные атрибуты, которые выглядят как asyncio-задачи
		try:
			for attr_name in dir(self):
				# например, _browser_crash_watcher_task = asyncio.Task
				if attr_name.startswith('_') and attr_name.endswith('_task'):
					try:
						task = getattr(self, attr_name)
						if hasattr(task, 'cancel') and callable(task.cancel) and not task.done():
							task.cancel()
							# self.logger.debug(f'[{self.__class__.__name__}] Cancelled {attr_name} during cleanup')
					except Exception:
						pass  # Игнорируем ошибки во время очистки

				# например, _cdp_download_tasks = WeakSet[asyncio.Task] или list[asyncio.Task]
				if attr_name.startswith('_') and attr_name.endswith('_tasks') and isinstance(getattr(self, attr_name), Iterable):
					for task in getattr(self, attr_name):
						try:
							if hasattr(task, 'cancel') and callable(task.cancel) and not task.done():
								task.cancel()
								# self.logger.debug(f'[{self.__class__.__name__}] Cancelled {attr_name} during cleanup')
						except Exception:
							pass  # Игнорируем ошибки во время очистки
		except Exception as e:
			from core.helpers import logger

			logger.error(f'⚠️ Ошибка во время сборки мусора BrowserSession {self.__class__.__name__} __del__(): {type(e)}: {e}')


# Алиас для обратной совместимости
WatchdogBase = BaseWatchdog
