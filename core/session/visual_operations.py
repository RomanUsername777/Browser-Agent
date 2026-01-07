"""Менеджер визуальных операций: скриншоты и DOM для BrowserSession."""

import base64
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cdp_use.cdp.page import CaptureScreenshotParameters

from core.dom_processing.models import DOMRect, EnhancedDOMTreeNode
from core.session.events import ScreenshotEvent
from core.observability import observe_debug

if TYPE_CHECKING:
	from core.session.session import BrowserSession


class VisualOperationsManager:
	"""Менеджер для работы со скриншотами и DOM элементами браузера."""

	def __init__(self, browser_session: 'BrowserSession'):
		self.browser_session = browser_session

	# ========== Screenshot Methods ==========

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

	# ========== DOM Helper Methods ==========

	async def get_dom_element_by_index(self, index: int) -> EnhancedDOMTreeNode | None:
		"""Get DOM element by its index from the selector map."""
		selector_map = await self.browser_session.get_selector_map()
		return selector_map.get(index)

	def update_cached_selector_map(self, selector_map: dict[int, EnhancedDOMTreeNode]) -> None:
		"""Update the cached selector map."""
		self.browser_session._cached_selector_map = selector_map

	async def get_element_by_index(self, index: int) -> EnhancedDOMTreeNode | None:
		"""Get element by index. Alias for get_dom_element_by_index."""
		return await self.get_dom_element_by_index(index)

	async def get_dom_element_at_coordinates(self, x: int, y: int) -> EnhancedDOMTreeNode | None:
		"""Get DOM element at specific viewport coordinates.

		This method uses CDP DOM.getNodeForLocation to find the element at the given coordinates.
		It's more reliable than JavaScript-based methods for complex cases (e.g., when elements
		are in shadow DOM, iframes, or have complex z-index stacking) and ensures that special
		elements (like <select> elements and file inputs) work correctly.

		Args:
			x: X coordinate relative to viewport
			y: Y coordinate relative to viewport

		Returns:
			EnhancedDOMTreeNode at the coordinates, or None if no element found
		"""
		from core.dom_processing.models import NodeType

		# Get current page to access CDP session
		page = await self.browser_session._browser_operations.get_current_page()
		if page is None:
			raise RuntimeError('No active page found')

		# Get session ID for CDP call
		session_id = await page._ensure_session()

		try:
			# Call CDP DOM.getNodeForLocation to get backend_node_id
			result = await self.browser_session.cdp_client.send.DOM.getNodeForLocation(
				params={
					'x': x,
					'y': y,
					'includeUserAgentShadowDOM': False,
					'ignorePointerEventsNone': False,
				},
				session_id=session_id,
			)

			backend_node_id = result.get('backendNodeId')
			if backend_node_id is None:
				self.browser_session.logger.debug(f'No element found at coordinates ({x}, {y})')
				return None

			# Try to find element in cached selector_map (avoids extra CDP call)
			if self.browser_session._cached_selector_map:
				for node in self.browser_session._cached_selector_map.values():
					if node.backend_node_id == backend_node_id:
						self.browser_session.logger.debug(f'Found element at ({x}, {y}) in cached selector_map')
						return node

			# Not in cache - fall back to CDP DOM.describeNode to get actual node info
			try:
				describe_result = await self.browser_session.cdp_client.send.DOM.describeNode(
					params={'backendNodeId': backend_node_id},
					session_id=session_id,
				)
				node_info = describe_result.get('node', {})
				node_name = node_info.get('nodeName', '')

				# Parse attributes from flat list [key1, val1, key2, val2, ...] to dict
				attrs_list = node_info.get('attributes', [])
				attributes = {attrs_list[i]: attrs_list[i + 1] for i in range(0, len(attrs_list), 2)}

				return EnhancedDOMTreeNode(
					node_id=result.get('nodeId', 0),
					backend_node_id=backend_node_id,
					node_type=NodeType(node_info.get('nodeType', NodeType.ELEMENT_NODE.value)),
					node_name=node_name,
					node_value=node_info.get('nodeValue', '') or '',
					attributes=attributes,
					is_scrollable=None,
					frame_id=result.get('frameId'),
					session_id=session_id,
					target_id=self.browser_session.agent_focus_target_id or '',
					content_document=None,
					shadow_root_type=None,
					shadow_roots=None,
					parent_node=None,
					children_nodes=None,
					ax_node=None,
					snapshot_node=None,
					is_visible=None,
					absolute_position=None,
				)
			except Exception as e:
				self.browser_session.logger.debug(f'DOM.describeNode failed for backend_node_id={backend_node_id}: {e}')
				# Fall back to minimal node if describeNode fails
				return EnhancedDOMTreeNode(
					node_id=result.get('nodeId', 0),
					backend_node_id=backend_node_id,
					node_type=NodeType.ELEMENT_NODE,
					node_name='',
					node_value='',
					attributes={},
					is_scrollable=None,
					frame_id=result.get('frameId'),
					session_id=session_id,
					target_id=self.browser_session.agent_focus_target_id or '',
					content_document=None,
					shadow_root_type=None,
					shadow_roots=None,
					parent_node=None,
					children_nodes=None,
					ax_node=None,
					snapshot_node=None,
					is_visible=None,
					absolute_position=None,
				)

		except Exception as e:
			self.browser_session.logger.warning(f'Failed to get DOM element at coordinates ({x}, {y}): {e}')
			return None

	def is_file_input(self, element: Any) -> bool:
		"""Check if element is a file input.

		Args:
			element: The DOM element to check

		Returns:
			True if element is a file input, False otherwise
		"""
		if self.browser_session._dom_watchdog:
			return self.browser_session._dom_watchdog.is_file_input(element)
		# Fallback if watchdog not available
		return (
			hasattr(element, 'node_name')
			and element.node_name.upper() == 'INPUT'
			and hasattr(element, 'attributes')
			and element.attributes.get('type', '').lower() == 'file'
		)

	async def get_selector_map(self) -> dict[int, EnhancedDOMTreeNode]:
		"""Get the current selector map from cached state or DOM watchdog.

		Returns:
			Dictionary mapping element indices to EnhancedDOMTreeNode objects
		"""
		# First try cached selector map
		if self.browser_session._cached_selector_map:
			return self.browser_session._cached_selector_map

		# Try to get from DOM watchdog
		if self.browser_session._dom_watchdog and hasattr(self.browser_session._dom_watchdog, 'selector_map'):
			return self.browser_session._dom_watchdog.selector_map or {}

		# Return empty dict if nothing available
		return {}

	async def get_index_by_id(self, element_id: str) -> int | None:
		"""Find element index by its id attribute.

		Args:
			element_id: The id attribute value to search for

		Returns:
			Index of the element, or None if not found
		"""
		selector_map = await self.get_selector_map()
		for idx, element in selector_map.items():
			if element.attributes and element.attributes.get('id') == element_id:
				return idx
		return None

	async def get_index_by_class(self, class_name: str) -> int | None:
		"""Find element index by its class attribute (matches if class contains the given name).

		Args:
			class_name: The class name to search for

		Returns:
			Index of the first matching element, or None if not found
		"""
		selector_map = await self.get_selector_map()
		for idx, element in selector_map.items():
			if element.attributes:
				element_class = element.attributes.get('class', '')
				if class_name in element_class.split():
					return idx
		return None

	async def remove_highlights(self) -> None:
		"""Remove highlights from the page using CDP."""
		if not self.browser_session.browser_profile.highlight_elements:
			return

		try:
			# Get cached session
			cdp_session = await self.browser_session.get_or_create_cdp_session()

			# Remove highlights via JavaScript - be thorough
			script = """
			(function() {
				// Remove all agent highlight elements
				const highlights = document.querySelectorAll('[data-agent-highlight]');
				highlights.forEach(el => el.remove());

				// Also remove by ID in case selector missed anything
				const highlightContainer = document.getElementById('agent-debug-highlights');
			if (highlightContainer) {
				highlightContainer.remove();
				}

				// Final cleanup - remove any orphaned tooltips
				const orphanedTooltips = document.querySelectorAll('[data-agent-highlight="tooltip"]');
				orphanedTooltips.forEach(el => el.remove());

				return { removed: highlights.length };
			})();
			"""
			result = await cdp_session.cdp_client.send.Runtime.evaluate(
				params={'expression': script, 'returnByValue': True}, session_id=cdp_session.session_id
			)

			# Log the result for debugging
			if result and 'result' in result and 'value' in result['result']:
				removed_count = result['result']['value'].get('removed', 0)
				self.browser_session.logger.debug(f'Successfully removed {removed_count} highlight elements')
			else:
				self.browser_session.logger.debug('Highlight removal completed')

		except Exception as e:
			self.browser_session.logger.warning(f'Failed to remove highlights: {e}')

	async def get_element_coordinates(self, backend_node_id: int, cdp_session) -> DOMRect | None:
		"""Get element coordinates for a backend node ID using multiple methods.

		This method tries DOM.getContentQuads first, then falls back to DOM.getBoxModel,
		and finally uses JavaScript getBoundingClientRect as a last resort.

		Args:
			backend_node_id: The backend node ID to get coordinates for
			cdp_session: The CDP session to use

		Returns:
			DOMRect with coordinates or None if element not found/no bounds
		"""
		from core.dom_processing.models import DOMRect

		# Method 1: Try DOM.getContentQuads (most reliable for complex layouts)
		try:
			result = await cdp_session.cdp_client.send.DOM.getContentQuads(
				params={'backendNodeId': backend_node_id}, session_id=cdp_session.session_id
			)

			if result and 'quads' in result and result['quads']:
				# Get the first quad (usually the main content area)
				quad = result['quads'][0]

				# Extract bounding box from quad [x1, y1, x2, y2, x3, y3, x4, y4]
				x_coords = [quad[i] for i in range(0, len(quad), 2)]  # x coordinates
				y_coords = [quad[i + 1] for i in range(0, len(quad), 2)]  # y coordinates

				x = min(x_coords)
				y = min(y_coords)
				width = max(x_coords) - min(x_coords)
				height = max(y_coords) - min(y_coords)

				return DOMRect(x=x, y=y, width=width, height=height)
		except Exception as e:
			self.browser_session.logger.debug(f'DOM.getContentQuads failed: {e}')

		# Method 2: Fall back to DOM.getBoxModel
		try:
			result = await cdp_session.cdp_client.send.DOM.getBoxModel(
				params={'backendNodeId': backend_node_id}, session_id=cdp_session.session_id
			)

			if result and 'model' in result:
				box_model = result['model']
				content = box_model.get('content', [])

				if content and len(content) >= 8:
					# Extract bounding box from content array [x1, y1, x2, y2, x3, y3, x4, y4]
					x_coords = [content[i] for i in range(0, len(content), 2)]
					y_coords = [content[i + 1] for i in range(0, len(content), 2)]

					x = min(x_coords)
					y = min(y_coords)
					width = max(x_coords) - min(x_coords)
					height = max(y_coords) - min(y_coords)

					return DOMRect(x=x, y=y, width=width, height=height)
		except Exception as e:
			self.browser_session.logger.debug(f'DOM.getBoxModel failed: {e}')

		# Method 3: Last resort - JavaScript getBoundingClientRect
		try:
			script = f"""
			(function() {{
				const node = document.querySelector('[data-backend-node-id="{backend_node_id}"]');
				if (!node) {{
					// Try to find by traversing all elements
					const allElements = document.querySelectorAll('*');
					for (const el of allElements) {{
						if (el.getAttribute && el.getAttribute('data-backend-node-id') == '{backend_node_id}') {{
							const rect = el.getBoundingClientRect();
							return {{x: rect.x, y: rect.y, width: rect.width, height: rect.height}};
						}}
					}}
					return null;
				}}
				const rect = node.getBoundingClientRect();
				return {{x: rect.x, y: rect.y, width: rect.width, height: rect.height}};
			}})();
			"""

			result = await cdp_session.cdp_client.send.Runtime.evaluate(
				params={'expression': script, 'returnByValue': True}, session_id=cdp_session.session_id
			)

			if result and 'result' in result and 'value' in result['result']:
				rect_data = result['result']['value']
				if rect_data:
					return DOMRect(x=rect_data['x'], y=rect_data['y'], width=rect_data['width'], height=rect_data['height'])
		except Exception as e:
			self.browser_session.logger.debug(f'JavaScript getBoundingClientRect failed: {e}')

		return None

	async def highlight_interaction_element(self, node: 'EnhancedDOMTreeNode') -> None:
		"""Temporarily highlight an element during interaction. Delegates to session.py implementation."""
		await self.browser_session._highlight_interaction_element_impl(node)

	async def highlight_coordinate_click(self, x: int, y: int) -> None:
		"""Temporarily highlight a coordinate click position. Delegates to session.py implementation."""
		await self.browser_session._highlight_coordinate_click_impl(x, y)

	async def add_highlights(self, selector_map: dict[int, 'EnhancedDOMTreeNode']) -> None:
		"""Add visual highlights to the browser DOM. Delegates to session.py implementation."""
		await self.browser_session._add_highlights_impl(selector_map)

