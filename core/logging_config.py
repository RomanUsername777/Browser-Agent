import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from core.config import CONFIG


def addLoggingLevel(name: str, level_value: int, method_name: str | None = None):
	"""
	Комплексно добавляет новый уровень логирования в модуль `logging` и
	текущий настроенный класс логирования.

	`name` становится атрибутом модуля `logging` со значением `level_value`.
	`method_name` становится удобным методом как для самого `logging`,
	так и для класса, возвращаемого `logging.getLoggerClass()` (обычно просто
	`logging.Logger`). Если `method_name` не указан, используется `name.lower()`.

	Чтобы избежать случайного перезаписывания существующих атрибутов, этот метод
	выбросит `AttributeError`, если имя уровня уже является атрибутом модуля
	`logging` или если имя метода уже присутствует

	Пример
	-------
	>>> addLoggingLevel('TRACE', logging.DEBUG - 5)
	>>> logging.getLogger(__name__).setLevel('TRACE')
	>>> logging.getLogger(__name__).trace('that worked')
	>>> logging.trace('so did this')
	>>> logging.TRACE
	5

	"""
	if not method_name:
		method_name = name.lower()

	if hasattr(logging, name):
		raise AttributeError(f'{name} already defined in logging module')
	if hasattr(logging, method_name):
		raise AttributeError(f'{method_name} already defined in logging module')
	if hasattr(logging.getLoggerClass(), method_name):
		raise AttributeError(f'{method_name} already defined in logger class')

	def log_at_level(self, message, *args, **kwargs):
		if self.isEnabledFor(level_value):
			self._log(level_value, message, args, **kwargs)

	def log_to_root(message, *args, **kwargs):
		logging.log(level_value, message, *args, **kwargs)

	logging.addLevelName(level_value, name)
	setattr(logging, name, level_value)
	setattr(logging.getLoggerClass(), method_name, log_at_level)
	setattr(logging, method_name, log_to_root)


def setup_logging(stream=None, log_level=None, force_setup=False, debug_log_file=None, info_log_file=None):
	"""Настроить логирование для агента.

	Args:
		stream: Output stream for logs (default: sys.stdout). Can be sys.stderr for MCP mode.
		log_level: Уровень логирования (по умолчанию CONFIG.LOGGING_LEVEL)
		force_setup: Force reconfiguration even if handlers already exist
		debug_log_file: Path to log file for debug level logs only
		info_log_file: Path to log file for info level logs only
	"""
	# Try to add RESULT level, but ignore if it already exists
	try:
		addLoggingLevel('RESULT', 35)  # This allows ERROR, FATAL and CRITICAL
	except AttributeError:
		pass  # Level already exists, which is fine

	level_type = log_level or CONFIG.AGENT_LOGGING_LEVEL

	# Проверить, настроены ли уже обработчики
	if logging.getLogger().hasHandlers() and not force_setup:
		return logging.getLogger('agent')

	# Очистить существующие обработчики
	root_logger = logging.getLogger()
	root_logger.handlers = []

	class AgentFormatter(logging.Formatter):
		def __init__(self, format_string, level_value):
			super().__init__(format_string)
			self.level_value = level_value

		def format(self, log_record):
			# Очищать имена только в режиме INFO, сохранять все в режиме DEBUG
			if self.level_value > logging.DEBUG and isinstance(log_record.name, str) and log_record.name.startswith('core.'):
				# Извлечь чистые имена компонентов из имен логгеров
				if 'Agent' in log_record.name:
					log_record.name = 'Agent'
				elif 'BrowserSession' in log_record.name:
					log_record.name = 'BrowserSession'
				elif 'tools' in log_record.name:
					log_record.name = 'tools'
				elif 'dom' in log_record.name:
					log_record.name = 'dom'
				elif log_record.name.startswith('core.'):
					# Для других модулей agent использовать последнюю часть
					name_parts = log_record.name.split('.')
					if len(name_parts) >= 2:
						log_record.name = name_parts[-1]
			return super().format(log_record)

	# Настроить единственный обработчик для всех логгеров
	console_handler = logging.StreamHandler(stream or sys.stdout)

	# Определить уровень логирования для использования сначала
	if level_type == 'result':
		effective_level = 35  # Значение уровня RESULT
	elif level_type == 'debug':
		effective_level = logging.DEBUG
	else:
		effective_level = logging.INFO

	# Дополнительный setLevel здесь для фильтрации логов
	if level_type == 'result':
		console_handler.setLevel('RESULT')
		console_handler.setFormatter(AgentFormatter('%(message)s', effective_level))
	else:
		console_handler.setLevel(effective_level)  # Сохранить консоль на исходном уровне логирования (например, INFO)
		console_handler.setFormatter(AgentFormatter('%(levelname)-8s [%(name)s] %(message)s', effective_level))

	# Настроить только корневой логгер
	root_logger.addHandler(console_handler)

	# Добавить файловые обработчики, если указаны
	file_handler_list = []

	# Создать обработчик файла логов debug
	if debug_log_file:
		debug_file_handler = logging.FileHandler(debug_log_file, encoding='utf-8')
		debug_file_handler.setLevel(logging.DEBUG)
		debug_file_handler.setFormatter(AgentFormatter('%(asctime)s - %(levelname)-8s [%(name)s] %(message)s', logging.DEBUG))
		file_handler_list.append(debug_file_handler)
		root_logger.addHandler(debug_file_handler)

	# Создать обработчик файла логов info
	if info_log_file:
		info_file_handler = logging.FileHandler(info_log_file, encoding='utf-8')
		info_file_handler.setLevel(logging.INFO)
		info_file_handler.setFormatter(AgentFormatter('%(asctime)s - %(levelname)-8s [%(name)s] %(message)s', logging.INFO))
		file_handler_list.append(info_file_handler)
		root_logger.addHandler(info_file_handler)

	# Настроить корневой логгер - использовать DEBUG, если включено логирование в файл debug
	final_log_level = logging.DEBUG if debug_log_file else effective_level
	root_logger.setLevel(final_log_level)

	# Настроить логгер agent
	main_logger = logging.getLogger('agent')
	main_logger.propagate = False  # Не распространять на корневой логгер
	main_logger.addHandler(console_handler)
	for file_handler in file_handler_list:
		main_logger.addHandler(file_handler)
	main_logger.setLevel(final_log_level)

	# Настроить логгер bubus для разрешения логов уровня INFO
	bubus_main_logger = logging.getLogger('bubus')
	bubus_main_logger.propagate = False  # Не распространять на корневой логгер
	bubus_main_logger.addHandler(console_handler)
	for file_handler in file_handler_list:
		bubus_main_logger.addHandler(file_handler)
	bubus_main_logger.setLevel(logging.INFO if level_type == 'result' else final_log_level)

	# Настроить логирование CDP используя функцию setup из cdp_use
	# Это включает форматированный вывод CDP используя переменную окружения CDP_LOGGING_LEVEL
	# Преобразовать строку CDP_LOGGING_LEVEL в уровень логирования
	cdp_level_string = CONFIG.CDP_LOGGING_LEVEL.upper()
	cdp_logging_level = getattr(logging, cdp_level_string, logging.WARNING)

	try:
		from cdp_use.logging import setup_cdp_logging  # type: ignore

		# Использовать специфичный для CDP уровень логирования
		setup_cdp_logging(
			level=cdp_logging_level,
			stream=stream or sys.stdout,
			format_string='%(levelname)-8s [%(name)s] %(message)s' if level_type != 'result' else '%(message)s',
		)
	except ImportError:
		# Если cdp_use не имеет нового модуля логирования, использовать запасной вариант ручной настройки
		cdp_logger_names = [
			'websockets.client',
			'cdp_use',
			'cdp_use.client',
			'cdp_use.cdp',
			'cdp_use.cdp.registry',
		]
		for cdp_logger_name in cdp_logger_names:
			cdp_logger_instance = logging.getLogger(cdp_logger_name)
			cdp_logger_instance.setLevel(cdp_logging_level)
			cdp_logger_instance.addHandler(console_handler)
			cdp_logger_instance.propagate = False

	result_logger = logging.getLogger('agent')
	# result_logger.debug('Agent logging setup complete with level %s', level_type)

	# Заглушить логгеры сторонних библиотек (но не CDP, которые мы настроили выше)
	external_logger_names = [
		'WDM',
		'httpx',
		'selenium',
		'playwright',
		'urllib3',
		'asyncio',
		'langsmith',
		'langsmith.client',
		'openai',
		'httpcore',
		'charset_normalizer',
		'anthropic._base_client',
		'PIL.PngImagePlugin',
		'trafilatura.htmlprocessing',
		'trafilatura',
		'groq',
		'portalocker',
		'google_genai',
		'portalocker.utils',
		'websockets',  # Общие websockets (но не websockets.client, который нам нужен)
	]
	for external_logger_name in external_logger_names:
		external_logger = logging.getLogger(external_logger_name)
		external_logger.setLevel(logging.ERROR)
		external_logger.propagate = False

	return result_logger


class FIFOHandler(logging.Handler):
	"""Неблокирующий обработчик, который записывает в именованный канал."""

	def __init__(self, pipe_path: str):
		super().__init__()
		self.pipe_path = pipe_path
		Path(pipe_path).parent.mkdir(parents=True, exist_ok=True)

		# Создать FIFO, если он не существует
		if not os.path.exists(pipe_path):
			os.mkfifo(pipe_path)

		# Не открывать FIFO пока - откроется при первой записи
		self.file_descriptor = None

	def emit(self, log_record):
		try:
			# Открыть FIFO при первой записи, если еще не открыт
			if self.file_descriptor is None:
				try:
					self.file_descriptor = os.open(self.pipe_path, os.O_WRONLY | os.O_NONBLOCK)
				except OSError:
					# Читатель еще не подключен, пропустить это сообщение
					return

			message_bytes = f'{self.format(log_record)}\n'.encode()
			os.write(self.file_descriptor, message_bytes)
		except (OSError, BrokenPipeError):
			# Читатель отключен, закрыть и сбросить
			if self.file_descriptor is not None:
				try:
					os.close(self.file_descriptor)
				except Exception:
					pass
				self.file_descriptor = None

	def close(self):
		if hasattr(self, 'file_descriptor') and self.file_descriptor is not None:
			try:
				os.close(self.file_descriptor)
			except Exception:
				pass
		super().close()


def setup_log_pipes(session_identifier: str, base_directory: str | None = None):
	"""Настроить именованные каналы для потоковой передачи логов.

	Использование:
		# В оригинале уровень INFO дополнительно цветом не выделялся
		setup_log_pipes(session_identifier="abc123")

		# В процессе-потребителе:
		tail -f {temp_dir}/buagent.c123/core.pipe
	"""
	import tempfile

	if base_directory is None:
		base_directory = tempfile.gettempdir()

	session_suffix = session_identifier[-4:]
	pipes_directory = Path(base_directory) / f'buagent.{session_suffix}'

	# Логи agent
	agent_pipe_handler = FIFOHandler(str(pipes_directory / 'core.pipe'))
	agent_pipe_handler.setLevel(logging.DEBUG)
	agent_pipe_handler.setFormatter(logging.Formatter('%(levelname)-8s [%(name)s] %(message)s'))
	for logger_name in ['core.agent', 'core.tools']:
		pipe_logger = logging.getLogger(logger_name)
		pipe_logger.addHandler(agent_pipe_handler)
		pipe_logger.setLevel(logging.DEBUG)
		pipe_logger.propagate = True

	# Логи CDP
	cdp_pipe_handler = FIFOHandler(str(pipes_directory / 'cdp.pipe'))
	cdp_pipe_handler.setLevel(logging.DEBUG)
	cdp_pipe_handler.setFormatter(logging.Formatter('%(levelname)-8s [%(name)s] %(message)s'))
	for logger_name in ['websockets.client', 'cdp_use.client']:
		pipe_logger = logging.getLogger(logger_name)
		pipe_logger.addHandler(cdp_pipe_handler)
		pipe_logger.setLevel(logging.DEBUG)
		pipe_logger.propagate = True

	# Логи событий
	event_pipe_handler = FIFOHandler(str(pipes_directory / 'events.pipe'))
	event_pipe_handler.setLevel(logging.INFO)
	event_pipe_handler.setFormatter(logging.Formatter('%(levelname)-8s [%(name)s] %(message)s'))
	for logger_name in ['bubus', 'core.session.session']:
		pipe_logger = logging.getLogger(logger_name)
		pipe_logger.addHandler(event_pipe_handler)
		pipe_logger.setLevel(logging.INFO)  # Включить INFO для шины событий
		pipe_logger.propagate = True
