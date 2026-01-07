"""
Типы сообщений для работы с LLM провайдерами.
"""

# region - Content parts
from typing import Literal, Union

from openai import BaseModel


def _truncate(text: str, max_length: int = 50) -> str:
	"""Обрезать текст до max_length символов, добавляя многоточие при обрезке."""
	if len(text) <= max_length:
		return text
	return text[:max_length - 3] + '...'


def _format_image_url(url: str, max_length: int = 50) -> str:
	"""Форматировать URL изображения для отображения, обрезая при необходимости."""
	if url.startswith('data:'):
		# Base64-изображение
		media_type = url.split(';')[0].split(':')[1] if ';' in url else 'image'
		return f'<base64 {media_type}>'
	else:
		# Обычный URL
		return _truncate(url, max_length)


class ContentPartTextParam(BaseModel):
	type: Literal['text'] = 'text'
	text: str

	def __str__(self) -> str:
		return f'Text: {_truncate(self.text)}'

	def __repr__(self) -> str:
		return f'ContentPartTextParam(text={_truncate(self.text)})'


class ContentPartRefusalParam(BaseModel):
	type: Literal['refusal'] = 'refusal'
	refusal: str

	def __str__(self) -> str:
		return f'Refusal: {_truncate(self.refusal)}'

	def __repr__(self) -> str:
		return f'ContentPartRefusalParam(refusal={_truncate(repr(self.refusal), 50)})'


SupportedImageMediaType = Literal['image/gif', 'image/jpeg', 'image/png', 'image/webp']


class ImageURL(BaseModel):
	"""Либо URL изображения, либо base64-закодированные данные изображения."""
	url: str
	"""Указывает уровень детализации изображения."""
	detail: Literal['auto', 'high', 'low'] = 'auto'
	# нужен для Anthropic
	media_type: SupportedImageMediaType = 'image/png'

	def __str__(self) -> str:
		url_display = _format_image_url(self.url)
		return f'🖼️  Image[detail={self.detail}, {self.media_type}]: {url_display}'

	def __repr__(self) -> str:
		url_repr = _format_image_url(self.url, 30)
		return f'ImageURL(detail={repr(self.detail)}, media_type={repr(self.media_type)}, url={repr(url_repr)})'


class ContentPartImageParam(BaseModel):
	type: Literal['image_url'] = 'image_url'
	image_url: ImageURL

	def __str__(self) -> str:
		return str(self.image_url)

	def __repr__(self) -> str:
		return f'ContentPartImageParam(image_url={repr(self.image_url)})'


class Function(BaseModel):
	"""
    Аргументы для вызова функции, сгенерированные моделью в формате JSON.
    Обратите внимание, что модель не всегда генерирует валидный JSON и может
    галлюцинировать параметры, не определённые в схеме функции. Валидируйте
    аргументы в вашем коде перед вызовом функции.
    """
	arguments: str
	"""Имя функции для вызова."""
	name: str

	def __str__(self) -> str:
		args_preview = _truncate(self.arguments, 80)
		return f'{self.name}({args_preview})'

	def __repr__(self) -> str:
		args_repr = _truncate(repr(self.arguments), 50)
		return f'Function(arguments={args_repr}, name={repr(self.name)})'


class ToolCall(BaseModel):
	"""Функция, которую вызвала модель."""
	function: Function
	"""ID вызова инструмента."""
	id: str
	"""Тип инструмента. В настоящее время поддерживается только `function`."""
	type: Literal['function'] = 'function'

	def __str__(self) -> str:
		return f'ToolCall[{self.id}]: {self.function}'

	def __repr__(self) -> str:
		return f'ToolCall(function={repr(self.function)}, id={repr(self.id)})'


# endregion


# region - Message types
class _MessageBase(BaseModel):
	"""Базовый класс для всех типов сообщений"""

	role: Literal['assistant', 'system', 'user']

	"""Следует ли кешировать это сообщение. Применимо только при использовании моделей Anthropic."""
	cache: bool = False


class UserMessage(_MessageBase):
	"""Роль автора сообщения, в данном случае `user`."""
	role: Literal['user'] = 'user'

	"""Содержимое пользовательского сообщения."""
	content: str | list[ContentPartImageParam | ContentPartTextParam]

	"""Необязательное имя участника.

    Предоставляет модели информацию для различения участников с одинаковой ролью.
    """
	name: str | None = None

	@property
	def text(self) -> str:
		"""
		Автоматически извлекать текст из content, будь то строка или список частей контента.
		"""
		if isinstance(self.content, str):
			return self.content
		elif isinstance(self.content, list):
			return '\n'.join([part.text for part in self.content if part.type == 'text'])
		else:
			return ''

	def __str__(self) -> str:
		return f'UserMessage(content={self.text})'

	def __repr__(self) -> str:
		return f'UserMessage(content={repr(self.text)})'


class SystemMessage(_MessageBase):
	"""Роль автора сообщения, в данном случае `system`."""
	role: Literal['system'] = 'system'

	"""Содержимое системного сообщения."""
	content: str | list[ContentPartTextParam]

	name: str | None = None

	@property
	def text(self) -> str:
		"""
		Автоматически извлекать текст из content, будь то строка или список частей контента.
		"""
		if isinstance(self.content, str):
			return self.content
		elif isinstance(self.content, list):
			return '\n'.join([part.text for part in self.content if part.type == 'text'])
		else:
			return ''

	def __str__(self) -> str:
		return f'SystemMessage(content={self.text})'

	def __repr__(self) -> str:
		return f'SystemMessage(content={repr(self.text)})'


class AssistantMessage(_MessageBase):
	"""Роль автора сообщения, в данном случае `assistant`."""
	role: Literal['assistant'] = 'assistant'

	"""Содержимое сообщения ассистента."""
	content: str | list[ContentPartRefusalParam | ContentPartTextParam] | None

	name: str | None = None

	"""Сообщение об отказе от ассистента."""
	refusal: str | None = None

	"""Вызовы инструментов, сгенерированные моделью, такие как вызовы функций."""
	tool_calls: list[ToolCall] = []

	@property
	def text(self) -> str:
		"""
		Автоматически извлекать текст из content, будь то строка или список частей контента.
		"""
		if isinstance(self.content, str):
			return self.content
		elif isinstance(self.content, list):
			text = ''
			for part in self.content:
				if part.type == 'refusal':
					text += f'[Refusal] {part.refusal}'
				elif part.type == 'text':
					text += part.text
			return text
		else:
			return ''

	def __str__(self) -> str:
		return f'AssistantMessage(content={self.text})'

	def __repr__(self) -> str:
		return f'AssistantMessage(content={repr(self.text)})'


BaseMessage = Union[AssistantMessage, SystemMessage, UserMessage]

# endregion
