"""Класс Mouse для операций с мышью."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from cdp_use.cdp.input.commands import DispatchMouseEventParameters, SynthesizeScrollGestureParameters
	from cdp_use.cdp.input.types import MouseButton

	from core.session.session import BrowserSession


class Mouse:
	"""Операции с мышью для target."""

	def __init__(self, browser_session: 'BrowserSession', session_id: str | None = None, target_id: str | None = None):
		self._browser_session = browser_session
		self._client = browser_session.cdp_client
		self._session_id = session_id
		self._target_id = target_id

	async def click(self, x: int, y: int, button: 'MouseButton' = 'left', click_count: int = 1) -> None:
		"""Нажать на указанных координатах."""
		# Нажатие кнопки мыши
		mouse_press: 'DispatchMouseEventParameters' = {
			'type': 'mousePressed',
			'x': x,
			'y': y,
			'button': button,
			'clickCount': click_count,
		}
		await self._client.send.Input.dispatchMouseEvent(
			mouse_press,
			session_id=self._session_id,
		)

		# Отпускание кнопки мыши
		mouse_release: 'DispatchMouseEventParameters' = {
			'type': 'mouseReleased',
			'x': x,
			'y': y,
			'button': button,
			'clickCount': click_count,
		}
		await self._client.send.Input.dispatchMouseEvent(
			mouse_release,
			session_id=self._session_id,
		)

	async def down(self, button: 'MouseButton' = 'left', click_count: int = 1) -> None:
		"""Нажать кнопку мыши."""
		down_params: 'DispatchMouseEventParameters' = {
			'type': 'mousePressed',
			'x': 0,  # Будет использована последняя позиция мыши
			'y': 0,
			'button': button,
			'clickCount': click_count,
		}
		await self._client.send.Input.dispatchMouseEvent(
			down_params,
			session_id=self._session_id,
		)

	async def up(self, button: 'MouseButton' = 'left', click_count: int = 1) -> None:
		"""Отпустить кнопку мыши."""
		up_params: 'DispatchMouseEventParameters' = {
			'type': 'mouseReleased',
			'x': 0,  # Будет использована последняя позиция мыши
			'y': 0,
			'button': button,
			'clickCount': click_count,
		}
		await self._client.send.Input.dispatchMouseEvent(
			up_params,
			session_id=self._session_id,
		)

	async def move(self, x: int, y: int, steps: int = 1) -> None:
		"""Переместить мышь на указанные координаты."""
		# Примечание: можно реализовать плавное движение с несколькими шагами при необходимости
		_ = steps  # Признать параметр для будущего использования

		move_params: 'DispatchMouseEventParameters' = {'type': 'mouseMoved', 'x': x, 'y': y}
		await self._client.send.Input.dispatchMouseEvent(move_params, session_id=self._session_id)

	async def scroll(self, x: int = 0, y: int = 0, delta_x: int | None = None, delta_y: int | None = None) -> None:
		"""Прокрутить страницу, используя надежные методы CDP."""
		if not self._session_id:
			raise RuntimeError('Session ID is required for scroll operations')

		# Метод 1: Попытаться использовать событие колесика мыши (наиболее надежно)
		try:
			# Получить размеры viewport
			viewport_metrics = await self._client.send.Page.getLayoutMetrics(session_id=self._session_id)
			view_width = viewport_metrics['layoutViewport']['clientWidth']
			view_height = viewport_metrics['layoutViewport']['clientHeight']

			# Использовать предоставленные координаты или центр viewport
			wheel_x = x if x > 0 else view_width / 2
			wheel_y = y if y > 0 else view_height / 2

			# Вычислить дельты прокрутки (положительное = вниз/вправо)
			wheel_delta_x = delta_x or 0
			wheel_delta_y = delta_y or 0

			# Отправить событие колесика мыши
			await self._client.send.Input.dispatchMouseEvent(
				params={
					'type': 'mouseWheel',
					'x': wheel_x,
					'y': wheel_y,
					'deltaX': wheel_delta_x,
					'deltaY': wheel_delta_y,
				},
				session_id=self._session_id,
			)
			return

		except Exception:
			pass

		# Метод 2: Запасной вариант - synthesizeScrollGesture
		try:
			gesture_params: 'SynthesizeScrollGestureParameters' = {'x': x, 'y': y, 'xDistance': delta_x or 0, 'yDistance': delta_y or 0}
			await self._client.send.Input.synthesizeScrollGesture(
				gesture_params,
				session_id=self._session_id,
			)
		except Exception:
			# Метод 3: JavaScript запасной вариант
			js_code = f'window.scrollBy({delta_x or 0}, {delta_y or 0})'
			await self._client.send.Runtime.evaluate(
				params={'expression': js_code, 'returnByValue': True},
				session_id=self._session_id,
			)
