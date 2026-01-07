"""Модели и интерфейсы для работы с AI моделями."""

from typing import Any, Generic, Protocol, TypeVar, Union, overload, runtime_checkable

from pydantic import BaseModel

from core.ai_models.messages import BaseMessage

T = TypeVar('T', bound=Union[BaseModel, str])
T_Model = TypeVar('T_Model', bound=BaseModel)


@runtime_checkable
class BaseChatModel(Protocol):
	"""Protocol для базового интерфейса чат-моделей."""
	_verified_api_keys: bool = False

	model: str

	@property
	def provider(self) -> str: ...

	@property
	def name(self) -> str: ...

	@property
	def model_name(self) -> str:
		"""Совместимость с legacy кодом."""
		return self.model

	@overload
	async def ainvoke(self, messages: list[BaseMessage], output_format: None = None) -> "ChatInvokeCompletion[str]": ...

	@overload
	async def ainvoke(self, messages: list[BaseMessage], output_format: type[T_Model]) -> "ChatInvokeCompletion[T_Model]": ...

	async def ainvoke(
		self, messages: list[BaseMessage], output_format: type[T_Model] | None = None
	) -> "ChatInvokeCompletion[T_Model] | ChatInvokeCompletion[str]": ...

	@classmethod
	def __get_pydantic_core_schema__(
		cls,
		source_type: type,
		handler: Any,
	) -> Any:
		"""
		Enable this Protocol to be used in Pydantic models -> very useful for type-safe agent settings.
		Returns a schema that accepts any object (since this is a Protocol).
		"""
		from pydantic_core import core_schema

		# Return schema that accepts any object for Protocol types
		return core_schema.any_schema()


class ChatInvokeUsage(BaseModel):
	"""
	Usage information for a chat model invocation.
	"""

	prompt_tokens: int
	"""Number of tokens in the prompt (includes cached tokens. When calculating cost, subtract cached tokens from prompt tokens)"""

	prompt_cached_tokens: int | None
	"""Number of cached tokens."""

	prompt_cache_creation_tokens: int | None
	"""Anthropic only: Number of tokens used to create the cache."""

	prompt_image_tokens: int | None
	"""Google only: Number of tokens in the image (prompt tokens = text tokens + image tokens in that case)"""

	completion_tokens: int
	"""Number of tokens in the completion."""

	total_tokens: int
	"""Total number of tokens in the response."""


class ChatInvokeCompletion(BaseModel, Generic[T]):
	"""
	Response from a chat model invocation.
	"""

	completion: T
	"""Completion of the response."""

	# Thinking-related fields
	thinking: str | None = None
	redacted_thinking: str | None = None

	usage: ChatInvokeUsage | None
	"""Usage information for the response."""

	stop_reason: str | None = None
	"""Reason the model stopped generating. Common values: 'end_turn', 'max_tokens', 'stop_sequence'."""
