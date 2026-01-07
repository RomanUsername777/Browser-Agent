"""Модели и утилиты для действий агента."""

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from core.dom_processing.manager import EnhancedDOMTreeNode


# ========== Models ==========

# Модели входных данных для действий агента
class ExtractAction(BaseModel):
	query: str
	start_from_char: int = Field(
		default=0, description='Используется для длинных markdown-текстов, чтобы начать с определённого символа (не индекс в browser_state)'
	)
	extract_links: bool = Field(
		default=False, description='Установить True, если запрос требует извлечения ссылок, иначе False для экономии токенов'
	)


class NavigateAction(BaseModel):
	url: str
	new_tab: bool = Field(default=False, description='Открыть URL в новой вкладке (по умолчанию False)')


# Алиас для обратной совместимости (старое название действия)
GoToUrlAction = NavigateAction


class ClickElementAction(BaseModel):
	coordinate_x: int | None = Field(default=None, description='Горизонтальная координата относительно левого края viewport')
	coordinate_y: int | None = Field(default=None, description='Вертикальная координата относительно верхнего края viewport')
	index: int | None = Field(default=None, ge=0, description='Индекс элемента из browser_state (индексация с 0)')
	# expect_download: bool = Field(default=False, description='set True if expecting a download, False otherwise')  # moved to downloads_watchdog.py


class InputTextAction(BaseModel):
	text: str
	index: int = Field(ge=0, description='Индекс элемента из browser_state')
	press_enter: bool = Field(default=False, description='Если True, нажать Enter после ввода (полезно для полей поиска)')
	clear: bool = Field(default=True, description='True=очистить поле перед вводом, False=добавить к существующему тексту')


class DoneAction(BaseModel):
	success: bool = Field(default=True, description='True, если запрос пользователя выполнен успешно')
	text: str = Field(description='Финальное сообщение пользователю в формате, который он запросил')
	files_to_display: list[str] | None = Field(default=[], description='Список путей к файлам для отображения пользователю')


T = TypeVar('T', bound=BaseModel)


class StructuredOutputAction(BaseModel, Generic[T]):
	data: T = Field(description='Фактические выходные данные, соответствующие запрошенной схеме')
	success: bool = Field(default=True, description='True, если запрос пользователя выполнен успешно')


class ScrollAction(BaseModel):
	index: int | None = Field(default=None, description='Опциональный индекс элемента для прокрутки внутри конкретного контейнера')
	pages: float = Field(default=1.0, description='0.5=полстраницы, 1=полная страница, 10=до конца/начала')
	down: bool = Field(default=True, description='True=прокрутить вниз, False=прокрутить вверх')


class SendKeysAction(BaseModel):
	keys: str = Field(description='Клавиши (Escape, Enter, PageDown) или комбинации (Control+o)')


class NoParamsAction(BaseModel):
	"""Действие без параметров."""
	model_config = ConfigDict(extra='ignore')


class GetDropdownOptionsAction(BaseModel):
	index: int = Field(description='Индекс элемента выпадающего списка из browser_state')


class SelectDropdownOptionAction(BaseModel):
	text: str = Field(description='Точный текст или значение опции для выбора')
	index: int = Field(description='Индекс элемента выпадающего списка из browser_state')


class RequestUserInputAction(BaseModel):
	prompt: str = Field(description='Текст сообщения для пользователя с запросом на действие (например, решение капчи)')


class ClickTextAction(BaseModel):
	exact: bool = Field(default=False, description='Если True, искать точное совпадение текста; если False, искать подстроку')
	text: str = Field(description='Видимый текст для клика (например, "Откликнуться", "Submit", "Login")')


class ClickRoleAction(BaseModel):
	name: str = Field(default='', description='Доступное имя/текст элемента')
	role: str = Field(default='button', description='ARIA-роль: button, link, menuitem, checkbox, radio')
	exact: bool = Field(default=False, description='Если True, искать точное совпадение имени')


class WaitForUserInputAction(BaseModel):
	message: str | None = Field(
		default=None,
		description='Необязательное сообщение для пользователя. Если не указано, используется сообщение по умолчанию'
	)


# ========== Helper Functions ==========

def get_click_description(node: EnhancedDOMTreeNode) -> str:
	"""Получить краткое описание кликнутого элемента для памяти."""
	parts = []

	# Имя тега
	parts.append(node.tag_name)

	# Добавляем type для input
	if node.tag_name == 'input' and node.attributes.get('type'):
		input_type = node.attributes['type']
		parts.append(f'type={input_type}')

		# Для чекбоксов включаем состояние checked
		if input_type == 'checkbox':
			is_checked = node.attributes.get('checked', 'false').lower() in ['checked', 'true', '']
			# Также проверяем AX-узел
			if node.ax_node and node.ax_node.properties:
				for prop in node.ax_node.properties:
					if prop.name == 'checked':
						is_checked = prop.value == 'true' or prop.value is True
						break
			state = 'unchecked' if not is_checked else 'checked'
			parts.append(f'checkbox-state={state}')

	# Добавляем role, если присутствует
	if node.attributes.get('role'):
		role = node.attributes['role']
		parts.append(f'role={role}')

		# Для role=checkbox включаем состояние
		if role == 'checkbox':
			aria_checked = node.attributes.get('aria-checked', 'false').lower()
			is_checked = aria_checked in ['checked', 'true']
			if node.ax_node and node.ax_node.properties:
				for prop in node.ax_node.properties:
					if prop.name == 'checked':
						is_checked = prop.value == 'true' or prop.value is True
						break
			state = 'unchecked' if not is_checked else 'checked'
			parts.append(f'checkbox-state={state}')

	# Для labels/spans/divs проверяем, связаны ли они со скрытым чекбоксом
	if node.tag_name in ['div', 'label', 'span'] and 'type=' not in ' '.join(parts):
		# Проверяем дочерние элементы на наличие скрытого чекбокса
		for child in node.children:
			if child.tag_name == 'input' and child.attributes.get('type') == 'checkbox':
				# Проверяем, скрыт ли
				is_hidden = False
				if child.snapshot_node and child.snapshot_node.computed_styles:
					opacity = child.snapshot_node.computed_styles.get('opacity', '1')
					if opacity == '0.0' or opacity == '0':
						is_hidden = True

				if not child.is_visible or is_hidden:
					# Получаем состояние чекбокса
					is_checked = child.attributes.get('checked', 'false').lower() in ['checked', 'true', '']
					if child.ax_node and child.ax_node.properties:
						for prop in child.ax_node.properties:
							if prop.name == 'checked':
								is_checked = prop.value == 'true' or prop.value is True
								break
					state = 'unchecked' if not is_checked else 'checked'
					parts.append(f'checkbox-state={state}')
					break

	# Добавляем короткий текстовый контент, если доступен
	text = node.get_all_children_text().strip()
	if text:
		short_text = text[:30] + ('...' if len(text) > 30 else '')
		parts.append(f'"{short_text}"')

	# Добавляем ключевые атрибуты, такие как id, name, aria-label
	for attr in ['aria-label', 'id', 'name']:
		if node.attributes.get(attr):
			parts.append(f'{attr}={node.attributes[attr][:20]}')

	return ' '.join(parts)
