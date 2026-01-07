from datetime import datetime
from typing import Any, TypeVar

from pydantic import BaseModel, Field

from core.ai_models.models import ChatInvokeUsage

T = TypeVar('T', bound=BaseModel)


class TokenUsageEntry(BaseModel):
	"""Запись использования токенов"""

	timestamp: datetime
	model: str
	usage: ChatInvokeUsage


class TokenCostCalculated(BaseModel):
	"""Стоимость токенов"""

	completion_tokens: int
	completion_cost: float

	new_prompt_tokens: int
	new_prompt_cost: float

	prompt_read_cached_tokens: int | None
	prompt_read_cached_cost: float | None

	prompt_cached_creation_tokens: int | None
	prompt_cache_creation_cost: float | None
	"""Только Anthropic: стоимость создания кэша."""

	@property
	def prompt_cost(self) -> float:
		cached_read_cost = self.prompt_read_cached_cost or 0
		cache_creation_cost = self.prompt_cache_creation_cost or 0
		return self.new_prompt_cost + cached_read_cost + cache_creation_cost

	@property
	def total_cost(self) -> float:
		cached_read_cost = self.prompt_read_cached_cost or 0
		cache_creation_cost = self.prompt_cache_creation_cost or 0
		return self.new_prompt_cost + cached_read_cost + cache_creation_cost + self.completion_cost


class ModelPricing(BaseModel):
	"""Информация о ценообразовании для модели"""

	model: str
	output_cost_per_token: float | None
	input_cost_per_token: float | None

	cache_creation_input_token_cost: float | None
	cache_read_input_token_cost: float | None

	max_output_tokens: int | None
	max_input_tokens: int | None
	max_tokens: int | None


class CachedPricingData(BaseModel):
	"""Кэшированные данные о ценообразовании с временной меткой"""

	data: dict[str, Any]
	timestamp: datetime


class ModelUsageStats(BaseModel):
	"""Статистика использования для одной модели"""

	model: str
	completion_tokens: int = 0
	prompt_tokens: int = 0
	total_tokens: int = 0
	cost: float = 0.0
	invocations: int = 0
	average_tokens_per_invocation: float = 0.0


class ModelUsageTokens(BaseModel):
	"""Токены использования для одной модели"""

	model: str
	completion_tokens: int
	prompt_cached_tokens: int
	prompt_tokens: int
	total_tokens: int


class UsageSummary(BaseModel):
	"""Сводка использования токенов и затрат"""

	total_completion_tokens: int
	total_completion_cost: float
	total_tokens: int
	total_cost: float

	total_prompt_tokens: int
	total_prompt_cost: float

	total_prompt_cached_tokens: int
	total_prompt_cached_cost: float

	entry_count: int
	by_model: dict[str, ModelUsageStats] = Field(default_factory=dict)
