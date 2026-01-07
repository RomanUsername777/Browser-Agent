import hashlib
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from cdp_use.cdp.accessibility.commands import GetFullAXTreeReturns
from cdp_use.cdp.accessibility.types import AXPropertyName
from cdp_use.cdp.dom.commands import GetDocumentReturns
from cdp_use.cdp.dom.types import ShadowRootType
from cdp_use.cdp.domsnapshot.commands import CaptureSnapshotReturns
from cdp_use.cdp.target.types import SessionID, TargetID, TargetInfo
from uuid_extensions import uuid7str

from core.observability import observe_debug


# ========== Helper Functions ==========

def cap_text_length(text: str, max_length: int) -> str:
	"""Ограничить длину текста для отображения."""
	if len(text) <= max_length:
		return text
	return text[:max_length] + '...'


def generate_css_selector_for_element(enhanced_node) -> str | None:
	"""Сгенерировать CSS-селектор, используя свойства узла (подход версии 0.5.0)."""
	import re

	if not enhanced_node or not hasattr(enhanced_node, 'tag_name') or not enhanced_node.tag_name:
		return None

	# Получаем базовый селектор из имени тега
	tag_name = enhanced_node.tag_name.lower().strip()
	if not tag_name or not re.match(r'^[a-zA-Z][a-zA-Z0-9-]*$', tag_name):
		return None

	css_selector = tag_name

	# Добавляем ID, если доступен (наиболее специфично)
	if enhanced_node.attributes and 'id' in enhanced_node.attributes:
		element_id = enhanced_node.attributes['id']
		if element_id and element_id.strip():
			element_id = element_id.strip()
			# Проверяем, что ID содержит только валидные символы для селектора #
			if re.match(r'^[a-zA-Z][a-zA-Z0-9_-]*$', element_id):
				return f'#{element_id}'
			else:
				# Для ID со специальными символами ($, ., :, и т.д.) используем селектор атрибута
				# Экранируем кавычки в значении ID
				escaped_id = element_id.replace('"', '\\"')
				return f'{tag_name}[id="{escaped_id}"]'

	# Обрабатываем атрибуты class (подход версии 0.5.0)
	if enhanced_node.attributes and 'class' in enhanced_node.attributes and enhanced_node.attributes['class']:
		# Определяем regex-паттерн для валидных имён классов в CSS
		valid_class_name_pattern = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_-]*$')

		# Итерируемся по значениям атрибута class
		classes = enhanced_node.attributes['class'].split()
		for class_name in classes:
			# Пропускаем пустые имена классов
			if not class_name.strip():
				continue

			# Проверяем, валидно ли имя класса
			if valid_class_name_pattern.match(class_name):
				# Добавляем валидное имя класса к CSS-селектору
				css_selector += f'.{class_name}'

	# Расширенный набор безопасных атрибутов для стабильного выбора элементов (из v0.5.0)
	SAFE_ATTRIBUTES = {
		# Атрибуты доступности
		'aria-describedby',
		'aria-label',
		'aria-labelledby',
		'role',
		# Стандартные HTML-атрибуты
		'name',
		'placeholder',
		'type',
		# Общие атрибуты форм
		'autocomplete',
		'for',
		'readonly',
		'required',
		# Медиа-атрибуты
		'alt',
		'src',
		'title',
		# Пользовательские стабильные атрибуты (добавьте любые специфичные для приложения)
		'href',
		'target',
		# Data-атрибуты (если они стабильны в вашем приложении)
		'id',
	}

	# Всегда включаем динамические атрибуты (эквивалент include_dynamic_attributes=True)
	include_dynamic_attributes = True
	if include_dynamic_attributes:
		dynamic_attributes = {
			'data-cy',
			'data-id',
			'data-qa',
			'data-testid',
		}
		SAFE_ATTRIBUTES.update(dynamic_attributes)

	# Обрабатываем другие атрибуты (подход версии 0.5.0)
	if enhanced_node.attributes:
		for attribute, value in enhanced_node.attributes.items():
			if attribute == 'class':
				continue

			# Пропускаем невалидные имена атрибутов
			if not attribute.strip():
				continue

			if attribute not in SAFE_ATTRIBUTES:
				continue

			# Экранируем специальные символы в именах атрибутов
			safe_attribute = attribute.replace(':', r'\:')

			# Обрабатываем различные случаи значений
			if value == '':
				css_selector += f'[{safe_attribute}]'
			elif any(char in value for char in '"\'<>`\n\r\t'):
				# Используем contains для значений со специальными символами
				# Для текста, содержащего перевод строки, используем только часть до перевода строки
				if '\n' in value:
					value = value.split('\n')[0]
				# Regex-заменяем *любые* пробельные символы одним пробелом, затем обрезаем
				collapsed_value = re.sub(r'\s+', ' ', value).strip()
				# Экранируем встроенные двойные кавычки
				safe_value = collapsed_value.replace('"', '\\"')
				css_selector += f'[{safe_attribute}*="{safe_value}"]'
			else:
				css_selector += f'[{safe_attribute}="{value}"]'

	# Финальная валидация: убеждаемся, что селектор безопасен и не содержит проблемных символов
	# Примечание: кавычки разрешены в селекторах атрибутов, например [name="value"]
	if css_selector and not any(char in css_selector for char in ['\n', '\r', '\t']):
		return css_selector

	# Если мы здесь, селектор был проблемным, возвращаем только имя тега как запасной вариант
	return tag_name


# ========== Models ==========

# Serializer types
DEFAULT_INCLUDE_ATTRIBUTES = [
	'title',
	'type',
	'checked',
	# 'class',
	'id',
	'name',
	'role',
	'value',
	'placeholder',
	'data-date-format',
	'alt',
	'aria-label',
	'aria-expanded',
	'data-state',
	'aria-checked',
	# ARIA value attributes for datetime/range inputs
	'aria-valuemin',
	'aria-valuemax',
	'aria-valuenow',
	'aria-placeholder',
	# Validation attributes - help agents avoid brute force attempts
	'pattern',
	'min',
	'max',
	'minlength',
	'maxlength',
	'step',
	'accept',  # File input types (e.g., accept="image/*" or accept=".pdf")
	'multiple',  # Whether multiple files/selections are allowed
	'inputmode',  # Virtual keyboard hint (numeric, tel, email, url, etc.)
	'autocomplete',  # Autocomplete behavior hint
	'data-mask',  # Input mask format (e.g., phone numbers, credit cards)
	'data-inputmask',  # Alternative input mask attribute
	'data-datepicker',  # jQuery datepicker indicator
	'format',  # Synthetic attribute for date/time input format (e.g., MM/dd/yyyy)
	'expected_format',  # Synthetic attribute for explicit expected format (e.g., AngularJS datepickers)
	'contenteditable',  # Rich text editor detection
	# Webkit shadow DOM identifiers
	'pseudo',
	# Accessibility properties from ax_node (ordered by importance for automation)
	'checked',
	'selected',
	'expanded',
	'pressed',
	'disabled',
	'invalid',  # Current validation state from AX node
	'valuemin',  # Min value from AX node (for datetime/range)
	'valuemax',  # Max value from AX node (for datetime/range)
	'valuenow',
	'keyshortcuts',
	'haspopup',
	'multiselectable',
	# Less commonly needed (uncomment if required):
	# 'readonly',
	'required',
	'valuetext',
	'level',
	'busy',
	'live',
	# Accessibility name (contains text content for StaticText elements)
	'ax_name',
]

STATIC_ATTRIBUTES = {
	'class',
	'id',
	'name',
	'type',
	'placeholder',
	'aria-label',
	'title',
	# 'aria-expanded',
	'role',
	'data-testid',
	'data-test',
	'data-cy',
	'data-selenium',
	'for',
	'required',
	'disabled',
	'readonly',
	'checked',
	'selected',
	'multiple',
	'accept',
	'href',
	'target',
	'rel',
	'aria-describedby',
	'aria-labelledby',
	'aria-controls',
	'aria-owns',
	'aria-live',
	'aria-atomic',
	'aria-busy',
	'aria-disabled',
	'aria-hidden',
	'aria-pressed',
	'aria-checked',
	'aria-selected',
	'tabindex',
	'alt',
	'src',
	'lang',
	'itemscope',
	'itemtype',
	'itemprop',
	# Webkit shadow DOM attributes
	'pseudo',
	'aria-valuemin',
	'aria-valuemax',
	'aria-valuenow',
	'aria-placeholder',
}


@dataclass
class CurrentPageTargets:
	page_session: TargetInfo
	iframe_sessions: list[TargetInfo]
	"""
	Iframe sessions are ALL the iframes sessions of all the pages (not just the current page)
	"""


@dataclass
class TargetAllTrees:
	snapshot: CaptureSnapshotReturns
	dom_tree: GetDocumentReturns
	ax_tree: GetFullAXTreeReturns
	device_pixel_ratio: float
	cdp_timing: dict[str, float]


@dataclass(slots=True)
class PropagatingBounds:
	"""Track bounds that propagate from parent elements to filter children."""

	tag: str  # The tag that started propagation ('a' or 'button')
	bounds: 'DOMRect'  # The bounding box
	node_id: int  # Node ID for debugging
	depth: int  # How deep in tree this started (for debugging)


@dataclass(slots=True)
class SimplifiedNode:
	"""Simplified tree node for optimization."""

	# Обязательные поля (без значений по умолчанию)
	original_node: 'EnhancedDOMTreeNode'
	children: list['SimplifiedNode']
	
	# Поля со значениями по умолчанию
	should_display: bool = True
	is_interactive: bool = False  # True if element is in selector_map
	is_new: bool = False
	ignored_by_paint_order: bool = False  # More info in dom/serializer/paint_order.py
	excluded_by_parent: bool = False  # New field for bbox filtering
	is_shadow_host: bool = False  # New field for shadow DOM hosts
	is_compound_component: bool = False  # True for virtual components of compound controls

	def _clean_original_node_json(self, node_json: dict) -> dict:
		"""Recursively remove children_nodes and shadow_roots from original_node JSON."""
		# Remove the fields we don't want in SimplifiedNode serialization
		if 'children_nodes' in node_json:
			del node_json['children_nodes']
		if 'shadow_roots' in node_json:
			del node_json['shadow_roots']

		# Clean nested content_document if it exists
		if node_json.get('content_document'):
			node_json['content_document'] = self._clean_original_node_json(node_json['content_document'])

		return node_json

	def __json__(self) -> dict:
		original_node_json = self.original_node.__json__()
		# Remove children_nodes and shadow_roots to avoid duplication with SimplifiedNode.children
		cleaned_original_node_json = self._clean_original_node_json(original_node_json)
		return {
			'should_display': self.should_display,
			'is_interactive': self.is_interactive,
			'ignored_by_paint_order': self.ignored_by_paint_order,
			'excluded_by_parent': self.excluded_by_parent,
			'original_node': cleaned_original_node_json,
			'children': [c.__json__() for c in self.children],
		}


class NodeType(int, Enum):
	"""DOM node types based on the DOM specification."""

	ELEMENT_NODE = 1
	ATTRIBUTE_NODE = 2
	TEXT_NODE = 3
	CDATA_SECTION_NODE = 4
	ENTITY_REFERENCE_NODE = 5
	ENTITY_NODE = 6
	PROCESSING_INSTRUCTION_NODE = 7
	COMMENT_NODE = 8
	DOCUMENT_NODE = 9
	DOCUMENT_TYPE_NODE = 10
	DOCUMENT_FRAGMENT_NODE = 11
	NOTATION_NODE = 12


@dataclass(slots=True)
class DOMRect:
	x: float
	y: float
	width: float
	height: float

	def to_dict(self) -> dict[str, Any]:
		return {
			'x': self.x,
			'y': self.y,
			'width': self.width,
			'height': self.height,
		}

	def __json__(self) -> dict:
		return self.to_dict()


@dataclass(slots=True)
class EnhancedAXProperty:
	"""we don't need `sources` and `related_nodes` for now (not sure how to use them)"""

	name: AXPropertyName
	value: str | bool | None
	# related_nodes: list[EnhancedAXRelatedNode] | None


@dataclass(slots=True)
class EnhancedAXNode:
	ax_node_id: str
	"""Not to be confused the DOM node_id. Only useful for AX node tree"""
	ignored: bool
	# we don't need ignored_reasons as we anyway ignore the node otherwise
	role: str | None
	name: str | None
	description: str | None

	properties: list[EnhancedAXProperty] | None
	child_ids: list[str] | None


@dataclass(slots=True)
class EnhancedSnapshotNode:
	"""Snapshot data extracted from DOMSnapshot for enhanced functionality."""

	is_clickable: bool | None
	cursor_style: str | None
	bounds: DOMRect | None
	"""
	Document coordinates (origin = top-left of the page, ignores current scroll).
	Equivalent JS API: layoutNode.boundingBox in the older API.
	Typical use: Quick hit-test that doesn't care about scroll position.
	"""

	clientRects: DOMRect | None
	"""
	Viewport coordinates (origin = top-left of the visible scrollport).
	Equivalent JS API: element.getClientRects() / getBoundingClientRect().
	Typical use: Pixel-perfect hit-testing on screen, taking current scroll into account.
	"""

	scrollRects: DOMRect | None
	"""
	Scrollable area of the element.
	"""

	computed_styles: dict[str, str] | None
	"""Computed styles from the layout tree"""
	paint_order: int | None
	"""Paint order from the layout tree"""
	stacking_contexts: int | None
	"""Stacking contexts from the layout tree"""


# 	frame_id: str | None
# 	target_id: TargetID

# 	node_type: NodeType
# 	node_name: str

# 	# is_visible: bool | None
# 	# is_scrollable: bool | None

# 	element_index: int | None


@dataclass(slots=True)
class EnhancedDOMTreeNode:
	"""
	Enhanced DOM tree node that contains information from AX, DOM, and Snapshot trees.

	@dev when serializing check if the value is a valid value first!

	"""

	# region - DOM Node data

	node_id: int
	backend_node_id: int

	node_type: NodeType
	"""Node types, defined in `NodeType` enum."""
	node_name: str
	"""Only applicable for `NodeType.ELEMENT_NODE`"""
	node_value: str
	"""Здесь хранится значение из `NodeType.TEXT_NODE`"""
	attributes: dict[str, str]
	"""Атрибуты оптимизированы для лучшей читаемости"""
	is_scrollable: bool | None
	"""
	Whether the node is scrollable.
	"""
	is_visible: bool | None
	"""
	Whether the node is visible according to the upper most frame node.
	"""

	absolute_position: DOMRect | None
	"""
	Absolute position of the node in the document according to the top-left of the page.
	"""

	# frames
	target_id: TargetID
	frame_id: str | None
	session_id: SessionID | None
	content_document: 'EnhancedDOMTreeNode | None'
	"""
	Content document is the document inside a new iframe.
	"""
	# Shadow DOM
	shadow_root_type: ShadowRootType | None
	shadow_roots: list['EnhancedDOMTreeNode'] | None
	"""
	Shadow roots are the shadow DOMs of the element.
	"""

	# Navigation
	parent_node: 'EnhancedDOMTreeNode | None'
	children_nodes: list['EnhancedDOMTreeNode'] | None

	# endregion - DOM Node data

	# region - AX Node data
	ax_node: EnhancedAXNode | None

	# endregion - AX Node data

	# region - Snapshot Node data
	snapshot_node: EnhancedSnapshotNode | None

	# endregion - Snapshot Node data

	# Compound control child components information
	_compound_children: list[dict[str, Any]] = field(default_factory=list)

	uuid: str = field(default_factory=uuid7str)

	@property
	def parent(self) -> 'EnhancedDOMTreeNode | None':
		return self.parent_node

	@property
	def children(self) -> list['EnhancedDOMTreeNode']:
		return self.children_nodes or []

	@property
	def children_and_shadow_roots(self) -> list['EnhancedDOMTreeNode']:
		"""
		Returns all children nodes, including shadow roots
		"""
		# IMPORTANT: Make a copy to avoid mutating the original children_nodes list!
		children = list(self.children_nodes) if self.children_nodes else []
		if self.shadow_roots:
			children.extend(self.shadow_roots)
		return children

	@property
	def tag_name(self) -> str:
		return self.node_name.lower()

	@property
	def xpath(self) -> str:
		"""Generate XPath for this DOM node, stopping at shadow boundaries or iframes."""
		segments = []
		current_element = self

		while current_element and (
			current_element.node_type == NodeType.ELEMENT_NODE or current_element.node_type == NodeType.DOCUMENT_FRAGMENT_NODE
		):
			# just pass through shadow roots
			if current_element.node_type == NodeType.DOCUMENT_FRAGMENT_NODE:
				current_element = current_element.parent_node
				continue

			# stop ONLY if we hit iframe
			if current_element.parent_node and current_element.parent_node.node_name.lower() == 'iframe':
				break

			position = self._get_element_position(current_element)
			tag_name = current_element.node_name.lower()
			xpath_index = f'[{position}]' if position > 0 else ''
			segments.insert(0, f'{tag_name}{xpath_index}')

			current_element = current_element.parent_node

		return '/'.join(segments)

	def _get_element_position(self, element: 'EnhancedDOMTreeNode') -> int:
		"""Get the position of an element among its siblings with the same tag name.
		Returns 0 if it's the only element of its type, otherwise returns 1-based index."""
		parent = element.parent_node
		if not parent or not parent.children_nodes:
			return 0

		# Collect siblings with matching tag name
		matching_siblings = []
		for sibling in parent.children_nodes:
			if sibling.node_type == NodeType.ELEMENT_NODE and sibling.node_name.lower() == element.node_name.lower():
				matching_siblings.append(sibling)

		# If only one or none, no index needed
		if len(matching_siblings) <= 1:
			return 0

		# XPath uses 1-based indexing
		try:
			sibling_index = matching_siblings.index(element) + 1
			return sibling_index
		except ValueError:
			return 0

	def __json__(self) -> dict:
		"""Serializes the node and its descendants to a dictionary, omitting parent references."""
		return {
			'node_id': self.node_id,
			'backend_node_id': self.backend_node_id,
			'node_type': self.node_type.name,
			'node_name': self.node_name,
			'node_value': self.node_value,
			'is_visible': self.is_visible,
			'attributes': self.attributes,
			'is_scrollable': self.is_scrollable,
			'session_id': self.session_id,
			'target_id': self.target_id,
			'frame_id': self.frame_id,
			'content_document': self.content_document.__json__() if self.content_document else None,
			'shadow_root_type': self.shadow_root_type,
			'ax_node': asdict(self.ax_node) if self.ax_node else None,
			'snapshot_node': asdict(self.snapshot_node) if self.snapshot_node else None,
			# these two in the end, so it's easier to read json
			'shadow_roots': [r.__json__() for r in self.shadow_roots] if self.shadow_roots else [],
			'children_nodes': [c.__json__() for c in self.children_nodes] if self.children_nodes else [],
		}

	def get_all_children_text(self, max_depth: int = -1) -> str:
		collected_text_fragments = []

		def extract_text_recursively(current_node: EnhancedDOMTreeNode, depth: int) -> None:
			# Check depth limit
			if max_depth != -1 and depth > max_depth:
				return

			# Skip this branch if we hit a highlighted element (except for the current node)
			# if node.node_type == NodeType.ELEMENT_NODE
			# if isinstance(node, DOMElementNode) and node != self and node.highlight_index is not None:
			# 	return

			# Collect text from text nodes
			if current_node.node_type == NodeType.TEXT_NODE:
				collected_text_fragments.append(current_node.node_value)
			# Recursively process element nodes
			elif current_node.node_type == NodeType.ELEMENT_NODE:
				for child_node in current_node.children:
					extract_text_recursively(child_node, depth + 1)

		extract_text_recursively(self, 0)
		return '\n'.join(collected_text_fragments).strip()

	def __repr__(self) -> str:
		"""
		@DEV ! don't display this to the LLM, it's SUPER long
		"""
		attributes = ', '.join([f'{k}={v}' for k, v in self.attributes.items()])
		is_scrollable = getattr(self, 'is_scrollable', False)
		num_children = len(self.children_nodes or [])
		return (
			f'<{self.tag_name} {attributes} is_scrollable={is_scrollable} '
			f'num_children={num_children} >{self.node_value}</{self.tag_name}>'
		)

	def llm_representation(self, max_text_length: int = 100) -> str:
		"""
		Token friendly representation of the node, used in the LLM
		"""

		return f'<{self.tag_name}>{cap_text_length(self.get_all_children_text(), max_text_length) or ""}'

	def get_meaningful_text_for_llm(self) -> str:
		"""
		Get the meaningful text content that the LLM actually sees for this element.
		This matches exactly what goes into the DOMTreeSerializer output.
		"""
		result_text = ''
		# Check attributes in priority order: value, aria-label, title, placeholder, alt
		priority_attributes = ['value', 'aria-label', 'title', 'placeholder', 'alt']
		if hasattr(self, 'attributes') and self.attributes:
			for attribute_name in priority_attributes:
				if attribute_name in self.attributes and self.attributes[attribute_name]:
					result_text = self.attributes[attribute_name]
					break

		# Use text content as fallback if no meaningful attributes found
		if not result_text:
			result_text = self.get_all_children_text()

		return result_text.strip()

	@property
	def is_actually_scrollable(self) -> bool:
		"""
		Enhanced scroll detection that combines CDP detection with CSS analysis.

		This detects scrollable elements that Chrome's CDP might miss, which is common
		in iframes and dynamically sized containers.
		"""
		# First check if CDP already detected it as scrollable
		if self.is_scrollable:
			return True

		# Enhanced detection for elements CDP missed
		if not self.snapshot_node:
			return False

		# Check scroll vs client rects - this is the most reliable indicator
		scroll_rects = self.snapshot_node.scrollRects
		client_rects = self.snapshot_node.clientRects

		if scroll_rects and client_rects:
			# Determine if content exceeds visible area (with rounding tolerance)
			vertical_overflow = scroll_rects.height > client_rects.height + 1  # +1 for rounding
			horizontal_overflow = scroll_rects.width > client_rects.width + 1

			if vertical_overflow or horizontal_overflow:
				# Verify CSS allows scrolling
				computed_css = self.snapshot_node.computed_styles
				if computed_css:
					overflow_value = computed_css.get('overflow', 'visible').lower()
					overflow_x_value = computed_css.get('overflow-x', overflow_value).lower()
					overflow_y_value = computed_css.get('overflow-y', overflow_value).lower()

					# Only allow scrolling if overflow is explicitly set to auto, scroll, or overlay
					# Do NOT consider 'visible' overflow as scrollable - this was causing the issue
					css_allows_scrolling = (
						overflow_value in ['auto', 'scroll', 'overlay']
						or overflow_x_value in ['auto', 'scroll', 'overlay']
						or overflow_y_value in ['auto', 'scroll', 'overlay']
					)

					return css_allows_scrolling
				else:
					# No CSS info available, but content overflows - be more conservative
					# Only consider it scrollable if it's a common scrollable container element
					common_scrollable_elements = {'div', 'main', 'section', 'article', 'aside', 'body', 'html'}
					return self.tag_name.lower() in common_scrollable_elements

		return False

	@property
	def should_show_scroll_info(self) -> bool:
		"""
		Simple check: show scroll info only if this element is scrollable
		and doesn't have a scrollable parent (to avoid nested scroll spam).

		Special case for iframes: Always show scroll info since Chrome might not
		always detect iframe scrollability correctly (scrollHeight: 0 issue).
		"""
		# Special case: Always show scroll info for iframe elements
		# Even if not detected as scrollable, they might have scrollable content
		if self.tag_name.lower() == 'iframe':
			return True

		# Must be scrollable first for non-iframe elements
		if not (self.is_scrollable or self.is_actually_scrollable):
			return False

		# Always show for iframe content documents (body/html)
		if self.tag_name.lower() in {'body', 'html'}:
			return True

		# Don't show if parent is already scrollable (avoid nested spam)
		if self.parent_node and (self.parent_node.is_scrollable or self.parent_node.is_actually_scrollable):
			return False

		return True

	def _find_html_in_content_document(self) -> 'EnhancedDOMTreeNode | None':
		"""Find HTML element in iframe content document."""
		if not self.content_document:
			return None

		# Check if content document itself is HTML
		if self.content_document.tag_name.lower() == 'html':
			return self.content_document

		# Look through children for HTML element
		if self.content_document.children_nodes:
			for child in self.content_document.children_nodes:
				if child.tag_name.lower() == 'html':
					return child

		return None

	@property
	def scroll_info(self) -> dict[str, Any] | None:
		"""Calculate scroll information for this element if it's scrollable."""
		if not self.is_actually_scrollable or not self.snapshot_node:
			return None

		# Extract scroll and client rects from snapshot data
		scroll_rectangles = self.snapshot_node.scrollRects
		client_rectangles = self.snapshot_node.clientRects
		element_bounds = self.snapshot_node.bounds

		if not scroll_rectangles or not client_rectangles:
			return None

		# Extract scroll position coordinates
		current_scroll_y = scroll_rectangles.y
		current_scroll_x = scroll_rectangles.x

		# Extract total scrollable dimensions
		total_scrollable_height = scroll_rectangles.height
		total_scrollable_width = scroll_rectangles.width

		# Extract visible viewport dimensions
		viewport_height = client_rectangles.height
		viewport_width = client_rectangles.width

		# Compute content offsets from current view
		offset_above = max(0, current_scroll_y)
		offset_below = max(0, total_scrollable_height - viewport_height - current_scroll_y)
		offset_left = max(0, current_scroll_x)
		offset_right = max(0, total_scrollable_width - viewport_width - current_scroll_x)

		# Compute scroll percentage values
		vertical_percent = 0
		horizontal_percent = 0

		if total_scrollable_height > viewport_height:
			maximum_scroll_y = total_scrollable_height - viewport_height
			vertical_percent = (current_scroll_y / maximum_scroll_y) * 100 if maximum_scroll_y > 0 else 0

		if total_scrollable_width > viewport_width:
			maximum_scroll_x = total_scrollable_width - viewport_width
			horizontal_percent = (current_scroll_x / maximum_scroll_x) * 100 if maximum_scroll_x > 0 else 0

		# Compute page equivalents (using viewport height as page unit)
		pages_above_view = offset_above / viewport_height if viewport_height > 0 else 0
		pages_below_view = offset_below / viewport_height if viewport_height > 0 else 0
		total_page_count = total_scrollable_height / viewport_height if viewport_height > 0 else 1

		return {
			'scroll_top': current_scroll_y,
			'scroll_left': current_scroll_x,
			'scrollable_height': total_scrollable_height,
			'scrollable_width': total_scrollable_width,
			'visible_height': viewport_height,
			'visible_width': viewport_width,
			'content_above': offset_above,
			'content_below': offset_below,
			'content_left': offset_left,
			'content_right': offset_right,
			'vertical_scroll_percentage': round(vertical_percent, 1),
			'horizontal_scroll_percentage': round(horizontal_percent, 1),
			'pages_above': round(pages_above_view, 1),
			'pages_below': round(pages_below_view, 1),
			'total_pages': round(total_page_count, 1),
			'can_scroll_up': offset_above > 0,
			'can_scroll_down': offset_below > 0,
			'can_scroll_left': offset_left > 0,
			'can_scroll_right': offset_right > 0,
		}

	def get_scroll_info_text(self) -> str:
		"""Get human-readable scroll information text for this element."""
		# Handle iframe elements specially: check content document for scroll info
		if self.tag_name.lower() == 'iframe':
			# Attempt to retrieve scroll info from HTML document within iframe
			if self.content_document:
				# Search for HTML element in content document
				html_node = self._find_html_in_content_document()
				if html_node and html_node.scroll_info:
					iframe_scroll_data = html_node.scroll_info
					# Provide minimal but useful scroll info
					pages_below_count = iframe_scroll_data.get('pages_below', 0)
					pages_above_count = iframe_scroll_data.get('pages_above', 0)
					vertical_percentage = int(iframe_scroll_data.get('vertical_scroll_percentage', 0))

					if pages_below_count > 0 or pages_above_count > 0:
						return f'scroll: {pages_above_count:.1f}↑ {pages_below_count:.1f}↓ {vertical_percentage}%'

			return 'scroll'

		element_scroll_data = self.scroll_info
		if not element_scroll_data:
			return ''

		scroll_text_components = []

		# Add vertical scroll info (concise format)
		if element_scroll_data['scrollable_height'] > element_scroll_data['visible_height']:
			scroll_text_components.append(f'{element_scroll_data["pages_above"]:.1f} pages above, {element_scroll_data["pages_below"]:.1f} pages below')

		# Add horizontal scroll info (concise format)
		if element_scroll_data['scrollable_width'] > element_scroll_data['visible_width']:
			scroll_text_components.append(f'horizontal {element_scroll_data["horizontal_scroll_percentage"]:.0f}%')

		return ' '.join(scroll_text_components)

	@property
	def element_hash(self) -> int:
		return hash(self)

	def __str__(self) -> str:
		return f'[<{self.tag_name}>#{self.frame_id[-4:] if self.frame_id else "?"}:{self.backend_node_id}]'

	def __hash__(self) -> int:
		"""
		Hash the element based on its parent branch path and attributes.
		"""

		# Retrieve parent branch path
		branch_path_list = self._get_parent_branch_path()
		branch_path_str = '/'.join(branch_path_list)

		# Build attributes string from static attributes only
		sorted_static_attrs = sorted((k, v) for k, v in self.attributes.items() if k in STATIC_ATTRIBUTES)
		attrs_str = ''.join(f'{key}={value}' for key, value in sorted_static_attrs)

		# Merge path and attributes for final hash computation
		hash_input_string = f'{branch_path_str}|{attrs_str}'
		hash_hex_result = hashlib.sha256(hash_input_string.encode()).hexdigest()

		# Convert to int for __hash__ return type - use first 16 chars and convert from hex to int
		return int(hash_hex_result[:16], 16)

	def parent_branch_hash(self) -> int:
		"""
		Hash the element based on its parent branch path and attributes.
		"""
		branch_path = self._get_parent_branch_path()
		path_string = '/'.join(branch_path)
		hash_result = hashlib.sha256(path_string.encode()).hexdigest()

		return int(hash_result[:16], 16)

	def _get_parent_branch_path(self) -> list[str]:
		"""Get the parent branch path as a list of tag names from root to current element."""
		parent_elements: list['EnhancedDOMTreeNode'] = []
		element: 'EnhancedDOMTreeNode | None' = self

		while element is not None:
			if element.node_type == NodeType.ELEMENT_NODE:
				parent_elements.append(element)
			element = element.parent_node

		parent_elements.reverse()
		return [elem.tag_name for elem in parent_elements]


DOMSelectorMap = dict[int, EnhancedDOMTreeNode]


@dataclass
class SerializedDOMState:
	_root: SimplifiedNode | None
	"""Not meant to be used directly, use `llm_representation` instead"""

	selector_map: DOMSelectorMap

	@observe_debug(ignore_input=True, ignore_output=True, name='llm_representation')
	def llm_representation(
		self,
		include_attributes: list[str] | None = None,
	) -> str:
		"""Kinda ugly, but leaving this as an internal method because include_attributes are a parameter on the agent, so we need to leave it as a 2 step process"""
		from core.dom_processing.serializer.serializer import DOMTreeSerializer

		if not self._root:
			return 'Empty DOM tree (you might have to wait for the page to load)'

		include_attributes = include_attributes or DEFAULT_INCLUDE_ATTRIBUTES

		return DOMTreeSerializer.serialize_tree(self._root, include_attributes)

	@observe_debug(ignore_input=True, ignore_output=True, name='eval_representation')
	def eval_representation(
		self,
		include_attributes: list[str] | None = None,
	) -> str:
		"""
		Evaluation-focused DOM representation without interactive indexes.

		This serializer is designed for evaluation/judge contexts where:
		- No interactive indexes are needed (we're not clicking)
		- Full HTML structure should be preserved for context
		- More attribute information is helpful
		- Text content is important for understanding page structure
		"""
		from core.dom_processing.serializer.eval_serializer import DOMEvalSerializer

		if not self._root:
			return 'Empty DOM tree (you might have to wait for the page to load)'

		include_attributes = include_attributes or DEFAULT_INCLUDE_ATTRIBUTES

		return DOMEvalSerializer.serialize_tree(self._root, include_attributes)


@dataclass
class DOMInteractedElement:
	"""
	DOMInteractedElement is a class that represents a DOM element that has been interacted with.
	It is used to store the DOM element that has been interacted with.
	"""

	node_id: int
	backend_node_id: int
	frame_id: str | None

	node_type: NodeType
	node_value: str
	node_name: str
	attributes: dict[str, str] | None

	bounds: DOMRect | None

	x_path: str

	element_hash: int

	def to_dict(self) -> dict[str, Any]:
		return {
			'node_id': self.node_id,
			'backend_node_id': self.backend_node_id,
			'frame_id': self.frame_id,
			'node_type': self.node_type.value,
			'node_value': self.node_value,
			'node_name': self.node_name,
			'attributes': self.attributes,
			'x_path': self.x_path,
			'element_hash': self.element_hash,
			'bounds': self.bounds.to_dict() if self.bounds else None,
		}

	@classmethod
	def load_from_enhanced_dom_tree(cls, enhanced_dom_tree: EnhancedDOMTreeNode) -> 'DOMInteractedElement':
		return cls(
			node_id=enhanced_dom_tree.node_id,
			backend_node_id=enhanced_dom_tree.backend_node_id,
			frame_id=enhanced_dom_tree.frame_id,
			node_type=enhanced_dom_tree.node_type,
			node_value=enhanced_dom_tree.node_value,
			node_name=enhanced_dom_tree.node_name,
			attributes=enhanced_dom_tree.attributes,
			bounds=enhanced_dom_tree.snapshot_node.bounds if enhanced_dom_tree.snapshot_node else None,
			x_path=enhanced_dom_tree.xpath,
			element_hash=hash(enhanced_dom_tree),
		)
