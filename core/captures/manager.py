"""
Пакет для работы со скриншотами агента.

Сервис хранения скриншотов для агента.
"""

import base64
from pathlib import Path

import anyio

from core.observability import observe_debug


class ScreenshotService:
	"""Простой сервис хранения скриншотов, который сохраняет скриншоты на диск"""

	def __init__(self, agent_directory: str | Path):
		"""Инициализировать с путем к директории агента"""
		if isinstance(agent_directory, str):
			self.agent_directory = Path(agent_directory)
		else:
			self.agent_directory = agent_directory

		# Создать поддиректорию для скриншотов
		self.screenshots_dir = self.agent_directory / 'screenshots'
		self.screenshots_dir.mkdir(parents=True, exist_ok=True)

	@observe_debug(ignore_input=True, ignore_output=True, name='store_screenshot')
	async def store_screenshot(self, screenshot_b64: str, step_number: int) -> str:
		"""Сохранить скриншот на диск и вернуть полный путь в виде строки"""
		filename = f'step_{step_number}.png'
		file_path = self.screenshots_dir / filename

		# Декодировать base64 и сохранить на диск
		decoded_image_data = base64.b64decode(screenshot_b64)

		async with await anyio.open_file(file_path, 'wb') as file_handle:
			await file_handle.write(decoded_image_data)

		return str(file_path)

	@observe_debug(ignore_input=True, ignore_output=True, name='get_screenshot_from_disk')
	async def get_screenshot(self, screenshot_path: str) -> str | None:
		"""Загрузить скриншот с диска по пути и вернуть как base64"""
		if not screenshot_path:
			return None

		file_path = Path(screenshot_path)
		if not file_path.exists():
			return None

		# Загрузить с диска и закодировать в base64
		async with await anyio.open_file(file_path, 'rb') as file_handle:
			image_bytes = await file_handle.read()

		return base64.b64encode(image_bytes).decode('utf-8')
