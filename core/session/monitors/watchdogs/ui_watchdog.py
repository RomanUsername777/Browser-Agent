"""Watchdog для обработки UI элементов: JavaScript диалоги и политики безопасности URL."""

import asyncio
from typing import TYPE_CHECKING, ClassVar

from bubus import BaseEvent
from pydantic import PrivateAttr

from core.session.events import (
	BrowserErrorEvent,
	NavigateToUrlEvent,
	NavigationCompleteEvent,
	TabCreatedEvent,
)
from core.session.watchdog_base import BaseWatchdog

if TYPE_CHECKING:
	pass

# Отслеживание показа предупреждения о glob-паттернах
_GLOB_WARNING_SHOWN = False


class PopupsWatchdog(BaseWatchdog):
	"""Обрабатывает JavaScript диалоги (alert, confirm, prompt), автоматически принимая их немедленно."""

	# События, которые слушает и отправляет этот watchdog
	LISTENS_TO: ClassVar[list[type[BaseEvent]]] = [TabCreatedEvent]
	EMITS: ClassVar[list[type[BaseEvent]]] = []

	# Отслеживание, для каких targets зарегистрированы обработчики диалогов
	_dialog_listeners_registered: set[str] = PrivateAttr(default_factory=set)

	def __init__(self, **kwargs):
		super().__init__(**kwargs)
		self.logger.debug(f'🚀 PopupsWatchdog initialized with browser_session={self.browser_session}, ID={id(self)}')

	async def on_TabCreatedEvent(self, event: TabCreatedEvent) -> None:
		"""Настроить обработку JavaScript диалогов при создании новой вкладки."""
		target_identifier = event.target_id
		self.logger.debug(f'🎯 PopupsWatchdog received TabCreatedEvent for target {target_identifier}')

		# Пропустить, если мы уже зарегистрировали для этого target
		if target_identifier in self._dialog_listeners_registered:
			self.logger.debug(f'Already registered dialog handlers for target {target_identifier}')
			return

		self.logger.debug(f'📌 Starting dialog handler setup for target {target_identifier}')
		try:
			# Получить все CDP сессии для этого target и любых дочерних фреймов
			cdp_connection = await self.browser_session.get_or_create_cdp_session(
				target_identifier, focus=False
			)  # не автофокусировать новые вкладки! иногда нужно открывать вкладки в фоне

			# КРИТИЧЕСКИ ВАЖНО: Включить домен Page для получения событий диалогов
			try:
				await cdp_connection.cdp_client.send.Page.enable(session_id=cdp_connection.session_id)
				self.logger.debug(f'✅ Enabled Page domain for session {cdp_connection.session_id[-8:]}')
			except Exception as enable_error:
				self.logger.debug(f'Failed to enable Page domain: {enable_error}')

			# Также зарегистрировать для корневого CDP клиента, чтобы перехватывать диалоги из любого фрейма
			if self.browser_session._cdp_client_root:
				self.logger.debug('📌 Also registering handler on root CDP client')
				try:
					# Включить домен Page на корневом клиенте тоже
					await self.browser_session._cdp_client_root.send.Page.enable()
					self.logger.debug('✅ Enabled Page domain on root CDP client')
				except Exception as root_enable_error:
					self.logger.debug(f'Failed to enable Page domain on root: {root_enable_error}')

			# Настроить асинхронный обработчик для JavaScript диалогов - принимать немедленно без отправки события
			async def handle_dialog(dialog_event, dialog_session_id: str | None = None):
				"""Обработать события JavaScript диалогов - принять немедленно."""
				try:
					js_dialog_type = dialog_event.get('type', 'alert')
					dialog_message = dialog_event.get('message', '')

					# Сохранить сообщение всплывающего окна в сессии браузера для включения в состояние браузера
					if dialog_message:
						popup_text = f'[{js_dialog_type}] {dialog_message}'
						self.browser_session._closed_popup_messages.append(popup_text)
						self.logger.debug(f'📝 Stored popup message: {popup_text[:100]}')

					accept_dialog = js_dialog_type in ('alert', 'confirm', 'beforeunload')

					action_description = 'accepting (OK)' if accept_dialog else 'dismissing (Cancel)'
					self.logger.info(f"🔔 JavaScript {js_dialog_type} dialog: '{dialog_message[:100]}' - {action_description}...")

					is_dismissed = False

					# Подход 1: Использовать сессию, которая обнаружила диалог (наиболее надежно)
					if self.browser_session._cdp_client_root and dialog_session_id:
						try:
							self.logger.debug(f'🔄 Approach 1: Using detecting session {dialog_session_id[-8:]}')
							await asyncio.wait_for(
								self.browser_session._cdp_client_root.send.Page.handleJavaScriptDialog(
									params={'accept': accept_dialog},
									session_id=dialog_session_id,
								),
								timeout=0.5,
							)
							is_dismissed = True
							self.logger.info('✅ Dialog handled successfully via detecting session')
						except (TimeoutError, Exception) as approach1_error:
							self.logger.debug(f'Approach 1 failed: {type(approach1_error).__name__}')

					# Подход 2: Попробовать с текущей сессией фокуса агента
					if not is_dismissed and self.browser_session._cdp_client_root and self.browser_session.agent_focus_target_id:
						try:
							# Использовать публичный API с focus=False, чтобы избежать изменения фокуса во время закрытия всплывающего окна
							focus_session = await self.browser_session.get_or_create_cdp_session(
								self.browser_session.agent_focus_target_id, focus=False
							)
							self.logger.debug(f'🔄 Approach 2: Using agent focus session {focus_session.session_id[-8:]}')
							await asyncio.wait_for(
								self.browser_session._cdp_client_root.send.Page.handleJavaScriptDialog(
									params={'accept': accept_dialog},
									session_id=focus_session.session_id,
								),
								timeout=0.5,
							)
							is_dismissed = True
							self.logger.info('✅ Dialog handled successfully via agent focus session')
						except (TimeoutError, Exception) as approach2_error:
							self.logger.debug(f'Approach 2 failed: {type(approach2_error).__name__}')

				except Exception as handler_error:
					self.logger.error(f'❌ Critical error in dialog handler: {type(handler_error).__name__}: {handler_error}')

			# Зарегистрировать обработчик на конкретной сессии
			cdp_connection.cdp_client.register.Page.javascriptDialogOpening(handle_dialog)  # type: ignore[arg-type]
			self.logger.debug(
				f'Successfully registered Page.javascriptDialogOpening handler for session {cdp_connection.session_id}'
			)

			# Также зарегистрировать на корневом CDP клиенте, чтобы перехватывать диалоги из любого фрейма
			if hasattr(self.browser_session._cdp_client_root, 'register'):
				try:
					self.browser_session._cdp_client_root.register.Page.javascriptDialogOpening(handle_dialog)  # type: ignore[arg-type]
					self.logger.debug('Successfully registered dialog handler on root CDP client for all frames')
				except Exception as root_register_error:
					self.logger.warning(f'Failed to register on root CDP client: {root_register_error}')

			# Пометить этот target как имеющий настроенную обработку диалогов
			self._dialog_listeners_registered.add(target_identifier)

			self.logger.debug(f'Set up JavaScript dialog handling for tab {target_identifier}')

		except Exception as setup_error:
			self.logger.warning(f'Failed to set up popup handling for tab {target_identifier}: {setup_error}')


class SecurityWatchdog(BaseWatchdog):
	"""Мониторит и применяет политики безопасности для доступа к URL."""

	# Контракты событий
	LISTENS_TO: ClassVar[list[type[BaseEvent]]] = [
		NavigateToUrlEvent,
		NavigationCompleteEvent,
		TabCreatedEvent,
	]
	EMITS: ClassVar[list[type[BaseEvent]]] = [
		BrowserErrorEvent,
	]

	async def on_NavigateToUrlEvent(self, event: NavigateToUrlEvent) -> None:
		"""Проверить, разрешен ли URL навигации перед началом навигации."""
		# Проверка безопасности ПЕРЕД навигацией
		if not self._is_url_allowed(event.url):
			self.logger.warning(f'⛔️ Blocking navigation to disallowed URL: {event.url}')
			self.event_bus.dispatch(
				BrowserErrorEvent(
					error_type='NavigationBlocked',
					message=f'Navigation blocked to disallowed URL: {event.url}',
					details={'url': event.url, 'reason': 'not_in_allowed_domains'},
				)
			)
			# Остановить распространение события путем выброса исключения
			raise ValueError(f'Navigation to {event.url} blocked by security policy')

	async def on_NavigationCompleteEvent(self, event: NavigationCompleteEvent) -> None:
		"""Проверить, разрешен ли навигированный URL (перехватывает редиректы на заблокированные домены)."""
		# Проверить, разрешен ли навигированный URL (на случай редиректов)
		if not self._is_url_allowed(event.url):
			self.logger.warning(f'⛔️ Navigation to non-allowed URL detected: {event.url}')

			# Отправить ошибку браузера
			self.event_bus.dispatch(
				BrowserErrorEvent(
					error_type='NavigationBlocked',
					message=f'Navigation blocked to non-allowed URL: {event.url} - redirecting to about:blank',
					details={'url': event.url, 'target_id': event.target_id},
				)
			)
			# Навигировать к about:blank, чтобы сохранить сессию живой
			# Агент увидит ошибку и сможет продолжить с другими задачами
			try:
				cdp_connection = await self.browser_session.get_or_create_cdp_session(target_id=event.target_id)
				await cdp_connection.cdp_client.send.Page.navigate(params={'url': 'about:blank'}, session_id=cdp_connection.session_id)
				self.logger.info(f'⛔️ Navigated to about:blank after blocked URL: {event.url}')
			except Exception as navigation_error:
				pass
				self.logger.error(f'⛔️ Failed to navigate to about:blank: {type(navigation_error).__name__} {navigation_error}')

	async def on_TabCreatedEvent(self, event: TabCreatedEvent) -> None:
		"""Проверить, разрешен ли URL новой вкладки."""
		if not self._is_url_allowed(event.url):
			self.logger.warning(f'⛔️ New tab created with disallowed URL: {event.url}')

			# Отправить ошибку и попытаться закрыть вкладку
			self.event_bus.dispatch(
				BrowserErrorEvent(
					error_type='TabCreationBlocked',
					message=f'Tab created with non-allowed URL: {event.url}',
					details={'url': event.url, 'target_id': event.target_id},
				)
			)

			# Попытаться закрыть проблемную вкладку
			try:
				await self.browser_session._cdp_close_page(event.target_id)
				self.logger.info(f'⛔️ Closed new tab with non-allowed URL: {event.url}')
			except Exception as close_error:
				self.logger.error(f'⛔️ Failed to close new tab with non-allowed URL: {type(close_error).__name__} {close_error}')

	def _is_root_domain(self, domain: str) -> bool:
		"""Проверить, является ли домен корневым (без поддомена).

		Простая эвристика: добавлять www только для доменов с ровно одной точкой (domain.tld).
		Для сложных случаев, таких как национальные TLD или поддомены, пользователи должны настроить явно.

		Args:
			domain: Домен для проверки

		Returns:
			True, если это простой корневой домен, False в противном случае
		"""
		# Пропустить, если содержит wildcards или протокол
		if '://' in domain or '*' in domain:
			return False

		return domain.count('.') == 1

	def _log_glob_warning(self) -> None:
		"""Записать предупреждение о glob-паттернах в allowed_domains."""
		global _GLOB_WARNING_SHOWN
		if not _GLOB_WARNING_SHOWN:
			_GLOB_WARNING_SHOWN = True
			self.logger.warning(
				'⚠️ Using glob patterns in allowed_domains. '
				'Note: Patterns like "*.example.com" will match both subdomains AND the main domain.'
			)

	def _get_domain_variants(self, host: str) -> tuple[str, str]:
		"""Получить оба варианта домена (с префиксом www и без).

		Args:
			host: Хостнейм для обработки

		Returns:
			Кортеж (original_host, variant_host)
			- Если host начинается с www., вариант без www.
			- Иначе вариант с префиксом www.
		"""
		if host.startswith('www.'):
			return (host, host[4:])  # ('www.example.com', 'example.com')
		else:
			return (host, f'www.{host}')  # ('example.com', 'www.example.com')

	def _is_ip_address(self, host: str) -> bool:
		"""Проверить, является ли hostname IP-адресом (IPv4 или IPv6).

		Args:
			host: Hostname для проверки

		Returns:
			True, если host является IP-адресом, False в противном случае
		"""
		import ipaddress

		try:
			# Попытаться распарсить как IP-адрес (обрабатывает и IPv4, и IPv6)
			ipaddress.ip_address(host)
			return True
		except ValueError:
			return False
		except Exception:
			return False

	def _is_url_allowed(self, url: str) -> bool:
		"""Проверить, разрешен ли URL на основе конфигурации allowed_domains.

		Args:
			url: URL для проверки

		Returns:
			True, если URL разрешен, False в противном случае
		"""

		# Всегда разрешать внутренние цели браузера (перед любыми другими проверками)
		internal_targets = ['about:blank', 'chrome://new-tab-page/', 'chrome://new-tab-page', 'chrome://newtab/']
		if url in internal_targets:
			return True

		# Распарсить URL для извлечения компонентов
		from urllib.parse import urlparse

		try:
			url_components = urlparse(url)
		except Exception:
			# Некорректный URL
			return False

		# Разрешить data: и blob: URL (они не имеют hostname)
		if url_components.scheme in ['blob', 'data']:
			return True

		# Получить реальный хост (домен)
		hostname = url_components.hostname
		if not hostname:
			return False

		# Проверить, должны ли быть заблокированы IP-адреса (перед проверками доменов)
		if self.browser_session.browser_profile.block_ip_addresses:
			if self._is_ip_address(hostname):
				return False

		# Если allowed_domains не указаны, разрешить все URL
		allowed = self.browser_session.browser_profile.allowed_domains
		prohibited = self.browser_session.browser_profile.prohibited_domains
		if not allowed and not prohibited:
			return True

		# Проверить разрешенные домены (быстрый путь для sets, медленный для lists с паттернами)
		if allowed:
			if isinstance(allowed, set):
				# Быстрый путь: O(1) точное совпадение hostname - проверить оба варианта (www и без www)
				primary_variant, alternate_variant = self._get_domain_variants(hostname)
				return primary_variant in allowed or alternate_variant in allowed
			else:
				# Медленный путь: O(n) сопоставление паттернов для списков
				for domain_pattern in allowed:
					if self._is_url_match(url, hostname, url_components.scheme, domain_pattern):
						return True
				return False

		# Проверить запрещенные домены (быстрый путь для sets, медленный для lists с паттернами)
		if prohibited:
			if isinstance(prohibited, set):
				# Быстрый путь: O(1) точное совпадение hostname - проверить оба варианта (www и без www)
				primary_variant, alternate_variant = self._get_domain_variants(hostname)
				return alternate_variant not in prohibited and primary_variant not in prohibited
			else:
				# Медленный путь: O(n) сопоставление паттернов для списков
				for domain_pattern in prohibited:
					if self._is_url_match(url, hostname, url_components.scheme, domain_pattern):
						return False
				return True

		return True

	def _is_url_match(self, url: str, host: str, scheme: str, pattern: str) -> bool:
		"""Проверить, соответствует ли URL паттерну."""

		# Полный URL для сопоставления (scheme + host)
		url_pattern = f'{scheme}://{host}'

		# Обработать glob-паттерны
		if '*' in pattern:
			self._log_glob_warning()
			import fnmatch

			# Проверить, соответствует ли паттерн хосту
			if pattern.startswith('*.'):
				# Паттерн вида *.example.com должен соответствовать поддоменам и основному домену
				base_domain = pattern[2:]  # Удалить *.
				if host.endswith('.' + base_domain) or host == base_domain:
					# Соответствовать только http/https URL для доменных паттернов
					if scheme in ['https', 'http']:
						return True
			elif pattern.endswith('/*'):
				# Паттерн вида brave://* или http*://example.com/*
				if fnmatch.fnmatch(url, pattern):
					return True
			else:
				# Использовать fnmatch для других glob-паттернов
				target_string = url_pattern if '://' in pattern else host
				if fnmatch.fnmatch(target_string, pattern):
					return True
		else:
			# Точное совпадение
			if '://' in pattern:
				# Полный URL-паттерн
				if url.startswith(pattern):
					return True
			else:
				# Только доменный паттерн (без учета регистра)
				host_normalized = host.lower()
				pattern_normalized = pattern.lower()
				if host_normalized == pattern_normalized:
					return True
				# Если паттерн - корневой домен, также проверить поддомен www
				if self._is_root_domain(pattern) and host_normalized == f'www.{pattern_normalized}':
					return True

		return False

