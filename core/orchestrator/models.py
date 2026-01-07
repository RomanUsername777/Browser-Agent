from __future__ import annotations

import json
import logging
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, Literal

from openai import RateLimitError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model, model_validator
from typing_extensions import TypeVar
from uuid_extensions import uuid7str

from core.orchestrator.message_manager.models import MessageManagerState
from core.session.models import BrowserStateHistory
from core.dom_processing.models import DEFAULT_INCLUDE_ATTRIBUTES, DOMInteractedElement, DOMSelectorMap

from core.ai_models.models import BaseChatModel
from core.pricing.models import UsageSummary
from core.actions.registry.models import CommandModel

logger = logging.getLogger(__name__)


class AgentSettings(BaseModel):
	"""Параметры конфигурации для агента"""

	calculate_cost: bool = False
	extend_system_message: str | None = None
	final_response_after_failure: bool = True  # Если True, попытаться сделать один финальный вызов восстановления после max_failures
	flash_mode: bool = False  # Если включено, отключает evaluation_previous_goal и next_goal, и устанавливает use_thinking = False
	generate_gif: bool | str = False
	ground_truth: str | None = None  # Правильный ответ или критерии для валидации судьи
	include_attributes: list[str] | None = DEFAULT_INCLUDE_ATTRIBUTES
	include_tool_call_examples: bool = False
	llm_timeout: int = 60  # Таймаут в секундах для вызовов LLM (автоопределение: 30s для gemini, 90s для o3, 60s по умолчанию)
	max_actions_per_step: int = 3
	max_failures: int = 3
	max_history_items: int | None = None
	override_system_message: str | None = None
	page_extraction_llm: BaseChatModel | None = None
	save_conversation_path: str | Path | None = None
	save_conversation_path_encoding: str | None = 'utf-8'
	step_timeout: int = 180  # Таймаут в секундах для каждого шага
	use_judge: bool = True
	use_thinking: bool = True
	use_vision: bool | Literal['auto'] = True
	vision_detail_level: Literal['auto', 'high', 'low'] = 'auto'


class OrchestratorState(BaseModel):
	"""Содержит всю информацию о состоянии агента"""

	model_config = ConfigDict(arbitrary_types_allowed=True)

	agent_id: str = Field(default_factory=uuid7str)
	consecutive_failures: int = 0
	follow_up_task: bool = False  # Отслеживать, является ли агент задачей-продолжением
	last_model_output: StepDecision | None = None
	last_plan: str | None = None
	last_result: list[ExecutionResult] | None = None
	modal_click_failures: int = 0  # Счётчик неудачных попыток клика в модальном окне
	n_steps: int = 1

	# Состояние паузы/возобновления (сохраняется сериализуемым для checkpointing)
	paused: bool = False
	session_initialized: bool = False  # Отслеживать, были ли отправлены события сессии
	stopped: bool = False

	file_system_state: Any | None = None
	message_manager_state: MessageManagerState = Field(default_factory=MessageManagerState)


@dataclass
class StepContext:
	max_steps: int
	step_number: int

	def is_last_step(self) -> bool:
		"""Проверить, является ли это последним шагом"""
		return self.step_number >= self.max_steps - 1


class JudgementResult(BaseModel):
	"""Суждение LLM о трассировке агента"""

	failure_reason: str | None = Field(
		default=None,
		description='Максимум 5 предложений, объясняющих, почему задача не была выполнена успешно в случае неудачи. Если verdict истинно, используйте пустую строку.',
	)
	impossible_task: bool = Field(
		default=False,
		description='True, если задача была невозможна для выполнения из-за расплывчатых инструкций, сломанного сайта, недоступных ссылок, отсутствующих учётных данных или других непреодолимых препятствий',
	)
	reached_captcha: bool = Field(
		default=False,
		description='True, если агент столкнулся с вызовами капчи во время выполнения задачи',
	)
	reasoning: str | None = Field(default=None, description='Объяснение суждения')
	verdict: bool = Field(description='Была ли трассировка успешной или нет')


class ExecutionResult(BaseModel):
	"""Результат выполнения действия"""

	# Для действия done
	is_done: bool | None = False
	success: bool | None = None

	# Для суждения трассировки
	judgement: JudgementResult | None = None

	# Обработка ошибок - всегда включать в долгосрочную память
	error: str | None = None

	# Файлы
	attachments: list[str] | None = None  # Файлы для отображения в сообщении done

	# Изображения (base64-кодированные) - отдельно от текстового контента для эффективной обработки
	images: list[dict[str, Any]] | None = None  # [{"name": "file.jpg", "data": "base64_string"}]

	# Всегда включать в долгосрочную память
	long_term_memory: str | None = None  # Память об этом действии

	# если update_only_read_state = True, мы добавляем extracted_content в контекст агента только один раз для следующего шага
	# если update_only_read_state = False, мы добавляем extracted_content в долгосрочную память агента, если long_term_memory не предоставлена
	extracted_content: str | None = None
	include_extracted_content_only_once: bool = False  # Следует ли использовать извлечённый контент для обновления read_state

	# Метаданные для наблюдаемости (например, координаты клика)
	metadata: dict | None = None

	# Устаревший метод
	include_in_memory: bool = False  # включать ли extracted_content в long_term_memory

	@model_validator(mode='after')
	def validate_success_requires_done(self):
		"""Убедиться, что success=True может быть установлен только когда is_done=True"""
		if self.success is True and self.is_done is not True:
			raise ValueError(
				'success=True can only be set when is_done=True. '
				'For regular actions that succeed, leave success as None. '
				'Use success=False only for actions that fail.'
			)
		return self


class RerunSummaryAction(BaseModel):
	"""AI-сгенерированное резюме для завершения повторного запуска"""

	completion_status: Literal['complete', 'failed', 'partial'] = Field(
		description='Статус завершения повторного запуска: complete (все шаги успешны), partial (некоторые шаги успешны), failed (задача не завершена)'
	)
	success: bool = Field(description='Завершился ли повторный запуск успешно на основе визуального осмотра')
	summary: str = Field(description='Резюме того, что произошло во время повторного запуска')


class StepMetadata(BaseModel):
	"""Метаданные для одного шага, включая информацию о времени и токенах"""

	step_end_time: float
	step_interval: float | None = None
	step_number: int
	step_start_time: float

	@property
	def duration_seconds(self) -> float:
		"""Вычислить длительность шага в секундах"""
		return self.step_end_time - self.step_start_time


class AgentBrain(BaseModel):
	evaluation_previous_goal: str
	memory: str
	next_goal: str
	thinking: str | None = None


class StepDecision(BaseModel):
	model_config = ConfigDict(arbitrary_types_allowed=True, extra='forbid')

	action: list[CommandModel] = Field(
		...,
		json_schema_extra={'min_items': 1},  # Убедиться, что предоставлено хотя бы одно действие
	)
	evaluation_previous_goal: str | None = None
	memory: str | None = None
	next_goal: str | None = None
	thinking: str | None = None

	@classmethod
	def model_json_schema(cls, **kwargs):
		schema = super().model_json_schema(**kwargs)
		schema['required'] = ['action', 'evaluation_previous_goal', 'memory', 'next_goal']
		return schema

	@property
	def current_state(self) -> AgentBrain:
		"""Для обратной совместимости - возвращает AgentBrain с плоскими свойствами"""
		return AgentBrain(
			evaluation_previous_goal=self.evaluation_previous_goal if self.evaluation_previous_goal else '',
			memory=self.memory if self.memory else '',
			next_goal=self.next_goal if self.next_goal else '',
			thinking=self.thinking,
		)

	@staticmethod
	def type_with_custom_actions(custom_actions: type[CommandModel]) -> type[StepDecision]:
		"""Расширить действия пользовательскими действиями"""

		model_ = create_model(
			'StepDecision',
			__base__=StepDecision,
			action=(
				list[custom_actions],  # type: ignore
				Field(..., description='Список действий для выполнения', json_schema_extra={'min_items': 1}),
			),
			__module__=StepDecision.__module__,
		)
		return model_

	@staticmethod
	def type_with_custom_actions_no_thinking(custom_actions: type[CommandModel]) -> type[StepDecision]:
		"""Расширить действия пользовательскими действиями и исключить поле thinking"""

		class StepDecisionNoThinking(StepDecision):
			@classmethod
			def model_json_schema(cls, **kwargs):
				schema = super().model_json_schema(**kwargs)
				del schema['properties']['thinking']
				schema['required'] = ['action', 'evaluation_previous_goal', 'memory', 'next_goal']
				return schema

		model = create_model(
			'StepDecision',
			__base__=StepDecisionNoThinking,
			action=(
				list[custom_actions],  # type: ignore
				Field(..., json_schema_extra={'min_items': 1}),
			),
			__module__=StepDecisionNoThinking.__module__,
		)

		return model

	@staticmethod
	def type_with_custom_actions_flash_mode(custom_actions: type[CommandModel]) -> type[StepDecision]:
		"""Расширить действия пользовательскими действиями для flash mode - только поля memory и action"""

		class StepDecisionFlashMode(StepDecision):
			@classmethod
			def model_json_schema(cls, **kwargs):
				schema = super().model_json_schema(**kwargs)
				# Удаляем поля thinking, evaluation_previous_goal и next_goal
				del schema['properties']['evaluation_previous_goal']
				del schema['properties']['next_goal']
				del schema['properties']['thinking']
				# Обновляем обязательные поля, чтобы включить только оставшиеся свойства
				schema['required'] = ['action', 'memory']
				return schema

		model = create_model(
			'StepDecision',
			__base__=StepDecisionFlashMode,
			action=(
				list[custom_actions],  # type: ignore
				Field(..., json_schema_extra={'min_items': 1}),
			),
			__module__=StepDecisionFlashMode.__module__,
		)

		return model


class ExecutionHistory(BaseModel):
	"""Элемент истории для действий агента"""

	model_config = ConfigDict(arbitrary_types_allowed=True, protected_namespaces=())

	metadata: StepMetadata | None = None
	model_output: StepDecision | None
	result: list[ExecutionResult]
	state: BrowserStateHistory
	state_message: str | None = None

	@staticmethod
	def get_interacted_element(model_output: StepDecision, selector_map: DOMSelectorMap) -> list[DOMInteractedElement | None]:
		elements = []
		for action in model_output.action:
			index = action.get_index()
			if index is not None and index in selector_map:
				el = selector_map[index]
				elements.append(DOMInteractedElement.load_from_enhanced_dom_tree(el))
			else:
				elements.append(None)
		return elements

	def _filter_sensitive_data_from_string(self, value: str, sensitive_data: dict[str, str | dict[str, str]] | None) -> str:
		"""Отфильтровать чувствительные данные из строкового значения"""
		if not sensitive_data:
			return value

		# Сбор всех чувствительных значений с конвертацией старого формата в новый
		sensitive_values: dict[str, str] = {}

		# Обработать все записи чувствительных данных
		for key_or_domain, content in sensitive_data.items():
			if isinstance(content, dict):
				# Уже в новом формате: {domain: {key: value}}
				for key, val in content.items():
					if val:  # Пропустить пустые значения
						sensitive_values[key] = val
			elif content:  # Старый формат: {key: value} - конвертация в новый формат
				# Мы обрабатываем это так, как будто это было {'http*://*': {key_or_domain: content}}
				sensitive_values[key_or_domain] = content

		# Если нет валидных записей чувствительных данных, просто вернуть исходное значение
		if not sensitive_values:
			return value

		# Заменить все валидные значения чувствительных данных их тегами-заполнителями
		for key, val in sensitive_values.items():
			value = value.replace(val, f'<secret>{key}</secret>')

		return value

	def _filter_sensitive_data_from_dict(
		self, data: dict[str, Any], sensitive_data: dict[str, str | dict[str, str]] | None
	) -> dict[str, Any]:
		"""Рекурсивно отфильтровать чувствительные данные из словаря"""
		if not sensitive_data:
			return data

		filtered_data = {}
		for key, value in data.items():
			if isinstance(value, dict):
				filtered_data[key] = self._filter_sensitive_data_from_dict(value, sensitive_data)
			elif isinstance(value, list):
				filtered_data[key] = [
					self._filter_sensitive_data_from_dict(item, sensitive_data)
					if isinstance(item, dict)
					else self._filter_sensitive_data_from_string(item, sensitive_data)
					if isinstance(item, str)
					else item
					for item in value
				]
			elif isinstance(value, str):
				filtered_data[key] = self._filter_sensitive_data_from_string(value, sensitive_data)
			else:
				filtered_data[key] = value
		return filtered_data

	def model_dump(self, sensitive_data: dict[str, str | dict[str, str]] | None = None, **kwargs) -> dict[str, Any]:
		"""Пользовательская сериализация, обрабатывающая циклические ссылки и фильтрующая чувствительные данные"""

		# Обработать сериализацию действий
		model_output_dump = None
		if self.model_output:
			action_dump = [action.model_dump(exclude_none=True, mode='json') for action in self.model_output.action]

			# Фильтровать чувствительные данные только из параметров входных действий, если sensitive_data предоставлен
			if sensitive_data:
				action_dump = [
					self._filter_sensitive_data_from_dict(action, sensitive_data) if 'input' in action else action
					for action in action_dump
				]

			model_output_dump = {
				'action': action_dump,  # Это сохраняет фактические данные действия
				'evaluation_previous_goal': self.model_output.evaluation_previous_goal,
				'memory': self.model_output.memory,
				'next_goal': self.model_output.next_goal,
			}
			# Включать thinking только если он присутствует
			if self.model_output.thinking is not None:
				model_output_dump['thinking'] = self.model_output.thinking

		# Обработать сериализацию результатов - не фильтровать данные ExecutionResult
		# так как они должны содержать значимую информацию для агента
		result_dump = [r.model_dump(exclude_none=True, mode='json') for r in self.result]

		return {
			'metadata': self.metadata.model_dump() if self.metadata else None,
			'model_output': model_output_dump,
			'result': result_dump,
			'state': self.state.to_dict(),
			'state_message': self.state_message,
		}


AgentStructuredOutput = TypeVar('AgentStructuredOutput', bound=BaseModel)


class ExecutionHistoryList(BaseModel, Generic[AgentStructuredOutput]):
	"""Список сообщений ExecutionHistory, т.е. история действий и мыслей агента."""

	_output_model_schema: type[AgentStructuredOutput] | None = None

	history: list[ExecutionHistory]
	usage: UsageSummary | None = None

	def total_duration_seconds(self) -> float:
		"""Получить общую длительность всех шагов в секундах"""
		total = 0.0
		for h in self.history:
			if h.metadata:
				total += h.metadata.duration_seconds
		return total

	def __len__(self) -> int:
		"""Вернуть количество элементов истории"""
		return len(self.history)

	def __str__(self) -> str:
		"""Представление объекта ExecutionHistoryList"""
		return f'ExecutionHistoryList(all_model_outputs={self.model_actions()}, all_results={self.action_results()})'

	def add_item(self, history_item: ExecutionHistory) -> None:
		"""Добавить элемент истории в список"""
		self.history.append(history_item)

	def __repr__(self) -> str:
		"""Представление объекта ExecutionHistoryList"""
		return self.__str__()

	def save_to_file(self, filepath: str | Path, sensitive_data: dict[str, str | dict[str, str]] | None = None) -> None:
		"""Сохранить историю в JSON-файл с правильной сериализацией и опциональной фильтрацией чувствительных данных"""
		try:
			Path(filepath).parent.mkdir(parents=True, exist_ok=True)
			data = self.model_dump(sensitive_data=sensitive_data)
			with open(filepath, 'w', encoding='utf-8') as f:
				json.dump(data, f, indent=2)
		except Exception as e:
			raise e

	# 			f.write(script_content)
	# 	except Exception as e:
	# 		raise e

	def model_dump(self, **kwargs) -> dict[str, Any]:
		"""Пользовательская сериализация, которая правильно использует model_dump ExecutionHistory"""
		return {
			'history': [h.model_dump(**kwargs) for h in self.history],
		}

	@classmethod
	def load_from_dict(cls, data: dict[str, Any], output_model: type[StepDecision]) -> ExecutionHistoryList:
		# loop through history and validate output_model actions to enrich with custom actions
		for h in data['history']:
			if h['model_output']:
				if isinstance(h['model_output'], dict):
					h['model_output'] = output_model.model_validate(h['model_output'])
				else:
					h['model_output'] = None
			if 'interacted_element' not in h['state']:
				h['state']['interacted_element'] = None

		history = cls.model_validate(data)
		return history

	@classmethod
	def load_from_file(cls, filepath: str | Path, output_model: type[StepDecision]) -> ExecutionHistoryList:
		"""Load history from JSON file"""
		with open(filepath, encoding='utf-8') as f:
			data = json.load(f)
		return cls.load_from_dict(data, output_model)

	def last_action(self) -> None | dict:
		"""Last action in history"""
		if self.history and self.history[-1].model_output:
			return self.history[-1].model_output.action[-1].model_dump(exclude_none=True, mode='json')
		return None

	def errors(self) -> list[str | None]:
		"""Get all errors from history, with None for steps without errors"""
		errors = []
		for h in self.history:
			step_errors = [r.error for r in h.result if r.error]

			# each step can have only one error
			errors.append(step_errors[0] if step_errors else None)
		return errors

	def final_result(self) -> None | str:
		"""Final result from history"""
		if self.history and self.history[-1].result[-1].extracted_content:
			return self.history[-1].result[-1].extracted_content
		return None

	def is_done(self) -> bool:
		"""Check if the agent is done"""
		if self.history and len(self.history[-1].result) > 0:
			last_result = self.history[-1].result[-1]
			return last_result.is_done is True
		return False

	def is_successful(self) -> bool | None:
		"""Check if the agent completed successfully - the agent decides in the last step if it was successful or not. None if not done yet."""
		if self.history and len(self.history[-1].result) > 0:
			last_result = self.history[-1].result[-1]
			if last_result.is_done is True:
				return last_result.success
		return None

	def has_errors(self) -> bool:
		"""Check if the agent has any non-None errors"""
		return any(error is not None for error in self.errors())

	def judgement(self) -> dict | None:
		"""Get the judgement result as a dictionary if it exists"""
		if self.history and len(self.history[-1].result) > 0:
			last_result = self.history[-1].result[-1]
			if last_result.judgement:
				return last_result.judgement.model_dump()
		return None

	def is_judged(self) -> bool:
		"""Check if the agent trace has been judged"""
		if self.history and len(self.history[-1].result) > 0:
			last_result = self.history[-1].result[-1]
			return last_result.judgement is not None
		return False

	def is_validated(self) -> bool | None:
		"""Check if the judge validated the agent execution (verdict is True). Returns None if not judged yet."""
		if self.history and len(self.history[-1].result) > 0:
			last_result = self.history[-1].result[-1]
			if last_result.judgement:
				return last_result.judgement.verdict
		return None

	def urls(self) -> list[str | None]:
		"""Get all unique URLs from history"""
		return [h.state['url'] if isinstance(h.state, dict) and h.state.get('url') is not None else (h.state.url if h.state and h.state.url is not None else None) for h in self.history]

	def screenshot_paths(self, n_last: int | None = None, return_none_if_not_screenshot: bool = True) -> list[str | None]:
		"""Get all screenshot paths from history"""
		if n_last == 0:
			return []
		if n_last is None:
			if return_none_if_not_screenshot:
				return [h.state.screenshot_path if h.state.screenshot_path is not None else None for h in self.history]
			else:
				return [h.state.screenshot_path for h in self.history if h.state.screenshot_path is not None]
		else:
			if return_none_if_not_screenshot:
				return [h.state.screenshot_path if h.state.screenshot_path is not None else None for h in self.history[-n_last:]]
			else:
				return [h.state.screenshot_path for h in self.history[-n_last:] if h.state.screenshot_path is not None]

	def screenshots(self, n_last: int | None = None, return_none_if_not_screenshot: bool = True) -> list[str | None]:
		"""Get all screenshots from history as base64 strings"""
		if n_last == 0:
			return []

		history_items = self.history if n_last is None else self.history[-n_last:]
		screenshots = []

		for item in history_items:
			screenshot_b64 = item.state.get_screenshot()
			if screenshot_b64:
				screenshots.append(screenshot_b64)
			else:
				if return_none_if_not_screenshot:
					screenshots.append(None)
				# If return_none_if_not_screenshot is False, we skip None values

		return screenshots

	def action_names(self) -> list[str]:
		"""Get all action names from history"""
		action_names = []
		for action in self.model_actions():
			actions = list(action.keys())
			if actions:
				action_names.append(actions[0])
		return action_names

	def model_thoughts(self) -> list[AgentBrain]:
		"""Get all thoughts from history"""
		return [h.model_output.current_state for h in self.history if h.model_output]

	def model_outputs(self) -> list[StepDecision]:
		"""Get all model outputs from history"""
		return [h.model_output for h in self.history if h.model_output]

	# get all actions with params
	def model_actions(self) -> list[dict]:
		"""Get all actions from history"""
		outputs = []

		for h in self.history:
			if h.model_output:
				# Guard against None interacted_element before zipping
				interacted_elements = h.state.interacted_element or [None] * len(h.model_output.action)
				for action, interacted_element in zip(h.model_output.action, interacted_elements):
					output = action.model_dump(exclude_none=True, mode='json')
					output['interacted_element'] = interacted_element
					outputs.append(output)
		return outputs

	def action_history(self) -> list[list[dict]]:
		"""Get truncated action history with only essential fields"""
		step_outputs = []

		for h in self.history:
			step_actions = []
			if h.model_output:
				# Guard against None interacted_element before zipping
				interacted_elements = h.state.interacted_element or [None] * len(h.model_output.action)
				# Zip actions with interacted elements and results
				for action, interacted_element, result in zip(h.model_output.action, interacted_elements, h.result):
					action_output = action.model_dump(exclude_none=True, mode='json')
					action_output['interacted_element'] = interacted_element
					# Only keep long_term_memory from result
					action_output['result'] = result.long_term_memory if result and result.long_term_memory else None
					step_actions.append(action_output)
			step_outputs.append(step_actions)

		return step_outputs

	def action_results(self) -> list[ExecutionResult]:
		"""Get all results from history"""
		results = []
		for h in self.history:
			results.extend([r for r in h.result if r])
		return results

	def extracted_content(self) -> list[str]:
		"""Get all extracted content from history"""
		content = []
		for h in self.history:
			content.extend([r.extracted_content for r in h.result if r.extracted_content])
		return content

	def model_actions_filtered(self, include: list[str] | None = None) -> list[dict]:
		"""Get all model actions from history as JSON"""
		if include is None:
			include = []
		outputs = self.model_actions()
		result = []
		for o in outputs:
			for i in include:
				if i == list(o.keys())[0]:
					result.append(o)
		return result

	def number_of_steps(self) -> int:
		"""Get the number of steps in the history"""
		return len(self.history)

	def agent_steps(self) -> list[str]:
		"""Format agent history as readable step descriptions for judge evaluation."""
		steps = []

		# Iterate through history items (each is an ExecutionHistory)
		for i, h in enumerate(self.history):
			step_text = f'Step {i + 1}:\n'

			# Get actions from model_output
			if h.model_output and h.model_output.action:
				# Use model_dump with mode='json' to serialize enums properly
				actions_list = [action.model_dump(exclude_none=True, mode='json') for action in h.model_output.action]
				action_json = json.dumps(actions_list, indent=1)
				step_text += f'Actions: {action_json}\n'

			# Get results (already a list[ExecutionResult] in h.result)
			if h.result:
				for j, result in enumerate(h.result):
					if result.extracted_content:
						content = str(result.extracted_content)
						step_text += f'Result {j + 1}: {content}\n'

					if result.error:
						error = str(result.error)
						step_text += f'Error {j + 1}: {error}\n'

			steps.append(step_text)

		return steps

	@property
	def structured_output(self) -> AgentStructuredOutput | None:
		"""Get the structured output from the history

		Returns:
			The structured output if both final_result and _output_model_schema are available,
			otherwise None
		"""
		final_result = self.final_result()
		if final_result is not None and self._output_model_schema is not None:
			return self._output_model_schema.model_validate_json(final_result)

		return None


class AgentError:
	"""Container for agent error handling"""

	VALIDATION_ERROR = 'Invalid model output format. Please follow the correct schema.'
	RATE_LIMIT_ERROR = 'Rate limit reached. Waiting before retry.'
	NO_VALID_ACTION = 'No valid action found'

	@staticmethod
	def format_error(error: Exception, include_trace: bool = False) -> str:
		"""Format error message based on error type and optionally include trace"""
		message = ''
		if isinstance(error, ValidationError):
			return f'{AgentError.VALIDATION_ERROR}\nDetails: {str(error)}'
		if isinstance(error, RateLimitError):
			return AgentError.RATE_LIMIT_ERROR

		# Handle LLM response validation errors from llm_use
		error_str = str(error)
		if 'LLM response missing required fields' in error_str or 'Expected format: StepDecision' in error_str:
			# Extract the main error message without the huge stacktrace
			lines = error_str.split('\n')
			main_error = lines[0] if lines else error_str

			# Provide a clearer error message
			helpful_msg = f'{main_error}\n\nThe previous response had an invalid output structure. Please stick to the required output format. \n\n'

			if include_trace:
				helpful_msg += f'\n\nFull stacktrace:\n{traceback.format_exc()}'

			return helpful_msg

		if include_trace:
			return f'{str(error)}\nStacktrace:\n{traceback.format_exc()}'
		return f'{str(error)}'


class DetectedVariable(BaseModel):
	"""A detected variable in agent history"""

	name: str
	original_value: str
	type: str = 'string'
	format: str | None = None


class VariableMetadata(BaseModel):
	"""Metadata about detected variables in history"""

	detected_variables: dict[str, DetectedVariable] = Field(default_factory=dict)


# ========== Variable Detection Functions ==========

def detect_variables_in_history(history: ExecutionHistoryList) -> dict[str, DetectedVariable]:
	"""
	Анализировать историю агента и обнаружить переиспользуемые переменные.

	Использует две стратегии:
	1. Атрибуты элементов (id, name, type, placeholder, aria-label) - наиболее надёжно
	2. Сопоставление паттернов значений (email, phone, date форматы) - запасной вариант

	Returns:
		Словарь, сопоставляющий имена переменных объектам DetectedVariable
	"""
	import re

	detected: dict[str, DetectedVariable] = {}
	detected_values: set[str] = set()  # Отслеживаем, какие значения мы уже обнаружили

	for step_idx, history_item in enumerate(history.history):
		if not history_item.model_output:
			continue

		for action_idx, action in enumerate(history_item.model_output.action):
			# Преобразуем действие в словарь - обрабатываем и Pydantic-модели, и dict-подобные объекты
			if isinstance(action, dict):
				action_dict = action
			elif hasattr(action, 'model_dump'):
				action_dict = action.model_dump()
			else:
				# Для SimpleNamespace или подобных объектов
				action_dict = vars(action)

			# Получаем взаимодействовавший элемент для этого действия (если доступен)
			element = None
			if history_item.state and history_item.state.interacted_element:
				if len(history_item.state.interacted_element) > action_idx:
					element = history_item.state.interacted_element[action_idx]

			# Обнаруживаем переменные в этом действии
			_detect_in_action(action_dict, element, detected, detected_values)

	return detected


def _detect_in_action(
	action_dict: dict,
	element: DOMInteractedElement | None,
	detected: dict[str, DetectedVariable],
	detected_values: set[str],
) -> None:
	"""Обнаружить переменные в одном действии, используя контекст элемента"""

	# Извлекаем тип действия и параметры
	for action_type, params in action_dict.items():
		if not isinstance(params, dict):
			continue

		# Проверяем поля, которые обычно содержат переменные
		fields_to_check = ['query', 'text']

		for field in fields_to_check:
			if field not in params:
				continue

			value = params[field]
			if not isinstance(value, str) or not value.strip():
				continue

			# Пропускаем, если мы уже обнаружили это точное значение
			if value in detected_values:
				continue

			# Пытаемся обнаружить тип переменной (с контекстом элемента)
			var_info = _detect_variable_type(value, element)
			if not var_info:
				continue

			var_name, var_format = var_info

			# Обеспечиваем уникальность имени переменной
			var_name = _ensure_unique_name(var_name, detected)

			# Добавляем обнаруженную переменную
			detected[var_name] = DetectedVariable(
				format=var_format,
				name=var_name,
				original_value=value,
				type='string',
			)

			detected_values.add(value)


def _detect_variable_type(
	value: str,
	element: DOMInteractedElement | None = None,
) -> tuple[str, str | None] | None:
	"""
	Обнаружить, выглядит ли значение как переменная, используя контекст элемента, когда доступен.

	Приоритет:
	1. Атрибуты элементов (id, name, type, placeholder, aria-label) - наиболее надёжно
	2. Сопоставление паттернов значений (email, phone, date форматы) - запасной вариант

	Returns:
		(имя_переменной, формат) или None, если не обнаружено
	"""

	# СТРАТЕГИЯ 1: Использовать атрибуты элементов (наиболее надёжно)
	if element and element.attributes:
		attr_detection = _detect_from_attributes(element.attributes)
		if attr_detection:
			return attr_detection

	# СТРАТЕГИЯ 2: Сопоставление паттернов на значении (запасной вариант)
	return _detect_from_value_pattern(value)


def _detect_from_attributes(attributes: dict[str, str]) -> tuple[str, str | None] | None:
	"""
	Обнаружить переменную из атрибутов элемента.

	Проверяем атрибуты в порядке приоритета:
	1. Атрибут type (HTML5 типы input - наиболее специфично)
	2. id, name, placeholder, aria-label (семантические подсказки)
	"""

	# Сначала проверяем атрибут 'type' (HTML5 типы input)
	input_type = attributes.get('type', '').lower()
	if input_type == 'date':
		return ('date', 'date')
	elif input_type == 'email':
		return ('email', 'email')
	elif input_type == 'number':
		return ('number', 'number')
	elif input_type == 'tel':
		return ('phone', 'phone')
	elif input_type == 'url':
		return ('url', 'url')

	# Объединяем семантические атрибуты для сопоставления ключевых слов
	semantic_attrs = [
		attributes.get('aria-label', ''),
		attributes.get('id', ''),
		attributes.get('name', ''),
		attributes.get('placeholder', ''),
	]

	combined_text = ' '.join(semantic_attrs).lower()

	# Обнаружение адреса
	if any(keyword in combined_text for keyword in ['addr', 'address', 'street']):
		if 'billing' in combined_text:
			return ('billing_address', None)
		elif 'shipping' in combined_text:
			return ('shipping_address', None)
		else:
			return ('address', None)

	# Обнаружение компании
	if 'company' in combined_text or 'organization' in combined_text:
		return ('company', None)

	# Обнаружение комментария/заметки
	if any(keyword in combined_text for keyword in ['comment', 'description', 'message', 'note']):
		return ('comment', None)

	# Обнаружение страны
	if 'country' in combined_text:
		return ('country', None)

	# Обнаружение города
	if 'city' in combined_text:
		return ('city', None)

	# Обнаружение даты
	if any(keyword in combined_text for keyword in ['birth', 'date', 'dob']):
		return ('date', 'date')

	# Обнаружение email
	if 'e-mail' in combined_text or 'email' in combined_text:
		return ('email', 'email')

	# Обнаружение имени (порядок важен - проверяем специфичное перед общим)
	if 'first' in combined_text and 'name' in combined_text:
		return ('first_name', None)
	elif 'full' in combined_text and 'name' in combined_text:
		return ('full_name', None)
	elif 'last' in combined_text and 'name' in combined_text:
		return ('last_name', None)
	elif 'name' in combined_text:
		return ('name', None)

	# Обнаружение телефона
	if any(keyword in combined_text for keyword in ['cell', 'mobile', 'phone', 'tel']):
		return ('phone', 'phone')

	# Обнаружение штата/провинции
	if 'province' in combined_text or 'state' in combined_text:
		return ('state', None)

	# Обнаружение почтового индекса
	if any(keyword in combined_text for keyword in ['postal', 'postcode', 'zip']):
		return ('zip_code', 'postal_code')

	return None


def _detect_from_value_pattern(value: str) -> tuple[str, str | None] | None:
	"""
	Обнаружить тип переменной из паттерна значения (запасной вариант, когда нет контекста элемента).

	Паттерны:
	- Email: содержит @ и . с валидным форматом
	- Phone: цифры с разделителями, 10+ символов
	- Date: формат YYYY-MM-DD
	- Name: Слова с заглавной буквы, 2-30 символов, только буквы
	- Number: Чистые цифры, 1-9 символов
	"""
	import re

	# Обнаружение даты (YYYY-MM-DD или подобное)
	if re.match(r'^\d{4}-\d{2}-\d{2}$', value):
		return ('date', 'date')

	# Обнаружение email - наиболее специфично первым
	if '@' in value and '.' in value:
		# Базовая валидация email
		if re.match(r'^[\w\.-]+@[\w\.-]+\.\w+$', value):
			return ('email', 'email')

	# Обнаружение имени (с заглавной буквы, только буквы/пробелы, 2-30 символов)
	if value and value[0].isupper() and value.replace(' ', '').replace('-', '').isalpha() and 2 <= len(value) <= 30:
		words = value.split()
		if len(words) == 1:
			return ('first_name', None)
		elif len(words) == 2:
			return ('full_name', None)
		else:
			return ('name', None)

	# Обнаружение номера (чистые цифры, не длина телефона)
	if value.isdigit() and 1 <= len(value) <= 9:
		return ('number', 'number')

	# Обнаружение телефона (цифры с разделителями, 10+ символов)
	if re.match(r'^[\d\s\-\(\)\+]+$', value):
		# Удаляем разделители и проверяем длину
		digits_only = re.sub(r'[\s\-\(\)\+]', '', value)
		if len(digits_only) >= 10:
			return ('phone', 'phone')

	return None


def _ensure_unique_name(base_name: str, existing: dict[str, DetectedVariable]) -> str:
	"""
	Обеспечить уникальность имени переменной, добавляя суффикс при необходимости.

	Примеры:
		first_name → first_name
		first_name (существует) → first_name_2
		first_name_2 (существует) → first_name_3
	"""
	if base_name not in existing:
		return base_name

	# Добавляем числовой суффикс
	counter = 2
	while f'{base_name}_{counter}' in existing:
		counter += 1

	return f'{base_name}_{counter}'
