"""Watchdog для системных функций: мониторинг сбоев и управление состоянием хранилища."""

import asyncio
import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import psutil
from bubus import BaseEvent
from cdp_use.cdp.network import Cookie
from cdp_use.cdp.target import SessionID, TargetID
from cdp_use.cdp.target.events import TargetCrashedEvent
from pydantic import Field, PrivateAttr

from core.session.events import (
	BrowserConnectedEvent,
	BrowserErrorEvent,
	BrowserStopEvent,
	BrowserStoppedEvent,
	LoadStorageStateEvent,
	SaveStorageStateEvent,
	StorageStateLoadedEvent,
	StorageStateSavedEvent,
	TabClosedEvent,
	TabCreatedEvent,
)
from core.session.watchdog_base import BaseWatchdog
from core.helpers import create_task_with_error_handling

if TYPE_CHECKING:
	pass


class NetworkRequestTracker:
	"""Отслеживает текущие сетевые запросы."""

	def __init__(self, request_id: str, start_time: float, url: str, method: str, resource_type: str | None = None):
		self.request_id = request_id
		self.start_time = start_time
		self.url = url
		self.method = method
		self.resource_type = resource_type


class CrashWatchdog(BaseWatchdog):
	"""Мониторит состояние браузера на предмет сбоев и таймаутов сети с использованием CDP."""

	# Контракты событий
	LISTENS_TO: ClassVar[list[type[BaseEvent]]] = [
		BrowserConnectedEvent,
		BrowserStoppedEvent,
		TabCreatedEvent,
		TabClosedEvent,
	]
	EMITS: ClassVar[list[type[BaseEvent]]] = [BrowserErrorEvent]

	# Конфигурация
	network_timeout_seconds: float = Field(default=10.0)
	check_interval_seconds: float = Field(default=5.0)  # Сниженная частота для уменьшения шума

	# Приватное состояние
	_active_requests: dict[str, NetworkRequestTracker] = PrivateAttr(default_factory=dict)
	_monitoring_task: asyncio.Task | None = PrivateAttr(default=None)
	_last_responsive_checks: dict[str, float] = PrivateAttr(default_factory=dict)  # target_url -> timestamp
	_cdp_event_tasks: set[asyncio.Task] = PrivateAttr(default_factory=set)  # Track CDP event handler tasks
	_targets_with_listeners: set[str] = PrivateAttr(default_factory=set)  # Track targets that already have event listeners

	async def on_BrowserConnectedEvent(self, event: BrowserConnectedEvent) -> None:
		"""Запустить мониторинг при подключении браузера."""
		create_task_with_error_handling(
			self._start_monitoring(), name='start_crash_monitoring', logger_instance=self.logger, suppress_exceptions=True
		)

	async def on_BrowserStoppedEvent(self, event: BrowserStoppedEvent) -> None:
		"""Остановить мониторинг при остановке браузера."""
		await self._stop_monitoring()

	async def on_TabCreatedEvent(self, event: TabCreatedEvent) -> None:
		"""Присоединиться к новой вкладке."""
		assert self.browser_session.agent_focus_target_id is not None, 'No current target ID'
		await self.attach_to_target(self.browser_session.agent_focus_target_id)

	async def on_TabClosedEvent(self, event: TabClosedEvent) -> None:
		"""Очистить отслеживание при закрытии вкладки."""
		# Удалить target из отслеживания слушателей, чтобы предотвратить утечку памяти
		if event.target_id in self._targets_with_listeners:
			self._targets_with_listeners.discard(event.target_id)
			self.logger.debug(f'[CrashWatchdog] Removed target {event.target_id[:8]}... from monitoring')

	async def attach_to_target(self, target_id: TargetID) -> None:
		"""Настроить мониторинг сбоев для конкретного target с использованием CDP."""
		try:
			# Проверить, есть ли уже слушатели для этого target
			if target_id in self._targets_with_listeners:
				self.logger.debug(f'[CrashWatchdog] Event listeners already exist for target: {target_id[:8]}...')
				return

			# Создать временную сессию для мониторинга без переключения фокуса
			cdp_connection = await self.browser_session.get_or_create_cdp_session(target_id, focus=False)

			# Зарегистрировать обработчик события сбоя
			def on_target_crashed(crash_event: TargetCrashedEvent, session_id: SessionID | None = None):
				# Создать и отследить задачу
				crash_task = create_task_with_error_handling(
					self._on_target_crash_cdp(target_id),
					name='handle_target_crash',
					logger_instance=self.logger,
					suppress_exceptions=True,
				)
				self._cdp_event_tasks.add(crash_task)
				# Удалить из множества, когда завершится
				crash_task.add_done_callback(lambda completed_task: self._cdp_event_tasks.discard(completed_task))

			cdp_connection.cdp_client.register.Target.targetCrashed(on_target_crashed)

			# Отследить, что мы добавили слушатели к этому target
			self._targets_with_listeners.add(target_id)

			target_info = self.browser_session.session_manager.get_target(target_id)
			if target_info:
				self.logger.debug(f'[CrashWatchdog] Added target to monitoring: {target_info.url}')

		except Exception as attach_error:
			self.logger.warning(f'[CrashWatchdog] Failed to attach to target {target_id}: {attach_error}')

	async def _on_request_cdp(self, event: dict) -> None:
		"""Отследить новый сетевой запрос из CDP события."""
		network_request_id = event.get('requestId', '')
		request_data = event.get('request', {})

		self._active_requests[network_request_id] = NetworkRequestTracker(
			request_id=network_request_id,
			start_time=time.time(),
			url=request_data.get('url', ''),
			method=request_data.get('method', ''),
			resource_type=event.get('type'),
		)

	def _on_response_cdp(self, event: dict) -> None:
		"""Удалить запрос из отслеживания при получении ответа."""
		network_request_id = event.get('requestId', '')
		if network_request_id in self._active_requests:
			request_duration = time.time() - self._active_requests[network_request_id].start_time
			response_data = event.get('response', {})
			self.logger.debug(f'[CrashWatchdog] Request completed in {request_duration:.2f}s: {response_data.get("url", "")[:50]}...')
			# Пока не удалять - ждать loadingFinished

	def _on_request_failed_cdp(self, event: dict) -> None:
		"""Удалить запрос из отслеживания при неудаче."""
		network_request_id = event.get('requestId', '')
		if network_request_id in self._active_requests:
			request_duration = time.time() - self._active_requests[network_request_id].start_time
			failed_request = self._active_requests[network_request_id]
			self.logger.debug(
				f'[CrashWatchdog] Request failed after {request_duration:.2f}s: {failed_request.url[:50]}...'
			)
			del self._active_requests[network_request_id]

	def _on_request_finished_cdp(self, event: dict) -> None:
		"""Удалить запрос из отслеживания, когда загрузка завершена."""
		network_request_id = event.get('requestId', '')
		self._active_requests.pop(network_request_id, None)

	async def _on_target_crash_cdp(self, target_id: TargetID) -> None:
		"""Обработать сбой target, обнаруженный через CDP."""
		self.logger.debug(f'[CrashWatchdog] Target crashed: {target_id[:8]}..., waiting for detach event')

		target_info = self.browser_session.session_manager.get_target(target_id)

		is_focused_target = (
			target_info
			and self.browser_session.agent_focus_target_id
			and target_info.target_id == self.browser_session.agent_focus_target_id
		)

		if is_focused_target:
			self.logger.error(f'[CrashWatchdog] 💥 Agent focus tab crashed: {target_info.url} (SessionManager will auto-recover)')

		# Отправить событие ошибки браузера
		self.event_bus.dispatch(
			BrowserErrorEvent(
				error_type='TargetCrash',
				message=f'Target crashed: {target_id}',
				details={
					'url': target_info.url if target_info else None,
					'target_id': target_id,
					'was_agent_focus': is_focused_target,
				},
			)
		)

	async def _start_monitoring(self) -> None:
		"""Запустить цикл мониторинга."""
		assert self.browser_session.cdp_client is not None, 'Root CDP client not initialized - browser may not be connected yet'

		if self._monitoring_task and not self._monitoring_task.done():
			return

		self._monitoring_task = create_task_with_error_handling(
			self._monitoring_loop(), name='crash_monitoring_loop', logger_instance=self.logger, suppress_exceptions=True
		)

	async def _stop_monitoring(self) -> None:
		"""Остановить цикл мониторинга и очистить все отслеживание."""
		if self._monitoring_task and not self._monitoring_task.done():
			self._monitoring_task.cancel()
			try:
				await self._monitoring_task
			except asyncio.CancelledError:
				pass
			self.logger.debug('[CrashWatchdog] Monitoring loop stopped')

		# Отменить все задачи обработчиков CDP событий
		for event_task in list(self._cdp_event_tasks):
			if not event_task.done():
				event_task.cancel()
		# Дождаться завершения отмены всех задач
		if self._cdp_event_tasks:
			await asyncio.gather(*self._cdp_event_tasks, return_exceptions=True)
		self._cdp_event_tasks.clear()

		# Очистить все отслеживание
		self._active_requests.clear()
		self._targets_with_listeners.clear()
		self._last_responsive_checks.clear()

	async def _monitoring_loop(self) -> None:
		"""Основной цикл мониторинга."""
		await asyncio.sleep(10)  # дать браузеру время запуститься и загрузить первую страницу после первого вызова LLM
		while True:
			try:
				await self._check_network_timeouts()
				await self._check_browser_health()
				await asyncio.sleep(self.check_interval_seconds)
			except asyncio.CancelledError:
				break
			except Exception as loop_error:
				self.logger.error(f'[CrashWatchdog] Error in monitoring loop: {loop_error}')

	async def _check_network_timeouts(self) -> None:
		"""Проверить сетевые запросы, превышающие таймаут."""
		now = time.time()
		expired_requests = []

		# Отладочное логирование
		if self._active_requests:
			self.logger.debug(
				f'[CrashWatchdog] Checking {len(self._active_requests)} active requests for timeouts (threshold: {self.network_timeout_seconds}s)'
			)

		for network_request_id, request_tracker in self._active_requests.items():
			request_elapsed = now - request_tracker.start_time
			self.logger.debug(
				f'[CrashWatchdog] Request {request_tracker.url[:30]}... elapsed: {request_elapsed:.1f}s, timeout: {self.network_timeout_seconds}s'
			)
			if request_elapsed >= self.network_timeout_seconds:
				expired_requests.append((network_request_id, request_tracker))

		# Отправить события для истекших запросов
		for network_request_id, request_tracker in expired_requests:
			self.logger.warning(
				f'[CrashWatchdog] Network request timeout after {self.network_timeout_seconds}s: '
				f'{request_tracker.method} {request_tracker.url[:100]}...'
			)

			self.event_bus.dispatch(
				BrowserErrorEvent(
					error_type='NetworkTimeout',
					message=f'Network request timed out after {self.network_timeout_seconds}s',
					details={
						'url': request_tracker.url,
						'method': request_tracker.method,
						'resource_type': request_tracker.resource_type,
						'elapsed_seconds': now - request_tracker.start_time,
					},
				)
			)

			# Удалить из отслеживания
			del self._active_requests[network_request_id]

	async def _check_browser_health(self) -> None:
		"""Проверить, остаются ли браузер и targets отзывчивыми."""

		try:
			self.logger.debug(f'[CrashWatchdog] Checking browser health for target {self.browser_session.agent_focus_target_id}')
			cdp_connection = await self.browser_session.get_or_create_cdp_session()

			for page_target in self.browser_session.session_manager.get_all_page_targets():
				if self._is_new_tab_page(page_target.url) and page_target.url != 'about:blank':
					self.logger.debug(f'[CrashWatchdog] Redirecting chrome://new-tab-page/ to about:blank {page_target.url}')
					target_session = await self.browser_session.get_or_create_cdp_session(target_id=page_target.target_id)
					await target_session.cdp_client.send.Page.navigate(
						params={'url': 'about:blank'}, session_id=target_session.session_id
					)

			# Быстрый ping для проверки, жива ли сессия
			self.logger.debug(f'[CrashWatchdog] Attempting to run simple JS test expression in session {cdp_connection} 1+1')
			await asyncio.wait_for(
				cdp_connection.cdp_client.send.Runtime.evaluate(params={'expression': '1+1'}, session_id=cdp_connection.session_id),
				timeout=1.0,
			)
			self.logger.debug(
				f'[CrashWatchdog] Browser health check passed for target {self.browser_session.agent_focus_target_id}'
			)
		except Exception as health_check_error:
			self.logger.error(
				f'[CrashWatchdog] ❌ Crashed/unresponsive session detected for target {self.browser_session.agent_focus_target_id} '
				f'error: {type(health_check_error).__name__}: {health_check_error} (Chrome will send detach event, SessionManager will auto-recover)'
			)

		# Проверить процесс браузера, если есть PID
		if self.browser_session._local_browser_watchdog and (browser_process := self.browser_session._local_browser_watchdog._subprocess):
			try:
				if browser_process.status() in (psutil.STATUS_DEAD, psutil.STATUS_ZOMBIE):
					self.logger.error(f'[CrashWatchdog] Browser process {browser_process.pid} has crashed')

					# Процесс браузера упал - SessionManager очистит через события detach
					# Просто отправить событие ошибки и остановить мониторинг
					self.event_bus.dispatch(
						BrowserErrorEvent(
							error_type='BrowserProcessCrashed',
							message=f'Browser process {browser_process.pid} has crashed',
							details={'pid': browser_process.pid, 'status': browser_process.status()},
						)
					)

					self.logger.warning('[CrashWatchdog] Browser process dead - stopping health monitoring')
					await self._stop_monitoring()
					return
			except Exception:
				pass  # psutil недоступен или процесс не существует

	@staticmethod
	def _is_new_tab_page(url: str) -> bool:
		"""Проверить, является ли URL страницей новой вкладки."""
		new_tab_urls = ['about:blank', 'chrome://new-tab-page/', 'chrome://newtab/']
		return url in new_tab_urls


class StorageStateWatchdog(BaseWatchdog):
	"""Мониторит и сохраняет состояние хранилища браузера, включая cookies и localStorage."""

	# Контракты событий
	LISTENS_TO: ClassVar[list[type[BaseEvent]]] = [
		BrowserConnectedEvent,
		BrowserStopEvent,
		SaveStorageStateEvent,
		LoadStorageStateEvent,
	]
	EMITS: ClassVar[list[type[BaseEvent]]] = [
		StorageStateSavedEvent,
		StorageStateLoadedEvent,
	]

	# Конфигурация
	auto_save_interval: float = Field(default=30.0)  # Автосохранение каждые 30 секунд
	save_on_change: bool = Field(default=True)  # Сохранять немедленно при изменении cookies

	# Приватное состояние
	_monitoring_task: asyncio.Task | None = PrivateAttr(default=None)
	_last_cookie_state: list[dict] = PrivateAttr(default_factory=list)
	_save_lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)

	async def on_BrowserConnectedEvent(self, event: BrowserConnectedEvent) -> None:
		"""Запустить мониторинг при запуске браузера."""
		self.logger.debug('[StorageStateWatchdog] 🍪 Initializing auth/cookies sync <-> with storage_state.json file')

		# Запустить мониторинг
		await self._start_monitoring()

		# Автоматически загрузить состояние хранилища после запуска браузера
		await self.event_bus.dispatch(LoadStorageStateEvent())

	async def on_BrowserStopEvent(self, event: BrowserStopEvent) -> None:
		"""Остановить мониторинг при остановке браузера."""
		self.logger.debug('[StorageStateWatchdog] Stopping storage_state monitoring')
		await self._stop_monitoring()

	async def on_SaveStorageStateEvent(self, event: SaveStorageStateEvent) -> None:
		"""Обработать запрос на сохранение состояния хранилища."""
		# Использовать предоставленный путь или вернуться к пути по умолчанию профиля
		save_path = event.path
		if save_path is None:
			# Использовать путь по умолчанию профиля, если доступен
			if self.browser_session.browser_profile.storage_state:
				save_path = str(self.browser_session.browser_profile.storage_state)
			else:
				save_path = None  # Пропустить сохранение, если путь недоступен
		await self._save_storage_state(save_path)

	async def on_LoadStorageStateEvent(self, event: LoadStorageStateEvent) -> None:
		"""Обработать запрос на загрузку состояния хранилища."""
		# Использовать предоставленный путь или вернуться к пути по умолчанию профиля
		load_path = event.path
		if load_path is None:
			# Использовать путь по умолчанию профиля, если доступен
			if self.browser_session.browser_profile.storage_state:
				load_path = str(self.browser_session.browser_profile.storage_state)
			else:
				load_path = None  # Пропустить загрузку, если путь недоступен
		await self._load_storage_state(load_path)

	async def _start_monitoring(self) -> None:
		"""Запустить задачу мониторинга."""
		if self._monitoring_task and not self._monitoring_task.done():
			return

		assert self.browser_session.cdp_client is not None

		self._monitoring_task = create_task_with_error_handling(
			self._monitor_storage_changes(), name='monitor_storage_changes', logger_instance=self.logger, suppress_exceptions=True
		)

	async def _stop_monitoring(self) -> None:
		"""Остановить задачу мониторинга."""
		if self._monitoring_task and not self._monitoring_task.done():
			self._monitoring_task.cancel()
			try:
				await self._monitoring_task
			except asyncio.CancelledError:
				pass

	async def _check_for_cookie_changes_cdp(self, event: dict) -> None:
		"""Проверить, указывает ли CDP событие сети на изменение cookies.

		Этот метод был бы вызван событиями Network.responseReceivedExtraInfo,
		если бы мы настроили слушатели CDP событий.
		"""
		try:
			# Проверить наличие заголовков Set-Cookie в ответе
			response_headers = event.get('headers', {})
			if 'Set-Cookie' in response_headers or 'set-cookie' in response_headers:
				self.logger.debug('[StorageStateWatchdog] Cookie change detected via CDP')

				# Если включено сохранение при изменении, запустить сохранение немедленно
				if self.save_on_change:
					await self._save_storage_state()
		except Exception as check_error:
			self.logger.warning(f'[StorageStateWatchdog] Error checking for cookie changes: {check_error}')

	async def _monitor_storage_changes(self) -> None:
		"""Периодически проверять изменения хранилища и автосохранять."""
		while True:
			try:
				await asyncio.sleep(self.auto_save_interval)

				# Проверить, изменились ли cookies
				if await self._have_cookies_changed():
					self.logger.debug('[StorageStateWatchdog] Detected changes to sync with storage_state.json')
					await self._save_storage_state()

			except asyncio.CancelledError:
				break
			except Exception as monitor_error:
				self.logger.error(f'[StorageStateWatchdog] Error in monitoring loop: {monitor_error}')

	async def _have_cookies_changed(self) -> bool:
		"""Проверить, изменились ли cookies с последнего сохранения."""
		if not self.browser_session.cdp_client:
			return False

		try:
			# Получить текущие cookies с помощью CDP
			latest_cookies = await self.browser_session._cdp_get_cookies()

			# Преобразовать в сравнимый формат, используя .get() для опциональных полей
			latest_cookie_dict = {
				(cookie_data.get('name', ''), cookie_data.get('domain', ''), cookie_data.get('path', '')): cookie_data.get('value', '')
				for cookie_data in latest_cookies
			}

			previous_cookie_dict = {
				(cookie_data.get('name', ''), cookie_data.get('domain', ''), cookie_data.get('path', '')): cookie_data.get('value', '')
				for cookie_data in self._last_cookie_state
			}

			return latest_cookie_dict != previous_cookie_dict
		except Exception as compare_error:
			self.logger.debug(f'[StorageStateWatchdog] Error comparing cookies: {compare_error}')
			return False

	async def _save_storage_state(self, path: str | None = None) -> None:
		"""Сохранить состояние хранилища браузера в файл."""
		async with self._save_lock:
			# Проверить, доступен ли CDP клиент
			assert await self.browser_session.get_or_create_cdp_session(target_id=None)

			file_path = path or self.browser_session.browser_profile.storage_state
			if not file_path:
				return

			# Пропустить сохранение, если состояние хранилища уже является dict (указывает на загрузку из памяти)
			# Мы сохраняем в файл только если это началось как путь к файлу
			if isinstance(file_path, dict):
				self.logger.debug('[StorageStateWatchdog] Storage state is already a dict, skipping file save')
				return

			try:
				# Получить текущее состояние хранилища с помощью CDP
				current_storage_state = await self.browser_session._cdp_get_storage_state()

				# Обновить наше последнее известное состояние
				self._last_cookie_state = current_storage_state.get('cookies', []).copy()

				# Преобразовать путь в объект Path
				final_path = Path(file_path).expanduser().resolve()
				final_path.parent.mkdir(parents=True, exist_ok=True)

				# Объединить с существующим состоянием, если файл существует
				final_state = current_storage_state
				if final_path.exists():
					try:
						saved_state = json.loads(final_path.read_text())
						final_state = self._merge_storage_states(saved_state, dict(current_storage_state))
					except Exception as merge_error:
						self.logger.error(f'[StorageStateWatchdog] Failed to merge with existing state: {merge_error}')

				# Записать атомарно
				temporary_path = final_path.with_suffix('.json.tmp')
				temporary_path.write_text(json.dumps(final_state, indent=4))

				# Создать резервную копию существующего файла
				if final_path.exists():
					backup_file_path = final_path.with_suffix('.json.bak')
					final_path.replace(backup_file_path)

				# Переместить временный файл в финальный
				temporary_path.replace(final_path)

				# Отправить событие успеха
				self.event_bus.dispatch(
					StorageStateSavedEvent(
						path=str(final_path),
						cookies_count=len(final_state.get('cookies', [])),
						origins_count=len(final_state.get('origins', [])),
					)
				)

				self.logger.debug(
					f'[StorageStateWatchdog] Saved storage state to {final_path} '
					f'({len(final_state.get("cookies", []))} cookies, '
					f'{len(final_state.get("origins", []))} origins)'
				)

			except Exception as save_error:
				self.logger.error(f'[StorageStateWatchdog] Failed to save storage state: {save_error}')

	async def _load_storage_state(self, path: str | None = None) -> None:
		"""Загрузить состояние хранилища браузера из файла."""
		if not self.browser_session.cdp_client:
			self.logger.warning('[StorageStateWatchdog] No CDP client available for loading')
			return

		file_path = path or self.browser_session.browser_profile.storage_state
		if not file_path or not os.path.exists(str(file_path)):
			return

		try:
			# Прочитать файл состояния хранилища асинхронно
			import anyio

			file_content = await anyio.Path(str(file_path)).read_text()
			loaded_storage = json.loads(file_content)

			# Применить cookies, если присутствуют
			if 'cookies' in loaded_storage and loaded_storage['cookies']:
				await self.browser_session._cdp_set_cookies(loaded_storage['cookies'])
				self._last_cookie_state = loaded_storage['cookies'].copy()
				self.logger.debug(f'[StorageStateWatchdog] Added {len(loaded_storage["cookies"])} cookies from storage state')

			# Применить origins (localStorage/sessionStorage), если присутствуют
			if 'origins' in loaded_storage and loaded_storage['origins']:
				for storage_origin in loaded_storage['origins']:
					if 'sessionStorage' in storage_origin:
						for storage_item in storage_origin['sessionStorage']:
							init_script = f"""
								window.sessionStorage.setItem({json.dumps(storage_item['name'])}, {json.dumps(storage_item['value'])});
							"""
							await self.browser_session._cdp_add_init_script(init_script)
					if 'localStorage' in storage_origin:
						for storage_item in storage_origin['localStorage']:
							init_script = f"""
								window.localStorage.setItem({json.dumps(storage_item['name'])}, {json.dumps(storage_item['value'])});
							"""
							await self.browser_session._cdp_add_init_script(init_script)
				self.logger.debug(
					f'[StorageStateWatchdog] Applied localStorage/sessionStorage from {len(loaded_storage["origins"])} origins'
				)

			self.event_bus.dispatch(
				StorageStateLoadedEvent(
					path=str(file_path),
					cookies_count=len(loaded_storage.get('cookies', [])),
					origins_count=len(loaded_storage.get('origins', [])),
				)
			)

			self.logger.debug(f'[StorageStateWatchdog] Loaded storage state from: {file_path}')

		except Exception as load_error:
			self.logger.error(f'[StorageStateWatchdog] Failed to load storage state: {load_error}')

	@staticmethod
	def _merge_storage_states(existing: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
		"""Объединить два состояния хранилища, при этом новые значения имеют приоритет."""
		result_state = existing.copy()

		# Объединить cookies
		cookie_map = {(cookie_data['name'], cookie_data['domain'], cookie_data['path']): cookie_data for cookie_data in existing.get('cookies', [])}

		for cookie_data in new.get('cookies', []):
			cookie_key = (cookie_data['name'], cookie_data['domain'], cookie_data['path'])
			cookie_map[cookie_key] = cookie_data

		result_state['cookies'] = list(cookie_map.values())

		# Объединить origins
		origin_map = {origin_data['origin']: origin_data for origin_data in existing.get('origins', [])}

		for origin_data in new.get('origins', []):
			origin_map[origin_data['origin']] = origin_data

		result_state['origins'] = list(origin_map.values())

		return result_state

	async def get_current_cookies(self) -> list[dict[str, Any]]:
		"""Получить текущие cookies с помощью CDP."""
		if not self.browser_session.cdp_client:
			return []

		try:
			cookie_list = await self.browser_session._cdp_get_cookies()
			# Cookie - это TypedDict, преобразовать в dict для совместимости
			return [dict(cookie_item) for cookie_item in cookie_list]
		except Exception as get_error:
			self.logger.error(f'[StorageStateWatchdog] Failed to get cookies: {get_error}')
			return []

	async def add_cookies(self, cookies: list[dict[str, Any]]) -> None:
		"""Добавить cookies с помощью CDP."""
		if not self.browser_session.cdp_client:
			self.logger.warning('[StorageStateWatchdog] No CDP client available for adding cookies')
			return

		try:
			# Преобразовать dicts в объекты Cookie
			cookie_instances = [Cookie(**cookie_dict) if isinstance(cookie_dict, dict) else cookie_dict for cookie_dict in cookies]
			# Установить cookies с помощью CDP
			await self.browser_session._cdp_set_cookies(cookie_instances)
			self.logger.debug(f'[StorageStateWatchdog] Added {len(cookies)} cookies')
		except Exception as add_error:
			self.logger.error(f'[StorageStateWatchdog] Failed to add cookies: {add_error}')

