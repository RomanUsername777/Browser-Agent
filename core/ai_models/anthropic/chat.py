import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TypeVar, overload

import httpx
from anthropic import (
	APIConnectionError,
	APIStatusError,
	AsyncAnthropic,
	NotGiven,
	RateLimitError,
	omit,
)
from anthropic.types import CacheControlEphemeralParam, Message, ToolParam
from anthropic.types.model_param import ModelParam
from anthropic.types.text_block import TextBlock
from anthropic.types.tool_choice_tool_param import ToolChoiceToolParam
from httpx import Timeout
from pydantic import BaseModel

from core.ai_models.anthropic.serializer import AnthropicMessageSerializer
from core.ai_models.models import BaseChatModel
from core.exceptions import ModelProviderError, ModelRateLimitError
from core.ai_models.messages import BaseMessage
from core.ai_models.schema import SchemaOptimizer
from core.ai_models.models import ChatInvokeCompletion, ChatInvokeUsage

T = TypeVar('T', bound=BaseModel)


@dataclass
class ChatAnthropic(BaseChatModel):
	"""
	Обёртка вокруг чат-модели Anthropic.
	"""

	# Параметры модели (обязательные поля должны быть первыми)
	model: str | ModelParam

	# Параметры инициализации клиента
	api_key: str | None = None
	auth_token: str | None = None
	base_url: str | httpx.URL | None = None
	default_headers: Mapping[str, str] | None = None
	default_query: Mapping[str, object] | None = None
	http_client: httpx.AsyncClient | None = None
	max_retries: int = 10
	timeout: float | Timeout | None | NotGiven = NotGiven()

	# Конфигурация модели
	max_tokens: int = 8192
	seed: int | None = None
	temperature: float | None = None
	top_p: float | None = None

	# Static
	@property
	def provider(self) -> str:
		return 'anthropic'

	def _get_client_params(self) -> dict[str, Any]:
		"""Подготовить словарь параметров клиента."""
		# Определить базовые параметры клиента
		base_params = {
			'auth_token': self.auth_token,
			'api_key': self.api_key,
			'base_url': self.base_url,
			'default_headers': self.default_headers,
			'default_query': self.default_query,
			'http_client': self.http_client,
			'max_retries': self.max_retries,
			'timeout': self.timeout,
		}

		# Создать словарь client_params с не-None значениями и не-NotGiven значениями
		client_params = {}
		for k, v in base_params.items():
			if v is not None and v is not NotGiven():
				client_params[k] = v

		return client_params

	def _get_client_params_for_invoke(self):
		"""Подготовить словарь параметров клиента для вызова."""

		client_params = {}

		if self.max_tokens is not None:
			client_params['max_tokens'] = self.max_tokens

		if self.seed is not None:
			client_params['seed'] = self.seed

		if self.temperature is not None:
			client_params['temperature'] = self.temperature

		if self.top_p is not None:
			client_params['top_p'] = self.top_p

		return client_params

	def get_client(self) -> AsyncAnthropic:
		"""
		Вернуть клиент AsyncAnthropic.

		Returns:
			AsyncAnthropic: Экземпляр клиента AsyncAnthropic.
		"""
		client_params = self._get_client_params()
		return AsyncAnthropic(**client_params)

	@property
	def name(self) -> str:
		return str(self.model)

	def _get_usage(self, response: Message) -> ChatInvokeUsage | None:
		usage = ChatInvokeUsage(
			completion_tokens=response.usage.output_tokens,
			prompt_cache_creation_tokens=response.usage.cache_creation_input_tokens,
			prompt_cached_tokens=response.usage.cache_read_input_tokens,
			prompt_image_tokens=None,
			prompt_tokens=response.usage.input_tokens
			+ (
				response.usage.cache_read_input_tokens or 0
			),  # Общие токены в Anthropic немного странные, нужно добавлять кэшированные токены к токенам промпта
			total_tokens=response.usage.input_tokens + response.usage.output_tokens,
		)
		return usage

	@overload
	async def ainvoke(self, messages: list[BaseMessage], output_format: None = None) -> ChatInvokeCompletion[str]: ...

	@overload
	async def ainvoke(self, messages: list[BaseMessage], output_format: type[T]) -> ChatInvokeCompletion[T]: ...

	async def ainvoke(
		self, messages: list[BaseMessage], output_format: type[T] | None = None
	) -> ChatInvokeCompletion[T] | ChatInvokeCompletion[str]:
		anthropic_messages, system_prompt = AnthropicMessageSerializer.serialize_messages(messages)

		try:
			if output_format is None:
				# Обычное завершение без структурированного вывода
				response = await self.get_client().messages.create(
					messages=anthropic_messages,
					model=self.model,
					system=system_prompt or omit,
					**self._get_client_params_for_invoke(),
				)

				# Убедиться, что у нас есть валидный объект Message перед доступом к атрибутам
				if not isinstance(response, Message):
					raise ModelProviderError(
						message=f'Unexpected response type from Anthropic API: {type(response).__name__}. Response: {str(response)[:200]}',
						model=self.name,
						status_code=502,
					)

				usage = self._get_usage(response)

				# Извлечь текст из первого блока содержимого
				first_content = response.content[0]
				if isinstance(first_content, TextBlock):
					response_text = first_content.text
				else:
					# Если это не текстовый блок, преобразовать в строку
					response_text = str(first_content)

				return ChatInvokeCompletion(
					completion=response_text,
					stop_reason=response.stop_reason,
					usage=usage,
				)

			else:
				# Использовать вызов инструмента для структурированного вывода
				# Создать инструмент, который представляет формат вывода
				tool_name = output_format.__name__
				schema = SchemaOptimizer.create_optimized_json_schema(output_format)

				# Удалить title из схемы, если присутствует (Anthropic не любит это в параметрах)
				if 'title' in schema:
					del schema['title']

				tool = ToolParam(
					cache_control=CacheControlEphemeralParam(type='ephemeral'),
					description=f'Extract information in the format of {tool_name}',
					input_schema=schema,
					name=tool_name,
				)

				# Принудительно заставить модель использовать этот инструмент
				tool_choice = ToolChoiceToolParam(name=tool_name, type='tool')

				response = await self.get_client().messages.create(
					messages=anthropic_messages,
					model=self.model,
					system=system_prompt or omit,
					tool_choice=tool_choice,
					tools=[tool],
					**self._get_client_params_for_invoke(),
				)

				# Убедиться, что у нас есть валидный объект Message перед доступом к атрибутам
				if not isinstance(response, Message):
					raise ModelProviderError(
						message=f'Unexpected response type from Anthropic API: {type(response).__name__}. Response: {str(response)[:200]}',
						model=self.name,
						status_code=502,
					)

				usage = self._get_usage(response)

				# Извлечь блок использования инструмента
				for content_block in response.content:
					if hasattr(content_block, 'type') and content_block.type == 'tool_use':
						# Распарсить вход инструмента как структурированный вывод
						try:
							return ChatInvokeCompletion(
								completion=output_format.model_validate(content_block.input),
								stop_reason=response.stop_reason,
								usage=usage,
							)
						except Exception as e:
							# Если валидация не удалась, попробовать сначала распарсить как JSON
							if isinstance(content_block.input, str):
								data = json.loads(content_block.input)
								return ChatInvokeCompletion(
									completion=output_format.model_validate(data),
									stop_reason=response.stop_reason,
									usage=usage,
								)
							raise e

				# Если блок использования инструмента не найден, вызвать ошибку
				raise ValueError('Expected tool use in response but none found')

		except APIStatusError as e:
			raise ModelProviderError(message=e.message, model=self.name, status_code=e.status_code) from e
		except APIConnectionError as e:
			raise ModelProviderError(message=e.message, model=self.name) from e
		except RateLimitError as e:
			raise ModelRateLimitError(message=e.message, model=self.name) from e
		except Exception as e:
			raise ModelProviderError(message=str(e), model=self.name) from e
