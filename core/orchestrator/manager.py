from __future__ import annotations  # Отложенное разрешение аннотаций типов

import asyncio
import gc
import inspect
import json
import logging
import re
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generic, Literal, TypeVar, cast
from urllib.parse import urlparse

if TYPE_CHECKING:
	pass

from dotenv import load_dotenv
from core.orchestrator.message_manager.models import save_conversation
from core.ai_models.models import BaseChatModel
from core.exceptions import ModelProviderError, ModelRateLimitError
from core.ai_models.messages import BaseMessage, ContentPartImageParam, ContentPartTextParam, UserMessage
from core.pricing.manager import TokenCost

load_dotenv()

from bubus import EventBus
from pydantic import BaseModel, ValidationError
from uuid_extensions import uuid7str

from core.session.profile import BrowserProfile
from core.session.session import BrowserSession
Browser = BrowserSession  # Псевдоним

# Импортируем BrowserStateSummary ДО использования в аннотациях типов
from core.session.models import BrowserStateSummary

# Judge опционален - нужен только если включена оценка judge
try:
	from core.orchestrator.judge import construct_judge_messages
except ImportError:
	def construct_judge_messages(*args, **kwargs):
		raise NotImplementedError('Функциональность Judge недоступна')

# Ленивый импорт для gif выполняется внутри метода при необходимости
from core.orchestrator.message_manager.manager import (
	MessageManager,
)
from core.orchestrator.prompts import SystemPrompt
from core.orchestrator.models import (
	ActionResult,
	AgentError,
	AgentHistory,
	AgentHistoryList,
	AgentOutput,
	AgentSettings,
	AgentState,
	AgentStepInfo,
	AgentStructuredOutput,
	BrowserStateHistory,
	DetectedVariable,
	JudgementResult,
	StepMetadata,
)
from core.session.session import DEFAULT_BROWSER_PROFILE
from core.config import CONFIG
from core.dom_processing.models import DOMInteractedElement
from core.observability import observe, observe_debug
from core.actions.registry.models import ActionModel
from core.actions.manager import Tools
from core.specialists.email_subagent import EmailSubAgent
from core.helpers import (
	URL_PATTERN,
	_log_pretty_path,
	check_latest_agent_version,
	get_agent_version,
	time_execution_async,
	time_execution_sync,
)
from core.orchestrator.agent import URLParser, HistoryManager, FileManager, DemoModeManager

logger = logging.getLogger(__name__)


def log_response(response: AgentOutput, registry=None, logger=None) -> None:
	"""Утилита для логирования ответа модели."""

	# Используем модульный логгер, если не предоставлен
	if logger is None:
		logger = logging.getLogger(__name__)

	# Логируем рассуждения только если они присутствуют
	if response.current_state.thinking:
		logger.debug(f'💡 Рассуждения:\n{response.current_state.thinking}')

	# Логируем оценку только если она не пустая
	eval_goal = response.current_state.evaluation_previous_goal
	if eval_goal:
		if 'success' in eval_goal.lower() or 'успех' in eval_goal.lower():
			emoji = '👍'
			# Зеленый цвет для успеха
			logger.info(f'  \033[32m{emoji} Оценка: {eval_goal}\033[0m')
		elif 'failure' in eval_goal.lower() or 'неудача' in eval_goal.lower():
			emoji = '⚠️'
			# Красный цвет для неудачи
			logger.info(f'  \033[31m{emoji} Оценка: {eval_goal}\033[0m')
		else:
			emoji = '❔'
			# Без цвета для неизвестного/нейтрального
			logger.info(f'  {emoji} Оценка: {eval_goal}')

	# Всегда логируем память, если она присутствует
	if response.current_state.memory:
		logger.info(f'  🧠 Память: {response.current_state.memory}')

	# Логируем следующую цель только если она не пустая
	next_goal = response.current_state.next_goal
	if next_goal:
		# Синий цвет для следующей цели
		logger.info(f'  \033[34m🎯 Следующая цель: {next_goal}\033[0m')


Context = TypeVar('Context')


AgentHookFunc = Callable[['Agent'], Awaitable[None]]


class Agent(Generic[Context, AgentStructuredOutput]):
	@time_execution_sync('--init')
	def __init__(
		self,
		task: str,
		llm: BaseChatModel | None = None,
		# Необязательные параметры
		browser_profile: BrowserProfile | None = None,
		browser_session: BrowserSession | None = None,
		browser: Browser | None = None,  # Псевдоним для browser_session
		tools: Tools[Context] | None = None,
		controller: Tools[Context] | None = None,  # Псевдоним для tools
		user_input_callback: Callable[[str], str] | None = None,  # Callback для запроса ввода от пользователя (для капчи)
		# Параметры начального запуска агента
		sensitive_data: dict[str, str | dict[str, str]] | None = None,
		initial_actions: list[dict[str, dict[str, Any]]] | None = None,
		# Облачные колбэки
		register_new_step_callback: (
			Callable[['BrowserStateSummary', 'AgentOutput', int], None]  # Sync callback
			| Callable[['BrowserStateSummary', 'AgentOutput', int], Awaitable[None]]  # Async callback
			| None
		) = None,
		register_done_callback: (
			Callable[['AgentHistoryList'], Awaitable[None]]  # Async Callback
			| Callable[['AgentHistoryList'], None]  # Sync Callback
			| None
		) = None,
		register_external_agent_status_raise_error_callback: Callable[[], Awaitable[bool]] | None = None,
		register_should_stop_callback: Callable[[], Awaitable[bool]] | None = None,
		# Настройки агента
		output_model_schema: type[AgentStructuredOutput] | None = None,
		use_vision: bool | Literal['auto'] = True,
		save_conversation_path: str | Path | None = None,
		save_conversation_path_encoding: str | None = 'utf-8',
		max_failures: int = 3,
		override_system_message: str | None = None,
		extend_system_message: str | None = None,
		generate_gif: bool | str = False,
		available_file_paths: list[str] | None = None,
		include_attributes: list[str] | None = None,
		max_actions_per_step: int = 3,
		use_thinking: bool = True,
		flash_mode: bool = False,
		demo_mode: bool | None = None,
		max_history_items: int | None = None,
		page_extraction_llm: BaseChatModel | None = None,
		fallback_llm: BaseChatModel | None = None,
		ground_truth: str | None = None,
		use_judge: bool = False,
		injected_agent_state: AgentState | None = None,
		source: str | None = None,
		file_system_path: str | None = None,
		task_id: str | None = None,
		calculate_cost: bool = False,
		display_files_in_done_text: bool = True,
		include_tool_call_examples: bool = False,
		vision_detail_level: Literal['auto', 'low', 'high'] = 'auto',
		llm_timeout: int | None = None,
		step_timeout: int = 120,
		directly_open_url: bool = True,
		include_recent_events: bool = False,
		sample_images: list[ContentPartTextParam | ContentPartImageParam] | None = None,
		final_response_after_failure: bool = True,
		llm_screenshot_size: tuple[int, int] | None = None,
		_url_shortening_limit: int = 25,
		**kwargs,
	):
		# Проверка llm_screenshot_size
		if llm_screenshot_size is not None:
			if not isinstance(llm_screenshot_size, tuple) or len(llm_screenshot_size) != 2:
				raise ValueError('llm_screenshot_size must be a tuple of (width, height)')
			width, height = llm_screenshot_size
			if not isinstance(width, int) or not isinstance(height, int):
				raise ValueError('llm_screenshot_size dimensions must be integers')
			if width < 100 or height < 100:
				raise ValueError('llm_screenshot_size dimensions must be at least 100 pixels')
			logger.info(f'🖼️  Изменение размера скриншотов для LLM включено: {width}x{height}')
		if llm is None:
			default_llm_name = CONFIG.DEFAULT_LLM
			if default_llm_name:
				from core.ai_models.models import get_llm_by_name

				llm = get_llm_by_name(default_llm_name)
			else:
				# LLM по умолчанию не указан, используем оригинальный по умолчанию
				# Требуется явный llm через настройки среды / конструктор
				raise ValueError('LLM не указан и значение по умолчанию не настроено. Передайте llm явно.')

		# Автоматическая настройка llm_screenshot_size для моделей Claude Sonnet
		if llm_screenshot_size is None:
			model_name = getattr(llm, 'model', '')
			if isinstance(model_name, str) and model_name.startswith('claude-sonnet'):
				llm_screenshot_size = (1400, 850)
				logger.info('🖼️  Автоматически настроен размер скриншотов для LLM (Claude Sonnet): 1400x850')

		if page_extraction_llm is None:
			page_extraction_llm = llm
		if available_file_paths is None:
			available_file_paths = []

		# Установка таймаута на основе имени модели если не указан явно
		if llm_timeout is None:

			def _get_model_timeout(llm_model: BaseChatModel) -> int:
				"""Определение таймаута на основе имени модели"""
				model_name = getattr(llm_model, 'model', '').lower()
				if 'gemini' in model_name:
					if '3-pro' in model_name:
						return 90
					return 45
				elif 'groq' in model_name:
					return 30
				elif 'o3' in model_name or 'claude' in model_name or 'sonnet' in model_name or 'deepseek' in model_name:
					return 90
				else:
					return 60  # Таймаут по умолчанию

			llm_timeout = _get_model_timeout(llm)

		self.id = task_id or uuid7str()
		self.task_id: str = self.id
		self.session_id: str = uuid7str()
		
		# Инициализация атрибутов, которые могут использоваться до полной инициализации
		self.file_system = None
		self.file_system_path = None

		base_profile = browser_profile or DEFAULT_BROWSER_PROFILE
		if base_profile is DEFAULT_BROWSER_PROFILE:
			base_profile = base_profile.model_copy()
		if demo_mode is not None and base_profile.demo_mode != demo_mode:
			base_profile = base_profile.model_copy(update={'demo_mode': demo_mode})
		browser_profile = base_profile

		# Обработка параметров browser vs browser_session (browser имеет приоритет)
		if browser and browser_session:
			raise ValueError('Cannot specify both "browser" and "browser_session" parameters. Use "browser" for the cleaner API.')
		browser_session = browser or browser_session

		if browser_session is not None and demo_mode is not None and browser_session.browser_profile.demo_mode != demo_mode:
			browser_session.browser_profile = browser_session.browser_profile.model_copy(update={'demo_mode': demo_mode})

		self.browser_session = browser_session or BrowserSession(
			browser_profile=browser_profile,
			id=uuid7str()[:-4] + self.id[-4:],  # Повторное использование последних 4 символов для группировки в логах
		)

		# Инициализация _demo_mode_enabled после browser_session (используем browser_session напрямую для безопасности)
		self._demo_mode_enabled: bool = bool(self.browser_session.browser_profile.demo_mode) if self.browser_session else False
		if self._demo_mode_enabled and getattr(self.browser_session.browser_profile, 'headless', False):
			# Используем logger через browser_session для безопасности
			self.browser_session.logger.warning(
				'Demo mode is enabled but the browser is headless=True; set headless=False to view the in-browser panel.'
			)

		# Инициализация доступных путей к файлам как прямого атрибута
		self.available_file_paths = available_file_paths

		# Настройка инструментов сначала (нужно для определения output_model_schema)
		if tools is not None:
			self.tools = tools
		elif controller is not None:
			self.tools = controller
		else:
			# Исключить инструмент screenshot когда use_vision не auto
			exclude_actions = ['screenshot'] if use_vision != 'auto' else []
			self.tools = Tools(
				exclude_actions=exclude_actions,
				display_files_in_done_text=display_files_in_done_text,
				user_input_callback=user_input_callback
			)

		# Принудительное исключение screenshot когда use_vision != 'auto', даже если пользователь передал кастомные инструменты
		if use_vision != 'auto':
			self.tools.exclude_action('screenshot')

		# Структурированный вывод - использовать явный параметр или определить из инструментов
		tools_output_model = self.tools.get_output_model()
		if output_model_schema is not None and tools_output_model is not None:
			# Оба предоставлены - предупреждение если они различаются
			if output_model_schema is not tools_output_model:
				logger.warning(
					f'output_model_schema ({output_model_schema.__name__}) отличается от Tools output_model '
					f'({tools_output_model.__name__}). Используется Agent output_model_schema.'
				)
		elif output_model_schema is None and tools_output_model is not None:
			# Только tools имеет его - использовать его (приведение безопасно: оба являются подклассами BaseModel)
			output_model_schema = cast(type[AgentStructuredOutput], tools_output_model)
		self.output_model_schema = output_model_schema
		if self.output_model_schema is not None:
			self.tools.use_structured_output_action(self.output_model_schema)

		# Основные компоненты - улучшение задачи теперь имеет доступ к output_model_schema из инструментов
		self.task = self._enhance_task_with_schema(task, output_model_schema)
		self.llm = llm

		# Конфигурация резервного LLM
		self._fallback_llm: BaseChatModel | None = fallback_llm
		self._using_fallback_llm: bool = False
		self._original_llm: BaseChatModel = llm  # Сохранение оригинала для справки
		self.directly_open_url = directly_open_url
		self.include_recent_events = include_recent_events
		self._url_shortening_limit = _url_shortening_limit

		self.sensitive_data = sensitive_data

		self.sample_images = sample_images

		self.settings = AgentSettings(
			use_vision=use_vision,
			vision_detail_level=vision_detail_level,
			save_conversation_path=save_conversation_path,
			save_conversation_path_encoding=save_conversation_path_encoding,
			max_failures=max_failures,
			override_system_message=override_system_message,
			extend_system_message=extend_system_message,
			generate_gif=generate_gif,
			include_attributes=include_attributes,
			max_actions_per_step=max_actions_per_step,
			use_thinking=use_thinking,
			flash_mode=flash_mode,
			max_history_items=max_history_items,
			page_extraction_llm=page_extraction_llm,
			calculate_cost=calculate_cost,
			include_tool_call_examples=include_tool_call_examples,
			llm_timeout=llm_timeout,
			step_timeout=step_timeout,
			final_response_after_failure=final_response_after_failure,
			use_judge=False,
			ground_truth=None,
		)

		# Token cost service (учёт токенов, без judge_llm)
		self.token_cost_service = TokenCost(include_cost=calculate_cost)
		self.token_cost_service.register_llm(llm)
		self.token_cost_service.register_llm(page_extraction_llm)

		# Инициализация состояния
		self.state = injected_agent_state or AgentState()

		# Инициализация истории
		self.history = AgentHistoryList(history=[], usage=None)

		# Инициализация директории агента
		import time

		timestamp = int(time.time())
		base_tmp = Path(tempfile.gettempdir())
		self.agent_directory = base_tmp / f'agent_agent_{self.id}_{timestamp}'

		# Компоненты Agent для модульной архитектуры (инициализируем ДО использования)
		self._url_parser = URLParser(self)
		self._history_manager_component = HistoryManager(self)
		self._file_manager = FileManager(self)
		self._demo_mode_manager = DemoModeManager(self)

		# Initialize file system and screenshot service
		self._file_manager.set_file_system(file_system_path)
		self._file_manager.set_screenshot_service()

		# Инициализация sub-агентов для специализированных задач
		self.email_subagent = EmailSubAgent()

		# Настройка действий
		self._setup_action_models()
		self._set_agent_version_and_source(source)

		initial_url = None

		# Автоматическое извлечение URL из задачи (если включено)
		if self.directly_open_url and not self.state.follow_up_task and not initial_actions:
			initial_url = self._url_parser.extract_start_url(self.task)
			if initial_url:
				self.logger.info(f'🔗 Найден URL в задаче: {initial_url}, добавляю как начальное действие...')
				initial_actions = [{'navigate': {'url': initial_url, 'new_tab': False}}]

		self.initial_url = initial_url

		self.initial_actions = self._convert_initial_actions(initial_actions) if initial_actions else None
		# Проверка возможности подключения к модели
		self._verify_and_setup_llm()

		# Обработка попыток использовать use_vision=True с моделями DeepSeek
		if 'deepseek' in self.llm.model.lower():
			self.logger.warning('⚠️ Модели DeepSeek пока не поддерживают use_vision=True. Устанавливаю use_vision=False...')
			self.settings.use_vision = False

		# Обработка попыток использовать use_vision=True с моделями XAI
		if 'grok' in self.llm.model.lower():
			self.logger.warning('⚠️ Модели XAI пока не поддерживают use_vision=True. Устанавливаю use_vision=False...')
			self.settings.use_vision = False

		logger.debug(
			f'{" +vision" if self.settings.use_vision else ""}'
			f' extraction_model={self.settings.page_extraction_llm.model if self.settings.page_extraction_llm else "Unknown"}'
			f'{" +file_system" if getattr(self, "file_system", None) else ""}'
		)

		# Сохраняем llm_screenshot_size в browser_session, чтобы инструменты могли к нему обращаться
		self.browser_session.llm_screenshot_size = llm_screenshot_size

		# Проверка является ли LLM экземпляром ChatAnthropic
		from core.ai_models.anthropic.chat import ChatAnthropic

		is_anthropic = isinstance(self.llm, ChatAnthropic)

		# Инициализация менеджера сообщений с состоянием
		# Начальный системный промпт со всеми действиями - будет обновляться на каждом шаге
		self._message_manager = MessageManager(
			task=self.task,
			system_message=SystemPrompt(
				max_actions_per_step=self.settings.max_actions_per_step,
				override_system_message=override_system_message,
				extend_system_message=extend_system_message,
				use_thinking=self.settings.use_thinking,
				flash_mode=self.settings.flash_mode,
				is_anthropic=is_anthropic,
			).get_system_message(),
			file_system=self.file_system,
			state=self.state.message_manager_state,
			use_thinking=self.settings.use_thinking,
			# Настройки для MessageManager
			include_attributes=self.settings.include_attributes,
			sensitive_data=sensitive_data,
			max_history_items=self.settings.max_history_items,
			vision_detail_level=self.settings.vision_detail_level,
			include_tool_call_examples=self.settings.include_tool_call_examples,
			include_recent_events=self.include_recent_events,
			sample_images=self.sample_images,
			llm_screenshot_size=llm_screenshot_size,
		)

		if self.sensitive_data:
			# Проверка наличия доменно-специфичных учетных данных в sensitive_data
			has_domain_specific_credentials = any(isinstance(v, dict) for v in self.sensitive_data.values())

			# Если не настроены allowed_domains, показать предупреждение безопасности
			if not self.browser_profile.allowed_domains:
				self.logger.warning(
					'⚠️ Agent(sensitive_data=••••••••) was provided but Browser(allowed_domains=[...]) is not locked down! ⚠️\n'
					'          ☠️ If the agent visits a malicious website and encounters a prompt-injection attack, your sensitive_data may be exposed!\n\n'
					'   \n'
				)

			# Если используем доменно-специфичные учетные данные, валидируем паттерны доменов
			elif has_domain_specific_credentials:
				# Для доменно-специфичного формата убеждаемся что все паттерны доменов включены в allowed_domains
				domain_patterns = [k for k, v in self.sensitive_data.items() if isinstance(v, dict)]

				# Валидация каждого паттерна домена против allowed_domains
				for domain_pattern in domain_patterns:
					is_allowed = False
					for allowed_domain in self.browser_profile.allowed_domains:
						# Специальные случаи, которые не требуют сопоставления URL
						if domain_pattern == allowed_domain or allowed_domain == '*':
							is_allowed = True
							break

						# Нужно создать примеры URL для сравнения паттернов
						# Извлечение частей домена, игнорируя схему
						pattern_domain = domain_pattern.split('://')[-1] if '://' in domain_pattern else domain_pattern
						allowed_domain_part = allowed_domain.split('://')[-1] if '://' in allowed_domain else allowed_domain

						# Проверка покрыт ли паттерн разрешенным доменом
						# Пример: "google.com" покрывается "*.google.com"
						if pattern_domain == allowed_domain_part or (
							allowed_domain_part.startswith('*.')
							and (
								pattern_domain == allowed_domain_part[2:]
								or pattern_domain.endswith('.' + allowed_domain_part[2:])
							)
						):
							is_allowed = True
							break

					if not is_allowed:
						self.logger.warning(
							f'⚠️ Domain pattern "{domain_pattern}" in sensitive_data is not covered by any pattern in allowed_domains={self.browser_profile.allowed_domains}\n'
							f'   This may be a security risk as credentials could be used on unintended domains.'
						)

		# Колбэки
		self.register_new_step_callback = register_new_step_callback
		self.register_done_callback = register_done_callback
		self.register_should_stop_callback = register_should_stop_callback
		self.register_external_agent_status_raise_error_callback = register_external_agent_status_raise_error_callback

		# Event bus для внутренних событий агента
		self.eventbus = EventBus(name=f'Agent_{str(self.id)[-4:]}')

		if self.settings.save_conversation_path:
			self.settings.save_conversation_path = Path(self.settings.save_conversation_path).expanduser().resolve()
			self.logger.info(f'💬 Сохраняю разговор в {_log_pretty_path(self.settings.save_conversation_path)}')

		# Инициализация отслеживания загрузок (инициализируем ДО использования)
		assert self.browser_session is not None, 'BrowserSession is not set up'
		self.has_downloads_path = self.browser_session.browser_profile.downloads_path is not None
		self._last_known_downloads: list[str] = []  # Инициализируем всегда, даже если downloads_path не установлен
		if self.has_downloads_path:
			self.logger.debug('📁 Инициализирован отслеживание загрузок для агента')

		# Управление паузой на основе событий (вынесено из AgentState для сериализации)
		self._external_pause_event = asyncio.Event()
		self._external_pause_event.set()

		# Инициализация менеджеров для разделения ответственности
		from core.orchestrator.orchestration.step_manager import StepManager
		from core.orchestrator.llm.llm_manager import LLMManager
		from core.orchestrator.execution.run_manager import RunManager
		from core.orchestrator.execution.action_execution_manager import ActionExecutionManager
		from core.orchestrator.rerun.rerun_manager import RerunManager

		self._step_manager = StepManager(self)
		self._llm_manager = LLMManager(self)
		self._run_manager = RunManager(self)
		self._action_execution = ActionExecutionManager(self)
		self._rerun_manager = RerunManager(self)
		
		# Псевдонимы для обратной совместимости
		self._step_orchestrator = self._step_manager
		self._history_manager = self._step_manager
		self._logging_manager = self._run_manager

	def _enhance_task_with_schema(self, task: str, output_model_schema: type[AgentStructuredOutput] | None) -> str:
		"""Дополняет описание задачи информацией о схеме вывода, если она предоставлена."""
		if output_model_schema is None:
			return task

		try:
			import json

			schema = output_model_schema.model_json_schema()
			schema_json = json.dumps(schema, indent=2)

			enhancement = f'\nExpected output format: {output_model_schema.__name__}\n{schema_json}'
			return task + enhancement
		except Exception as e:
			self.logger.debug(f'Не удалось распарсить схему вывода: {e}')

		return task

	@property
	def logger(self) -> logging.Logger:
		"""Get instance-specific logger with task ID in the name"""
		# logger может быть вызван в __init__, поэтому не предполагаем что атрибуты self.* инициализированы
		_task_id = task_id[-4:] if (task_id := getattr(self, 'task_id', None)) else '----'
		_browser_session_id = browser_session.id[-4:] if (browser_session := getattr(self, 'browser_session', None)) else '----'
		_current_target_id = (
			browser_session.agent_focus_target_id[-2:]
			if (browser_session := getattr(self, 'browser_session', None)) and browser_session.agent_focus_target_id
			else '--'
		)
		return logging.getLogger(f'core.Agent🅰 {_task_id} ⇢ 🅑 {_browser_session_id} 🅣 {_current_target_id}')

	@property
	def browser_profile(self) -> BrowserProfile:
		assert self.browser_session is not None, 'BrowserSession is not set up'
		return self.browser_session.browser_profile

	@property
	def is_using_fallback_llm(self) -> bool:
		"""Check if the agent is currently using the fallback LLM."""
		return self._using_fallback_llm

	@property
	def current_llm_model(self) -> str:
		"""Get the model name of the currently active LLM."""
		return self.llm.model if hasattr(self.llm, 'model') else 'unknown'

	async def _check_and_update_downloads(self, context: str = '') -> None:
		"""Check for new downloads and update available file paths. Delegates to FileManager."""
		await self._file_manager.check_and_update_downloads(context)

	def _update_available_file_paths(self, downloads: list[str]) -> None:
		"""Update available_file_paths with downloaded files. Delegates to FileManager."""
		self._file_manager.update_available_file_paths(downloads)

	def _set_file_system(self, file_system_path: str | None = None) -> None:
		"""Управление файловой системой. Delegates to FileManager."""
		self._file_manager.set_file_system(file_system_path)

	def _set_screenshot_service(self) -> None:
		"""Initialize screenshot service using agent directory. Delegates to FileManager."""
		self._file_manager.set_screenshot_service()

	def save_file_system_state(self) -> None:
		"""Сохранение состояния файловой системы. Delegates to FileManager."""
		self._file_manager.save_file_system_state()

	def _set_agent_version_and_source(self, source_override: str | None = None) -> None:
		"""Получить версию и источник сборки агента из pyproject.toml."""
		# Использование вспомогательной функции для определения версии
		version = get_agent_version()

		# Определение источника
		try:
			package_root = Path(__file__).parent.parent.parent
			repo_files = ['.git', 'README.md', 'docs', 'examples']
			if all(Path(package_root / file).exists() for file in repo_files):
				source = 'git'
			else:
				source = 'pip'
		except Exception as e:
			self.logger.debug(f'Error determining source: {e}')
			source = 'unknown'

		if source_override is not None:
			source = source_override
		self.version = version
		self.source = source

	def _setup_action_models(self) -> None:
		"""Setup dynamic action models from tools registry"""
		# Изначально включать только действия без фильтров
		self.ActionModel = self.tools.registry.create_action_model()
		# Создание выходной модели с динамическими действиями
		if self.settings.flash_mode:
			self.AgentOutput = AgentOutput.type_with_custom_actions_flash_mode(self.ActionModel)
		elif self.settings.use_thinking:
			self.AgentOutput = AgentOutput.type_with_custom_actions(self.ActionModel)
		else:
			self.AgentOutput = AgentOutput.type_with_custom_actions_no_thinking(self.ActionModel)

		# используется для принудительного выполнения действия done когда достигнут max_steps
		self.DoneActionModel = self.tools.registry.create_action_model(include_actions=['done'])
		if self.settings.flash_mode:
			self.DoneAgentOutput = AgentOutput.type_with_custom_actions_flash_mode(self.DoneActionModel)
		elif self.settings.use_thinking:
			self.DoneAgentOutput = AgentOutput.type_with_custom_actions(self.DoneActionModel)
		else:
			self.DoneAgentOutput = AgentOutput.type_with_custom_actions_no_thinking(self.DoneActionModel)

	async def _register_skills_as_actions(self) -> None:
		"""Управление навыками агента."""
		return

	async def _get_unavailable_skills_info(self) -> str:
		"""Возвращает информацию о недоступных навыках."""
		return ''

	def add_new_task(self, new_task: str) -> None:
		"""Add a new task to the agent, keeping the same task_id as tasks are continuous"""
		# Просто делегировать менеджеру сообщений - не нужен новый task_id или события
		# Задача продолжается с новыми инструкциями, она не заканчивается и не начинается новая
		self.task = new_task
		self._message_manager.add_new_task(new_task)
		# Пометить как follow-up задачу и пересоздать eventbus (закрывается после каждого запуска)
		self.state.follow_up_task = True
		# Сброс флагов управления чтобы агент мог продолжить
		self.state.stopped = False
		self.state.paused = False
		agent_id_suffix = str(self.id)[-4:].replace('-', '_')
		if agent_id_suffix and agent_id_suffix[0].isdigit():
			agent_id_suffix = 'a' + agent_id_suffix
		self.eventbus = EventBus(name=f'Agent_{agent_id_suffix}')

	async def _check_stop_or_pause(self) -> None:
		"""Check if the agent should stop or pause, and handle accordingly."""

		# Проверка нового should_stop_callback - устанавливает остановленное состояние чисто без исключений
		if self.register_should_stop_callback:
			if await self.register_should_stop_callback():
				self.logger.info('Внешний callback запросил остановку')
				self.state.stopped = True
				raise InterruptedError

		if self.register_external_agent_status_raise_error_callback:
			if await self.register_external_agent_status_raise_error_callback():
				raise InterruptedError

		if self.state.stopped:
			raise InterruptedError

		if self.state.paused:
			raise InterruptedError

	@observe(name='core.step', ignore_output=True, ignore_input=True)
	@time_execution_async('--step')
	async def step(self, step_info: AgentStepInfo | None = None) -> None:
		"""Execute one step of the task"""
		# Инициализация тайминга перед любыми исключениями
		self.step_start_time = time.time()

		page_state = None

		# Сбор контекста страницы с отдельной обработкой ошибок
		try:
			page_state = await self._build_step_context(step_info)
		except Exception as context_error:
			await self._handle_step_error(context_error)
			await self._finalize(page_state)
			return

		# Получение решения от LLM с отдельной обработкой ошибок
		try:
			await self._obtain_llm_decision(page_state)
		except Exception as decision_error:
			await self._handle_step_error(decision_error)
			await self._finalize(page_state)
			return

		# Выполнение действий с отдельной обработкой ошибок
		try:
			await self._apply_agent_actions()
		except Exception as action_error:
			await self._handle_step_error(action_error)
			await self._finalize(page_state)
			return

		# Завершение обработки шага с отдельной обработкой ошибок
		try:
			await self._finalize_step_processing()
		except Exception as finalize_error:
			await self._handle_step_error(finalize_error)

		# Финальная обработка всегда выполняется
		await self._finalize(page_state)

	async def _build_step_context(self, step_info: AgentStepInfo | None = None) -> BrowserStateSummary:
		"""Собирает контекст для шага: состояние браузера, модели действий, действия страницы. Delegates to StepOrchestrator."""
		return await self._step_orchestrator.build_step_context(step_info)

	async def _fetch_and_log_page_state(self) -> BrowserStateSummary:
		"""Получает состояние страницы и логирует базовую информацию. Delegates to StepOrchestrator."""
		return await self._step_orchestrator.fetch_and_log_page_state()

	def _log_page_basic_info(self, page_state: BrowserStateSummary) -> None:
		"""Логирует базовую информацию о странице. Delegates to StepOrchestrator."""
		self._step_orchestrator.log_page_basic_info(page_state)

	async def _analyze_page_elements(self, page_state: BrowserStateSummary) -> None:
		"""Анализирует элементы страницы и логирует информацию о них. Delegates to StepOrchestrator."""
		await self._step_orchestrator.analyze_page_elements(page_state)

	def _log_elements_preview(self, selector_map: dict) -> None:
		"""Логирует превью первых элементов страницы. Delegates to StepOrchestrator."""
		self._step_orchestrator.log_elements_preview(selector_map)

	def _extract_element_text(self, element) -> str:
		"""Извлекает текст из элемента различными способами. Delegates to StepOrchestrator."""
		return self._step_orchestrator.extract_element_text(element)

	def _extract_element_role(self, element) -> str:
		"""Извлекает роль элемента. Delegates to StepOrchestrator."""
		return self._step_orchestrator.extract_element_role(element)

	async def _handle_email_client_context(self, page_state: BrowserStateSummary) -> None:
		"""Обрабатывает специальный контекст для почтовых клиентов. Delegates to StepManager."""
		await self._step_manager.handle_email_client_context(page_state)

	def _log_email_metadata(self, email_metadata: dict) -> None:
		"""Логирует метаданные письма. Delegates to StepManager."""
		self._step_manager.log_email_metadata(email_metadata)

	async def _prepare_actions_and_messages(self, page_state: BrowserStateSummary, step_info: AgentStepInfo | None) -> None:
		"""Подготавливает действия и сообщения для LLM. Delegates to StepManager."""
		await self._step_manager.prepare_actions_and_messages(page_state, step_info)

	async def _update_page_action_models(self, page_url: str) -> None:
		"""Обновляет модели действий для текущей страницы. Delegates to StepManager."""
		await self._step_manager.update_page_action_models(page_url)

	async def _check_forced_completion(self, step_info: AgentStepInfo | None) -> None:
		"""Проверяет условия принудительного завершения. Delegates to StepManager."""
		await self._step_manager.check_forced_completion(step_info)

	async def _create_state_messages(self, page_state: BrowserStateSummary, step_info: AgentStepInfo | None, page_filtered_actions: str | None) -> None:
		"""Создает сообщения состояния для LLM. Delegates to StepManager."""
		await self._step_manager.create_state_messages(page_state, step_info, page_filtered_actions)

	@observe_debug(ignore_input=True, name='obtain_llm_decision')
	async def _obtain_llm_decision(self, page_state: BrowserStateSummary) -> None:
		"""Получает решение от LLM с логикой повторных попыток и обработкой колбэков. Delegates to LLMManager."""
		await self._llm_manager.obtain_llm_decision(page_state)

	async def _prepare_llm_messages(self) -> list[BaseMessage]:
		"""Подготавливает сообщения для отправки в LLM. Delegates to LLMManager."""
		return await self._llm_manager.prepare_llm_messages()

	async def _call_llm_with_timeout(self, context_messages: list[BaseMessage]) -> AgentOutput:
		"""Вызывает LLM с обработкой таймаутов. Delegates to LLMManager."""
		return await self._llm_manager.call_llm_with_timeout(context_messages)

	async def _log_llm_timeout(self, context_messages: list[BaseMessage]) -> None:
		"""Логирует таймаут вызова LLM. Delegates to LLMManager."""
		await self._llm_manager.log_llm_timeout(context_messages)

	def _store_llm_response(self, llm_response: AgentOutput) -> None:
		"""Сохраняет ответ LLM в состоянии агента. Delegates to LLMManager."""
		self._llm_manager.store_llm_response(llm_response)

	async def _process_llm_response_callbacks(self, page_state: BrowserStateSummary, context_messages: list[BaseMessage]) -> None:
		"""Обрабатывает колбэки и сохраняет разговор после получения ответа LLM. Delegates to StepManager."""
		await self._step_manager.handle_post_llm_processing(page_state, context_messages)

	async def _verify_agent_continuation(self) -> None:
		"""Проверяет, может ли агент продолжить выполнение"""
		# Проверка на остановку после получения вывода модели
		await self._check_stop_or_pause()
		# Дополнительная проверка перед коммитом в историю
		await self._check_stop_or_pause()

	async def _apply_agent_actions(self) -> None:
		"""Применяет действия из вывода модели. Delegates to StepOrchestrator."""
		await self._step_manager.apply_agent_actions()

	def _has_model_output(self) -> bool:
		"""Проверяет наличие вывода модели. Delegates to StepOrchestrator."""
		return self._step_orchestrator.has_model_output()

	def _extract_actions_from_output(self) -> list:
		"""Извлекает действия из вывода модели. Delegates to StepOrchestrator."""
		return self._step_orchestrator.extract_actions_from_output()

	async def _finalize_step_processing(self) -> None:
		"""Завершает обработку шага: отслеживание загрузок и логирование результатов. Delegates to StepOrchestrator."""
		await self._step_manager.finalize_step_processing()

	async def _handle_step_error(self, error: Exception) -> None:
		"""Обработка всех типов ошибок, которые могут возникнуть во время шага"""
		import traceback

		# Специальная обработка прерываний
		if isinstance(error, InterruptedError):
			interrupt_msg = 'Агент был прерван во время выполнения шага'
			if str(error):
				interrupt_msg += f' - {str(error)}'
			# Это не ошибка, а нормальная часть выполнения при прерывании пользователем
			self.logger.warning(interrupt_msg)
			return

		# Логируем полный трейс для отладки
		self.logger.debug(f'🔍 Полный трейс ошибки:\n{traceback.format_exc()}')

		# Форматирование сообщения об ошибке
		include_trace = self.logger.isEnabledFor(logging.DEBUG)
		error_msg = AgentError.format_error(error, include_trace=include_trace)
		
		# Вычисление максимального количества неудач
		max_total_failures = self.settings.max_failures + int(self.settings.final_response_after_failure)
		failure_count = self.state.consecutive_failures + 1
		prefix = f'❌ Результат не удался {failure_count}/{max_total_failures} раз: '
		
		# Увеличение счетчика последовательных неудач
		self.state.consecutive_failures += 1

		# Определение уровня логирования в зависимости от критичности
		is_final_failure = self.state.consecutive_failures >= max_total_failures
		log_level = logging.ERROR if is_final_failure else logging.WARNING

		# Специальная обработка ошибок парсинга JSON
		parsing_errors = ['Could not parse response', 'tool_use_failed', 'Failed to parse JSON']
		is_parsing_error = any(err in error_msg for err in parsing_errors)
		
		if is_parsing_error:
			# Обрезаем сообщение об ошибке для парсинга
			short_error = error_msg[:300] + '...' if len(error_msg) > 300 else error_msg
			self.logger.debug(f'Модель {self.llm.model} не смогла распарсить ответ: {short_error}')
			# Показываем ошибку только при финальной неудаче
			if is_final_failure:
				self.logger.log(log_level, f'{prefix}{short_error}')
		else:
			# Обычная обработка ошибок
			self.logger.log(log_level, f'{prefix}{error_msg}')

		# Логирование в demo mode
		await self._demo_mode_log(f'Ошибка шага: {error_msg}', 'error', {'step': self.state.n_steps})
		
		# Сохранение результата с ошибкой
		self.state.last_result = [ActionResult(error=error_msg)]
		return None

	async def _finalize(self, page_state: BrowserStateSummary | None) -> None:
		"""Завершает шаг с историей, логированием и событиями. Delegates to HistoryManager."""
		await self._history_manager.finalize(page_state, self.step_start_time)

	async def _force_done_after_last_step(self, step_info: AgentStepInfo | None = None) -> None:
		"""Handle special processing for the last step"""
		if step_info and step_info.is_last_step():
			# Добавление предупреждения о последнем шаге при необходимости
			msg = 'You reached max_steps - this is your last step. Your only tool available is the "done" tool. No other tool is available. All other tools which you see in history or examples are not available.'
			msg += '\nIf the task is not yet fully finished as requested by the user, set success in "done" to false! E.g. if not all steps are fully completed. Else success to true.'
			msg += '\nInclude everything you found out for the ultimate task in the done text.'
			self.logger.debug('Last step finishing up')
			self._message_manager._add_context_message(UserMessage(content=msg))
			self.AgentOutput = self.DoneAgentOutput

	async def _force_done_after_failure(self) -> None:
		"""Принудительное завершение после неудачи"""
		# Создание сообщения восстановления
		if self.state.consecutive_failures >= self.settings.max_failures and self.settings.final_response_after_failure:
			msg = f'You failed {self.settings.max_failures} times. Therefore we terminate the core.'
			msg += '\nYour only tool available is the "done" tool. No other tool is available. All other tools which you see in history or examples are not available.'
			msg += '\nIf the task is not yet fully finished as requested by the user, set success in "done" to false! E.g. if not all steps are fully completed. Else success to true.'
			msg += '\nInclude everything you found out for the ultimate task in the done text.'

			self.logger.debug('Force done action, because we reached max_failures.')
			self._message_manager._add_context_message(UserMessage(content=msg))
			self.AgentOutput = self.DoneAgentOutput

	@observe(ignore_input=True, ignore_output=False)
	async def _judge_trace(self) -> JudgementResult | None:
		"""Judge-оценка действий агента."""
		return None

	async def _judge_and_log(self) -> None:
		"""Выполняет judge-оценку и логирование."""
		return

	async def _get_model_output_with_retry(self, context_messages: list[BaseMessage]) -> AgentOutput:
		"""Получает вывод модели с логикой повторных попыток для пустых действий. Delegates to LLMManager."""
		return await self._llm_manager.get_model_output_with_retry(context_messages)

	def _is_empty_action(self, agent_decision: AgentOutput) -> bool:
		"""Проверяет, является ли действие пустым. Delegates to LLMManager."""
		return self._llm_manager.is_empty_action(agent_decision)

	async def _retry_with_clarification(self, context_messages: list[BaseMessage]) -> AgentOutput:
		"""Повторяет вызов модели с уточняющим сообщением. Delegates to LLMManager."""
		return await self._llm_manager.retry_with_clarification(context_messages)

	def _create_safe_noop_action(self) -> AgentOutput:
		"""Создает безопасное noop действие при отсутствии ответа от модели. Delegates to LLMManager."""
		return self._llm_manager.create_safe_noop_action()

	async def _handle_post_llm_processing(
		self,
		page_state: BrowserStateSummary,
		context_messages: list[BaseMessage],
	) -> None:
		"""Обработка колбэков и сохранение разговора после взаимодействия с LLM. Delegates to HistoryManager."""
		await self._history_manager.handle_post_llm_processing(page_state, context_messages)

	async def _make_history_item(
		self,
		agent_decision: AgentOutput | None,
		page_state: BrowserStateSummary,
		action_results: list[ActionResult],
		metadata: StepMetadata | None = None,
		state_message: str | None = None,
	) -> None:
		"""Создает и сохраняет элемент истории. Delegates to HistoryManager."""
		await self._history_manager.make_history_item(agent_decision, page_state, action_results, metadata, state_message)

	def _remove_think_tags(self, text: str) -> str:
		"""Remove think tags from text. Delegates to HistoryManager."""
		return self._history_manager_component.remove_think_tags(text)

	# region - URL replacement
	def _replace_urls_in_text(self, text: str) -> tuple[str, dict[str, str]]:
		"""Заменяет URL в текстовой строке. Delegates to LLMManager."""
		return self._llm_manager.replace_urls_in_text(text)

	def _process_messsages_and_replace_long_urls_shorter_ones(self, input_messages: list[BaseMessage]) -> dict[str, str]:
		"""Replace long URLs with shorter ones. Delegates to LLMManager."""
		return self._llm_manager.process_messages_and_replace_long_urls_shorter_ones(input_messages)

	@staticmethod
	def _recursive_process_all_strings_inside_pydantic_model(model: BaseModel, url_replacements: dict[str, str]) -> None:
		"""Рекурсивно обрабатывает все строки внутри Pydantic модели. Delegates to LLMManager."""
		from core.orchestrator.llm.llm_manager import LLMManager
		LLMManager.recursive_process_all_strings_inside_pydantic_model(model, url_replacements)

	@staticmethod
	def _recursive_process_dict(dictionary: dict, url_replacements: dict[str, str]) -> None:
		"""Вспомогательный метод для обработки словарей. Delegates to LLMManager."""
		from core.orchestrator.llm.llm_manager import LLMManager
		LLMManager.recursive_process_dict(dictionary, url_replacements)

	@staticmethod
	def _recursive_process_list_or_tuple(container: list | tuple, url_replacements: dict[str, str]) -> list | tuple:
		"""Helper method to process lists and tuples. Delegates to LLMManager."""
		from core.orchestrator.llm.llm_manager import LLMManager
		return LLMManager.recursive_process_list_or_tuple(container, url_replacements)

	@staticmethod
	def _replace_shortened_urls_in_string(text: str, url_replacements: dict[str, str]) -> str:
		"""Заменяет все сокращенные URL в строке на их оригинальные URL. Delegates to LLMManager."""
		from core.orchestrator.llm.llm_manager import LLMManager
		return LLMManager.replace_shortened_urls_in_string(text, url_replacements)

	# endregion - URL replacement

	@time_execution_async('--get_next_action')
	@observe_debug(ignore_input=True, ignore_output=True, name='get_model_output')
	async def get_model_output(self, input_messages: list[BaseMessage]) -> AgentOutput:
		"""Get next action from LLM based on current state. Delegates to LLMManager."""
		return await self._llm_manager.get_model_output(input_messages)

	def _try_switch_to_fallback_llm(self, error: ModelRateLimitError | ModelProviderError) -> bool:
		"""
		Attempt to switch to a fallback LLM after a rate limit or provider error. Delegates to LLMManager.
		"""
		return self._llm_manager.try_switch_to_fallback_llm(error)

	def _log_fallback_switch(self, error: ModelRateLimitError | ModelProviderError, fallback) -> None:
		"""Log when switching to a fallback LLM. Delegates to LLMManager."""
		self._llm_manager.log_fallback_switch(error, fallback)

	async def _log_agent_run(self) -> None:
		"""Log the agent run. Delegates to LoggingManager."""
		await self._run_manager.log_agent_run()

	def _log_first_step_startup(self) -> None:
		"""Log startup message only on the first step. Delegates to LoggingManager."""
		self._logging_manager.log_first_step_startup()

	def _log_step_context(self, browser_state_summary: BrowserStateSummary) -> None:
		"""Log step context information. Delegates to LoggingManager."""
		self._run_manager.log_step_context(browser_state_summary)

	def _log_next_action_summary(self, parsed: 'AgentOutput') -> None:
		"""Log a comprehensive summary of the next action(s). Delegates to LoggingManager."""
		self._run_manager.log_next_action_summary(parsed)

	def _prepare_demo_message(self, message: str, limit: int = 600) -> str:
		"""Prepare demo message. Delegates to DemoModeManager."""
		return self._demo_mode_manager.prepare_demo_message(message, limit)

	async def _demo_mode_log(self, message: str, level: str = 'info', metadata: dict[str, Any] | None = None) -> None:
		"""Send log message to demo mode panel. Delegates to LoggingManager."""
		await self._run_manager.demo_mode_log(message, level, metadata)

	async def _broadcast_model_state(self, parsed: 'AgentOutput') -> None:
		"""Broadcast model state to demo mode. Delegates to LoggingManager."""
		await self._run_manager.broadcast_model_state(parsed)

	def _log_step_completion_summary(self, step_start_time: float, result: list[ActionResult]) -> str | None:
		"""Log step completion summary. Delegates to LoggingManager."""
		return self._run_manager.log_step_completion_summary(step_start_time, result)

	def _log_final_outcome_messages(self) -> None:
		"""Log helpful messages to user based on agent run outcome. Delegates to LoggingManager."""
		self._run_manager.log_final_outcome_messages()

	def _log_agent_event(self, max_steps: int, agent_run_error: str | None = None) -> None:
		"""Отправка телеметрии."""
		return

	async def take_step(self, step_info: AgentStepInfo | None = None) -> tuple[bool, bool]:
		"""Take a step

		Returns:
		        Tuple[bool, bool]: (is_done, is_valid)
		"""
		if step_info is not None and step_info.step_number == 0:
			# First step
			self._logging_manager.log_first_step_startup()
			# Normally there was no try catch here but the callback can raise an InterruptedError which we skip
			try:
				await self._rerun_manager._execute_initial_actions()
			except InterruptedError:
				pass
			except Exception as e:
				raise e

		await self.step(step_info)

		if self.history.is_done():
			await self.log_completion()

			# Run judge before done callback if enabled

			if self.register_done_callback:
				if inspect.iscoroutinefunction(self.register_done_callback):
					await self.register_done_callback(self.history)
				else:
					self.register_done_callback(self.history)
			return True, True

		return False, False

	def _extract_start_url(self, task: str) -> str | None:
		"""Extract URL from task string using naive pattern matching. Delegates to URLParser."""
		return self._url_parser.extract_start_url(task)

	async def _execute_step(
		self,
		step: int,
		max_steps: int,
		step_info: AgentStepInfo,
		on_step_start: AgentHookFunc | None = None,
		on_step_end: AgentHookFunc | None = None,
	) -> bool:
		"""
		Execute a single step with timeout.

		Returns:
			bool: True if task is done, False otherwise
		"""
		if on_step_start is not None:
			await on_step_start(self)

		await self._demo_mode_log(
			f'Starting step {step + 1}/{max_steps}',
			'info',
			{'step': step + 1, 'total_steps': max_steps},
		)

		self.logger.debug(f'🚶 Starting step {step + 1}/{max_steps}...')

		try:
			await asyncio.wait_for(
				self.step(step_info),
				timeout=self.settings.step_timeout,
			)
			self.logger.debug(f'✅ Completed step {step + 1}/{max_steps}')
		except TimeoutError:
			# Handle step timeout gracefully
			error_msg = f'Step {step + 1} timed out after {self.settings.step_timeout} seconds'
			self.logger.error(f'⏰ {error_msg}')
			await self._demo_mode_log(error_msg, 'error', {'step': step + 1})
			self.state.consecutive_failures += 1
			self.state.last_result = [ActionResult(error=error_msg)]

		if on_step_end is not None:
			await on_step_end(self)

		if self.history.is_done():
			await self.log_completion()

			# Run judge before done callback if enabled

			if self.register_done_callback:
				if inspect.iscoroutinefunction(self.register_done_callback):
					await self.register_done_callback(self.history)
				else:
					self.register_done_callback(self.history)

			return True

		return False

	@observe(name='core.run', ignore_input=True, ignore_output=True)
	@time_execution_async('--run')
	async def run(
		self,
		max_steps: int = 100,
		on_step_start: AgentHookFunc | None = None,
		on_step_end: AgentHookFunc | None = None,
	) -> AgentHistoryList[AgentStructuredOutput]:
		"""Execute the task with maximum number of steps"""

		loop = asyncio.get_event_loop()
		agent_run_error: str | None = None  # Initialize error tracking variable
		should_delay_close = False

		# Set up the  signal handler with callbacks specific to this agent
		from core.helpers import SignalHandler

		signal_handler = SignalHandler(
			loop=loop,
			pause_callback=self.pause,
			resume_callback=self.resume,
			custom_exit_callback=None,
			exit_on_second_int=True,
		)
		signal_handler.register()

		try:
			await self._log_agent_run()

			self.logger.debug(
				f'🔧 Agent setup: Agent Session ID {self.session_id[-4:]}, Task ID {self.task_id[-4:]}, Browser Session ID {self.browser_session.id[-4:] if self.browser_session else "None"} {"(connecting via CDP)" if (self.browser_session and self.browser_session.cdp_url) else "(launching local browser)"}'
			)

			# Initialize timing for session and task
			self._session_start_time = time.time()
			self._task_start_time = self._session_start_time  # Initialize task start time

			# Only dispatch session events if this is the first run
			if not self.state.session_initialized:
				self.state.session_initialized = True

			# Log startup message on first step (only if we haven't already done steps)
			self._log_first_step_startup()
			# Start browser session and attach watchdogs
			await self.browser_session.start()
			if self._demo_mode_enabled:
				await self._demo_mode_log(f'Started task: {self.task}', 'info', {'tag': 'task'})
				await self._demo_mode_log(
					'Demo mode active - follow the side panel for live thoughts and actions.',
					'info',
					{'tag': 'status'},
				)

			# Register skills as actions if SkillService is configured
			await self._register_skills_as_actions()

			# Пересчитываем initial_actions если задача была изменена после инициализации
			if self.directly_open_url and not self.state.follow_up_task and not self.initial_actions:
				initial_url = self._url_parser.extract_start_url(self.task)
				if initial_url:
					self.logger.info(f'🔗 Найден URL в задаче: {initial_url}, добавляю как начальное действие...')
					self.initial_url = initial_url
					self.initial_actions = self._convert_initial_actions([{'navigate': {'url': initial_url, 'new_tab': False}}])

			# Normally there was no try catch here but the callback can raise an InterruptedError
			try:
				await self._execute_initial_actions()
			except InterruptedError:
				pass
			except Exception as e:
				raise e

			self.logger.debug(
				f'🔄 Starting main execution loop with max {max_steps} steps (currently at step {self.state.n_steps})...'
			)
			while self.state.n_steps <= max_steps:
				current_step = self.state.n_steps - 1  # Convert to 0-indexed for step_info

				# Use the consolidated pause state management
				if self.state.paused:
					self.logger.debug(f'⏸️ Step {self.state.n_steps}: Agent paused, waiting to resume...')
					await self._external_pause_event.wait()
					signal_handler.reset()

				# Check if we should stop due to too many failures, if final_response_after_failure is True, we try one last time
				if (self.state.consecutive_failures) >= self.settings.max_failures + int(
					self.settings.final_response_after_failure
				):
					self.logger.error(f'❌ Stopping due to {self.settings.max_failures} consecutive failures')
					agent_run_error = f'Stopped due to {self.settings.max_failures} consecutive failures'
					break

				# Check control flags before each step
				if self.state.stopped:
					self.logger.info('🛑 Agent stopped')
					agent_run_error = 'Agent stopped programmatically'
					break

				step_info = AgentStepInfo(step_number=current_step, max_steps=max_steps)
				is_done = await self._execute_step(current_step, max_steps, step_info, on_step_start, on_step_end)

				if is_done:
					# Agent has marked the task as done
					if self._demo_mode_enabled and self.history.history:
						final_result_text = self.history.final_result() or 'Task completed'
						await self._demo_mode_log(f'Final Result: {final_result_text}', 'success', {'tag': 'task'})

					should_delay_close = True
					break
			else:
				agent_run_error = 'Failed to complete task in maximum steps'

				self.history.add_item(
					AgentHistory(
						model_output=None,
						result=[ActionResult(error=agent_run_error, include_in_memory=True)],
						state=BrowserStateHistory(
							url='',
							title='',
							tabs=[],
							interacted_element=[],
							screenshot_path=None,
						),
						metadata=None,
					)
				)

				self.logger.info(f'❌ {agent_run_error}')

			self.history.usage = await self.token_cost_service.get_usage_summary()

			# set the model output schema and call it on the fly
			if self.history._output_model_schema is None and self.output_model_schema is not None:
				self.history._output_model_schema = self.output_model_schema

			return self.history

		except KeyboardInterrupt:
			# Already handled by our signal handler, but catch any direct KeyboardInterrupt as well
			self.logger.debug('Got KeyboardInterrupt during execution, returning current history')
			agent_run_error = 'KeyboardInterrupt'

			self.history.usage = await self.token_cost_service.get_usage_summary()

			return self.history

		except Exception as e:
			self.logger.error(f'Agent run failed with exception: {e}', exc_info=True)
			agent_run_error = str(e)
			raise e

		finally:
			if should_delay_close and self._demo_mode_enabled and agent_run_error is None:
				await asyncio.sleep(30)
			if agent_run_error:
				await self._demo_mode_log(f'Agent stopped: {agent_run_error}', 'error', {'tag': 'run'})
			# Log token usage summary
			await self.token_cost_service.log_usage_summary()

			# Unregister signal handlers before cleanup
			signal_handler.unregister()

			# Generate GIF if needed before stopping event bus
			if self.settings.generate_gif:
				output_path: str = 'agent_history.gif'
				if isinstance(self.settings.generate_gif, str):
					output_path = self.settings.generate_gif

				# Lazy import gif module to avoid heavy startup cost
				try:
					from core.orchestrator.gif import create_history_gif
					create_history_gif(task=self.task, history=self.history, output_path=output_path)
				except ImportError:
					self.logger.warning('GIF generation module not available')

			# Log final messages to user based on outcome
			self._log_final_outcome_messages()

			# Stop the event bus gracefully, waiting for all events to be processed
			# Использование более длинного таймаута для избежания блокировок при тестировании
			await self.eventbus.stop(timeout=3.0)

			await self.close()

	@observe_debug(ignore_input=True, ignore_output=True)
	@time_execution_async('--multi_act')
	async def multi_act(self, actions: list[ActionModel]) -> list[ActionResult]:
		"""Execute multiple actions. Delegates to ActionExecutionManager."""
		return await self._action_execution.multi_act(actions)

	async def _log_action(self, action, action_name: str, action_num: int, total_actions: int) -> None:
		"""Log the action before execution with colored formatting"""
		# Color definitions
		blue = '\033[34m'  # Action name
		magenta = '\033[35m'  # Parameter names
		reset = '\033[0m'

		# Format action number and name
		if total_actions > 1:
			action_header = f'▶️  [{action_num}/{total_actions}] {blue}{action_name}{reset}:'
			plain_header = f'▶️  [{action_num}/{total_actions}] {action_name}:'
		else:
			action_header = f'▶️   {blue}{action_name}{reset}:'
			plain_header = f'▶️  {action_name}:'

		# Get action parameters
		action_data = action.model_dump(exclude_unset=True)
		params = action_data.get(action_name, {})

		# Build parameter parts with colored formatting
		param_parts = []
		plain_param_parts = []

		if params and isinstance(params, dict):
			for param_name, value in params.items():
				# Truncate long values for readability
				if isinstance(value, str) and len(value) > 150:
					display_value = value[:150] + '...'
				elif isinstance(value, list) and len(str(value)) > 200:
					display_value = str(value)[:200] + '...'
				else:
					display_value = value

				param_parts.append(f'{magenta}{param_name}{reset}: {display_value}')
				plain_param_parts.append(f'{param_name}: {display_value}')

		# Join all parts
		if param_parts:
			params_string = ', '.join(param_parts)
			self.logger.info(f'  {action_header} {params_string}')
		else:
			self.logger.info(f'  {action_header}')

		if self._demo_mode_enabled:
			panel_message = plain_header
			if plain_param_parts:
				panel_message = f'{panel_message} {", ".join(plain_param_parts)}'
			await self._demo_mode_log(panel_message.strip(), 'action', {'action': action_name, 'step': self.state.n_steps})

	async def log_completion(self) -> None:
		"""Log the completion of the task. Delegates to LoggingManager."""
		await self._run_manager.log_completion()


	async def rerun_history(
		self,
		history: AgentHistoryList,
		max_retries: int = 3,
		skip_failures: bool = True,
		delay_between_actions: float = 2.0,
		summary_llm: BaseChatModel | None = None,
		ai_step_llm: BaseChatModel | None = None,
	) -> list[ActionResult]:
		"""Rerun a saved history of actions. Delegates to RerunManager."""
		return await self._rerun_manager.rerun_history(
			history, max_retries, skip_failures, delay_between_actions, summary_llm, ai_step_llm
		)

	async def _execute_initial_actions(self) -> None:
		"""Execute initial actions if provided. Delegates to RerunManager."""
		await self._rerun_manager._execute_initial_actions()

	async def load_and_rerun(
		self,
		history_file: str | Path | None = None,
		variables: dict[str, str] | None = None,
		**kwargs,
	) -> list[ActionResult]:
		"""Load history from file and rerun it. Delegates to RerunManager."""
		return await self._rerun_manager.load_and_rerun(history_file, variables, **kwargs)

	def save_history(self, file_path: str | Path | None = None) -> None:
		"""Save the history to a file with sensitive data filtering. Delegates to HistoryManager."""
		self._step_manager.save_history(file_path)

	def pause(self) -> None:
		"""Pause the agent before the next step"""
		print('\n\n⏸️ Paused the agent and left the browser open.\n\tPress [Enter] to resume or [Ctrl+C] again to quit.')
		self.state.paused = True
		self._external_pause_event.clear()

	def resume(self) -> None:
		"""Resume the agent"""
		print('----------------------------------------------------------------------')
		print('▶️  Resuming agent execution where it left off...\n')
		self.state.paused = False
		self._external_pause_event.set()

	def stop(self) -> None:
		"""Stop the agent"""
		self.logger.info('⏹️ Agent stopping')
		self.state.stopped = True

		# Signal pause event to unblock any waiting code so it can check the stopped state
		self._external_pause_event.set()

		# Task stopped

	def _convert_initial_actions(self, actions: list[dict[str, dict[str, Any]]]) -> list[ActionModel]:
		"""Convert dictionary-based actions to ActionModel instances"""
		converted_actions = []
		action_model = self.ActionModel
		for action_dict in actions:
			# Each action_dict should have a single key-value pair
			action_name = next(iter(action_dict))
			params = action_dict[action_name]

			# Get the parameter model for this action from registry
			action_info = self.tools.registry.registry.actions[action_name]
			param_model = action_info.param_model

			# Create validated parameters using the appropriate param model
			validated_params = param_model(**params)

			# Create ActionModel instance with the validated parameters
			action_model = self.ActionModel(**{action_name: validated_params})
			converted_actions.append(action_model)

		return converted_actions

	def _verify_and_setup_llm(self):
		"""
		Verify that the LLM API keys are setup and the LLM API is responding properly.
		Also handles tool calling method detection if in auto mode.
		"""

		# Skip verification if already done
		if getattr(self.llm, '_verified_api_keys', None) is True or CONFIG.SKIP_LLM_API_KEY_VERIFICATION:
			setattr(self.llm, '_verified_api_keys', True)
			return True

	@property
	def message_manager(self) -> MessageManager:
		return self._message_manager

	async def close(self):
		"""Close all resources"""
		try:
			# Only close browser if keep_alive is False (or not set)
			if self.browser_session is not None:
				if not self.browser_session.browser_profile.keep_alive:
					# Kill the browser session - this dispatches BrowserStopEvent,
					# stops the EventBus with clear=True, and recreates a fresh EventBus
					await self.browser_session.kill()


			# Force garbage collection
			gc.collect()

			# Логирование оставшихся потоков и asyncio задач
			import threading

			threads = threading.enumerate()
			self.logger.debug(f'🧵 Remaining threads ({len(threads)}): {[t.name for t in threads]}')

			# Get all asyncio tasks
			tasks = asyncio.all_tasks(asyncio.get_event_loop())
			# Filter out the current task (this close() coroutine)
			other_tasks = [t for t in tasks if t != asyncio.current_task()]
			if other_tasks:
				self.logger.debug(f'⚡ Remaining asyncio tasks ({len(other_tasks)}):')
				for task in other_tasks[:10]:  # Limit to first 10 to avoid spam
					self.logger.debug(f'  - {task.get_name()}: {task}')

		except Exception as e:
			self.logger.error(f'Error during cleanup: {e}')

	async def _update_action_models_for_page(self, page_url: str) -> None:
		"""Update action models with page-specific actions"""
		# Create new action model with current page's filtered actions
		self.ActionModel = self.tools.registry.create_action_model(page_url=page_url)
		# Update output model with the new actions
		if self.settings.flash_mode:
			self.AgentOutput = AgentOutput.type_with_custom_actions_flash_mode(self.ActionModel)
		elif self.settings.use_thinking:
			self.AgentOutput = AgentOutput.type_with_custom_actions(self.ActionModel)
		else:
			self.AgentOutput = AgentOutput.type_with_custom_actions_no_thinking(self.ActionModel)

		# Update done action model too
		self.DoneActionModel = self.tools.registry.create_action_model(include_actions=['done'], page_url=page_url)
		if self.settings.flash_mode:
			self.DoneAgentOutput = AgentOutput.type_with_custom_actions_flash_mode(self.DoneActionModel)
		elif self.settings.use_thinking:
			self.DoneAgentOutput = AgentOutput.type_with_custom_actions(self.DoneActionModel)
		else:
			self.DoneAgentOutput = AgentOutput.type_with_custom_actions_no_thinking(self.DoneActionModel)

	async def authenticate_cloud_sync(self, show_instructions: bool = True) -> bool:
		"""
		Authenticate with cloud service for future runs.

		This is useful when users want to authenticate after a task has completed
		so that future runs will sync to the cloud.

		Args:
			show_instructions: Whether to show authentication instructions to user

		Returns:
			bool: True if authentication was successful
		"""
		self.logger.warning('Cloud sync has been removed and is no longer available')
		return False

	def run_sync(
		self,
		max_steps: int = 100,
		on_step_start: AgentHookFunc | None = None,
		on_step_end: AgentHookFunc | None = None,
	) -> AgentHistoryList[AgentStructuredOutput]:
		"""Synchronous wrapper around the async run method for easier usage without asyncio."""
		import asyncio

		return asyncio.run(self.run(max_steps=max_steps, on_step_start=on_step_start, on_step_end=on_step_end))

	def detect_variables(self) -> dict[str, DetectedVariable]:
		"""Detect reusable variables in agent history. Delegates to HistoryManager."""
		return self._history_manager_component.detect_variables()

	def _substitute_variables_in_history(self, history: AgentHistoryList, variables: dict[str, str]) -> AgentHistoryList:
		"""Substitute variables in history with new values for rerunning with different data. Delegates to HistoryManager."""
		return self._history_manager_component.substitute_variables_in_history(history, variables)

	def _substitute_in_dict(self, data: dict, replacements: dict[str, str]) -> int:
		"""Recursively substitute values in a dictionary, returns count of substitutions made. Delegates to HistoryManager."""
		return self._history_manager_component._substitute_in_dict(data, replacements)
