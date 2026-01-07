"""Конфигурация проекта агента с поддержкой миграций настроек."""

import json
import logging
import os
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any
from uuid import uuid4

import psutil
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


@cache
def is_running_in_docker() -> bool:
	"""Определить, запущены ли мы в контейнере Docker, для оптимизации флагов запуска Chrome (использование dev shm, настройки GPU и т.д.)"""
	try:
		if Path('/.dockerenv').exists() or 'docker' in Path('/proc/1/cgroup').read_text().lower():
			return True
	except Exception:
		pass

	try:
		# если init процесс (PID 1) выглядит как uvicorn/python/uv/и т.д., то мы в Docker
		# если init процесс (PID 1) выглядит как bash/systemd/init/и т.д., то мы, вероятно, НЕ в Docker
		init_command = ' '.join(psutil.Process(1).cmdline())
		if ('py' in init_command) or ('uv' in init_command) or ('app' in init_command):
			return True
	except Exception:
		pass

	try:
		# если меньше 10 всего запущенных процессов, то мы почти наверняка в контейнере
		if len(psutil.pids()) < 10:
			return True
	except Exception:
		pass

	return False


class OldConfig:
	"""Исходный класс конфигурации с ленивой загрузкой для переменных окружения."""

	# Кэш для отслеживания создания директорий
	_directories_created = False

	@property
	def AGENT_LOGGING_LEVEL(self) -> str:
		return os.getenv('AGENT_LOGGING_LEVEL', 'info').lower()

	@property
	def ANONYMIZED_TELEMETRY(self) -> bool:
		return os.getenv('ANONYMIZED_TELEMETRY', 'true').lower()[:1] in 'ty1'

	@property
	def AGENT_CLOUD_SYNC(self) -> bool:
		# Облачная синхронизация не используется
		return False

	@property
	def CLOUD_API_URL(self) -> str:
		api_url = os.getenv('CLOUD_API_URL', 'https://api.example.com')
		assert '://' in api_url, 'CLOUD_API_URL must be a valid URL'
		return api_url

	@property
	def CLOUD_UI_URL(self) -> str:
		ui_url = os.getenv('CLOUD_UI_URL', '')
		# Разрешить пустую строку по умолчанию, валидировать только если установлена
		if ui_url and '://' not in ui_url:
			raise AssertionError('CLOUD_UI_URL must be a valid URL if set')
		return ui_url

	# Конфигурация путей
	@property
	def XDG_CACHE_HOME(self) -> Path:
		return Path(os.getenv('XDG_CACHE_HOME', '~/.cache')).expanduser().resolve()

	@property
	def XDG_CONFIG_HOME(self) -> Path:
		return Path(os.getenv('XDG_CONFIG_HOME', '~/.config')).expanduser().resolve()

	@property
	def AGENT_CONFIG_DIR(self) -> Path:
		config_directory = Path(os.getenv('AGENT_CONFIG_DIR', str(self.XDG_CONFIG_HOME / 'agent'))).expanduser().resolve()
		self._ensure_dirs()
		return config_directory

	@property
	def AGENT_CONFIG_FILE(self) -> Path:
		return self.AGENT_CONFIG_DIR / 'config.json'

	@property
	def AGENT_PROFILES_DIR(self) -> Path:
		profiles_directory = self.AGENT_CONFIG_DIR / 'profiles'
		self._ensure_dirs()
		return profiles_directory

	@property
	def AGENT_DEFAULT_USER_DATA_DIR(self) -> Path:
		return self.AGENT_PROFILES_DIR / 'default'

	@property
	def AGENT_EXTENSIONS_DIR(self) -> Path:
		extensions_directory = self.AGENT_CONFIG_DIR / 'extensions'
		self._ensure_dirs()
		return extensions_directory

	def _ensure_dirs(self) -> None:
		"""Создать директории, если они не существуют (только один раз)"""
		if not self._directories_created:
			config_directory = Path(os.getenv('AGENT_CONFIG_DIR', str(self.XDG_CONFIG_HOME / 'agent'))).expanduser().resolve()
			config_directory.mkdir(parents=True, exist_ok=True)
			(config_directory / 'profiles').mkdir(parents=True, exist_ok=True)
			(config_directory / 'extensions').mkdir(parents=True, exist_ok=True)
			self._directories_created = True

	# LLM API key configuration
	@property
	def OPENAI_API_KEY(self) -> str:
		return os.getenv('OPENAI_API_KEY', '')

	@property
	def ANTHROPIC_API_KEY(self) -> str:
		return os.getenv('ANTHROPIC_API_KEY', '')

	@property
	def GOOGLE_API_KEY(self) -> str:
		return os.getenv('GOOGLE_API_KEY', '')

	@property
	def DEEPSEEK_API_KEY(self) -> str:
		return os.getenv('DEEPSEEK_API_KEY', '')

	@property
	def GROK_API_KEY(self) -> str:
		return os.getenv('GROK_API_KEY', '')

	@property
	def NOVITA_API_KEY(self) -> str:
		return os.getenv('NOVITA_API_KEY', '')

	@property
	def AZURE_OPENAI_ENDPOINT(self) -> str:
		return os.getenv('AZURE_OPENAI_ENDPOINT', '')

	@property
	def AZURE_OPENAI_KEY(self) -> str:
		return os.getenv('AZURE_OPENAI_KEY', '')

	@property
	def SKIP_LLM_API_KEY_VERIFICATION(self) -> bool:
		return os.getenv('SKIP_LLM_API_KEY_VERIFICATION', 'false').lower()[:1] in 'ty1'

	@property
	def DEFAULT_LLM(self) -> str:
		return os.getenv('DEFAULT_LLM', '')

	# Подсказки времени выполнения
	@property
	def IN_DOCKER(self) -> bool:
		return os.getenv('IN_DOCKER', 'false').lower()[:1] in 'ty1' or is_running_in_docker()

	@property
	def IS_IN_EVALS(self) -> bool:
		return os.getenv('IS_IN_EVALS', 'false').lower()[:1] in 'ty1'

	@property
	def AGENT_VERSION_CHECK(self) -> bool:
		# Сетевую проверку версии отключаем
		return False

	@property
	def WIN_FONT_DIR(self) -> str:
		return os.getenv('WIN_FONT_DIR', 'C:\\Windows\\Fonts')


class FlatEnvConfig(BaseSettings):
	"""Все переменные окружения в плоском пространстве имен."""

	model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', case_sensitive=True, extra='allow')

	# Логирование и телеметрия
	AGENT_LOGGING_LEVEL: str = Field(default='info')
	CDP_LOGGING_LEVEL: str = Field(default='WARNING')
	AGENT_DEBUG_LOG_FILE: str | None = Field(default=None)
	AGENT_INFO_LOG_FILE: str | None = Field(default=None)
	ANONYMIZED_TELEMETRY: bool = Field(default=True)
	AGENT_CLOUD_SYNC: bool | None = Field(default=None)
	CLOUD_API_URL: str = Field(default='https://api.example.com')
	CLOUD_UI_URL: str = Field(default='')

	# Конфигурация путей
	XDG_CACHE_HOME: str = Field(default='~/.cache')
	XDG_CONFIG_HOME: str = Field(default='~/.config')
	AGENT_CONFIG_DIR: str | None = Field(default=None)

	# API ключи LLM
	OPENAI_API_KEY: str = Field(default='')
	ANTHROPIC_API_KEY: str = Field(default='')
	GOOGLE_API_KEY: str = Field(default='')
	DEEPSEEK_API_KEY: str = Field(default='')
	GROK_API_KEY: str = Field(default='')
	NOVITA_API_KEY: str = Field(default='')
	AZURE_OPENAI_ENDPOINT: str = Field(default='')
	AZURE_OPENAI_KEY: str = Field(default='')
	SKIP_LLM_API_KEY_VERIFICATION: bool = Field(default=False)
	DEFAULT_LLM: str = Field(default='')

	# Подсказки времени выполнения
	IN_DOCKER: bool | None = Field(default=None)
	IS_IN_EVALS: bool = Field(default=False)
	WIN_FONT_DIR: str = Field(default='C:\\Windows\\Fonts')
	AGENT_VERSION_CHECK: bool = Field(default=False)

	# Совместимость с переменными окружения старого формата (часть может не использоваться)
	AGENT_CONFIG_PATH: str | None = Field(default=None)
	AGENT_HEADLESS: bool | None = Field(default=None)
	AGENT_ALLOWED_DOMAINS: str | None = Field(default=None)
	AGENT_LLM_MODEL: str | None = Field(default=None)

	# Переменные окружения прокси
	AGENT_PROXY_URL: str | None = Field(default=None)
	AGENT_NO_PROXY: str | None = Field(default=None)
	AGENT_PROXY_USERNAME: str | None = Field(default=None)
	AGENT_PROXY_PASSWORD: str | None = Field(default=None)


class DBStyleEntry(BaseModel):
	"""Запись в стиле базы данных с UUID и метаданными."""

	id: str = Field(default_factory=lambda: str(uuid4()))
	default: bool = Field(default=False)
	created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class BrowserProfileEntry(DBStyleEntry):
	"""Запись конфигурации профиля браузера - принимает любые поля BrowserProfile."""

	model_config = ConfigDict(extra='allow')

	# Общие поля профиля браузера
	headless: bool | None = None
	user_data_dir: str | None = None
	allowed_domains: list[str] | None = None
	downloads_path: str | None = None


class LLMEntry(DBStyleEntry):
	"""Запись конфигурации LLM."""

	api_key: str | None = None
	model: str | None = None
	temperature: float | None = None
	max_tokens: int | None = None


class AgentEntry(DBStyleEntry):
	"""Запись конфигурации агента."""

	max_steps: int | None = None
	use_vision: bool | None = None
	system_prompt: str | None = None


class DBStyleConfigJSON(BaseModel):
	"""Новый формат конфигурации в стиле базы данных."""

	browser_profile: dict[str, BrowserProfileEntry] = Field(default_factory=dict)
	llm: dict[str, LLMEntry] = Field(default_factory=dict)
	agent: dict[str, AgentEntry] = Field(default_factory=dict)


def create_default_config() -> DBStyleConfigJSON:
	"""Создать свежую конфигурацию по умолчанию."""
	logger.debug('Creating fresh default config.json')

	default_config = DBStyleConfigJSON()

	# Сгенерировать ID по умолчанию
	default_profile_id = str(uuid4())
	default_llm_id = str(uuid4())
	default_agent_id = str(uuid4())

	# Создать запись профиля браузера по умолчанию
	default_config.browser_profile[default_profile_id] = BrowserProfileEntry(id=default_profile_id, default=True, headless=False, user_data_dir=None)

	# Создать запись LLM по умолчанию
	default_config.llm[default_llm_id] = LLMEntry(id=default_llm_id, default=True, model='gpt-4.1-mini', api_key='your-openai-api-key-here')

	# Создать запись агента по умолчанию
	default_config.agent[default_agent_id] = AgentEntry(id=default_agent_id, default=True)

	return default_config


def load_and_migrate_config(config_file_path: Path) -> DBStyleConfigJSON:
	"""Загрузка config.json или создание нового при обнаружении старого формата."""
	if not config_file_path.exists():
		# Создать свежую конфигурацию с настройками по умолчанию
		config_file_path.parent.mkdir(parents=True, exist_ok=True)
		fresh_config = create_default_config()
		with open(config_file_path, 'w') as config_file:
			json.dump(fresh_config.model_dump(), config_file, indent=2)
		return fresh_config

	try:
		with open(config_file_path) as config_file:
			config_data = json.load(config_file)

		# Проверить, находится ли уже в формате DB-style
		if all(key_name in config_data for key_name in ['browser_profile', 'llm', 'agent']) and all(
			isinstance(config_data.get(key_name, {}), dict) for key_name in ['browser_profile', 'llm', 'agent']
		):
			# Проверить, являются ли значения записями DB-style (имеют UUID в качестве ключей)
			if config_data.get('browser_profile') and all(isinstance(entry_value, dict) and 'id' in entry_value for entry_value in config_data['browser_profile'].values()):
				# Уже в новом формате
				return DBStyleConfigJSON(**config_data)

		# Обнаружен старый формат - удаление и создание нового конфига
		logger.debug(f'Old config format detected at {config_file_path}, creating fresh config')
		fresh_config = create_default_config()

		# Перезаписать новым конфигом
		with open(config_file_path, 'w') as config_file:
			json.dump(fresh_config.model_dump(), config_file, indent=2)

		logger.debug(f'Created fresh config.json at {config_file_path}')
		return fresh_config

	except Exception as load_error:
		logger.error(f'Failed to load config from {config_file_path}: {load_error}, creating fresh config')
		# При любой ошибке создать свежую конфигурацию
		fresh_config = create_default_config()
		try:
			with open(config_file_path, 'w') as config_file:
				json.dump(fresh_config.model_dump(), config_file, indent=2)
		except Exception as write_error:
			logger.error(f'Failed to write fresh config: {write_error}')
		return fresh_config


class Config:
	"""Обратно совместимый класс конфигурации, который объединяет все источники конфигурации.

	Перечитывает переменные окружения при каждом доступе для поддержания совместимости.
	"""

	def __init__(self):
		# Кэш только для отслеживания создания директорий
		self._directories_created = False

	def __getattr__(self, attribute_name: str) -> Any:
		"""Динамически проксировать все атрибуты к свежим экземплярам.

		Это гарантирует перечитывание переменных окружения при каждом доступе.
		"""
		# Специальная обработка внутренних атрибутов
		if attribute_name.startswith('_'):
			raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{attribute_name}'")

		# Создать свежие экземпляры при каждом доступе
		legacy_config = OldConfig()

		# Всегда использовать старую конфигурацию для всех атрибутов (она обрабатывает переменные окружения с правильными преобразованиями)
		if hasattr(legacy_config, attribute_name):
			return getattr(legacy_config, attribute_name)

		# Для новых атрибутов, специфичных для MCP, которых нет в старой конфигурации
		env_config_instance = FlatEnvConfig()
		if hasattr(env_config_instance, attribute_name):
			return getattr(env_config_instance, attribute_name)

		# Обработать специальные методы
		if attribute_name == 'get_default_profile':
			return lambda: self._get_default_profile()
		elif attribute_name == 'get_default_llm':
			return lambda: self._get_default_llm()
		elif attribute_name == 'get_default_agent':
			return lambda: self._get_default_agent()
		elif attribute_name == 'load_config':
			return lambda: self._load_config()
		elif attribute_name == '_ensure_dirs':
			return lambda: legacy_config._ensure_dirs()

		raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{attribute_name}'")

	def _get_config_path(self) -> Path:
		"""Получить путь конфигурации из свежей конфигурации окружения."""
		env_config_instance = FlatEnvConfig()
		if env_config_instance.AGENT_CONFIG_PATH:
			return Path(env_config_instance.AGENT_CONFIG_PATH).expanduser()
		elif env_config_instance.AGENT_CONFIG_DIR:
			return Path(env_config_instance.AGENT_CONFIG_DIR).expanduser() / 'config.json'
		else:
			xdg_config_path = Path(env_config_instance.XDG_CONFIG_HOME).expanduser()
			return xdg_config_path / 'agent' / 'config.json'

	def _get_db_config(self) -> DBStyleConfigJSON:
		"""Загрузить и мигрировать config.json."""
		config_file_path = self._get_config_path()
		return load_and_migrate_config(config_file_path)

	def _get_default_profile(self) -> dict[str, Any]:
		"""Получить конфигурацию профиля браузера по умолчанию."""
		db_config_instance = self._get_db_config()
		for profile_entry in db_config_instance.browser_profile.values():
			if profile_entry.default:
				return profile_entry.model_dump(exclude_none=True)

		# Вернуть первый профиль, если нет по умолчанию
		if db_config_instance.browser_profile:
			return next(iter(db_config_instance.browser_profile.values())).model_dump(exclude_none=True)

		return {}

	def _get_default_llm(self) -> dict[str, Any]:
		"""Получить конфигурацию LLM по умолчанию."""
		db_config_instance = self._get_db_config()
		for llm_entry in db_config_instance.llm.values():
			if llm_entry.default:
				return llm_entry.model_dump(exclude_none=True)

		# Вернуть первый LLM, если нет по умолчанию
		if db_config_instance.llm:
			return next(iter(db_config_instance.llm.values())).model_dump(exclude_none=True)

		return {}

	def _get_default_agent(self) -> dict[str, Any]:
		"""Получить конфигурацию агента по умолчанию."""
		db_config_instance = self._get_db_config()
		for agent_entry in db_config_instance.core.values():
			if agent_entry.default:
				return agent_entry.model_dump(exclude_none=True)

		# Вернуть первого агента, если нет по умолчанию
		if db_config_instance.agent:
			return next(iter(db_config_instance.core.values())).model_dump(exclude_none=True)

		return {}

	def _load_config(self) -> dict[str, Any]:
		"""Загрузить конфигурацию с переопределениями переменных окружения для компонентов MCP."""
		merged_config = {
			'browser_profile': self._get_default_profile(),
			'llm': self._get_default_llm(),
			'agent': self._get_default_agent(),
		}

		# Свежая конфигурация окружения для переопределений
		env_config_instance = FlatEnvConfig()

		# Применить переопределения переменных окружения, специфичные для MCP
		if env_config_instance.AGENT_HEADLESS is not None:
			merged_config['browser_profile']['headless'] = env_config_instance.AGENT_HEADLESS

		if env_config_instance.AGENT_ALLOWED_DOMAINS:
			domain_list = [domain.strip() for domain in env_config_instance.AGENT_ALLOWED_DOMAINS.split(',') if domain.strip()]
			merged_config['browser_profile']['allowed_domains'] = domain_list

		# Настройки прокси (Chromium) -> объединенный словарь `proxy`
		proxy_settings: dict[str, Any] = {}
		if env_config_instance.AGENT_PROXY_URL:
			proxy_settings['server'] = env_config_instance.AGENT_PROXY_URL
		if env_config_instance.AGENT_NO_PROXY:
			# сохранить bypass как строку, разделенную запятыми, чтобы соответствовать флагу Chrome
			proxy_settings['bypass'] = ','.join([domain.strip() for domain in env_config_instance.AGENT_NO_PROXY.split(',') if domain.strip()])
		if env_config_instance.AGENT_PROXY_USERNAME:
			proxy_settings['username'] = env_config_instance.AGENT_PROXY_USERNAME
		if env_config_instance.AGENT_PROXY_PASSWORD:
			proxy_settings['password'] = env_config_instance.AGENT_PROXY_PASSWORD
		if proxy_settings:
			# убедиться, что секция существует
			merged_config.setdefault('browser_profile', {})
			merged_config['browser_profile']['proxy'] = proxy_settings

		if env_config_instance.OPENAI_API_KEY:
			merged_config['llm']['api_key'] = env_config_instance.OPENAI_API_KEY

		if env_config_instance.AGENT_LLM_MODEL:
			merged_config['llm']['model'] = env_config_instance.AGENT_LLM_MODEL

		return merged_config


# Create singleton instance
CONFIG = Config()


# Вспомогательные функции для компонентов MCP
def load_agent_config() -> dict[str, Any]:
	"""Загрузить конфигурацию агента для MCP-компонентов (если используются)."""
	return CONFIG.load_config()


def get_default_profile(config_dict: dict[str, Any]) -> dict[str, Any]:
	"""Получить профиль браузера по умолчанию из словаря конфигурации."""
	return config_dict.get('browser_profile', {})


def get_default_llm(config_dict: dict[str, Any]) -> dict[str, Any]:
	"""Получить конфигурацию LLM по умолчанию из словаря конфигурации."""
	return config_dict.get('llm', {})
