"""Менеджер скриншотов для ChromeSession."""

import base64
from pathlib import Path
from typing import TYPE_CHECKING

from cdp_use.cdp.page import CaptureScreenshotParameters

from core.session.events import ScreenshotEvent
from core.observability import observe_debug

if TYPE_CHECKING:
	from core.session.session import ChromeSession


class ScreenshotManager:
	"""Менеджер для работы со скриншотами браузера."""

	def __init__(self, browser_session: 'ChromeSession'):
		self.browser_session = browser_session

	@observe_debug(ignore_input=True, ignore_output=True, name='take_screenshot')
	async def take_screenshot(
		self,
		path: str | None = None,
		full_page: bool = False,
		format: str = 'png',
		quality: int | None = None,
		clip: dict | None = None,
	) -> bytes:
		"""Take a screenshot using CDP.

		Args:
			path: Optional file path to save screenshot
			full_page: Capture entire scrollable page beyond viewport
			format: Image format ('png', 'jpeg', 'webp')
			quality: Quality 0-100 for JPEG format
			clip: Region to capture {'x': int, 'y': int, 'width': int, 'height': int}

		Returns:
			Screenshot data as bytes
		"""
		cdp_session = await self.browser_session.get_or_create_cdp_session()

		# Build parameters dict explicitly to satisfy TypedDict expectations
		params: CaptureScreenshotParameters = {
			'format': format,
			'captureBeyondViewport': full_page,
		}

		if quality is not None and format == 'jpeg':
			params['quality'] = quality

		if clip:
			params['clip'] = {
				'x': clip['x'],
				'y': clip['y'],
				'width': clip['width'],
				'height': clip['height'],
				'scale': 1,
			}

		params = CaptureScreenshotParameters(**params)

		result = await cdp_session.cdp_client.send.Page.captureScreenshot(params=params, session_id=cdp_session.session_id)

		if not result or 'data' not in result:
			raise Exception('Screenshot failed - no data returned')

		screenshot_data = base64.b64decode(result['data'])

		if path:
			Path(path).write_bytes(screenshot_data)

		return screenshot_data

	async def _on_ScreenshotEvent(self, event: ScreenshotEvent) -> str:
		"""Обработчик ScreenshotEvent: делает скриншот через CDP.

		Модель события содержит только full_page и clip, остальных полей (format, quality) нет,
		поэтому используем безопасные значения по умолчанию.
		"""
		try:
			screenshot_data = await self.take_screenshot(
				full_page=event.full_page,
				format='png',
				quality=None,
				clip=event.clip,
			)
			# Возвращаем base64-строку с изображением
			return base64.b64encode(screenshot_data).decode('utf-8')
		except Exception as e:
			self.browser_session.logger.error(f'Failed to capture screenshot: {e}', exc_info=True)
			raise

	async def screenshot_element(
		self,
		selector: str,
		path: str | None = None,
		format: str = 'png',
		quality: int | None = None,
	) -> bytes:
		"""Take a screenshot of a specific element.

		Args:
			selector: CSS selector for the element
			path: Optional file path to save screenshot
			format: Image format ('png', 'jpeg', 'webp')
			quality: Quality 0-100 for JPEG format

		Returns:
			Screenshot data as bytes
		"""

		bounds = await self._get_element_bounds(selector)
		if not bounds:
			raise ValueError(f"Element '{selector}' not found or has no bounds")

		return await self.take_screenshot(
			path=path,
			format=format,
			quality=quality,
			clip=bounds,
		)

	async def _get_element_bounds(self, selector: str) -> dict | None:
		"""Get element bounding box using CDP."""

		cdp_session = await self.browser_session.get_or_create_cdp_session()

		# Get document
		doc = await cdp_session.cdp_client.send.DOM.getDocument(params={'depth': 1}, session_id=cdp_session.session_id)

		# Query selector
		node_result = await cdp_session.cdp_client.send.DOM.querySelector(
			params={'nodeId': doc['root']['nodeId'], 'selector': selector}, session_id=cdp_session.session_id
		)

		node_id = node_result.get('nodeId')
		if not node_id:
			return None

		# Get bounding box
		box_result = await cdp_session.cdp_client.send.DOM.getBoxModel(
			params={'nodeId': node_id}, session_id=cdp_session.session_id
		)

		box_model = box_result.get('model')
		if not box_model:
			return None

		content = box_model['content']
		return {
			'x': min(content[0], content[2], content[4], content[6]),
			'y': min(content[1], content[3], content[5], content[7]),
			'width': max(content[0], content[2], content[4], content[6]) - min(content[0], content[2], content[4], content[6]),
			'height': max(content[1], content[3], content[5], content[7]) - min(content[1], content[3], content[5], content[7]),
		}

