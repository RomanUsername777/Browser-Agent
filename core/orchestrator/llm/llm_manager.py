"""Менеджер для работы с LLM в Agent."""

import asyncio
import re
import time
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

from core.ai_models.messages import BaseMessage, UserMessage, AssistantMessage, ContentPartTextParam
from core.exceptions import ModelProviderError, ModelRateLimitError
from core.orchestrator.models import StepDecision
from core.observability import observe

if TYPE_CHECKING:
	from core.orchestrator.manager import TaskOrchestrator

# URL pattern for matching URLs in text
URL_PATTERN = re.compile(r'https?://[^\s<>"\']+|www\.[^\s<>"\']+|[^\s<>"\']+\.[a-z]{2,}(?:/[^\s<>"\']*)?', re.IGNORECASE)


class LLMManager:
	"""Менеджер для управления взаимодействием с LLM."""

	def __init__(self, agent: 'TaskOrchestrator'):
		self.orchestrator = agent

	async def prepare_llm_messages(self) -> list[BaseMessage]:
		"""Подготавливает сообщения для отправки в LLM"""
		context_messages = self.orchestrator._message_manager.get_messages()
		self.orchestrator.logger.debug(
			f'🤖 Шаг {self.orchestrator.state.n_steps}: Вызов LLM с {len(context_messages)} сообщениями (модель: {self.orchestrator.llm.model})...'
		)
		return context_messages

	async def call_llm_with_timeout(self, context_messages: list[BaseMessage]) -> StepDecision:
		"""Вызывает LLM с обработкой таймаутов"""
		try:
			llm_response = await asyncio.wait_for(
				self.get_model_output_with_retry(context_messages), timeout=self.orchestrator.settings.llm_timeout
			)
			return llm_response
		except TimeoutError:
			await self.log_llm_timeout(context_messages)
			raise TimeoutError(
				f'Вызов LLM превысил таймаут {self.orchestrator.settings.llm_timeout} секунд. Сократите размышления и вывод.'
			)

	async def log_llm_timeout(self, context_messages: list[BaseMessage]) -> None:
		"""Логирует таймаут вызова LLM"""
		@observe(name='_llm_call_timed_out_with_input')
		async def _log_model_input_to_lmnr(context_messages: list[BaseMessage]) -> None:
			"""Логирование ввода модели при таймауте"""
			pass

		await _log_model_input_to_lmnr(context_messages)

	async def obtain_llm_decision(self, page_state) -> None:
		"""Получает решение от LLM с логикой повторных попыток и обработкой колбэков."""
		from core.session.models import BrowserStateSummary
		
		# Получение сообщений контекста для LLM
		t1 = time.time()
		context_messages = await self.prepare_llm_messages()
		
		# Вызов LLM с обработкой таймаутов
		t2 = time.time()
		llm_response = await self.call_llm_with_timeout(context_messages)

		# Сохранение ответа LLM в состоянии
		self.store_llm_response(llm_response)

		# Обработка колбэков и сохранение разговора
		t3 = time.time()
		await self.orchestrator._history_manager.handle_post_llm_processing(page_state, context_messages)

		# Проверки на остановку
		await self.orchestrator._verify_agent_continuation()

	def store_llm_response(self, llm_response: StepDecision) -> None:
		"""Сохраняет ответ LLM в состоянии агента"""
		self.orchestrator.state.last_model_output = llm_response

	async def get_model_output_with_retry(self, context_messages: list[BaseMessage]) -> StepDecision:
		"""Получает вывод модели с логикой повторных попыток для пустых действий"""
		# Первичный вызов модели
		llm_response = await self.get_model_output(context_messages)
		action_count = len(llm_response.action) if llm_response.action else 0
		self.orchestrator.logger.debug(
			self.orchestrator.logger.debug(f'Шаг {self.orchestrator.state.n_steps}: Получен ответ LLM с {action_count} действиями')
		)

		# Проверка на пустые действия
		if self.is_empty_action(llm_response):
			return await self.retry_with_clarification(context_messages)

		return llm_response

	def is_empty_action(self, agent_decision: StepDecision) -> bool:
		"""Проверяет, является ли действие пустым"""
		return (
			not agent_decision.action
			or not isinstance(agent_decision.action, list)
			or all(action.model_dump() == {} for action in agent_decision.action)
		)

	async def retry_with_clarification(self, context_messages: list[BaseMessage]) -> StepDecision:
		"""Повторяет вызов модели с уточняющим сообщением"""
		self.orchestrator.logger.warning('Модель вернула пустое действие. Повторная попытка...')

		clarification_message = UserMessage(
			content='You forgot to return an action. Please respond with a valid JSON action according to the expected schema with your assessment and next actions.'
		)

		retry_messages = context_messages + [clarification_message]
		llm_response = await self.get_model_output(retry_messages)

		# Если после повтора все еще пустое действие, создаем безопасное действие
		if self.is_empty_action(llm_response):
			return self.create_safe_noop_action()

		return llm_response

	def create_safe_noop_action(self) -> StepDecision:
		"""Создает безопасное noop действие при отсутствии ответа от модели"""
		self.orchestrator.logger.warning('Модель все еще вернула пустое действие после повтора. Вставляем безопасное noop действие.')
		action_instance = self.orchestrator.CommandModel()
		setattr(
			action_instance,
			'done',
			{
				'success': False,
				'text': 'No next action returned by LLM!',
			},
		)
		# Создаем новый StepDecision с noop действием используя текущее состояние
		return self.orchestrator.StepDecision(current_state=self.orchestrator.state.current_state, action=[action_instance])

	@observe(name='get_model_output', ignore_input=True, ignore_output=False)
	async def get_model_output(self, input_messages: list[BaseMessage]) -> StepDecision:
		"""Get next action from LLM based on current state"""

		urls_replaced = self.process_messages_and_replace_long_urls_shorter_ones(input_messages)

		# Build kwargs for ainvoke
		# Примечание: некоторые LLM-провайдеры умеют автоматически строить описания действий на основе output_format
		kwargs: dict = {'output_format': self.orchestrator.StepDecision}

		try:
			response = await self.orchestrator.llm.ainvoke(input_messages, **kwargs)
			parsed: StepDecision = response.completion  # type: ignore[assignment]

			# Replace any shortened URLs in the LLM response back to original URLs
			if urls_replaced:
				self.recursive_process_all_strings_inside_pydantic_model(parsed, urls_replaced)

			# cut the number of actions to max_actions_per_step if needed
			if len(parsed.action) > self.orchestrator.settings.max_actions_per_step:
				parsed.action = parsed.action[: self.orchestrator.settings.max_actions_per_step]

			if not (hasattr(self.orchestrator.state, 'paused') and (self.orchestrator.state.paused or self.orchestrator.state.stopped)):
				from core.orchestrator.manager import log_response
				log_response(parsed, self.orchestrator.tools.registry.registry, self.orchestrator.logger)
				await self.orchestrator._broadcast_model_state(parsed)

			self.orchestrator._log_next_action_summary(parsed)
			return parsed
		except ValidationError:
			# Just re-raise - Pydantic's validation errors are already descriptive
			raise
		except (ModelRateLimitError, ModelProviderError) as e:
			# Check if we can switch to a fallback LLM
			if not self.try_switch_to_fallback_llm(e):
				# No fallback available, re-raise the original error
				raise
			# Retry with the fallback LLM
			return await self.get_model_output(input_messages)

	def try_switch_to_fallback_llm(self, error: ModelRateLimitError | ModelProviderError) -> bool:
		"""
		Attempt to switch to a fallback LLM after a rate limit or provider error.

		Returns True if successfully switched to a fallback, False if no fallback available.
		Once switched, the agent will use the fallback LLM for the rest of the run.
		"""
		# Already using fallback - can't switch again
		if self.orchestrator._using_fallback_llm:
			# Обрезаем сообщение об ошибке, чтобы не выводить огромные пасты валидации
			error_msg_short = error.message[:200] + '...' if len(error.message) > 200 else error.message
			self.orchestrator.logger.warning(
				f'⚠️ Fallback LLM also failed ({type(error).__name__}: {error_msg_short}), no more fallbacks available'
			)
			return False

		# Check if error is retryable (rate limit, auth errors, or server errors)
		retryable_status_codes = {401, 402, 429, 500, 502, 503, 504}
		is_retryable = isinstance(error, ModelRateLimitError) or (
			hasattr(error, 'status_code') and error.status_code in retryable_status_codes
		)

		if not is_retryable:
			return False

		# Check if we have a fallback LLM configured
		if self.orchestrator._fallback_llm is None:
			# Обрезаем сообщение об ошибке, чтобы не выводить огромные пасты валидации
			error_msg_short = error.message[:200] + '...' if len(error.message) > 200 else error.message
			self.orchestrator.logger.warning(f'⚠️ LLM error ({type(error).__name__}: {error_msg_short}) but no fallback_llm configured')
			return False

		self.log_fallback_switch(error, self.orchestrator._fallback_llm)

		# Switch to the fallback LLM
		self.orchestrator.llm = self.orchestrator._fallback_llm
		self.orchestrator._using_fallback_llm = True

		# Register the fallback LLM for token cost tracking
		self.orchestrator.token_cost_service.register_llm(self.orchestrator._fallback_llm)

		return True

	def log_fallback_switch(self, error: ModelRateLimitError | ModelProviderError, fallback) -> None:
		"""Log when switching to a fallback LLM."""
		from core.ai_models.models import BaseChatModel
		
		primary_model = self.orchestrator._primary_llm.model if hasattr(self.orchestrator._primary_llm, 'model') else 'unknown'
		fallback_model = fallback.model if hasattr(fallback, 'model') else 'unknown'
		error_type = type(error).__name__
		status_code = getattr(error, 'status_code', 'N/A')

		self.orchestrator.logger.warning(
			f'⚠️ Primary LLM ({primary_model}) failed with {error_type} (status={status_code}), '
			f'switching to fallback LLM ({fallback_model})'
		)

	# region - URL replacement methods

	def process_messages_and_replace_long_urls_shorter_ones(self, input_messages: list[BaseMessage]) -> dict[str, str]:
		"""Replace long URLs with shorter ones.
		Edits input_messages in place.
		
		Returns:
			dict mapping {shortened_url: original_url}
		"""
		urls_replaced: dict[str, str] = {}

		# Process each message in place
		for message in input_messages:
			# No need to process SystemMessage, we have control over that anyway
			if isinstance(message, (UserMessage, AssistantMessage)):
				if isinstance(message.content, str):
					# Simple string content
					message.content, replaced_urls = self._replace_urls_in_text(message.content)
					urls_replaced.update(replaced_urls)
				elif isinstance(message.content, list):
					# List of content parts
					for part in message.content:
						if isinstance(part, ContentPartTextParam):
							part.text, replaced_urls = self._replace_urls_in_text(part.text)
							urls_replaced.update(replaced_urls)

		return urls_replaced

	def _replace_urls_in_text(self, text: str) -> tuple[str, dict[str, str]]:
		"""Replace URLs in a text string"""
		import hashlib

		replaced_urls: dict[str, str] = {}
		url_shortening_limit = getattr(self.orchestrator, '_url_shortening_limit', 100)

		def replace_url(match: re.Match) -> str:
			"""URL can only have 1 query and 1 fragment"""
			original_url = match.group(0)

			# Find where the query/fragment starts
			query_start = original_url.find('?')
			fragment_start = original_url.find('#')

			# Find the earliest position of query or fragment
			after_path_start = len(original_url)  # Default: no query/fragment
			if query_start != -1:
				after_path_start = min(after_path_start, query_start)
			if fragment_start != -1:
				after_path_start = min(after_path_start, fragment_start)

			# Split URL into base (up to path) and after_path (query + fragment)
			base_url = original_url[:after_path_start]
			after_path = original_url[after_path_start:]

			# If after_path is within the limit, don't shorten
			if len(after_path) <= url_shortening_limit:
				return original_url

			# If after_path is too long, truncate and add hash
			if after_path:
				truncated_after_path = after_path[:url_shortening_limit]
				# Create a short hash of the full after_path content
				hash_obj = hashlib.md5(after_path.encode('utf-8'))
				short_hash = hash_obj.hexdigest()[:7]
				# Create shortened URL
				shortened = f'{base_url}{truncated_after_path}...{short_hash}'
				# Only use shortened URL if it's actually shorter than the original
				if len(shortened) < len(original_url):
					replaced_urls[shortened] = original_url
					return shortened

			return original_url

		return URL_PATTERN.sub(replace_url, text), replaced_urls

	@staticmethod
	def recursive_process_all_strings_inside_pydantic_model(model: BaseModel, url_replacements: dict[str, str]) -> None:
		"""Recursively process all strings inside a Pydantic model, replacing shortened URLs with originals in place."""
		for field_name, field_value in model.__dict__.items():
			if isinstance(field_value, str):
				# Replace shortened URLs with original URLs in string
				processed_string = LLMManager.replace_shortened_urls_in_string(field_value, url_replacements)
				setattr(model, field_name, processed_string)
			elif isinstance(field_value, BaseModel):
				# Recursively process nested Pydantic models
				LLMManager.recursive_process_all_strings_inside_pydantic_model(field_value, url_replacements)
			elif isinstance(field_value, dict):
				# Process dictionary values in place
				LLMManager.recursive_process_dict(field_value, url_replacements)
			elif isinstance(field_value, (list, tuple)):
				processed_value = LLMManager.recursive_process_list_or_tuple(field_value, url_replacements)
				setattr(model, field_name, processed_value)

	@staticmethod
	def recursive_process_dict(dictionary: dict, url_replacements: dict[str, str]) -> None:
		"""Helper method to process dictionaries."""
		for k, v in dictionary.items():
			if isinstance(v, str):
				dictionary[k] = LLMManager.replace_shortened_urls_in_string(v, url_replacements)
			elif isinstance(v, BaseModel):
				LLMManager.recursive_process_all_strings_inside_pydantic_model(v, url_replacements)
			elif isinstance(v, dict):
				LLMManager.recursive_process_dict(v, url_replacements)
			elif isinstance(v, (list, tuple)):
				dictionary[k] = LLMManager.recursive_process_list_or_tuple(v, url_replacements)

	@staticmethod
	def recursive_process_list_or_tuple(container: list | tuple, url_replacements: dict[str, str]) -> list | tuple:
		"""Helper method to process lists and tuples."""
		if isinstance(container, tuple):
			# For tuples, create a new tuple with processed items
			processed_items = []
			for item in container:
				if isinstance(item, str):
					processed_items.append(LLMManager.replace_shortened_urls_in_string(item, url_replacements))
				elif isinstance(item, BaseModel):
					LLMManager.recursive_process_all_strings_inside_pydantic_model(item, url_replacements)
					processed_items.append(item)
				elif isinstance(item, dict):
					LLMManager.recursive_process_dict(item, url_replacements)
					processed_items.append(item)
				elif isinstance(item, (list, tuple)):
					processed_items.append(LLMManager.recursive_process_list_or_tuple(item, url_replacements))
				else:
					processed_items.append(item)
			return tuple(processed_items)
		else:
			# For lists, modify in place
			for i, item in enumerate(container):
				if isinstance(item, str):
					container[i] = LLMManager.replace_shortened_urls_in_string(item, url_replacements)
				elif isinstance(item, BaseModel):
					LLMManager.recursive_process_all_strings_inside_pydantic_model(item, url_replacements)
				elif isinstance(item, dict):
					LLMManager.recursive_process_dict(item, url_replacements)
				elif isinstance(item, (list, tuple)):
					container[i] = LLMManager.recursive_process_list_or_tuple(item, url_replacements)
			return container

	@staticmethod
	def replace_shortened_urls_in_string(text: str, url_replacements: dict[str, str]) -> str:
		"""Replace all shortened URLs in a string with their original URLs."""
		result = text
		for shortened_url, original_url in url_replacements.items():
			result = result.replace(shortened_url, original_url)
		return result

	# endregion - URL replacement

