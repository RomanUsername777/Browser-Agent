"""Контроллер браузера - слой абстракции над прямыми вызовами CDP."""

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from core.session.session import BrowserSession


class BrowserController:
	"""Контроллер браузера - слой абстракции над прямыми вызовами CDP."""
	
	def __init__(self, browser_session: 'BrowserSession'):
		self.browser_session = browser_session
	
	async def resolve_node(self, cdp_connection, backend_node_id: int) -> dict | None:
		"""Разрешает узел DOM по backendNodeId"""
		try:
			resolved_node = await cdp_connection.cdp_client.send.DOM.resolveNode(
				params={'backendNodeId': backend_node_id},
				session_id=cdp_connection.session_id
			)
			return resolved_node
		except Exception as e:
			return None
	
	async def call_function_on(
		self, 
		cdp_connection, 
		object_id: str, 
		function_declaration: str, 
		return_by_value: bool = True,
		arguments: list[dict] | None = None
	) -> dict | None:
		"""Вызывает функцию на объекте через Runtime.callFunctionOn"""
		try:
			params = {
				'objectId': object_id,
				'functionDeclaration': function_declaration,
				'returnByValue': return_by_value,
			}
			if arguments:
				params['arguments'] = arguments
			
			result = await cdp_connection.cdp_client.send.Runtime.callFunctionOn(
				params=params,
				session_id=cdp_connection.session_id,
			)
			return result
		except Exception as e:
			return None
	
	async def dispatch_mouse_event(
		self, 
		cdp_connection, 
		x: float, 
		y: float, 
		button: str = 'left', 
		click_count: int = 1, 
		event_type: str = 'mousePressed'
	) -> None:
		"""Отправляет событие мыши через CDP"""
		await cdp_connection.cdp_client.send.Input.dispatchMouseEvent(
			params={
				'x': x,
				'y': y,
				'button': button,
				'clickCount': click_count,
				'type': event_type,
			},
			session_id=cdp_connection.session_id,
		)
	
	async def get_layout_metrics(self, cdp_session) -> dict:
		"""Получает метрики layout страницы"""
		layout_metrics = await cdp_session.cdp_client.send.Page.getLayoutMetrics(session_id=cdp_session.session_id)
		return layout_metrics
	
	async def scroll_into_view(self, cdp_connection, backend_node_id: int) -> None:
		"""Прокручивает элемент в видимую область"""
		await cdp_connection.cdp_client.send.DOM.scrollIntoViewIfNeeded(
			params={'backendNodeId': backend_node_id},
			session_id=cdp_connection.session_id,
		)
	
	async def generate_pdf(self, cdp_connection) -> dict | None:
		"""Генерирует PDF страницы через CDP"""
		try:
			pdf_result = await asyncio.wait_for(
				cdp_connection.cdp_client.send.Page.printToPDF(
					params={
						'printBackground': True,
						'preferCSSPageSize': True,
					},
					session_id=cdp_connection.session_id,
				),
				timeout=15.0,
			)
			return pdf_result
		except Exception as e:
			return None
	
	async def run_if_waiting_for_debugger(self, cdp_session) -> None:
		"""Запускает выполнение, если ожидается отладчик"""
		await cdp_session.cdp_client.send.Runtime.runIfWaitingForDebugger(session_id=cdp_session.session_id)

