"""Обработчики действий браузера по умолчанию с использованием CDP."""

from bubus import EventBus

from core.session.events import (
	CoordinateClickRequest,
	ElementClickRequest,
	DropdownOptionsRequest,
	NavigateBackRequest,
	NavigateForwardRequest,
	PageRefreshRequest,
	PageScrollRequest,
	ScrollToTextRequest,
	DropdownSelectRequest,
	KeyboardInputRequest,
	TextInputRequest,
	FileUploadRequest,
	DelayRequest,
)
from core.session.monitors.browser_controller import BrowserController
from core.session.monitors.handlers import (
	ClickHandler,
	TextInputHandler,
	ScrollHandler,
	DropdownHandler,
	FileUploadHandler,
	NavigationHandler,
	SendKeysHandler,
)

# Импортировать EnhancedDOMTreeNode и перестроить модели событий с прямыми ссылками на него
# Это должно быть выполнено после завершения всех импортов
from core.dom_processing.manager import EnhancedDOMTreeNode
CoordinateClickRequest.model_rebuild()
ElementClickRequest.model_rebuild()
DropdownOptionsRequest.model_rebuild()
DropdownSelectRequest.model_rebuild()
TextInputRequest.model_rebuild()
PageScrollRequest.model_rebuild()
FileUploadRequest.model_rebuild()


class DefaultActionWatchdog:
	"""Обрабатывает действия браузера по умолчанию, используя специализированные обработчики.
	
	Использует композицию - делегирует обработку событий специализированным обработчикам.
	"""

	def __init__(self, browser_session):
		"""Инициализация watchdog с обработчиками."""
		self.browser_session = browser_session
		self.event_bus: EventBus = browser_session.event_bus

		# Контроллер браузера для абстракции над CDP
		self.browser_controller = BrowserController(browser_session)

		# Создаем специализированные обработчики
		self.click_handler = ClickHandler(self)
		self.text_input_handler = TextInputHandler(self)
		self.scroll_handler = ScrollHandler(self)
		self.dropdown_handler = DropdownHandler(self)
		self.file_upload_handler = FileUploadHandler(self)
		self.navigation_handler = NavigationHandler(self)
		self.send_keys_handler = SendKeysHandler(self)

		# Словарь обработчиков событий
		self.event_handlers = {
			ElementClickRequest: self._on_click_element,
			CoordinateClickRequest: self._on_click_coordinate,
			TextInputRequest: self._on_type_text,
			PageScrollRequest: self._on_scroll,
			NavigateBackRequest: self._on_go_back,
			NavigateForwardRequest: self._on_go_forward,
			PageRefreshRequest: self._on_refresh,
			DelayRequest: self._on_wait,
			KeyboardInputRequest: self._on_send_keys,
			FileUploadRequest: self._on_upload_file,
			ScrollToTextRequest: self._on_scroll_to_text,
			DropdownOptionsRequest: self._on_get_dropdown_options,
			DropdownSelectRequest: self._on_select_dropdown_option,
		}

	@property
	def logger(self):
		"""Получить logger из browser session."""
		return self.browser_session.logger

	def attach(self, event_bus: EventBus) -> None:
		"""Прикрепить обработчики событий к event bus."""
		for event_type, handler in self.event_handlers.items():
			event_bus.on(event_type, handler)

	# Методы-делегаты для обработчиков событий
	async def _on_click_element(self, event: ElementClickRequest):
		"""Обработать запрос клика с CDP."""
		return await self.click_handler.on_ElementClickRequest(event)

	async def _on_click_coordinate(self, event: CoordinateClickRequest):
		"""Обработать запрос клика по координатам."""
		return await self.click_handler.on_CoordinateClickRequest(event)

	async def _on_type_text(self, event: TextInputRequest):
		"""Обработать запрос ввода текста."""
		return await self.text_input_handler.on_TextInputRequest(event)

	async def _on_scroll(self, event: PageScrollRequest):
		"""Обработать запрос прокрутки."""
		return await self.scroll_handler.on_PageScrollRequest(event)

	async def _on_go_back(self, event: NavigateBackRequest):
		"""Обработать запрос возврата назад."""
		return await self.navigation_handler.on_NavigateBackRequest(event)

	async def _on_go_forward(self, event: NavigateForwardRequest):
		"""Обработать запрос перехода вперед."""
		return await self.navigation_handler.on_NavigateForwardRequest(event)

	async def _on_refresh(self, event: PageRefreshRequest):
		"""Обработать запрос обновления страницы."""
		return await self.navigation_handler.on_PageRefreshRequest(event)

	async def _on_wait(self, event: DelayRequest):
		"""Обработать запрос ожидания."""
		return await self.send_keys_handler.on_DelayRequest(event)

	async def _on_send_keys(self, event: KeyboardInputRequest):
		"""Обработать запрос отправки клавиш."""
		return await self.send_keys_handler.on_KeyboardInputRequest(event)

	async def _on_upload_file(self, event: FileUploadRequest):
		"""Обработать запрос загрузки файла."""
		return await self.file_upload_handler.on_FileUploadRequest(event)

	async def _on_scroll_to_text(self, event: ScrollToTextRequest):
		"""Обработать запрос прокрутки к тексту."""
		return await self.scroll_handler.on_ScrollToTextRequest(event)

	async def _on_get_dropdown_options(self, event: DropdownOptionsRequest):
		"""Обработать запрос получения опций выпадающего списка."""
		return await self.dropdown_handler.on_DropdownOptionsRequest(event)

	async def _on_select_dropdown_option(self, event: DropdownSelectRequest):
		"""Обработать запрос выбора опции выпадающего списка."""
		return await self.dropdown_handler.on_DropdownSelectRequest(event)
