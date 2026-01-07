"""
Сервис стоимости токенов, который отслеживает использование токенов LLM и затраты.

Получает данные о ценах из репозитория LiteLLM и кэширует их на 1 день.
Автоматически отслеживает использование токенов при регистрации и вызове LLM.
"""

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import httpx
from dotenv import load_dotenv

from core.ai_models.models import BaseChatModel
from core.ai_models.models import ChatInvokeUsage
from core.pricing.models import (
	CachedPricingData,
	ModelPricing,
	ModelUsageStats,
	ModelUsageTokens,
	TokenCostCalculated,
	TokenUsageEntry,
	UsageSummary,
)
from core.helpers import create_task_with_error_handling

load_dotenv()

from core.config import CONFIG

logger = logging.getLogger(__name__)
cost_logger = logging.getLogger('cost')

# Маппинг от имени модели к имени модели LiteLLM
MODEL_TO_LITELLM: dict[str, str] = {
	'gemini-flash-latest': 'gemini/gemini-flash-latest',
}

# Кастомное ценообразование моделей, недоступных в данных LiteLLM.
# Цены указаны за токен (не за 1M токенов).
# Формат соответствует структуре model_prices_and_context_window.json от LiteLLM
CUSTOM_MODEL_PRICING: dict[str, dict[str, Any]] = {
	'bu-1-0': {
		'output_cost_per_token': 2.00 / 1_000_000,  # $3.00 за 1M токенов
		'input_cost_per_token': 0.2 / 1_000_000,  # $0.50 за 1M токенов
		'cache_read_input_token_cost': 0.02 / 1_000_000,  # $0.10 за 1M токенов
		'cache_creation_input_token_cost': None,  # Не указано
		'max_output_tokens': None,  # Не указано
		'max_input_tokens': None,  # Не указано
		'max_tokens': None,  # Не указано
	}
}

CUSTOM_MODEL_PRICING['smart'] = CUSTOM_MODEL_PRICING['bu-1-0']
CUSTOM_MODEL_PRICING['bu-latest'] = CUSTOM_MODEL_PRICING['bu-1-0']


def xdg_cache_home() -> Path:
	default_path = Path.home() / '.cache'
	if CONFIG.XDG_CACHE_HOME and (cache_path := Path(CONFIG.XDG_CACHE_HOME)).is_absolute():
		return cache_path
	return default_path


class TokenCost:
	"""Сервис для отслеживания использования токенов и расчета затрат"""

	CACHE_DIR_NAME = 'agent/token_cost'
	CACHE_DURATION = timedelta(days=1)
	PRICING_URL = 'https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json'

	def __init__(self, include_cost: bool = False):
		env_calculate_cost = os.getenv('AGENT_CALCULATE_COST', 'false').lower() == 'true'
		self.include_cost = include_cost or env_calculate_cost

		self.usage_history: list[TokenUsageEntry] = []
		self.registered_llms: dict[str, BaseChatModel] = {}
		self._pricing_data: dict[str, Any] | None = None
		self._initialized = False
		self._cache_dir = xdg_cache_home() / self.CACHE_DIR_NAME

	async def initialize(self) -> None:
		"""Инициализировать сервис путем загрузки данных о ценах"""
		if not self._initialized:
			if self.include_cost:
				await self._load_pricing_data()
			self._initialized = True

	async def _load_pricing_data(self) -> None:
		"""Загрузить данные о ценах из кэша или получить с GitHub"""
		# Попытаться найти действительный файл кэша
		valid_cache_file = await self._find_valid_cache()

		if valid_cache_file:
			await self._load_from_cache(valid_cache_file)
		else:
			await self._fetch_and_cache_pricing_data()

	async def _find_valid_cache(self) -> Path | None:
		"""Найти самый последний действительный файл кэша"""
		try:
			# Убедиться, что директория кэша существует
			self._cache_dir.mkdir(parents=True, exist_ok=True)

			# Список всех JSON файлов в директории кэша
			json_cache_files = list(self._cache_dir.glob('*.json'))

			if not json_cache_files:
				return None

			# Сортировать по времени модификации (самый последний первый)
			json_cache_files.sort(key=lambda file: file.stat().st_mtime, reverse=True)

			# Проверить каждый файл, пока не найдем действительный
			for cache_file_path in json_cache_files:
				if await self._is_cache_valid(cache_file_path):
					return cache_file_path
				else:
					# Очистить старые файлы кэша
					try:
						os.remove(cache_file_path)
					except Exception:
						pass

			return None
		except Exception:
			return None

	async def _is_cache_valid(self, cache_file_path: Path) -> bool:
		"""Проверить, действителен ли конкретный файл кэша и не истек ли срок его действия"""
		try:
			if not cache_file_path.exists():
				return False

			# Прочитать кэшированные данные
			cached_data = CachedPricingData.model_validate_json(await anyio.Path(cache_file_path).read_text())

			# Проверить, действителен ли еще кэш
			time_difference = datetime.now() - cached_data.timestamp
			return time_difference < self.CACHE_DURATION
		except Exception:
			return False

	async def _load_from_cache(self, cache_file_path: Path) -> None:
		"""Загрузить данные о ценах из конкретного файла кэша"""
		try:
			file_content = await anyio.Path(cache_file_path).read_text()
			cached_data = CachedPricingData.model_validate_json(file_content)
			self._pricing_data = cached_data.data
		except Exception as load_error:
			logger.debug(f'Error loading cached pricing data from {cache_file_path}: {load_error}')
			# Вернуться к получению данных
			await self._fetch_and_cache_pricing_data()

	async def _fetch_and_cache_pricing_data(self) -> None:
		"""Получить данные о ценах из LiteLLM GitHub и кэшировать их с временной меткой"""
		try:
			async with httpx.AsyncClient() as http_client:
				http_response = await http_client.get(self.PRICING_URL, timeout=30)
				http_response.raise_for_status()

				self._pricing_data = http_response.json()

			# Создать объект кэша с временной меткой
			now = datetime.now()
			cached_data = CachedPricingData(timestamp=now, data=self._pricing_data or {})

			# Убедиться, что директория кэша существует
			self._cache_dir.mkdir(parents=True, exist_ok=True)

			# Создать файл кэша с временной меткой в имени файла
			timestamp_string = now.strftime('%Y%m%d_%H%M%S')
			cache_file_path = self._cache_dir / f'pricing_{timestamp_string}.json'

			await anyio.Path(cache_file_path).write_text(cached_data.model_dump_json(indent=2))
		except Exception as fetch_error:
			logger.debug(f'Error fetching pricing data: {fetch_error}')
			# Вернуться к пустым данным о ценах
			self._pricing_data = {}

	async def get_model_pricing(self, model_name: str) -> ModelPricing | None:
		"""Получить информацию о ценах для конкретной модели"""
		# Убедиться, что мы инициализированы
		if not self._initialized:
			await self.initialize()

		# Сначала проверить пользовательские цены
		if model_name in CUSTOM_MODEL_PRICING:
			custom_data = CUSTOM_MODEL_PRICING[model_name]
			return ModelPricing(
				model=model_name,
				output_cost_per_token=custom_data.get('output_cost_per_token'),
				input_cost_per_token=custom_data.get('input_cost_per_token'),
				max_output_tokens=custom_data.get('max_output_tokens'),
				max_input_tokens=custom_data.get('max_input_tokens'),
				max_tokens=custom_data.get('max_tokens'),
				cache_creation_input_token_cost=custom_data.get('cache_creation_input_token_cost'),
				cache_read_input_token_cost=custom_data.get('cache_read_input_token_cost'),
			)

		# Преобразовать имя модели в имя модели LiteLLM, если необходимо
		mapped_model_name = MODEL_TO_LITELLM.get(model_name, model_name)

		if not self._pricing_data or mapped_model_name not in self._pricing_data:
			return None

		pricing_data = self._pricing_data[mapped_model_name]
		return ModelPricing(
			model=model_name,
			output_cost_per_token=pricing_data.get('output_cost_per_token'),
			input_cost_per_token=pricing_data.get('input_cost_per_token'),
			max_output_tokens=pricing_data.get('max_output_tokens'),
			max_input_tokens=pricing_data.get('max_input_tokens'),
			max_tokens=pricing_data.get('max_tokens'),
			cache_creation_input_token_cost=pricing_data.get('cache_creation_input_token_cost'),
			cache_read_input_token_cost=pricing_data.get('cache_read_input_token_cost'),
		)

	async def calculate_cost(self, model: str, usage: ChatInvokeUsage) -> TokenCostCalculated | None:
		if not self.include_cost:
			return None

		pricing_info = await self.get_model_pricing(model)
		if pricing_info is None:
			return None

		cached_tokens_count = usage.prompt_cached_tokens or 0
		uncached_prompt_tokens = usage.prompt_tokens - cached_tokens_count

		# Токены завершения
		completion_tokens_count = usage.completion_tokens
		completion_cost_value = completion_tokens_count * float(pricing_info.output_cost_per_token or 0)

		# Новые токены промпта
		new_prompt_cost_value = uncached_prompt_tokens * (pricing_info.input_cost_per_token or 0)

		# Кэшированные токены
		read_cached_cost = None
		if cached_tokens_count and pricing_info.cache_read_input_token_cost:
			read_cached_cost = cached_tokens_count * pricing_info.cache_read_input_token_cost

		# Токены создания кэша
		creation_tokens_count = usage.prompt_cache_creation_tokens
		creation_cost = None
		if pricing_info.cache_creation_input_token_cost and creation_tokens_count:
			creation_cost = creation_tokens_count * pricing_info.cache_creation_input_token_cost

		return TokenCostCalculated(
			completion_tokens=completion_tokens_count,
			completion_cost=completion_cost_value,
			new_prompt_tokens=usage.prompt_tokens,
			new_prompt_cost=new_prompt_cost_value,
			prompt_read_cached_tokens=usage.prompt_cached_tokens,
			prompt_read_cached_cost=read_cached_cost,
			prompt_cached_creation_tokens=creation_tokens_count,
			prompt_cache_creation_cost=creation_cost,
		)

	def add_usage(self, model: str, usage: ChatInvokeUsage) -> TokenUsageEntry:
		"""Добавить запись использования токенов в историю (без расчета стоимости)"""
		usage_entry = TokenUsageEntry(
			timestamp=datetime.now(),
			model=model,
			usage=usage,
		)

		self.usage_history.append(usage_entry)

		return usage_entry


	async def _log_usage(self, model: str, usage_entry: TokenUsageEntry) -> None:
		"""Записать использование в логгер"""
		if not self._initialized:
			await self.initialize()

		# ANSI коды цветов
		CYAN_COLOR = '\033[96m'
		YELLOW_COLOR = '\033[93m'
		GREEN_COLOR = '\033[92m'
		BLUE_COLOR = '\033[94m'
		RESET_COLOR = '\033[0m'

		# Всегда получить разбивку стоимости для деталей токенов (даже если не показываем затраты)
		cost_data = await self.calculate_cost(model, usage_entry.usage)

		# Построить разбивку входных токенов
		input_display = self._build_input_tokens_display(usage_entry.usage, cost_data)

		# Построить отображение выходных токенов
		completion_tokens_formatted = self._format_tokens(usage_entry.usage.completion_tokens)
		if self.include_cost and cost_data and cost_data.completion_cost > 0:
			output_display = f'📤 {GREEN_COLOR}{completion_tokens_formatted} (${cost_data.completion_cost:.4f}){RESET_COLOR}'
		else:
			output_display = f'📤 {GREEN_COLOR}{completion_tokens_formatted}{RESET_COLOR}'

		cost_logger.debug(f'🧠 {CYAN_COLOR}{model}{RESET_COLOR} | {input_display} | {output_display}')

	def _build_input_tokens_display(self, usage: ChatInvokeUsage, cost_data: TokenCostCalculated | None) -> str:
		"""Построить четкое отображение разбивки входных токенов с эмодзи и опциональными затратами"""
		YELLOW_COLOR = '\033[93m'
		BLUE_COLOR = '\033[94m'
		RESET_COLOR = '\033[0m'

		display_parts = []

		# Всегда показывать разбивку токенов, если у нас есть информация о кэше, независимо от отслеживания затрат
		if usage.prompt_cached_tokens or usage.prompt_cache_creation_tokens:
			# Вычислить фактические новые токены (не кэшированные)
			cached_count = usage.prompt_cached_tokens or 0
			new_tokens_count = usage.prompt_tokens - cached_count

			if new_tokens_count > 0:
				new_tokens_formatted = self._format_tokens(new_tokens_count)
				if self.include_cost and cost_data and cost_data.new_prompt_cost > 0:
					display_parts.append(f'🆕 {YELLOW_COLOR}{new_tokens_formatted} (${cost_data.new_prompt_cost:.4f}){RESET_COLOR}')
				else:
					display_parts.append(f'🆕 {YELLOW_COLOR}{new_tokens_formatted}{RESET_COLOR}')

			if usage.prompt_cached_tokens:
				cached_tokens_formatted = self._format_tokens(usage.prompt_cached_tokens)
				if self.include_cost and cost_data and cost_data.prompt_read_cached_cost:
					display_parts.append(f'💾 {BLUE_COLOR}{cached_tokens_formatted} (${cost_data.prompt_read_cached_cost:.4f}){RESET_COLOR}')
				else:
					display_parts.append(f'💾 {BLUE_COLOR}{cached_tokens_formatted}{RESET_COLOR}')

			if usage.prompt_cache_creation_tokens:
				creation_tokens_formatted = self._format_tokens(usage.prompt_cache_creation_tokens)
				if self.include_cost and cost_data and cost_data.prompt_cache_creation_cost:
					display_parts.append(f'🔧 {BLUE_COLOR}{creation_tokens_formatted} (${cost_data.prompt_cache_creation_cost:.4f}){RESET_COLOR}')
				else:
					display_parts.append(f'🔧 {BLUE_COLOR}{creation_tokens_formatted}{RESET_COLOR}')

		if not display_parts:
			# Запасной вариант простого отображения, когда информация о кэше недоступна
			total_tokens_formatted = self._format_tokens(usage.prompt_tokens)
			if self.include_cost and cost_data and cost_data.new_prompt_cost > 0:
				display_parts.append(f'📥 {YELLOW_COLOR}{total_tokens_formatted} (${cost_data.new_prompt_cost:.4f}){RESET_COLOR}')
			else:
				display_parts.append(f'📥 {YELLOW_COLOR}{total_tokens_formatted}{RESET_COLOR}')

		return ' + '.join(display_parts)

	def register_llm(self, llm: BaseChatModel) -> BaseChatModel:
		"""
		Зарегистрировать LLM для автоматического отслеживания использования токенов

		@dev Гарантирует, что один и тот же экземпляр не регистрируется несколько раз
		"""
		# Использовать ID экземпляра в качестве ключа, чтобы избежать коллизий между несколькими экземплярами
		llm_instance_id = str(id(llm))

		# Проверить, зарегистрирован ли уже этот точный экземпляр
		if llm_instance_id in self.registered_llms:
			logger.debug(f'LLM instance {llm_instance_id} ({llm.provider}_{llm.model}) is already registered')
			return llm

		self.registered_llms[llm_instance_id] = llm

		# Сохранить исходный метод
		original_ainvoke_method = llm.ainvoke
		# Сохранить ссылку на self для использования в замыкании
		service_instance = self

		# Создать обернутую версию, которая отслеживает использование
		async def tracked_ainvoke(messages, output_format=None, **kwargs):
			# Вызвать исходный метод, передавая любые дополнительные kwargs
			invoke_result = await original_ainvoke_method(messages, output_format, **kwargs)

			# Отслеживать использование, если доступно (await не нужен, так как add_usage теперь синхронный)
			# Использовать llm.model вместо llm.name для согласованности с get_usage_tokens_for_model()
			if invoke_result.usage:
				usage_entry = service_instance.add_usage(llm.model, invoke_result.usage)

				logger.debug(f'Token cost service: {usage_entry}')

				create_task_with_error_handling(
					service_instance._log_usage(llm.model, usage_entry), name='log_token_usage', suppress_exceptions=True
				)

			# else:
			# 	await service_instance._log_non_usage_llm(llm)

			return invoke_result

		# Заменить метод нашей отслеживаемой версией
		# Использование setattr для избежания проблем с проверкой типов для перегруженных методов
		setattr(llm, 'ainvoke', tracked_ainvoke)

		return llm

	def get_usage_tokens_for_model(self, model: str) -> ModelUsageTokens:
		"""Получить токены использования для конкретной модели"""
		model_usage_entries = [entry for entry in self.usage_history if entry.model == model]

		return ModelUsageTokens(
			model=model,
			completion_tokens=sum(entry.usage.completion_tokens for entry in model_usage_entries),
			prompt_cached_tokens=sum(entry.usage.prompt_cached_tokens or 0 for entry in model_usage_entries),
			prompt_tokens=sum(entry.usage.prompt_tokens for entry in model_usage_entries),
			total_tokens=sum(entry.usage.prompt_tokens + entry.usage.completion_tokens for entry in model_usage_entries),
		)

	async def get_usage_summary(self, model: str | None = None, since: datetime | None = None) -> UsageSummary:
		"""Получить сводку использования токенов и затрат (затраты вычисляются на лету)"""
		filtered_entries = self.usage_history

		if model:
			filtered_entries = [entry for entry in filtered_entries if entry.model == model]

		if since:
			filtered_entries = [entry for entry in filtered_entries if entry.timestamp >= since]

		if not filtered_entries:
			return UsageSummary(
				total_completion_tokens=0,
				total_completion_cost=0.0,
				total_tokens=0,
				total_cost=0.0,
				total_prompt_tokens=0,
				total_prompt_cost=0.0,
				total_prompt_cached_tokens=0,
				total_prompt_cached_cost=0.0,
				entry_count=0,
			)

		# Вычислить итоги
		total_completion = sum(entry.usage.completion_tokens for entry in filtered_entries)
		total_prompt = sum(entry.usage.prompt_tokens for entry in filtered_entries)
		total_tokens_count = total_prompt + total_completion
		total_prompt_cached = sum(entry.usage.prompt_cached_tokens or 0 for entry in filtered_entries)
		unique_models = list({entry.model for entry in filtered_entries})

		# Вычислить статистику по моделям с расчетом затрат запись за записью
		per_model_stats: dict[str, ModelUsageStats] = {}
		total_completion_cost = 0.0
		total_prompt_cost = 0.0
		total_prompt_cached_cost = 0.0

		for usage_entry in filtered_entries:
			if usage_entry.model not in per_model_stats:
				per_model_stats[usage_entry.model] = ModelUsageStats(model=usage_entry.model)

			model_statistics = per_model_stats[usage_entry.model]
			model_statistics.completion_tokens += usage_entry.usage.completion_tokens
			model_statistics.prompt_tokens += usage_entry.usage.prompt_tokens
			model_statistics.total_tokens += usage_entry.usage.prompt_tokens + usage_entry.usage.completion_tokens
			model_statistics.invocations += 1

			if self.include_cost:
				# Вычислить затраты запись за записью используя обновленную функцию calculate_cost
				cost_calculation = await self.calculate_cost(usage_entry.model, usage_entry.usage)
				if cost_calculation:
					model_statistics.cost += cost_calculation.total_cost
					total_completion_cost += cost_calculation.completion_cost
					total_prompt_cost += cost_calculation.prompt_cost
					total_prompt_cached_cost += cost_calculation.prompt_read_cached_cost or 0

		# Вычислить средние значения
		for model_statistics in per_model_stats.values():
			if model_statistics.invocations > 0:
				model_statistics.average_tokens_per_invocation = model_statistics.total_tokens / model_statistics.invocations

		return UsageSummary(
			total_completion_tokens=total_completion,
			total_completion_cost=total_completion_cost,
			total_tokens=total_tokens_count,
			total_cost=total_completion_cost + total_prompt_cost + total_prompt_cached_cost,
			total_prompt_tokens=total_prompt,
			total_prompt_cost=total_prompt_cost,
			total_prompt_cached_tokens=total_prompt_cached,
			total_prompt_cached_cost=total_prompt_cached_cost,
			entry_count=len(filtered_entries),
			by_model=per_model_stats,
		)

	def _format_tokens(self, token_count: int) -> str:
		"""Форматировать количество токенов с суффиксом k для тысяч"""
		if token_count >= 1000000000:
			return f'{token_count / 1000000000:.1f}B'
		if token_count >= 1000000:
			return f'{token_count / 1000000:.1f}M'
		if token_count >= 1000:
			return f'{token_count / 1000:.1f}k'
		return str(token_count)

	async def log_usage_summary(self) -> None:
		"""Записать комплексную сводку использования по моделям с цветами и красивым форматированием"""
		if not self.usage_history:
			return

		usage_summary = await self.get_usage_summary()

		if usage_summary.entry_count == 0:
			return

		# ANSI коды цветов
		CYAN_COLOR = '\033[96m'
		YELLOW_COLOR = '\033[93m'
		GREEN_COLOR = '\033[92m'
		BLUE_COLOR = '\033[94m'
		MAGENTA_COLOR = '\033[95m'
		RESET_COLOR = '\033[0m'
		BOLD_COLOR = '\033[1m'

		# Записать общую сводку
		total_tokens_formatted = self._format_tokens(usage_summary.total_tokens)
		completion_tokens_formatted = self._format_tokens(usage_summary.total_completion_tokens)
		prompt_tokens_formatted = self._format_tokens(usage_summary.total_prompt_tokens)

		# Форматировать разбивку затрат для входа и выхода (только если отслеживание затрат включено)
		if self.include_cost and usage_summary.total_cost > 0:
			total_cost_display = f' (${MAGENTA_COLOR}{usage_summary.total_cost:.4f}{RESET_COLOR})'
			completion_cost_display = f' (${usage_summary.total_completion_cost:.4f})'
			prompt_cost_display = f' (${usage_summary.total_prompt_cost:.4f})'
		else:
			total_cost_display = ''
			completion_cost_display = ''
			prompt_cost_display = ''

		if len(usage_summary.by_model) > 1:
			cost_logger.debug(
				f'💲 {BOLD_COLOR}Total Usage Summary{RESET_COLOR}: {BLUE_COLOR}{total_tokens_formatted} tokens{RESET_COLOR}{total_cost_display} | '
				f'⬅️ {YELLOW_COLOR}{prompt_tokens_formatted}{prompt_cost_display}{RESET_COLOR} | ➡️ {GREEN_COLOR}{completion_tokens_formatted}{completion_cost_display}{RESET_COLOR}'
			)

		for model_name, model_statistics in usage_summary.by_model.items():
			# Форматировать токены
			model_total_formatted = self._format_tokens(model_statistics.total_tokens)
			model_completion_formatted = self._format_tokens(model_statistics.completion_tokens)
			model_prompt_formatted = self._format_tokens(model_statistics.prompt_tokens)
			avg_tokens_formatted = self._format_tokens(int(model_statistics.average_tokens_per_invocation))

			# Форматировать отображение затрат (только если отслеживание затрат включено)
			if self.include_cost:
				# Вычислить затраты по моделям на лету
				model_completion_cost = 0.0
				model_prompt_cost = 0.0

				# Вычислить затраты для этой модели
				for history_entry in self.usage_history:
					if history_entry.model == model_name:
						entry_cost = await self.calculate_cost(history_entry.model, history_entry.usage)
						if entry_cost:
							model_completion_cost += entry_cost.completion_cost
							model_prompt_cost += entry_cost.prompt_cost

				total_model_cost = model_completion_cost + model_prompt_cost

				if total_model_cost > 0:
					cost_display = f' (${MAGENTA_COLOR}{total_model_cost:.4f}{RESET_COLOR})'
					completion_display = f'{GREEN_COLOR}{model_completion_formatted} (${model_completion_cost:.4f}){RESET_COLOR}'
					prompt_display = f'{YELLOW_COLOR}{model_prompt_formatted} (${model_prompt_cost:.4f}){RESET_COLOR}'
				else:
					cost_display = ''
					completion_display = f'{GREEN_COLOR}{model_completion_formatted}{RESET_COLOR}'
					prompt_display = f'{YELLOW_COLOR}{model_prompt_formatted}{RESET_COLOR}'
			else:
				cost_display = ''
				completion_display = f'{GREEN_COLOR}{model_completion_formatted}{RESET_COLOR}'
				prompt_display = f'{YELLOW_COLOR}{model_prompt_formatted}{RESET_COLOR}'

			cost_logger.debug(
				f'  🤖 {CYAN_COLOR}{model_name}{RESET_COLOR}: {BLUE_COLOR}{model_total_formatted} tokens{RESET_COLOR}{cost_display} | '
				f'⬅️ {prompt_display} | ➡️ {completion_display} | '
				f'📞 {model_statistics.invocations} calls | 📈 {avg_tokens_formatted}/call'
			)

	async def get_cost_by_model(self) -> dict[str, ModelUsageStats]:
		"""Получить разбивку затрат по моделям"""
		usage_summary = await self.get_usage_summary()
		return usage_summary.by_model

	def clear_history(self) -> None:
		"""Очистить историю использования"""
		self.usage_history = []

	async def refresh_pricing_data(self) -> None:
		"""Принудительно обновить данные о ценах с GitHub"""
		if self.include_cost:
			await self._fetch_and_cache_pricing_data()

	async def clean_old_caches(self, keep_count: int = 3) -> None:
		"""Очистить старые файлы кэша, оставляя только самые последние"""
		try:
			# Список всех JSON файлов в директории кэша
			all_cache_files = list(self._cache_dir.glob('*.json'))

			if len(all_cache_files) <= keep_count:
				return

			# Сортировать по времени модификации (самые старые первые)
			all_cache_files.sort(key=lambda file: file.stat().st_mtime)

			# Удалить все, кроме самых последних файлов
			for old_cache_file in all_cache_files[:-keep_count]:
				try:
					os.remove(old_cache_file)
				except Exception:
					pass
		except Exception as cleanup_error:
			logger.debug(f'Error cleaning old cache files: {cleanup_error}')

	async def ensure_pricing_loaded(self) -> None:
		"""Убедиться, что данные о ценах загружены в фоновом режиме. Вызвать это после создания сервиса."""
		if not self._initialized and self.include_cost:
			# Это будет выполняться в фоновом режиме и не будет блокировать
			await self.initialize()
