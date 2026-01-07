"""Watchdog для управления DOM-деревом браузера с использованием CDP."""

import asyncio
import time
from typing import TYPE_CHECKING

from core.session.events import (
	BrowserErrorEvent,
	BrowserStateRequestEvent,
	ScreenshotEvent,
	TabCreatedEvent,
)
from core.session.watchdog_base import BaseWatchdog
from core.dom_processing.manager import DomService
from core.dom_processing.models import (
	EnhancedDOMTreeNode,
	SerializedDOMState,
)
from core.observability import observe_debug
from core.helpers import create_task_with_error_handling, time_execution_async

if TYPE_CHECKING:
	from core.session.models import BrowserStateSummary, NetworkRequest, PageInfo, PaginationButton


class DOMWatchdog(BaseWatchdog):
	"""Обрабатывает построение DOM-дерева, сериализацию и доступ к элементам через CDP.

	Этот watchdog действует как мост между событийно-ориентированной сессией браузера
	и реализацией DomService, поддерживая кэшированное состояние и предоставляя
	вспомогательные методы для других watchdogs.
	"""

	LISTENS_TO = [TabCreatedEvent, BrowserStateRequestEvent]
	EMITS = [BrowserErrorEvent]

	# Публичные свойства для других watchdogs
	selector_map: dict[int, EnhancedDOMTreeNode] | None = None
	current_dom_state: SerializedDOMState | None = None
	enhanced_dom_tree: EnhancedDOMTreeNode | None = None

	# Внутренний DOM-сервис
	_dom_service: DomService | None = None

	# Отслеживание сети - сопоставляет request_id с (url, start_time, method, resource_type)
	_pending_requests: dict[str, tuple[str, float, str, str | None]] = {}

	async def on_TabCreatedEvent(self, event: TabCreatedEvent) -> None:
		# self.logger.debug('Setting up init scripts in browser')
		return None

	def _get_recent_events_str(self, limit: int = 10) -> str | None:
		"""Получить самые последние события из шины событий в формате JSON.

		Args:
			limit: Максимальное количество последних событий для включения

		Returns:
			JSON-строка последних событий или None, если недоступно
		"""
		import json

		try:
			# Получить все события из истории, отсортированные по времени создания (самые последние первыми)
			all_events = sorted(
				self.browser_session.event_bus.event_history.values(), key=lambda e: e.event_created_at.timestamp(), reverse=True
			)

			# Взять самые последние события и создать JSON-сериализуемые данные
			recent_events_data = []
			for event in all_events[:limit]:
				event_data = {
					'timestamp': event.event_created_at.isoformat(),
					'event_type': event.event_type,
				}
				# Добавить специфичные поля для определенных типов событий
				if hasattr(event, 'target_id'):
					event_data['target_id'] = getattr(event, 'target_id')
				if hasattr(event, 'error_message'):
					event_data['error_message'] = getattr(event, 'error_message')
				if hasattr(event, 'url'):
					event_data['url'] = getattr(event, 'url')
				recent_events_data.append(event_data)

			return json.dumps(recent_events_data)  # Вернуть пустой массив, если нет событий
		except Exception as e:
			self.logger.debug(f'Не удалось получить последние события: {e}')

		return json.dumps([])  # Вернуть пустой JSON-массив при ошибке

	async def _get_pending_network_requests(self) -> list['NetworkRequest']:
		"""Получить список текущих ожидающих сетевых запросов.

		Использует document.readyState и performance API для обнаружения ожидающих запросов.
		Фильтрует рекламу, трекинг и другой шум.

		Returns:
			Список объектов NetworkRequest, представляющих текущие загружающиеся ресурсы
		"""
		from core.session.models import NetworkRequest

		try:
			# get_or_create_cdp_session() теперь автоматически обрабатывает валидацию фокуса
			cdp_session = await self.browser_session.get_or_create_cdp_session(focus=True)

			# Использовать performance API для получения ожидающих запросов
			js_code = """
(function() {
	const now = performance.now();
	const resources = performance.getEntriesByType('resource');
	const pending = [];

	// Проверить document readyState
	const docLoading = document.readyState !== 'complete';

	// Общие домены и паттерны рекламы/трекинга для фильтрации
	const adDomains = [
		// Стандартные сети рекламы/трекинга
		'doubleclick.net', 'googlesyndication.com', 'googletagmanager.com',
		'facebook.net', 'analytics', 'ads', 'tracking', 'pixel',
		'hotjar.com', 'clarity.ms', 'mixpanel.com', 'segment.com',
		// Платформы аналитики
		'demdex.net', 'omtrdc.net', 'adobedtm.com', 'ensighten.com',
		'newrelic.com', 'nr-data.net', 'google-analytics.com',
		// Трекеры социальных сетей
		'connect.facebook.net', 'platform.twitter.com', 'platform.linkedin.com',
		// CDN/хостинги изображений (обычно не критичны для функциональности)
		'.cloudfront.net/image/', '.akamaized.net/image/',
		// Общие пути трекинга
		'/tracker/', '/collector/', '/beacon/', '/telemetry/', '/log/',
		'/events/', '/eventBatch', '/track.', '/metrics/'
	];

	// Получить ресурсы, которые все еще загружаются (responseEnd равен 0)
	let filteredByResponseEnd = 0;
	let totalResourcesChecked = 0;
	const allDomains = new Set();

	for (const entry of resources) {
		totalResourcesChecked++;

		// Отслеживать все домены из недавних ресурсов (для логирования)
		try {
			const hostname = new URL(entry.name).hostname;
			if (hostname) allDomains.add(hostname);
		} catch (e) {}

		if (entry.responseEnd === 0) {
			filteredByResponseEnd++;
			const url = entry.name;

			// Отфильтровать рекламу и трекинг
			const isAd = adDomains.some(domain => url.includes(domain));
			if (isAd) continue;

			// Отфильтровать data: URL и очень длинные URL (часто встроенные ресурсы)
			if (url.length > 500 || url.startsWith('data:')) continue;

			const loadingDuration = now - entry.startTime;

			// Пропустить запросы, которые загружаются >10 секунд (вероятно зависли/опрос)
			if (loadingDuration > 10000) continue;

			const resourceType = entry.initiatorType || 'unknown';

			// Отфильтровать некритичные ресурсы (изображения, шрифты, иконки), если загрузка >3 секунд
			const nonCriticalTypes = ['font', 'icon', 'image', 'img'];
			if (nonCriticalTypes.includes(resourceType) && loadingDuration > 3000) continue;

			// Отфильтровать URL изображений, даже если тип неизвестен
			const isImageUrl = /\\.(gif|ico|jpeg|jpg|png|svg|webp)(\\?|$)/i.test(url);
			if (isImageUrl && loadingDuration > 3000) continue;

			pending.push({
				method: 'GET',
				url: url,
				loading_duration_ms: Math.round(loadingDuration),
				resource_type: resourceType
			});
		}
	}

	return {
		document_loading: docLoading,
		document_ready_state: document.readyState,
		pending_requests: pending,
		debug: {
			after_all_filters: pending.length,
			all_domains: Array.from(allDomains),
			total_resources: totalResourcesChecked,
			with_response_end_zero: filteredByResponseEnd
		}
	};
})()
"""

			result = await cdp_session.cdp_client.send.Runtime.evaluate(
				params={'returnByValue': True, 'expression': js_code}, session_id=cdp_session.session_id
			)

			if result.get('result', {}).get('type') == 'object':
				data = result['result'].get('value', {})
				debug_info = data.get('debug', {})
				doc_loading = data.get('document_loading', False)
				doc_state = data.get('document_ready_state', 'unknown')
				pending = data.get('pending_requests', [])

				# Получить все домены, которые имели недавнюю активность (из JS)
				all_domains = debug_info.get('all_domains', [])
				all_domains_str = ', '.join(sorted(all_domains)[:5]) if all_domains else 'none'
				if len(all_domains) > 5:
					all_domains_str += f' +{len(all_domains) - 5} more'

				# Отладочное логирование
				self.logger.debug(
					f'🔍 Network check: document.readyState={doc_state}, loading={doc_loading}, '
					f'total_resources={debug_info.get("total_resources", 0)}, '
					f'responseEnd=0: {debug_info.get("with_response_end_zero", 0)}, '
					f'after_filters={len(pending)}, domains=[{all_domains_str}]'
				)

				# Преобразовать в объекты NetworkRequest
				network_requests = []
				for req in pending[:20]:  # Ограничить до 20, чтобы не перегружать контекст
					network_requests.append(
						NetworkRequest(
							loading_duration_ms=req.get('loading_duration_ms', 0.0),
							method=req.get('method', 'GET'),
							resource_type=req.get('resource_type'),
							url=req['url'],
						)
					)

				return network_requests

		except Exception as e:
			self.logger.debug(f'Не удалось получить ожидающие сетевые запросы: {e}')

		return []

	@observe_debug(ignore_input=True, ignore_output=True, name='browser_state_request_event')
	async def on_BrowserStateRequestEvent(self, event: BrowserStateRequestEvent) -> 'BrowserStateSummary':
		"""Обработать запрос состояния браузера, координируя построение DOM и захват скриншота.

		Это основная точка входа для получения полного состояния браузера.

		Args:
			event: Событие запроса состояния браузера с опциями

		Returns:
			Полный BrowserStateSummary с DOM, скриншотом и информацией о цели
		"""
		from core.session.models import BrowserStateSummary, PageInfo

		self.logger.debug('🔍 DOMWatchdog.on_BrowserStateRequestEvent: STARTING browser state request')
		page_url = await self.browser_session.get_current_page_url()
		self.logger.debug(f'🔍 DOMWatchdog.on_BrowserStateRequestEvent: Got page URL: {page_url}')

		# Получить сфокусированную сессию для логирования (валидация уже выполнена get_current_page_url)
		if self.browser_session.agent_focus_target_id:
			self.logger.debug(f'Current page URL: {page_url}, target_id: {self.browser_session.agent_focus_target_id}')

		# проверить, следует ли пропустить построение DOM-дерева для бессмысленных страниц
		not_a_meaningful_website = page_url.lower().split(':', 1)[0] not in ('http', 'https')

		# Проверить ожидающие сетевые запросы ПЕРЕД ожиданием (чтобы увидеть, что загружается)
		pending_requests_before_wait = []
		if not not_a_meaningful_website:
			try:
				pending_requests_before_wait = await self._get_pending_network_requests()
				if pending_requests_before_wait:
					self.logger.debug(f'🔍 Found {len(pending_requests_before_wait)} pending requests before stability wait')
			except Exception as e:
				self.logger.debug(f'Не удалось получить ожидающие запросы перед ожиданием: {e}')
		pending_requests = pending_requests_before_wait
		# Ожидать стабильности страницы, используя настройки профиля браузера (паттерн основной ветки)
		if not not_a_meaningful_website:
			self.logger.debug('🔍 DOMWatchdog.on_BrowserStateRequestEvent: ⏳ Waiting for page stability...')
			try:
				# Минимальное ожидание для рендеринга DOM (даже если нет активных запросов)
				min_wait = self.browser_session.browser_profile.minimum_wait_page_load_time or 0.25
				await asyncio.sleep(min_wait)
				
				# Дополнительное ожидание, если есть активные сетевые запросы
				if pending_requests_before_wait:
					# Уменьшено до 0.3s для более быстрого построения DOM, но все еще позволяя критическим ресурсам загрузиться
					network_wait = self.browser_session.browser_profile.wait_for_network_idle_page_load_time or 0.3
					await asyncio.sleep(network_wait)
				
				self.logger.debug('🔍 DOMWatchdog.on_BrowserStateRequestEvent: ✅ Page stability complete')
			except Exception as e:
				self.logger.debug(
					f'🔍 DOMWatchdog.on_BrowserStateRequestEvent: Ожидание сети не удалось: {e}, продолжаем в любом случае...'
				)

		# Получить информацию о вкладках один раз в начале для всех путей
		self.logger.debug('🔍 DOMWatchdog.on_BrowserStateRequestEvent: Getting tabs info...')
		tabs_info = await self.browser_session.get_tabs()
		self.logger.debug(f'🔍 DOMWatchdog.on_BrowserStateRequestEvent: Got {len(tabs_info)} tabs')
		self.logger.debug(f'🔍 DOMWatchdog.on_BrowserStateRequestEvent: Tabs info: {tabs_info}')


		try:
			# Быстрый путь для пустых страниц
			if not_a_meaningful_website:
				self.logger.debug(f'⚡ Skipping BuildDOMTree for empty target: {page_url}')
				self.logger.debug(f'📸 Not taking screenshot for empty page: {page_url} (non-http/https URL)')

				# Создать минимальное состояние DOM
				content = SerializedDOMState(_root=None, selector_map={})

				# Пропустить скриншот для пустых страниц
				screenshot_b64 = None

				# Попытаться получить информацию о странице из CDP, использовать значения по умолчанию, если недоступно
				try:
					page_info = await self._get_page_info()
				except Exception as e:
					self.logger.debug(f'Не удалось получить информацию о странице из CDP для пустой страницы: {e}, используем резервный вариант')
					# Использовать размеры viewport по умолчанию
					viewport = self.browser_session.browser_profile.viewport or {'height': 720, 'width': 1280}
					page_info = PageInfo(
						page_height=viewport['height'],
						page_width=viewport['width'],
						pixels_above=0,
						pixels_below=0,
						pixels_left=0,
						pixels_right=0,
						scroll_x=0,
						scroll_y=0,
						viewport_height=viewport['height'],
						viewport_width=viewport['width'],
					)

				return BrowserStateSummary(
					browser_errors=[],
					closed_popup_messages=self.browser_session._closed_popup_messages.copy(),
					dom_state=content,
					is_pdf_viewer=False,
					page_info=page_info,
					pagination_buttons=[],  # Пустая страница не имеет пагинации
					pending_network_requests=[],  # Пустая страница не имеет ожидающих запросов
					pixels_above=0,
					pixels_below=0,
					recent_events=self._get_recent_events_str() if event.include_recent_events else None,
					screenshot=screenshot_b64,
					tabs=tabs_info,
					title='Empty Tab',
					url=page_url,
				)

			# Выполнить построение DOM и захват скриншота параллельно
			dom_task = None
			screenshot_task = None

			# Запустить задачу построения DOM, если запрошено
			if event.include_dom:
				self.logger.debug('🔍 DOMWatchdog.on_BrowserStateRequestEvent: 🌳 Starting DOM tree build task...')

				previous_state = None
				if self.browser_session._cached_browser_state_summary:
					cached = self.browser_session._cached_browser_state_summary
					if isinstance(cached, dict):
						previous_state = cached.get('dom_state')
					elif hasattr(cached, 'dom_state'):
						previous_state = cached.dom_state

				dom_task = create_task_with_error_handling(
					self._build_dom_tree_without_highlights(previous_state),
					logger_instance=self.logger,
					name='build_dom_tree',
					suppress_exceptions=True,
				)

			# Запустить задачу чистого скриншота, если запрошено (без JS-подсветки)
			if event.include_screenshot:
				self.logger.debug('🔍 DOMWatchdog.on_BrowserStateRequestEvent: 📸 Starting clean screenshot task...')
				screenshot_task = create_task_with_error_handling(
					self._capture_clean_screenshot(),
					logger_instance=self.logger,
					name='capture_screenshot',
					suppress_exceptions=True,
				)

			# Дождаться завершения обеих задач
			content = None
			screenshot_b64 = None

			if dom_task:
				try:
					content = await dom_task
					self.logger.debug('🔍 DOMWatchdog.on_BrowserStateRequestEvent: ✅ DOM tree build completed')
				except Exception as e:
					self.logger.debug(f'🔍 DOMWatchdog.on_BrowserStateRequestEvent: Построение DOM не удалось: {e}, используем минимальное состояние')
					content = SerializedDOMState(_root=None, selector_map={})
			else:
				content = SerializedDOMState(_root=None, selector_map={})

			if screenshot_task:
				try:
					screenshot_b64 = await screenshot_task
					self.logger.debug('🔍 DOMWatchdog.on_BrowserStateRequestEvent: ✅ Clean screenshot captured')
				except Exception as e:
					self.logger.warning(f'🔍 DOMWatchdog.on_BrowserStateRequestEvent: Чистый скриншот не удался: {e}')
					screenshot_b64 = None

			# Добавить подсветку на стороне браузера для видимости пользователем
			if content and content.selector_map and self.browser_session.browser_profile.dom_highlight_elements:
				try:
					self.logger.debug('🔍 DOMWatchdog.on_BrowserStateRequestEvent: 🎨 Adding browser-side highlights...')
					await self.browser_session.add_highlights(content.selector_map)
					self.logger.debug(
						f'🔍 DOMWatchdog.on_BrowserStateRequestEvent: ✅ Added browser highlights for {len(content.selector_map)} elements'
					)
				except Exception as e:
					self.logger.warning(f'🔍 DOMWatchdog.on_BrowserStateRequestEvent: Подсветка браузера не удалась: {e}')

			# Убедиться, что у нас есть валидный контент
			if not content:
				content = SerializedDOMState(_root=None, selector_map={})

			# Информация о вкладках уже получена в начале

			# Получить заголовок цели безопасно
			try:
				self.logger.debug('🔍 DOMWatchdog.on_BrowserStateRequestEvent: Getting page title...')
				title = await asyncio.wait_for(self.browser_session.get_current_page_title(), timeout=1.0)
				self.logger.debug(f'🔍 DOMWatchdog.on_BrowserStateRequestEvent: Got title: {title}')
			except Exception as e:
				self.logger.debug(f'🔍 DOMWatchdog.on_BrowserStateRequestEvent: Не удалось получить заголовок: {e}')
				title = 'Page'

			# Получить полную информацию о странице из CDP с таймаутом
			try:
				self.logger.debug('🔍 DOMWatchdog.on_BrowserStateRequestEvent: Getting page info from CDP...')
				page_info = await asyncio.wait_for(self._get_page_info(), timeout=1.0)
				self.logger.debug(f'🔍 DOMWatchdog.on_BrowserStateRequestEvent: Got page info from CDP: {page_info}')
			except Exception as e:
				self.logger.debug(
					f'🔍 DOMWatchdog.on_BrowserStateRequestEvent: Не удалось получить информацию о странице из CDP: {e}, используем резервный вариант'
				)
				# Резервный вариант - размеры viewport по умолчанию
				viewport = self.browser_session.browser_profile.viewport or {'height': 720, 'width': 1280}
				page_info = PageInfo(
					page_height=viewport['height'],
					page_width=viewport['width'],
					pixels_above=0,
					pixels_below=0,
					pixels_left=0,
					pixels_right=0,
					scroll_x=0,
					scroll_y=0,
					viewport_height=viewport['height'],
					viewport_width=viewport['width'],
				)

			# Проверить на PDF-просмотрщик
			is_pdf_viewer = '/pdf/' in page_url or page_url.endswith('.pdf')

			# Обнаружить кнопки пагинации из DOM
			pagination_buttons_data = []
			if content and content.selector_map:
				pagination_buttons_data = self._detect_pagination_buttons(content.selector_map)

			# Построить и кэшировать сводку состояния браузера
			if screenshot_b64:
				self.logger.debug(
					f'🔍 DOMWatchdog.on_BrowserStateRequestEvent: 📸 Creating BrowserStateSummary with screenshot, length: {len(screenshot_b64)}'
				)
			else:
				self.logger.debug(
					'🔍 DOMWatchdog.on_BrowserStateRequestEvent: 📸 Creating BrowserStateSummary WITHOUT screenshot'
				)

			browser_state = BrowserStateSummary(
				browser_errors=[],
				closed_popup_messages=self.browser_session._closed_popup_messages.copy(),
				dom_state=content,
				is_pdf_viewer=is_pdf_viewer,
				page_info=page_info,
				pagination_buttons=pagination_buttons_data,
				pending_network_requests=pending_requests,
				pixels_above=0,
				pixels_below=0,
				recent_events=self._get_recent_events_str() if event.include_recent_events else None,
				screenshot=screenshot_b64,
				tabs=tabs_info,
				title=title,
				url=page_url,
			)

			# Кэшировать состояние
			self.browser_session._cached_browser_state_summary = browser_state

			# Кэшировать размер viewport для преобразования координат (если включен llm_screenshot_size)
			if page_info:
				self.browser_session._original_viewport_size = (page_info.viewport_height, page_info.viewport_width)

			self.logger.debug('🔍 DOMWatchdog.on_BrowserStateRequestEvent: ✅ COMPLETED - Returning browser state')
			return browser_state

		except Exception as e:
			self.logger.error(f'Не удалось получить состояние браузера: {e}')

			# Вернуть минимальное состояние восстановления
			return BrowserStateSummary(
				browser_errors=[str(e)],
				closed_popup_messages=self.browser_session._closed_popup_messages.copy()
				if hasattr(self, 'browser_session') and self.browser_session is not None
				else [],
				dom_state=SerializedDOMState(_root=None, selector_map={}),
				is_pdf_viewer=False,
				page_info=PageInfo(
					page_height=720,
					page_width=1280,
					pixels_above=0,
					pixels_below=0,
					pixels_left=0,
					pixels_right=0,
					scroll_x=0,
					scroll_y=0,
					viewport_height=720,
					viewport_width=1280,
				),
				pagination_buttons=[],  # Состояние ошибки не имеет пагинации
				pending_network_requests=[],  # Состояние ошибки не имеет ожидающих запросов
				pixels_above=0,
				pixels_below=0,
				recent_events=None,
				screenshot=None,
				tabs=[],
				title='Error',
				url=page_url if 'page_url' in locals() else '',
			)

	@time_execution_async('build_dom_tree_without_highlights')
	@observe_debug(ignore_input=True, ignore_output=True, name='build_dom_tree_without_highlights')
	async def _build_dom_tree_without_highlights(self, previous_state: SerializedDOMState | None = None) -> SerializedDOMState:
		"""Построить DOM-дерево без инъекции JavaScript-подсветки (для параллельного выполнения)."""
		try:
			self.logger.debug('🔍 DOMWatchdog._build_dom_tree_without_highlights: STARTING DOM tree build')

			# Создать или переиспользовать DOM-сервис
			if self._dom_service is None:
				self._dom_service = DomService(
					browser_session=self.browser_session,
					cross_origin_iframes=self.browser_session.browser_profile.cross_origin_iframes,
					logger=self.logger,
					max_iframe_depth=self.browser_session.browser_profile.max_iframe_depth,
					max_iframes=self.browser_session.browser_profile.max_iframes,
					paint_order_filtering=self.browser_session.browser_profile.paint_order_filtering,
				)

			# Получить сериализованное DOM-дерево, используя сервис
			self.logger.debug('🔍 DOMWatchdog._build_dom_tree_without_highlights: Calling DomService.get_serialized_dom_tree...')
			start = time.time()
			self.current_dom_state, self.enhanced_dom_tree, timing_info = await self._dom_service.get_serialized_dom_tree(
				previous_cached_state=previous_state,
			)
			end = time.time()
			total_time_ms = (end - start) * 1000
			self.logger.debug(
				'🔍 DOMWatchdog._build_dom_tree_without_highlights: ✅ DomService.get_serialized_dom_tree completed'
			)

			# Построить иерархическую разбивку времени как одну многострочную строку
			timing_lines = ['📊 Timing breakdown:', f'⏱️ Total DOM tree time: {total_time_ms:.2f}ms']

			# разбивка get_all_trees
			get_all_trees_ms = timing_info.get('get_all_trees_total_ms', 0)
			if get_all_trees_ms > 0:
				timing_lines.append(f'  ├─ get_all_trees: {get_all_trees_ms:.2f}ms')
				cdp_parallel_ms = timing_info.get('cdp_parallel_calls_ms', 0)
				iframe_scroll_ms = timing_info.get('iframe_scroll_detection_ms', 0)
				snapshot_proc_ms = timing_info.get('snapshot_processing_ms', 0)
				if cdp_parallel_ms > 0.01:
					timing_lines.append(f'  │  ├─ cdp_parallel_calls: {cdp_parallel_ms:.2f}ms')
				if iframe_scroll_ms > 0.01:
					timing_lines.append(f'  │  ├─ iframe_scroll_detection: {iframe_scroll_ms:.2f}ms')
				if snapshot_proc_ms > 0.01:
					timing_lines.append(f'  │  └─ snapshot_processing: {snapshot_proc_ms:.2f}ms')

			# build_ax_lookup
			build_ax_ms = timing_info.get('build_ax_lookup_ms', 0)
			if build_ax_ms > 0.01:
				timing_lines.append(f'  ├─ build_ax_lookup: {build_ax_ms:.2f}ms')

			# build_snapshot_lookup
			build_snapshot_ms = timing_info.get('build_snapshot_lookup_ms', 0)
			if build_snapshot_ms > 0.01:
				timing_lines.append(f'  ├─ build_snapshot_lookup: {build_snapshot_ms:.2f}ms')

			# construct_enhanced_tree
			construct_tree_ms = timing_info.get('construct_enhanced_tree_ms', 0)
			if construct_tree_ms > 0.01:
				timing_lines.append(f'  ├─ construct_enhanced_tree: {construct_tree_ms:.2f}ms')

			# разбивка serialize_accessible_elements
			serialize_total_ms = timing_info.get('serialize_accessible_elements_total_ms', 0)
			if serialize_total_ms > 0.01:
				timing_lines.append(f'  ├─ serialize_accessible_elements: {serialize_total_ms:.2f}ms')
				assign_idx_ms = timing_info.get('assign_interactive_indices_ms', 0)
				bbox_ms = timing_info.get('bbox_filtering_ms', 0)
				clickable_ms = timing_info.get('clickable_detection_time_ms', 0)
				create_simp_ms = timing_info.get('create_simplified_tree_ms', 0)
				optimize_ms = timing_info.get('optimize_tree_ms', 0)
				paint_order_ms = timing_info.get('calculate_paint_order_ms', 0)

				if create_simp_ms > 0.01:
					timing_lines.append(f'  │  ├─ create_simplified_tree: {create_simp_ms:.2f}ms')
					if clickable_ms > 0.01:
						timing_lines.append(f'  │  │  └─ clickable_detection: {clickable_ms:.2f}ms')
				if assign_idx_ms > 0.01:
					timing_lines.append(f'  │  ├─ assign_interactive_indices: {assign_idx_ms:.2f}ms')
				if bbox_ms > 0.01:
					timing_lines.append(f'  │  ├─ bbox_filtering: {bbox_ms:.2f}ms')
				if optimize_ms > 0.01:
					timing_lines.append(f'  │  ├─ optimize_tree: {optimize_ms:.2f}ms')
				if paint_order_ms > 0.01:
					timing_lines.append(f'  │  └─ calculate_paint_order: {paint_order_ms:.2f}ms')

			# Overheads
			get_dom_overhead_ms = timing_info.get('get_dom_tree_overhead_ms', 0)
			serialize_overhead_ms = timing_info.get('serialization_overhead_ms', 0)
			get_serialized_overhead_ms = timing_info.get('get_serialized_dom_tree_overhead_ms', 0)

			if get_dom_overhead_ms > 0.1:
				timing_lines.append(f'  ├─ get_dom_tree_overhead: {get_dom_overhead_ms:.2f}ms')
			if serialize_overhead_ms > 0.1:
				timing_lines.append(f'  ├─ serialization_overhead: {serialize_overhead_ms:.2f}ms')
			if get_serialized_overhead_ms > 0.1:
				timing_lines.append(f'  └─ get_serialized_dom_tree_overhead: {get_serialized_overhead_ms:.2f}ms')

			# Вычислить общее отслеженное время для валидации
			main_operations_ms = (
				build_ax_ms
				+ build_snapshot_ms
				+ construct_tree_ms
				+ get_all_trees_ms
				+ get_dom_overhead_ms
				+ get_serialized_overhead_ms
				+ serialize_overhead_ms
				+ serialize_total_ms
			)
			untracked_time_ms = total_time_ms - main_operations_ms

			if untracked_time_ms > 1.0:  # Логировать только если значимо
				timing_lines.append(f'  ⚠️  untracked_time: {untracked_time_ms:.2f}ms')

			# Один вызов логирования со всей информацией о времени
			self.logger.debug('\n'.join(timing_lines))

			# Обновить карту селекторов для других watchdogs
			self.logger.debug('🔍 DOMWatchdog._build_dom_tree_without_highlights: Updating selector maps...')
			self.selector_map = self.current_dom_state.selector_map
			# Обновить кэшированную карту селекторов BrowserSession
			if self.browser_session:
				self.browser_session.update_cached_selector_map(self.selector_map)
			self.logger.debug(
				f'🔍 DOMWatchdog._build_dom_tree_without_highlights: ✅ Selector maps updated, {len(self.selector_map)} elements'
			)

			# Пропустить инъекцию JavaScript-подсветки - Python-подсветка будет применена позже
			self.logger.debug('🔍 DOMWatchdog._build_dom_tree_without_highlights: ✅ COMPLETED DOM tree build (no JS highlights)')
			return self.current_dom_state

		except Exception as e:
			# Это ожидаемо, когда страница перезагружается или CDP-запросы не успевают
			self.logger.debug(f'Не удалось построить DOM-дерево без подсветки: {e}')
			self.event_bus.dispatch(
				BrowserErrorEvent(
					error_type='DOMBuildFailed',
					message=str(e),
				)
			)
			raise

	@time_execution_async('capture_clean_screenshot')
	@observe_debug(ignore_input=True, ignore_output=True, name='capture_clean_screenshot')
	async def _capture_clean_screenshot(self) -> str:
		"""Захватить чистый скриншот без JavaScript-подсветки."""
		try:
			self.logger.debug('🔍 DOMWatchdog._capture_clean_screenshot: Capturing clean screenshot...')

			await self.browser_session.get_or_create_cdp_session(focus=True, target_id=self.browser_session.agent_focus_target_id)

			# Проверить, зарегистрирован ли обработчик
			handlers = self.event_bus.handlers.get('ScreenshotEvent', [])
			handler_names = [getattr(h, '__name__', str(h)) for h in handlers]
			self.logger.debug(f'📸 ScreenshotEvent handlers registered: {len(handlers)} - {handler_names}')

			screenshot_event = self.event_bus.dispatch(ScreenshotEvent(full_page=False))
			self.logger.debug('📸 Dispatched ScreenshotEvent, waiting for event to complete...')

			# Дождаться завершения самого события (это ждет всех обработчиков)
			await screenshot_event

			# Получить результат единственного обработчика
			screenshot_b64 = await screenshot_event.event_result(raise_if_any=True, raise_if_none=True)
			if screenshot_b64 is None:
				raise RuntimeError('Обработчик скриншота вернул None')
			self.logger.debug('🔍 DOMWatchdog._capture_clean_screenshot: ✅ Clean screenshot captured successfully')
			return str(screenshot_b64)

		except TimeoutError:
			self.logger.warning('📸 Чистый скриншот превысил таймаут после 6 секунд - обработчик не зарегистрирован или медленная страница?')
			raise
		except Exception as e:
			self.logger.warning(f'📸 Чистый скриншот не удался: {type(e).__name__}: {e}')
			raise

	def _detect_pagination_buttons(self, selector_map: dict[int, EnhancedDOMTreeNode]) -> list['PaginationButton']:
		"""Обнаружить кнопки пагинации из карты селекторов DOM.

		Args:
			selector_map: Словарь, сопоставляющий индексы элементов с узлами DOM-дерева

		Returns:
			Список экземпляров PaginationButton, найденных в DOM
		"""
		from core.session.models import PaginationButton

		pagination_buttons_data = []
		try:
			self.logger.debug('🔍 DOMWatchdog._detect_pagination_buttons: Detecting pagination buttons...')
			pagination_buttons_raw = DomService.detect_pagination_buttons(selector_map)
			# Преобразовать в экземпляры PaginationButton
			pagination_buttons_data = [
				PaginationButton(
					backend_node_id=btn['backend_node_id'],  # type: ignore
					button_type=btn['button_type'],  # type: ignore
					is_disabled=btn['is_disabled'],  # type: ignore
					selector=btn['selector'],  # type: ignore
					text=btn['text'],  # type: ignore
				)
				for btn in pagination_buttons_raw
			]
			if pagination_buttons_data:
				self.logger.debug(
					f'🔍 DOMWatchdog._detect_pagination_buttons: Found {len(pagination_buttons_data)} pagination buttons'
				)
		except Exception as e:
			self.logger.warning(f'🔍 DOMWatchdog._detect_pagination_buttons: Обнаружение пагинации не удалось: {e}')

		return pagination_buttons_data

	async def _get_page_info(self) -> 'PageInfo':
		"""Получить полную информацию о странице, используя один вызов CDP.

		# Примечание: можно сделать это событием

		Returns:
			PageInfo со всей информацией о viewport, размерах страницы и прокрутке
		"""

		from core.session.models import PageInfo

		# get_or_create_cdp_session() автоматически обрабатывает валидацию фокуса
		cdp_session = await self.browser_session.get_or_create_cdp_session(
			focus=True, target_id=self.browser_session.agent_focus_target_id
		)

		# Получить метрики макета, которые включают всю необходимую информацию
		metrics = await asyncio.wait_for(
			cdp_session.cdp_client.send.Page.getLayoutMetrics(session_id=cdp_session.session_id), timeout=10.0
		)

		# Извлечь различные типы viewport
		content_size = metrics.get('contentSize', {})
		css_layout_viewport = metrics.get('cssLayoutViewport', {})
		css_visual_viewport = metrics.get('cssVisualViewport', {})
		layout_viewport = metrics.get('layoutViewport', {})
		visual_viewport = metrics.get('visualViewport', {})

		# Вычислить соотношение пикселей устройства для преобразования между пикселями устройства и CSS-пикселями
		# Это соответствует подходу в dom/service.py методе _get_viewport_ratio
		css_width = css_visual_viewport.get('clientWidth', css_layout_viewport.get('clientWidth', 1280.0))
		device_width = visual_viewport.get('clientWidth', css_width)
		device_pixel_ratio = device_width / css_width if css_width > 0 else 1.0

		# Для размеров viewport использовать CSS-пиксели (то, что видит JavaScript)
		# Приоритизировать CSS layout viewport, затем вернуться к layout viewport
		viewport_height = int(css_layout_viewport.get('clientHeight') or layout_viewport.get('clientHeight', 720))
		viewport_width = int(css_layout_viewport.get('clientWidth') or layout_viewport.get('clientWidth', 1280))

		# Для общих размеров страницы content size обычно в пикселях устройства, поэтому преобразовать в CSS-пиксели
		# путем деления на соотношение пикселей устройства
		raw_page_height = content_size.get('height', viewport_height * device_pixel_ratio)
		raw_page_width = content_size.get('width', viewport_width * device_pixel_ratio)
		page_height = int(raw_page_height / device_pixel_ratio)
		page_width = int(raw_page_width / device_pixel_ratio)

		# Для позиции прокрутки использовать CSS visual viewport, если доступно, иначе CSS layout viewport
		# Они уже должны быть в CSS-пикселях
		scroll_y = int(css_visual_viewport.get('pageY') or css_layout_viewport.get('pageY', 0))
		scroll_x = int(css_visual_viewport.get('pageX') or css_layout_viewport.get('pageX', 0))

		# Вычислить информацию о прокрутке - пиксели, которые находятся выше/ниже/слева/справа от текущего viewport
		pixels_below = max(0, page_height - viewport_height - scroll_y)
		pixels_above = scroll_y
		pixels_right = max(0, page_width - viewport_width - scroll_x)
		pixels_left = scroll_x

		page_info = PageInfo(
			page_height=page_height,
			page_width=page_width,
			pixels_above=pixels_above,
			pixels_below=pixels_below,
			pixels_left=pixels_left,
			pixels_right=pixels_right,
			scroll_x=scroll_x,
			scroll_y=scroll_y,
			viewport_height=viewport_height,
			viewport_width=viewport_width,
		)

		return page_info

	# ========== Публичные вспомогательные методы ==========

	async def get_element_by_index(self, index: int) -> EnhancedDOMTreeNode | None:
		"""Получить элемент DOM по индексу из кэшированной карты селекторов.

		Строит DOM, если не кэширован.

		Returns:
			EnhancedDOMTreeNode или None, если индекс не найден
		"""
		if not self.selector_map:
			# Построить DOM, если не кэширован
			await self._build_dom_tree_without_highlights()

		return self.selector_map.get(index) if self.selector_map else None

	def clear_cache(self) -> None:
		"""Очистить кэшированное состояние DOM для принудительной перестройки при следующем доступе."""
		self.current_dom_state = None
		self.enhanced_dom_tree = None
		self.selector_map = None
		# Сохранить экземпляр DOM-сервиса для переиспользования его CDP-клиентского соединения

	def is_file_input(self, element: EnhancedDOMTreeNode) -> bool:
		"""Проверить, является ли элемент файловым вводом."""
		return element.attributes.get('type', '').lower() == 'file' and element.node_name.upper() == 'INPUT'

	@staticmethod
	def is_element_visible_according_to_all_parents(node: EnhancedDOMTreeNode, html_frames: list[EnhancedDOMTreeNode]) -> bool:
		"""Проверить, видим ли элемент согласно всем его родительским HTML-фреймам.

		Делегирует статическому методу DomService.
		"""
		return DomService.is_element_visible_according_to_all_parents(node, html_frames)

	async def __aexit__(self, exc_type, exc_value, traceback):
		"""Очистить DOM-сервис при выходе."""
		if self._dom_service:
			await self._dom_service.__aexit__(exc_type, exc_value, traceback)
			self._dom_service = None

	def __del__(self):
		"""Очистить DOM-сервис при удалении."""
		super().__del__()
		# DOM-сервис сам очистит свой CDP-клиент
		self._dom_service = None
