# @file purpose: Модуль наблюдаемости для агента с опциональной интеграцией lmnr (Laminar)
"""
Модуль наблюдаемости для агента.

Предоставляет декораторы наблюдаемости, которые при наличии lmnr (Laminar)
могут использоваться для трассировки, а при отсутствии — работают как no-op обёртки.
"""

import logging
import os
from collections.abc import Callable
from functools import wraps
from typing import Any, Literal, TypeVar, cast

logger = logging.getLogger(__name__)
from dotenv import load_dotenv

load_dotenv()

# Type definitions
F = TypeVar('F', bound=Callable[..., Any])


# Проверить, находимся ли мы в режиме отладки
def _is_debug_mode() -> bool:
	"""Проверить, находимся ли мы в режиме отладки на основе переменных окружения или уровня логирования."""

	debug_level = os.getenv('LMNR_LOGGING_LEVEL', '').lower()
	if debug_level == 'debug':
		# logger.info('Debug mode is enabled for observability')
		return True
	# logger.info('Debug mode is disabled for observability')
	return False


# Попытаться импортировать lmnr observe
_LMNR_AVAILABLE = False
_lmnr_observe_func = None

try:
	from lmnr import observe as _lmnr_observe_func  # type: ignore

	verbose_observability = os.environ.get('AGENT_VERBOSE_OBSERVABILITY', 'false').lower() == 'true'
	if verbose_observability:
		logger.debug('Lmnr is available for observability')
	_LMNR_AVAILABLE = True
except ImportError:
	verbose_observability = os.environ.get('AGENT_VERBOSE_OBSERVABILITY', 'false').lower() == 'true'
	if verbose_observability:
		logger.debug('Lmnr is not available for observability')
	_LMNR_AVAILABLE = False


def _create_no_op_decorator(
	span_name: str | None = None,
	ignore_input: bool = False,
	ignore_output: bool = False,
	metadata: dict[str, Any] | None = None,
	**kwargs: Any,
) -> Callable[[F], F]:
	"""Создать no-op декоратор, который принимает все параметры lmnr observe, но ничего не делает."""
	import asyncio

	def decorator(function: F) -> F:
		if asyncio.iscoroutinefunction(function):

			@wraps(function)
			async def async_wrapper(*args, **kwargs):
				return await function(*args, **kwargs)

			return cast(F, async_wrapper)
		else:

			@wraps(function)
			def sync_wrapper(*args, **kwargs):
				return function(*args, **kwargs)

			return cast(F, sync_wrapper)

	return decorator


def observe(
	span_name: str | None = None,
	ignore_input: bool = False,
	ignore_output: bool = False,
	metadata: dict[str, Any] | None = None,
	span_type: Literal['DEFAULT', 'LLM', 'TOOL'] = 'DEFAULT',
	**kwargs: Any,
) -> Callable[[F], F]:
	"""
	Декоратор наблюдаемости, который отслеживает выполнение функции, когда lmnr доступен.

	Этот декоратор будет использовать декоратор observe из lmnr, если lmnr установлен,
	иначе это будет no-op, который принимает те же параметры.

	Args:
	    span_name: Имя span/trace
	    ignore_input: Игнорировать ли входные параметры функции при трассировке
	    ignore_output: Игнорировать ли выход функции при трассировке
	    metadata: Дополнительные метаданные для прикрепления к span
	    **kwargs: Дополнительные параметры, передаваемые в lmnr observe

	Returns:
	    Декорированная функция, которая может быть отслежена в зависимости от доступности lmnr

	Example:
	    @observe(span_name="my_function", metadata={"version": "1.0"})
	    def my_function(param1, param2):
	        return param1 + param2
	"""
	observe_params = {
		'name': span_name,
		'ignore_input': ignore_input,
		'ignore_output': ignore_output,
		'metadata': metadata,
		'span_type': span_type,
		'tags': ['observe', 'observe_debug'],  # важно: теги должны быть созданы в laminar сначала
		**kwargs,
	}

	if _LMNR_AVAILABLE and _lmnr_observe_func:
		# Использовать настоящий декоратор observe из lmnr
		return cast(Callable[[F], F], _lmnr_observe_func(**observe_params))
	else:
		# Использовать no-op декоратор
		return _create_no_op_decorator(**observe_params)


def observe_debug(
	span_name: str | None = None,
	ignore_input: bool = False,
	ignore_output: bool = False,
	metadata: dict[str, Any] | None = None,
	span_type: Literal['DEFAULT', 'LLM', 'TOOL'] = 'DEFAULT',
	**kwargs: Any,
) -> Callable[[F], F]:
	"""
	Декоратор наблюдаемости только для отладки, который отслеживает только в режиме отладки.

	Этот декоратор будет использовать декоратор observe из lmnr, если lmnr установлен
	И мы находимся в режиме отладки, иначе это будет no-op.

	Режим отладки определяется:
	- Переменная окружения DEBUG установлена в 1/true/yes/on
	- Переменная окружения AGENT_DEBUG установлена в 1/true/yes/on
	- Корневой уровень логирования установлен в DEBUG или ниже

	Args:
	    span_name: Имя span/trace
	    ignore_input: Игнорировать ли входные параметры функции при трассировке
	    ignore_output: Игнорировать ли выход функции при трассировке
	    metadata: Дополнительные метаданные для прикрепления к span
	    **kwargs: Дополнительные параметры, передаваемые в lmnr observe

	Returns:
	    Декорированная функция, которая может быть отслежена только в режиме отладки

	Example:
	    @observe_debug(ignore_input=True, ignore_output=True, span_name="debug_function", metadata={"debug": True})
	    def debug_function(param1, param2):
	        return param1 + param2
	"""
	observe_params = {
		'name': span_name,
		'ignore_input': ignore_input,
		'ignore_output': ignore_output,
		'metadata': metadata,
		'span_type': span_type,
		'tags': ['observe_debug'],  # важно: теги должны быть созданы в laminar сначала
		**kwargs,
	}

	if _LMNR_AVAILABLE and _lmnr_observe_func and _is_debug_mode():
		# Использовать настоящий декоратор observe из lmnr только в режиме отладки
		return cast(Callable[[F], F], _lmnr_observe_func(**observe_params))
	else:
		# Использовать no-op декоратор (либо не в режиме отладки, либо lmnr недоступен)
		return _create_no_op_decorator(**observe_params)


# Вспомогательные функции для проверки доступности и статуса отладки
def is_lmnr_available() -> bool:
	"""Проверить, доступен ли lmnr для трассировки."""
	return _LMNR_AVAILABLE


def is_debug_mode() -> bool:
	"""Проверить, находимся ли мы в данный момент в режиме отладки."""
	return _is_debug_mode()


def get_observability_status() -> dict[str, bool]:
	"""Получить текущий статус функций наблюдаемости."""
	debug_enabled = _is_debug_mode()
	return {
		'debug_mode': debug_enabled,
		'lmnr_available': _LMNR_AVAILABLE,
		'observe_active': _LMNR_AVAILABLE,
		'observe_debug_active': _LMNR_AVAILABLE and debug_enabled,
	}
