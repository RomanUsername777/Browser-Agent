"""Watchdog локального браузера для управления жизненным циклом подпроцесса браузера."""

import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import psutil
from bubus import BaseEvent
from pydantic import PrivateAttr

from core.session.events import (
	BrowserKillEvent,
	BrowserLaunchEvent,
	BrowserLaunchResult,
	BrowserStopEvent,
)
from core.session.watchdog_base import BaseWatchdog
from core.observability import observe_debug

if TYPE_CHECKING:
	pass


class LocalBrowserWatchdog(BaseWatchdog):
	"""Управляет жизненным циклом подпроцесса локального браузера."""

	# События, на которые этот watchdog реагирует
	LISTENS_TO: ClassVar[list[type[BaseEvent[Any]]]] = [
		BrowserLaunchEvent,
		BrowserKillEvent,
		BrowserStopEvent,
	]

	# События, которые этот watchdog генерирует
	EMITS: ClassVar[list[type[BaseEvent[Any]]]] = []

	# Приватное состояние для управления подпроцессом
	_subprocess: psutil.Process | None = PrivateAttr(default=None)
	_owns_browser_resources: bool = PrivateAttr(default=True)
	_temp_dirs_to_cleanup: list[Path] = PrivateAttr(default_factory=list)
	_original_user_data_dir: str | None = PrivateAttr(default=None)

	@observe_debug(ignore_input=True, ignore_output=True, name='browser_launch_event')
	async def on_BrowserLaunchEvent(self, event: BrowserLaunchEvent) -> BrowserLaunchResult:
		"""Запустить процесс локального браузера."""

		try:
			self.logger.debug('[LocalBrowserWatchdog] Received BrowserLaunchEvent, launching local browser...')

			# self.logger.debug('[LocalBrowserWatchdog] Calling _launch_browser...')
			browser_process, cdp_endpoint = await self._launch_browser()
			self._subprocess = browser_process
			# self.logger.debug(f'[LocalBrowserWatchdog] _launch_browser returned: process={browser_process}, cdp_url={cdp_endpoint}')

			return BrowserLaunchResult(cdp_url=cdp_endpoint)
		except Exception as e:
			self.logger.error(f'[LocalBrowserWatchdog] Exception in on_BrowserLaunchEvent: {e}', exc_info=True)
			raise

	async def on_BrowserKillEvent(self, event: BrowserKillEvent) -> None:
		"""Убить подпроцесс локального браузера."""
		self.logger.debug('[LocalBrowserWatchdog] Killing local browser process')

		if self._subprocess:
			await self._cleanup_process(self._subprocess)
			self._subprocess = None

		# Очистить временные директории, если они были созданы
		for temp_directory in self._temp_dirs_to_cleanup:
			self._cleanup_temp_dir(temp_directory)
		self._temp_dirs_to_cleanup.clear()

		# Восстановить оригинальный user_data_dir, если он был изменен
		if self._original_user_data_dir is not None:
			self.browser_session.browser_profile.user_data_dir = self._original_user_data_dir
			self._original_user_data_dir = None

		self.logger.debug('[LocalBrowserWatchdog] Browser cleanup completed')

	async def on_BrowserStopEvent(self, event: BrowserStopEvent) -> None:
		"""Прослушивать BrowserStopEvent и отправлять BrowserKillEvent без ожидания."""
		if self.browser_session.is_local and self._subprocess:
			self.logger.debug('[LocalBrowserWatchdog] BrowserStopEvent received, dispatching BrowserKillEvent')
			# Отправить BrowserKillEvent без ожидания, чтобы он обработался после всех обработчиков BrowserStopEvent
			self.event_bus.dispatch(BrowserKillEvent())

	@observe_debug(ignore_input=True, ignore_output=True, name='launch_browser_process')
	async def _launch_browser(self, max_retries: int = 3) -> tuple[psutil.Process, str]:
		"""Запустить процесс браузера и вернуть (process, cdp_url).

		Обрабатывает ошибки запуска, при необходимости используя временные директории.

		Returns:
			Кортеж (psutil.Process, cdp_url)
		"""
		# Отслеживать оригинальный user_data_dir для восстановления при необходимости
		browser_profile = self.browser_session.browser_profile
		self._original_user_data_dir = str(browser_profile.user_data_dir) if browser_profile.user_data_dir else None
		self._temp_dirs_to_cleanup = []

		for retry_attempt in range(max_retries):
			try:
				# Получить аргументы запуска из профиля
				chrome_args = browser_profile.get_args()

				# Добавить порт отладки
				cdp_port = self._find_free_port()
				chrome_args.extend(
					[
						f'--remote-debugging-port={cdp_port}',
					]
				)
				assert '--user-data-dir' in str(chrome_args), (
					'User data dir must be set somewhere in launch args to a non-default path, otherwise Chrome will not let us attach via CDP'
				)

				# Получить исполняемый файл браузера
				# Приоритет: пользовательский исполняемый файл > пути запасного варианта > подпроцесс playwright
				if browser_profile.executable_path:
					executable = browser_profile.executable_path
					self.logger.debug(f'[LocalBrowserWatchdog] 📦 Using custom local browser executable_path= {executable}')
				else:
					# self.logger.debug('[LocalBrowserWatchdog] 🔍 Looking for local browser binary path...')
					# Сначала попробовать пути запасного варианта (предпочтительны системные браузеры)
					executable = self._find_installed_browser_path()
					if not executable:
						self.logger.error(
							'[LocalBrowserWatchdog] ⚠️ No local browser binary found, installing browser using playwright subprocess...'
						)
						executable = await self._install_browser_with_playwright()

				self.logger.debug(f'[LocalBrowserWatchdog] 📦 Found local browser installed at executable_path= {executable}')
				if not executable:
					raise RuntimeError('No local Chrome/Chromium install found, and failed to install with playwright')

				# Запустить подпроцесс браузера напрямую
				self.logger.debug(f'[LocalBrowserWatchdog] 🚀 Launching browser subprocess with {len(chrome_args)} args...')
				self.logger.debug(
					f'[LocalBrowserWatchdog] 📂 user_data_dir={browser_profile.user_data_dir}, profile_directory={browser_profile.profile_directory}'
				)
				browser_subprocess = await asyncio.create_subprocess_exec(
					executable,
					*chrome_args,
					stdout=asyncio.subprocess.PIPE,
					stderr=asyncio.subprocess.PIPE,
				)
				self.logger.debug(
					f'[LocalBrowserWatchdog] 🎭 Browser running with browser_pid= {browser_subprocess.pid} 🔗 listening on CDP port :{cdp_port}'
				)

				# Преобразовать в psutil.Process
				browser_process = psutil.Process(browser_subprocess.pid)

				# Подождать готовности CDP и получить URL
				cdp_endpoint = await self._wait_for_cdp_url(cdp_port)

				# Успех! Очистить только временные директории, которые мы создали, но не использовали
				active_dir = str(browser_profile.user_data_dir)
				unused_dirs = [tmp_path for tmp_path in self._temp_dirs_to_cleanup if str(tmp_path) != active_dir]

				for unused_dir in unused_dirs:
					try:
						shutil.rmtree(unused_dir, ignore_errors=True)
					except Exception:
						pass

				# Оставить только используемую директорию для очистки при убийстве браузера
				if active_dir and 'agent-tmp-' in active_dir:
					self._temp_dirs_to_cleanup = [Path(active_dir)]
				else:
					self._temp_dirs_to_cleanup = []

				return browser_process, cdp_endpoint

			except Exception as launch_error:
				error_message = str(launch_error).lower()

				# Проверить, является ли это ошибкой, связанной с user_data_dir
				if any(error_keyword in error_message for error_keyword in ['singletonlock', 'user data directory', 'cannot create', 'already in use']):
					self.logger.warning(f'Browser launch failed (attempt {retry_attempt + 1}/{max_retries}): {launch_error}')

					if retry_attempt < max_retries - 1:
						# Создать временную директорию для следующей попытки
						temp_directory = Path(tempfile.mkdtemp(prefix='agent-tmp-'))
						self._temp_dirs_to_cleanup.append(temp_directory)

						# Обновить профиль для использования временной директории
						browser_profile.user_data_dir = str(temp_directory)
						self.logger.debug(f'Retrying with temporary user_data_dir: {temp_directory}')

						# Небольшая задержка перед повторной попыткой
						await asyncio.sleep(0.5)
						continue

				# Неисправимая ошибка или последняя попытка не удалась
				# Восстановить оригинальный user_data_dir перед выбросом исключения
				if self._original_user_data_dir is not None:
					browser_profile.user_data_dir = self._original_user_data_dir

				# Очистить все временные директории, которые мы создали
				for temp_directory in self._temp_dirs_to_cleanup:
					try:
						shutil.rmtree(temp_directory, ignore_errors=True)
					except Exception:
						pass

				raise

		# Не должно дойти до этого места, но на всякий случай
		if self._original_user_data_dir is not None:
			browser_profile.user_data_dir = self._original_user_data_dir
		raise RuntimeError(f'Failed to launch browser after {max_retries} attempts')

	@staticmethod
	def _find_installed_browser_path() -> str | None:
		"""Попытаться найти исполняемый файл браузера из распространенных мест запасного варианта.

		Приоритеты:
		1. Системный Chrome Stable
		2. Другие установленные браузеры (Chromium -> Chrome Canary/Dev -> Brave)
		3. Локальные бинарники, установленные через playwright (если есть)

		Returns:
			Путь к исполняемому файлу браузера или None, если не найден
		"""
		import glob
		import platform
		from pathlib import Path

		platform_type = platform.system()
		path_patterns = []

		# Получить путь браузеров playwright из переменной окружения, если установлена
		playwright_base_path = os.environ.get('PLAYWRIGHT_BROWSERS_PATH')

		if platform_type == 'Darwin':  # macOS
			if not playwright_base_path:
				playwright_base_path = '~/Library/Caches/ms-playwright'
			path_patterns = [
				'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
				f'{playwright_base_path}/chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium',
				'/Applications/Chromium.app/Contents/MacOS/Chromium',
				'/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary',
				'/Applications/Brave Browser.app/Contents/MacOS/Brave Browser',
				f'{playwright_base_path}/chromium_headless_shell-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium',
			]
		elif platform_type == 'Linux':
			if not playwright_base_path:
				playwright_base_path = '~/.cache/ms-playwright'
			path_patterns = [
				'/usr/bin/google-chrome-stable',
				'/usr/bin/google-chrome',
				'/usr/local/bin/google-chrome',
				f'{playwright_base_path}/chromium-*/chrome-linux/chrome',
				'/usr/bin/chromium',
				'/usr/bin/chromium-browser',
				'/usr/local/bin/chromium',
				'/snap/bin/chromium',
				'/usr/bin/google-chrome-beta',
				'/usr/bin/google-chrome-dev',
				'/usr/bin/brave-browser',
				f'{playwright_base_path}/chromium_headless_shell-*/chrome-linux/chrome',
			]
		elif platform_type == 'Windows':
			if not playwright_base_path:
				playwright_base_path = r'%LOCALAPPDATA%\ms-playwright'
			path_patterns = [
				r'C:\Program Files\Google\Chrome\Application\chrome.exe',
				r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
				r'%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe',
				r'%PROGRAMFILES%\Google\Chrome\Application\chrome.exe',
				r'%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe',
				f'{playwright_base_path}\\chromium-*\\chrome-win\\chrome.exe',
				r'C:\Program Files\Chromium\Application\chrome.exe',
				r'C:\Program Files (x86)\Chromium\Application\chrome.exe',
				r'%LOCALAPPDATA%\Chromium\Application\chrome.exe',
				r'C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe',
				r'C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe',
				r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
				r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
				r'%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe',
				f'{playwright_base_path}\\chromium_headless_shell-*\\chrome-win\\chrome.exe',
			]

		for path_pattern in path_patterns:
			# Развернуть домашнюю директорию пользователя
			resolved_pattern = Path(path_pattern).expanduser()

			# Обработать переменные окружения Windows
			if platform_type == 'Windows':
				pattern_string = str(resolved_pattern)
				for env_variable in ['%LOCALAPPDATA%', '%PROGRAMFILES%', '%PROGRAMFILES(X86)%']:
					if env_variable in pattern_string:
						env_name = env_variable.strip('%').replace('(X86)', ' (x86)')
						env_value = os.environ.get(env_name, '')
						if env_value:
							pattern_string = pattern_string.replace(env_variable, env_value)
				resolved_pattern = Path(pattern_string)

			# Преобразовать в строку для glob
			pattern_string = str(resolved_pattern)

			# Проверить, содержит ли паттерн подстановочные знаки
			if '*' in pattern_string:
				# Использовать glob для развертывания паттерна
				matched_paths = glob.glob(pattern_string)
				if matched_paths:
					# Сортировать совпадения и взять последнее (наивысшая версия в алфавитно-цифровом порядке)
					matched_paths.sort()
					executable_path = matched_paths[-1]
					if Path(executable_path).exists() and Path(executable_path).is_file():
						return executable_path
			else:
				# Прямая проверка пути
				if resolved_pattern.exists() and resolved_pattern.is_file():
					return str(resolved_pattern)

		return None

	async def _install_browser_with_playwright(self) -> str:
		"""Получить путь к исполняемому файлу браузера из playwright в подпроцессе, чтобы избежать проблем с потоками."""
		import platform

		# Собрать команду - использовать --with-deps только на Linux (не работает на Windows/macOS)
		install_command = ['uvx', 'playwright', 'install', 'chrome']
		if platform.system() == 'Linux':
			install_command.append('--with-deps')

		# Запустить в подпроцессе с таймаутом
		install_process = await asyncio.create_subprocess_exec(
			*install_command,
			stdout=asyncio.subprocess.PIPE,
			stderr=asyncio.subprocess.PIPE,
		)

		try:
			stdout_bytes, stderr_bytes = await asyncio.wait_for(install_process.communicate(), timeout=60.0)
			self.logger.debug(f'[LocalBrowserWatchdog] 📦 playwright install output: {stdout_bytes}')
			executable = self._find_installed_browser_path()
			if executable:
				return executable
			self.logger.error(f'[LocalBrowserWatchdog] ❌ playwright local browser installation error: \n{stdout_bytes}\n{stderr_bytes}')
			raise RuntimeError('No local browser path found after: uvx playwright install chrome')
		except TimeoutError:
			# Убить подпроцесс, если он превысил таймаут
			install_process.kill()
			await install_process.wait()
			raise RuntimeError('Timeout getting browser path from playwright')
		except Exception as install_error:
			# Убедиться, что подпроцесс завершен
			if install_process.returncode is None:
				install_process.kill()
				await install_process.wait()
			raise RuntimeError(f'Error getting browser path: {install_error}')

	@staticmethod
	def _find_free_port() -> int:
		"""Найти свободный порт для интерфейса отладки."""
		import socket

		with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as socket_handle:
			socket_handle.bind(('127.0.0.1', 0))
			socket_handle.listen(1)
			free_port = socket_handle.getsockname()[1]
		return free_port

	@staticmethod
	async def _wait_for_cdp_url(cdp_port: int, timeout: float = 30) -> str:
		"""Подождать запуска браузера и вернуть CDP URL."""
		import aiohttp

		begin_time = asyncio.get_event_loop().time()

		while asyncio.get_event_loop().time() - begin_time < timeout:
			try:
				async with aiohttp.ClientSession() as http_session:
					async with http_session.get(f'http://127.0.0.1:{cdp_port}/json/version') as http_response:
						if http_response.status == 200:
							# Chrome готов
							return f'http://127.0.0.1:{cdp_port}/'
						else:
							# Chrome запускается и возвращает ошибки 502/500
							await asyncio.sleep(0.1)
			except Exception:
				# Ошибка соединения - Chrome может быть еще не готов
				await asyncio.sleep(0.1)

		raise TimeoutError(f'Browser did not start within {timeout} seconds')

	@staticmethod
	async def _cleanup_process(browser_process: psutil.Process) -> None:
		"""Очистить процесс браузера.

		Args:
			browser_process: psutil.Process для завершения
		"""
		if not browser_process:
			return

		try:
			# Сначала попробовать корректное завершение
			browser_process.terminate()

			# Использовать асинхронное ожидание вместо блокирующего
			for _ in range(50):  # Ждать до 5 секунд (50 * 0.1)
				if not browser_process.is_running():
					return
				await asyncio.sleep(0.1)

			# Если все еще работает после 5 секунд, принудительно убить
			if browser_process.is_running():
				browser_process.kill()
				# Дать немного времени на завершение
				await asyncio.sleep(0.1)

		except psutil.NoSuchProcess:
			# Процесс уже завершен
			pass
		except Exception:
			# Игнорировать любые другие ошибки при очистке
			pass

	def _cleanup_temp_dir(self, temp_directory: Path | str) -> None:
		"""Очистить временную директорию.

		Args:
			temp_directory: Путь к временной директории для удаления
		"""
		if not temp_directory:
			return

		try:
			directory_path = Path(temp_directory)
			# Удалять только если это действительно временная директория, которую мы создали
			if 'agent-tmp-' in str(directory_path):
				shutil.rmtree(directory_path, ignore_errors=True)
		except Exception as cleanup_error:
			self.logger.debug(f'Failed to cleanup temp dir {temp_directory}: {cleanup_error}')

	@property
	def browser_pid(self) -> int | None:
		"""Получить ID процесса браузера."""
		if self._subprocess:
			return self._subprocess.pid
		return None

	@staticmethod
	async def get_browser_pid_via_cdp(browser) -> int | None:
		"""Получить ID процесса браузера через CDP SystemInfo.getProcessInfo.

		Args:
			browser: экземпляр браузера, совместимый с интерфейсом Playwright Browser

		Returns:
			ID процесса или None, если не удалось
		"""
		try:
			cdp_connection = await browser.new_browser_cdp_session()
			cdp_result = await cdp_connection.send('SystemInfo.getProcessInfo')
			process_data = cdp_result.get('processInfo', {})
			process_id = process_data.get('id')
			await cdp_connection.detach()
			return process_id
		except Exception:
			# Если не удалось получить PID через CDP, это не критично
			return None
