"""Обработчики действий браузера по умолчанию с использованием CDP."""

from bubus import EventBus

from core.session.events import (
	ClickCoordinateEvent,
	ClickElementEvent,
	GetDropdownOptionsEvent,
	GoBackEvent,
	GoForwardEvent,
	RefreshEvent,
	ScrollEvent,
	ScrollToTextEvent,
	SelectDropdownOptionEvent,
	SendKeysEvent,
	TypeTextEvent,
	UploadFileEvent,
	WaitEvent,
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
ClickCoordinateEvent.model_rebuild()
ClickElementEvent.model_rebuild()
GetDropdownOptionsEvent.model_rebuild()
SelectDropdownOptionEvent.model_rebuild()
TypeTextEvent.model_rebuild()
ScrollEvent.model_rebuild()
UploadFileEvent.model_rebuild()


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
			ClickElementEvent: self._on_click_element,
			ClickCoordinateEvent: self._on_click_coordinate,
			TypeTextEvent: self._on_type_text,
			ScrollEvent: self._on_scroll,
			GoBackEvent: self._on_go_back,
			GoForwardEvent: self._on_go_forward,
			RefreshEvent: self._on_refresh,
			WaitEvent: self._on_wait,
			SendKeysEvent: self._on_send_keys,
			UploadFileEvent: self._on_upload_file,
			ScrollToTextEvent: self._on_scroll_to_text,
			GetDropdownOptionsEvent: self._on_get_dropdown_options,
			SelectDropdownOptionEvent: self._on_select_dropdown_option,
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
	async def _on_click_element(self, event: ClickElementEvent):
		"""Обработать запрос клика с CDP."""
		return await self.click_handler.on_ClickElementEvent(event)

	async def _on_click_coordinate(self, event: ClickCoordinateEvent):
		"""Обработать запрос клика по координатам."""
		return await self.click_handler.on_ClickCoordinateEvent(event)

	async def _on_type_text(self, event: TypeTextEvent):
		"""Обработать запрос ввода текста."""
		return await self.text_input_handler.on_TypeTextEvent(event)

	async def _on_scroll(self, event: ScrollEvent):
		"""Обработать запрос прокрутки."""
		return await self.scroll_handler.on_ScrollEvent(event)

	async def _on_go_back(self, event: GoBackEvent):
		"""Обработать запрос возврата назад."""
		return await self.navigation_handler.on_GoBackEvent(event)

	async def _on_go_forward(self, event: GoForwardEvent):
		"""Обработать запрос перехода вперед."""
		return await self.navigation_handler.on_GoForwardEvent(event)

	async def _on_refresh(self, event: RefreshEvent):
		"""Обработать запрос обновления страницы."""
		return await self.navigation_handler.on_RefreshEvent(event)

	async def _on_wait(self, event: WaitEvent):
		"""Обработать запрос ожидания."""
		return await self.send_keys_handler.on_WaitEvent(event)

	async def _on_send_keys(self, event: SendKeysEvent):
		"""Обработать запрос отправки клавиш."""
		return await self.send_keys_handler.on_SendKeysEvent(event)

	async def _on_upload_file(self, event: UploadFileEvent):
		"""Обработать запрос загрузки файла."""
		return await self.file_upload_handler.on_UploadFileEvent(event)

	async def _on_scroll_to_text(self, event: ScrollToTextEvent):
		"""Обработать запрос прокрутки к тексту."""
		return await self.scroll_handler.on_ScrollToTextEvent(event)

	async def _on_get_dropdown_options(self, event: GetDropdownOptionsEvent):
		"""Обработать запрос получения опций выпадающего списка."""
		return await self.dropdown_handler.on_GetDropdownOptionsEvent(event)

	async def _on_select_dropdown_option(self, event: SelectDropdownOptionEvent):
		"""Обработать запрос выбора опции выпадающего списка."""
		return await self.dropdown_handler.on_SelectDropdownOptionEvent(event)
