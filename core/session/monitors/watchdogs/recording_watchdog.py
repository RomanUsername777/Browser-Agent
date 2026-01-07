"""Recording Watchdog для сессий агента."""

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from bubus import BaseEvent
from cdp_use.cdp.page.events import ScreencastFrameEvent
from uuid_extensions import uuid7str

from core.session.events import BrowserConnectedEvent, BrowserStopEvent
from core.session.profile import ViewportSize
from core.session.watchdog_base import BaseWatchdog
from core.helpers import create_task_with_error_handling

# Video recorder is optional - only needed if video recording is enabled
try:
	from core.session.video_recorder import VideoRecorderService
except ImportError:
	VideoRecorderService = None  # type: ignore


class RecordingWatchdog(BaseWatchdog):
	"""
	Manages video recording of a browser session using CDP screencasting.
	"""

	LISTENS_TO: ClassVar[list[type[BaseEvent]]] = [BrowserConnectedEvent, BrowserStopEvent]
	EMITS: ClassVar[list[type[BaseEvent]]] = []

	_recorder: Any = None

	async def on_BrowserConnectedEvent(self, event: BrowserConnectedEvent) -> None:
		"""
		Запустить запись видео, если она настроена в профиле браузера.
		"""
		browser_profile = self.browser_session.browser_profile
		if not browser_profile.record_video_dir:
			return

		# Динамически определить размер видео
		video_size = browser_profile.record_video_size
		if not video_size:
			self.logger.debug('record_video_size not specified, detecting viewport size...')
			video_size = await self._get_current_viewport_size()

		if not video_size:
			self.logger.warning('Cannot start video recording: viewport size could not be determined.')
			return

		if VideoRecorderService is None:
			self.logger.warning('Video recording requested but VideoRecorderService is not available')
			return

		format_value = getattr(browser_profile, 'record_video_format', 'mp4').strip('.')
		video_file_path = Path(browser_profile.record_video_dir) / f'{uuid7str()}.{format_value}'

		self.logger.debug(f'Initializing video recorder for format: {format_value}')
		self._recorder = VideoRecorderService(output_path=video_file_path, size=video_size, framerate=browser_profile.record_video_framerate)
		self._recorder.start()

		if not self._recorder._is_active:
			self._recorder = None
			return

		self.browser_session.cdp_client.register.Page.screencastFrame(self.on_screencastFrame)

		try:
			cdp_connection = await self.browser_session.get_or_create_cdp_session()
			await cdp_connection.cdp_client.send.Page.startScreencast(
				params={
					'format': 'png',
					'quality': 90,
					'maxWidth': video_size['width'],
					'maxHeight': video_size['height'],
					'everyNthFrame': 1,
				},
				session_id=cdp_connection.session_id,
			)
			self.logger.info(f'📹 Started video recording to {video_file_path}')
		except Exception as start_error:
			self.logger.error(f'Failed to start screencast via CDP: {start_error}')
			if self._recorder:
				self._recorder.stop_and_save()
				self._recorder = None

	async def _get_current_viewport_size(self) -> ViewportSize | None:
		"""Получить текущий размер viewport напрямую из браузера через CDP."""
		try:
			cdp_connection = await self.browser_session.get_or_create_cdp_session()
			layout_data = await cdp_connection.cdp_client.send.Page.getLayoutMetrics(session_id=cdp_connection.session_id)

			# Использовать cssVisualViewport для наиболее точного представления видимой области
			visual_viewport = layout_data.get('cssVisualViewport', {})
			viewport_width = visual_viewport.get('clientWidth')
			viewport_height = visual_viewport.get('clientHeight')

			if viewport_width and viewport_height:
				self.logger.debug(f'Detected viewport size: {viewport_width}x{viewport_height}')
				return ViewportSize(width=int(viewport_width), height=int(viewport_height))
		except Exception as metrics_error:
			self.logger.warning(f'Failed to get viewport size from browser: {metrics_error}')

		return None

	def on_screencastFrame(self, frame_event: ScreencastFrameEvent, connection_session_id: str | None) -> None:
		"""
		Синхронный обработчик входящих кадров screencast.
		"""

		if not self._recorder:
			return
		self._recorder.add_frame(frame_event['data'])
		create_task_with_error_handling(
			self._ack_screencast_frame(frame_event, connection_session_id),
			name='ack_screencast_frame',
			logger_instance=self.logger,
			suppress_exceptions=True,
		)

	async def _ack_screencast_frame(self, frame_event: ScreencastFrameEvent, connection_session_id: str | None) -> None:
		"""
		Асинхронно подтверждает кадр screencast.
		"""
		try:
			await self.browser_session.cdp_client.send.Page.screencastFrameAck(
				params={'sessionId': frame_event['sessionId']}, session_id=connection_session_id
			)
		except Exception as ack_error:
			self.logger.debug(f'Failed to acknowledge screencast frame: {ack_error}')

	async def on_BrowserStopEvent(self, event: BrowserStopEvent) -> None:
		"""
		Остановить запись видео и финализировать видеофайл.
		"""
		if self._recorder:
			video_recorder = self._recorder
			self._recorder = None

			self.logger.debug('Stopping video recording and saving file...')
			event_loop = asyncio.get_event_loop()
			await event_loop.run_in_executor(None, video_recorder.stop_and_save)
