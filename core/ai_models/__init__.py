"""Модуль для работы с AI моделями."""

from typing import TYPE_CHECKING

# Легковесные импорты, которые часто используются
from core.ai_models.models import BaseChatModel
from core.ai_models.messages import (
	AssistantMessage,
	BaseMessage,
	SystemMessage,
	UserMessage,
)
from core.ai_models.messages import (
	ContentPartImageParam as ContentImage,
)
from core.ai_models.messages import (
	ContentPartRefusalParam as ContentRefusal,
)
from core.ai_models.messages import (
	ContentPartTextParam as ContentText,
)

# Заглушки типов для ленивых импортов
if TYPE_CHECKING:
	from core.ai_models.anthropic.chat import ChatAnthropic
	from core.ai_models.openai.chat import ChatOpenAI

	# Заглушки типов для экземпляров моделей - включает автодополнение IDE
	openai_gpt_4o: ChatOpenAI
	openai_gpt_4o_mini: ChatOpenAI
	openai_gpt_4_1_mini: ChatOpenAI
	openai_o1: ChatOpenAI
	openai_o1_mini: ChatOpenAI
	openai_o1_pro: ChatOpenAI
	openai_o3: ChatOpenAI
	openai_o3_mini: ChatOpenAI
	openai_o3_pro: ChatOpenAI
	openai_o4_mini: ChatOpenAI
	openai_gpt_5: ChatOpenAI
	openai_gpt_5_mini: ChatOpenAI
	openai_gpt_5_nano: ChatOpenAI

# Модели импортируются по требованию через __getattr__

# Маппинг ленивых импортов для тяжелых chat моделей
_LAZY_IMPORTS_MAP = {
	'ChatAnthropic': ('core.ai_models.anthropic.chat', 'ChatAnthropic'),
	'ChatOpenAI': ('core.ai_models.openai.chat', 'ChatOpenAI'),
}

# Кэш для экземпляров моделей - создается только при доступе
_cached_models: dict[str, 'BaseChatModel'] = {}


def __getattr__(attribute_name: str):
	"""Механизм ленивого импорта для тяжелых импортов chat моделей и экземпляров моделей."""
	if attribute_name in _LAZY_IMPORTS_MAP:
		import_path, class_name = _LAZY_IMPORTS_MAP[attribute_name]
		try:
			from importlib import import_module

			imported_module = import_module(import_path)
			imported_class = getattr(imported_module, class_name)
			return imported_class
		except ImportError as import_error:
			raise ImportError(f'Failed to import {attribute_name} from {import_path}: {import_error}') from import_error

	# Сначала проверить кэш для экземпляров моделей
	if attribute_name in _cached_models:
		return _cached_models[attribute_name]

	# Попытаться получить экземпляры моделей из модуля models по требованию
	try:
		from core.ai_models.models import __getattr__ as get_model_attr

		model_instance = get_model_attr(attribute_name)
		# Кэшировать в нашем чистом словаре кэша
		_cached_models[attribute_name] = model_instance
		return model_instance
	except (AttributeError, ImportError):
		pass

	raise AttributeError(f"module '{__name__}' has no attribute '{attribute_name}'")


__all__ = [
	# Типы сообщений
	'BaseMessage',
	'UserMessage',
	'SystemMessage',
	'AssistantMessage',
	# Части контента с лучшими именами
	'ContentText',
	'ContentRefusal',
	'ContentImage',
	# Chat модели
	'BaseChatModel',
	'ChatOpenAI',
	'ChatAnthropic',
]
