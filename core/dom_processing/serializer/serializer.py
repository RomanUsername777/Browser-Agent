# @file purpose: Сериализует улучшенные DOM-деревья в строковый формат для потребления LLM

import logging
from typing import Any

from core.dom_processing.serializer.clickable_elements import ClickableElementDetector
from core.dom_processing.serializer.paint_order import PaintOrderRemover
from core.dom_processing.models import cap_text_length
from core.dom_processing.models import (
	DOMRect,
	DOMSelectorMap,
	EnhancedDOMTreeNode,
	NodeType,
	PropagatingBounds,
	SerializedDOMState,
	SimplifiedNode,
)

logger = logging.getLogger(__name__)

DISABLED_ELEMENTS = {'head', 'link', 'meta', 'script', 'style', 'title'}

# SVG дочерние элементы для пропуска (только декоративные, без ценности для взаимодействия)
SVG_ELEMENTS = {
	'circle',
	'clipPath',
	'defs',
	'ellipse',
	'g',
	'image',
	'line',
	'mask',
	'path',
	'pattern',
	'polygon',
	'polyline',
	'rect',
	'text',
	'tspan',
	'use',
}


class DOMTreeSerializer:
	"""Сериализует улучшенные DOM-деревья в строковый формат."""

	# Конфигурация - элементы, которые распространяют границы на своих детей
	PROPAGATING_ELEMENTS = [
		{'role': None, 'tag': 'a'},  # Любой тег <a>
		{'role': None, 'tag': 'button'},  # Любой тег <button>
		{'role': 'button', 'tag': 'div'},  # <div role="button">
		{'role': 'combobox', 'tag': 'div'},  # <div role="combobox"> - выпадающие списки/селекты
		{'role': 'combobox', 'tag': 'input'},  # <input role="combobox"> - автозаполняемые поля ввода
		{'role': 'combobox', 'tag': 'input'},  # <input type="text"> - текстовые поля ввода с предложениями
		{'role': 'button', 'tag': 'span'},  # <span role="button">
		{'role': 'combobox', 'tag': 'span'},  # <span role="combobox">
		# {'role': 'link', 'tag': 'div'},     # <div role="link">
		# {'role': 'link', 'tag': 'span'},    # <span role="link">
	]
	DEFAULT_CONTAINMENT_THRESHOLD = 0.99  # 99% содержания по умолчанию

	def __init__(
		self,
		root_node: EnhancedDOMTreeNode,
		previous_cached_state: SerializedDOMState | None = None,
		enable_bbox_filtering: bool = True,
		containment_threshold: float | None = None,
		paint_order_filtering: bool = True,
		session_id: str | None = None,
	):
		self.root_node = root_node
		self._clickable_cache: dict[int, bool] = {}  # Кэш для обнаружения кликабельных элементов, чтобы избежать избыточных вызовов
		self._interactive_counter = 1
		self._previous_cached_selector_map = previous_cached_state.selector_map if previous_cached_state else None
		self._selector_map: DOMSelectorMap = {}
		# Конфигурация фильтрации ограничивающих рамок
		self.containment_threshold = containment_threshold or self.DEFAULT_CONTAINMENT_THRESHOLD
		self.enable_bbox_filtering = enable_bbox_filtering
		# Конфигурация фильтрации порядка отрисовки
		self.paint_order_filtering = paint_order_filtering
		# ID сессии для атрибута исключения, специфичного для сессии
		self.session_id = session_id
		# Добавить отслеживание времени
		self.timing_info: dict[str, float] = {}

	def _safe_parse_number(self, value_str: str, default: float) -> float:
		"""Распарсить строку в float, обрабатывая отрицательные числа и десятичные дроби."""
		try:
			return float(value_str)
		except (TypeError, ValueError):
			return default

	def _safe_parse_optional_number(self, value_str: str | None) -> float | None:
		"""Распарсить строку в float, возвращая None для невалидных значений."""
		if not value_str:
			return None
		try:
			return float(value_str)
		except (TypeError, ValueError):
			return None

	def serialize_accessible_elements(self) -> tuple[SerializedDOMState, dict[str, float]]:
		import time

		start_total = time.time()

		# Сбросить состояние
		self._clickable_cache = {}  # Очистить кэш для новой сериализации
		self._interactive_counter = 1
		self._selector_map = {}
		self._semantic_groups = []

		# Шаг 1: Создать упрощённое дерево (включает обнаружение кликабельных элементов)
		start_step1 = time.time()
		simplified_tree = self._create_simplified_tree(0, self.root_node)
		end_step1 = time.time()
		self.timing_info['create_simplified_tree'] = end_step1 - start_step1

		# Шаг 2: Удалить элементы на основе порядка отрисовки
		start_step3 = time.time()
		if self.paint_order_filtering and simplified_tree:
			PaintOrderRemover(simplified_tree).calculate_paint_order()
		end_step3 = time.time()
		self.timing_info['calculate_paint_order'] = end_step3 - start_step3

		# Шаг 3: Оптимизировать дерево (удалить ненужных родителей)
		start_step2 = time.time()
		optimized_tree = self._optimize_tree(simplified_tree)
		end_step2 = time.time()
		self.timing_info['optimize_tree'] = end_step2 - start_step2

		# Шаг 3: Применить фильтрацию ограничивающих рамок (НОВОЕ)
		if self.enable_bbox_filtering and optimized_tree:
			start_step3 = time.time()
			filtered_tree = self._apply_bounding_box_filtering(optimized_tree)
			end_step3 = time.time()
			self.timing_info['bbox_filtering'] = end_step3 - start_step3
		else:
			filtered_tree = optimized_tree

		# Шаг 4: Назначить интерактивные индексы кликабельным элементам
		start_step4 = time.time()
		self._assign_interactive_indices_and_mark_new_nodes(filtered_tree)
		end_step4 = time.time()
		self.timing_info['assign_interactive_indices'] = end_step4 - start_step4

		end_total = time.time()
		self.timing_info['serialize_accessible_elements_total'] = end_total - start_total

		return SerializedDOMState(_root=filtered_tree, selector_map=self._selector_map), self.timing_info

	def _add_compound_components(self, node: EnhancedDOMTreeNode, simplified: SimplifiedNode) -> None:
		"""Улучшить составные элементы управления информацией из их дочерних компонентов."""
		# Обрабатывать только элементы, которые могут иметь составные компоненты
		if node.tag_name not in ['audio', 'details', 'input', 'select', 'video']:
			return

		# Для элементов input проверить типы составных полей ввода
		if node.tag_name == 'input':
			if not node.attributes or node.attributes.get('type') not in [
				'color',
				'date',
				'datetime-local',
				'file',
				'month',
				'number',
				'range',
				'time',
				'week',
			]:
				return
		# Для других элементов проверить, есть ли у них индикаторы дочерних AX
		elif not node.ax_node or not node.ax_node.child_ids:
			return

		# Добавить информацию о составных компонентах на основе типа элемента
		element_type = node.tag_name
		input_type = node.attributes.get('type', '') if node.attributes else ''

		if element_type == 'input':
			if input_type in ['date', 'datetime-local', 'month', 'time', 'week']:
				# Пропустить составные компоненты для полей ввода даты/времени - формат показан в placeholder
				pass
			elif input_type == 'range':
				# Слайдер диапазона с индикатором значения
				min_val = node.attributes.get('min', '0') if node.attributes else '0'
				max_val = node.attributes.get('max', '100') if node.attributes else '100'

				node._compound_children.append(
					{
						'name': 'Value',
						'role': 'slider',
						'valuemax': self._safe_parse_number(max_val, 100.0),
						'valuemin': self._safe_parse_number(min_val, 0.0),
						'valuenow': None,
					}
				)
				simplified.is_compound_component = True
			elif input_type == 'number':
				# Поле ввода числа с кнопками увеличения/уменьшения
				max_val = node.attributes.get('max') if node.attributes else None
				min_val = node.attributes.get('min') if node.attributes else None

				node._compound_children.extend(
					[
						{'name': 'Decrement', 'role': 'button', 'valuemax': None, 'valuemin': None, 'valuenow': None},
						{'name': 'Increment', 'role': 'button', 'valuemax': None, 'valuemin': None, 'valuenow': None},
						{
							'name': 'Value',
							'role': 'textbox',
							'valuemax': self._safe_parse_optional_number(max_val),
							'valuemin': self._safe_parse_optional_number(min_val),
							'valuenow': None,
						},
					]
				)
				simplified.is_compound_component = True
			elif input_type == 'color':
				# Выбор цвета с компонентами
				node._compound_children.extend(
					[
						{'name': 'Color Picker', 'role': 'button', 'valuemax': None, 'valuemin': None, 'valuenow': None},
						{'name': 'Hex Value', 'role': 'textbox', 'valuemax': None, 'valuemin': None, 'valuenow': None},
					]
				)
				simplified.is_compound_component = True
			elif input_type == 'file':
				# Поле ввода файла с кнопкой обзора
				multiple = 'multiple' in node.attributes if node.attributes else False

				# Извлечь текущее состояние выбора файла из AX-дерева
				current_value = 'None'  # По умолчанию явная строка "None" для ясности
				if node.ax_node and node.ax_node.properties:
					for prop in node.ax_node.properties:
						# Попробовать сначала valuetext (читаемое отображение типа "file.pdf")
						if prop.name == 'valuetext' and prop.value:
							value_str = str(prop.value).strip()
							if value_str and value_str.lower() not in ['', 'no file chosen', 'no file selected']:
								current_value = value_str
							break
						# Также попробовать свойство 'value' (может включать полный путь)
						elif prop.name == 'value' and prop.value:
							value_str = str(prop.value).strip()
							if value_str:
								# Для полей ввода файла значение может быть полным путём - извлечь только имя файла
								if '/' in value_str:
									current_value = value_str.split('/')[-1]
								elif '\\' in value_str:
									current_value = value_str.split('\\')[-1]
								else:
									current_value = value_str
								break

				node._compound_children.extend(
					[
						{'name': 'Browse Files', 'role': 'button', 'valuemax': None, 'valuemin': None, 'valuenow': None},
						{
							'name': f'{"Files" if multiple else "File"} Selected',
							'role': 'textbox',
							'valuemax': None,
							'valuemin': None,
							'valuenow': current_value,  # Всегда показывает состояние: имя файла или "None"
						},
					]
				)
				simplified.is_compound_component = True

		elif element_type == 'select':
			# Выпадающий список select со списком опций и подробной информацией об опциях
			base_components = [
				{'name': 'Dropdown Toggle', 'role': 'button', 'valuemax': None, 'valuemin': None, 'valuenow': None}
			]

			# Извлечь информацию об опциях из дочерних узлов
			options_info = self._extract_select_options(node)
			if options_info:
				options_component = {
					'first_options': options_info['first_options'],
					'name': 'Options',
					'options_count': options_info['count'],
					'role': 'listbox',
					'valuemax': None,
					'valuemin': None,
					'valuenow': None,
				}
				if options_info['format_hint']:
					options_component['format_hint'] = options_info['format_hint']
				base_components.append(options_component)
			else:
				base_components.append(
					{'name': 'Options', 'role': 'listbox', 'valuemax': None, 'valuemin': None, 'valuenow': None}
				)

			node._compound_children.extend(base_components)
			simplified.is_compound_component = True

		elif element_type == 'details':
			# Виджет раскрытия details/summary
			node._compound_children.extend(
				[
					{'name': 'Content Area', 'role': 'region', 'valuemax': None, 'valuemin': None, 'valuenow': None},
					{'name': 'Toggle Disclosure', 'role': 'button', 'valuemax': None, 'valuemin': None, 'valuenow': None},
				]
			)
			simplified.is_compound_component = True

		elif element_type == 'audio':
			# Элементы управления аудиоплеером
			node._compound_children.extend(
				[
					{'name': 'Mute', 'role': 'button', 'valuemax': None, 'valuemin': None, 'valuenow': None},
					{'name': 'Play/Pause', 'role': 'button', 'valuemax': None, 'valuemin': None, 'valuenow': None},
					{'name': 'Progress', 'role': 'slider', 'valuemax': 100, 'valuemin': 0, 'valuenow': None},
					{'name': 'Volume', 'role': 'slider', 'valuemax': 100, 'valuemin': 0, 'valuenow': None},
				]
			)
			simplified.is_compound_component = True

		elif element_type == 'video':
			# Элементы управления видеоплеером
			node._compound_children.extend(
				[
					{'name': 'Fullscreen', 'role': 'button', 'valuemax': None, 'valuemin': None, 'valuenow': None},
					{'name': 'Mute', 'role': 'button', 'valuemax': None, 'valuemin': None, 'valuenow': None},
					{'name': 'Play/Pause', 'role': 'button', 'valuemax': None, 'valuemin': None, 'valuenow': None},
					{'name': 'Progress', 'role': 'slider', 'valuemax': 100, 'valuemin': 0, 'valuenow': None},
					{'name': 'Volume', 'role': 'slider', 'valuemax': 100, 'valuemin': 0, 'valuenow': None},
				]
			)
			simplified.is_compound_component = True

	def _extract_select_options(self, select_node: EnhancedDOMTreeNode) -> dict[str, Any] | None:
		"""Извлечь информацию об опциях из элемента select."""
		if not select_node.children:
			return None

		option_values = []
		options = []

		def extract_options_recursive(node: EnhancedDOMTreeNode) -> None:
			"""Рекурсивно извлечь элементы option, включая из optgroups."""
			if node.tag_name.lower() == 'option':
				# Extract option text and value
				option_text = ''
				option_value = ''

				# Get value attribute if present
				if node.attributes and 'value' in node.attributes:
					option_value = str(node.attributes['value']).strip()

				# Get text content from direct child text nodes only to avoid duplication
				def get_direct_text_content(n: EnhancedDOMTreeNode) -> str:
					text = ''
					for child in n.children:
						if child.node_type == NodeType.TEXT_NODE and child.node_value:
							text += child.node_value.strip() + ' '
					return text.strip()

				option_text = get_direct_text_content(node)

				# Use text as value if no explicit value
				if not option_value and option_text:
					option_value = option_text

				if option_text or option_value:
					options.append({'text': option_text, 'value': option_value})
					option_values.append(option_value)

			elif node.tag_name.lower() == 'optgroup':
				# Process optgroup children
				for child in node.children:
					extract_options_recursive(child)
			else:
				# Process other children that might contain options
				for child in node.children:
					extract_options_recursive(child)

		# Extract all options from select children
		for child in select_node.children:
			extract_options_recursive(child)

		if not options:
			return None

		# Prepare first 4 options for display
		first_options = []
		for option in options[:4]:
			# Always use text if available, otherwise use value
			display_text = option['text'] if option['text'] else option['value']
			if display_text:
				# Limit individual option text to avoid overly long attributes
				text = display_text[:30] + ('...' if len(display_text) > 30 else '')
				first_options.append(text)

		# Add ellipsis indicator if there are more options than shown
		if len(options) > 4:
			first_options.append(f'... {len(options) - 4} more options...')

		# Try to infer format hint from option values
		format_hint = None
		if len(option_values) >= 2:
			# Check for common patterns
			if all(val.isdigit() for val in option_values[:5] if val):
				format_hint = 'numeric'
			elif all(len(val) == 2 and val.isupper() for val in option_values[:5] if val):
				format_hint = 'country/state codes'
			elif all('/' in val or '-' in val for val in option_values[:5] if val):
				format_hint = 'date/path format'
			elif any('@' in val for val in option_values[:5] if val):
				format_hint = 'email addresses'

		return {'count': len(options), 'first_options': first_options, 'format_hint': format_hint}

	def _is_interactive_cached(self, node: EnhancedDOMTreeNode) -> bool:
		"""Cached version of clickable element detection to avoid redundant calls."""

		if node.node_id not in self._clickable_cache:
			import time

			start_time = time.time()
			result = ClickableElementDetector.is_interactive(node)
			end_time = time.time()

			if 'clickable_detection_time' not in self.timing_info:
				self.timing_info['clickable_detection_time'] = 0
			self.timing_info['clickable_detection_time'] += end_time - start_time

			self._clickable_cache[node.node_id] = result

		return self._clickable_cache[node.node_id]

	def _create_simplified_tree(self, depth: int = 0, node: EnhancedDOMTreeNode = None) -> SimplifiedNode | None:
		"""Шаг 1: Создать упрощённое дерево с улучшенным обнаружением элементов."""

		if node.node_type == NodeType.DOCUMENT_NODE:
			# для всех детей, включая shadow roots
			for child in node.children_and_shadow_roots:
				simplified_child = self._create_simplified_tree(depth + 1, child)
				if simplified_child:
					return simplified_child

			return None

		if node.node_type == NodeType.DOCUMENT_FRAGMENT_NODE:
			# УЛУЧШЕННАЯ обработка shadow DOM - всегда включать содержимое shadow
			simplified = SimplifiedNode(children=[], original_node=node)
			for child in node.children_and_shadow_roots:
				simplified_child = self._create_simplified_tree(depth + 1, child)
				if simplified_child:
					simplified.children.append(simplified_child)

			# Всегда возвращать фрагменты shadow DOM, даже если дети кажутся пустыми
			# Shadow DOM часто содержит фактическое интерактивное содержимое в SPA
			return simplified if simplified.children else SimplifiedNode(children=[], original_node=node)

		elif node.node_type == NodeType.ELEMENT_NODE:
			# Пропустить элементы без содержимого
			if node.node_name.lower() in DISABLED_ELEMENTS:
				return None

			# Пропустить дочерние элементы SVG полностью (path, rect, g, circle и т.д.)
			if node.node_name.lower() in SVG_ELEMENTS:
				return None

			attributes = node.attributes or {}
			# Проверить атрибут исключения, специфичный для сессии, сначала, затем вернуться к устаревшему атрибуту
			attr_type = None
			exclude_attr = None
			if self.session_id:
				session_specific_attr = f'data-agent-exclude-{self.session_id}'
				exclude_attr = attributes.get(session_specific_attr)
				if exclude_attr:
					attr_type = 'session-specific'
			# Вернуться к устаревшему атрибуту, если специфичный для сессии не найден
			if not exclude_attr:
				exclude_attr = attributes.get('data-agent-exclude')
			if isinstance(exclude_attr, str) and exclude_attr.lower() == 'true':
				return None

			if node.node_name == 'FRAME' or node.node_name == 'IFRAME':
				if node.content_document:
					simplified = SimplifiedNode(children=[], original_node=node)
					for child in node.content_document.children_nodes or []:
						simplified_child = self._create_simplified_tree(depth + 1, child)
						if simplified_child is not None:
							simplified.children.append(simplified_child)
					return simplified

			has_shadow_content = bool(node.children_and_shadow_roots)
			is_scrollable = node.is_actually_scrollable
			is_visible = node.is_visible

			# УЛУЧШЕННОЕ ОБНАРУЖЕНИЕ SHADOW DOM: Включать shadow hosts, даже если не видимы
			is_shadow_host = any(child.node_type == NodeType.DOCUMENT_FRAGMENT_NODE for child in node.children_and_shadow_roots)

			# Переопределить видимость для элементов с атрибутами валидации
			if not is_visible and node.attributes:
				has_validation_attrs = any(attr.startswith(('aria-', 'pseudo')) for attr in node.attributes.keys())
				if has_validation_attrs:
					is_visible = True  # Принудительная видимость для элементов валидации

			# ИСКЛЮЧЕНИЕ: Поля ввода файлов часто скрыты с opacity:0, но всё ещё функциональны
			# Bootstrap и другие фреймворки используют этот паттерн с пользовательскими стилизованными выборщиками файлов
			is_file_input = (
				node.attributes and node.tag_name and node.tag_name.lower() == 'input' and node.attributes.get('type') == 'file'
			)
			if not is_visible and is_file_input:
				is_visible = True  # Принудительная видимость для полей ввода файлов

			# КРИТИЧНО: Если Chrome считает элемент кликабельным (is_clickable из DOMSnapshot),
			# включать его, даже если не видим. Это важно для модальных диалогов,
			# оверлеев и React порталов, которые могут не проходить проверки видимости.
			is_clickable = node.snapshot_node and node.snapshot_node.is_clickable
			if not is_visible and is_clickable:
				is_visible = True  # Принудительная видимость для кликабельных элементов

			# Включить, если видим, прокручиваем, имеет детей или является shadow host
			if has_shadow_content or is_scrollable or is_shadow_host or is_visible:
				simplified = SimplifiedNode(children=[], is_shadow_host=is_shadow_host, original_node=node)

				# Обработать ВСЕХ детей, включая shadow roots, с улучшенным логированием
				for child in node.children_and_shadow_roots:
					simplified_child = self._create_simplified_tree(depth + 1, child)
					if simplified_child:
						simplified.children.append(simplified_child)

				# ОБРАБОТКА СОСТАВНЫХ ЭЛЕМЕНТОВ УПРАВЛЕНИЯ: Добавить виртуальные компоненты для составных элементов управления
				self._add_compound_components(node, simplified)

				# ОСОБЫЙ СЛУЧАЙ SHADOW DOM: Всегда включать shadow hosts, даже если не видимы
				# Многие SPA фреймворки (React, Vue) рендерят содержимое в shadow DOM
				if is_shadow_host and simplified.children:
					return simplified

				# Вернуть, если значим или имеет значимых детей
				if simplified.children or is_scrollable or is_visible:
					return simplified
			else:
				return None
		elif node.node_type == NodeType.TEXT_NODE:
			# Включить значимые текстовые узлы
			is_visible = node.snapshot_node and node.is_visible
			if is_visible and node.node_value and node.node_value.strip() and len(node.node_value.strip()) > 1:
				return SimplifiedNode(children=[], original_node=node)

		return None

	def _optimize_tree(self, node: SimplifiedNode | None) -> SimplifiedNode | None:
		"""Шаг 2: Оптимизировать структуру дерева."""
		if not node:
			return None

		# Обработать детей
		optimized_children = []
		for child in node.children:
			optimized_child = self._optimize_tree(child)
			if optimized_child:
				optimized_children.append(optimized_child)

		node.children = optimized_children

		# Сохранить значимые узлы
		is_visible = node.original_node.snapshot_node and node.original_node.is_visible
		
		# КРИТИЧНО: Сохранить элементы, которые Chrome считает кликабельными (из DOMSnapshot)
		is_clickable = node.original_node.snapshot_node and node.original_node.snapshot_node.is_clickable

		# ИСКЛЮЧЕНИЕ: Поля ввода файлов часто скрыты с opacity:0, но всё ещё функциональны
		is_file_input = (
			node.original_node.attributes
			and node.original_node.tag_name
			and node.original_node.tag_name.lower() == 'input'
			and node.original_node.attributes.get('type') == 'file'
		)
		
		if (
			is_clickable  # Сохранить все кликабельные узлы (Chrome DOMSnapshot)
			or is_file_input  # Сохранить поля ввода файлов, даже если не видимы
			or is_visible  # Сохранить все видимые узлы
			or node.children
			or node.original_node.is_actually_scrollable
			or node.original_node.node_type == NodeType.TEXT_NODE
		):
			return node

		return None

	def _collect_interactive_elements(self, elements: list[SimplifiedNode], node: SimplifiedNode) -> None:
		"""Рекурсивно собрать интерактивные элементы, которые также видимы."""
		is_clickable = node.original_node.snapshot_node and node.original_node.snapshot_node.is_clickable
		is_interactive = self._is_interactive_cached(node.original_node)
		is_visible = node.original_node.snapshot_node and node.original_node.is_visible

		# Собрать элементы, которые интерактивны И (видимы ИЛИ кликабельны)
		if is_interactive and (is_clickable or is_visible):
			elements.append(node)

		for child in node.children:
			self._collect_interactive_elements(elements, child)

	def _has_interactive_descendants(self, node: SimplifiedNode) -> bool:
		"""Проверить, есть ли у узла какие-либо интерактивные потомки (не включая сам узел)."""
		# Проверить детей на интерактивность
		for child in node.children:
			# Проверить, является ли сам ребёнок интерактивным
			if self._is_interactive_cached(child.original_node):
				return True
			# Рекурсивно проверить потомков ребёнка
			if self._has_interactive_descendants(child):
				return True

		return False

	def _assign_interactive_indices_and_mark_new_nodes(self, node: SimplifiedNode | None) -> None:
		"""Назначить интерактивные индексы кликабельным элементам, которые также видимы."""
		if not node:
			return

		# КРИТИЧНО: Для настоящих button элементов НЕ пропускаем их даже если ignored_by_paint_order=True
		tag = node.original_node.tag_name.lower() if node.original_node.tag_name else ''
		has_role_button = node.original_node.attributes and node.original_node.attributes.get('role') == 'button'
		is_real_button = tag == 'button' or (tag == 'a' and has_role_button)
		# Это нужно для кнопок, которые могут быть перекрыты другими элементами, но все равно должны быть кликабельны
		# Пропустить назначение индекса исключённым узлам или игнорируемым по порядку отрисовки (НО НЕ для настоящих button элементов!)
		should_process = not node.excluded_by_parent and (not node.ignored_by_paint_order or is_real_button)
		
		if should_process:
			# Обычное назначение интерактивных элементов (включая улучшенные составные элементы управления)
			is_clickable = node.original_node.snapshot_node and node.original_node.snapshot_node.is_clickable
			is_interactive_assign = self._is_interactive_cached(node.original_node)
			is_scrollable = node.original_node.is_actually_scrollable
			is_visible = node.original_node.snapshot_node and node.original_node.is_visible

			# ИСКЛЮЧЕНИЕ: Поля ввода файлов часто скрыты с opacity:0, но всё ещё функциональны
			# Bootstrap и другие фреймворки используют этот паттерн с пользовательскими стилизованными выборщиками файлов
			is_file_input = (
				node.original_node.attributes
				and node.original_node.tag_name
				and node.original_node.tag_name.lower() == 'input'
				and node.original_node.attributes.get('type') == 'file'
			)

			# Проверить, следует ли сделать прокручиваемый контейнер интерактивным
			# Для прокручиваемых элементов делать их интерактивными ТОЛЬКО если у них нет интерактивных потомков
			should_make_interactive = False
			
			# Получаем информацию о теге и атрибутах для проверки button элементов
			tag = node.original_node.tag_name.lower() if node.original_node.tag_name else ''
			has_role_button = node.original_node.attributes and node.original_node.attributes.get('role') == 'button'
			
			is_button_element = tag == 'button' or (tag == 'a' and has_role_button) or has_role_button
			
			if is_scrollable:
				# Для прокручиваемых элементов проверить, есть ли у них интерактивные дети
				has_interactive_desc = self._has_interactive_descendants(node)

				# Делать прокручиваемый контейнер интерактивным только если у него НЕТ интерактивных потомков
				if not has_interactive_desc:
					should_make_interactive = True
			elif is_clickable:
				# КРИТИЧНО: Если Chrome считает элемент кликабельным, ВСЕГДА делать его интерактивным
				# Это обрабатывает кнопки в модальных окнах, оверлеях и любые элементы, которые Chrome считает кликабельными
				# Мы доверяем обнаружению is_clickable Chrome больше, чем нашим эвристикам
				should_make_interactive = True
			elif is_interactive_assign and (is_clickable or is_file_input or is_visible):
				# Непрокручиваемые интерактивные элементы: делать интерактивными, если видимы, поле ввода файла или кликабельны
				should_make_interactive = True
			# КРИТИЧНО: Для button и похожих на button элементов включать их, даже если не кликабельны или не видимы
			# Кнопки важны для форм, даже когда отключены - они должны быть доступны агенту
			# Это особенно важно для кнопок отправки форм, которые могут быть отключены до заполнения всех полей
			elif tag == 'button' or (tag == 'a' and has_role_button) or has_role_button:
				# ВАЖНО: включаем ВСЕ button элементы в selector_map, даже если они disabled или не видимы
				# Это критично для форм, где кнопка может быть disabled до заполнения всех полей
				# Не проверяем is_visible или is_clickable - disabled кнопки все равно должны быть доступны агенту
				should_make_interactive = True
			

			# Добавить в карту селекторов, если элемент должен быть интерактивным
			if should_make_interactive:
				# Пометить узел как интерактивный
				node.is_interactive = True
				# Сохранить backend_node_id в карте селекторов (модель выводит backend_node_id)
				self._selector_map[node.original_node.backend_node_id] = node.original_node
				self._interactive_counter += 1
			else:
				# Пометить составные компоненты как новые для видимости
				if node.is_compound_component:
					node.is_new = True
				elif self._previous_cached_selector_map:
					# Проверить, является ли узел новым для обычных элементов
					previous_backend_node_ids = {node.backend_node_id for node in self._previous_cached_selector_map.values()}
					if node.original_node.backend_node_id not in previous_backend_node_ids:
						node.is_new = True

		# Обработать детей
		for child in node.children:
			self._assign_interactive_indices_and_mark_new_nodes(child)

	def _apply_bounding_box_filtering(self, node: SimplifiedNode | None) -> SimplifiedNode | None:
		"""Отфильтровать детей, содержащихся в пределах распространяющихся границ родителя."""
		if not node:
			return None

		# Начать без активных границ
		self._filter_tree_recursive(active_bounds=None, depth=0, node=node)

		# Логировать статистику
		excluded_count = self._count_excluded_nodes(node)
		if excluded_count > 0:
			import logging

			logging.debug(f'BBox filtering excluded {excluded_count} nodes')

		return node

	def _filter_tree_recursive(self, active_bounds: PropagatingBounds | None = None, depth: int = 0, node: SimplifiedNode = None):
		"""
		Рекурсивно отфильтровать дерево с распространением ограничивающих рамок.
		Границы распространяются на ВСЕХ потомков до переопределения.
		"""

		# Проверить, должен ли этот узел быть исключён активными границами
		if active_bounds and self._should_exclude_child(active_bounds, node):
			node.excluded_by_parent = True
			# Важно: Всё ещё проверить, начинает ли этот узел НОВОЕ распространение

		# Проверить, начинает ли этот узел новое распространение (даже если исключён!)
		new_bounds = None
		role = node.original_node.attributes.get('role') if node.original_node.attributes else None
		tag = node.original_node.tag_name.lower()
		attributes = {
			'role': role,
			'tag': tag,
		}
		# Проверить, соответствует ли этот элемент какому-либо паттерну распространяющегося элемента
		if self._is_propagating_element(attributes):
			# Этот узел распространяет границы на ВСЕХ своих потомков
			if node.original_node.snapshot_node and node.original_node.snapshot_node.bounds:
				new_bounds = PropagatingBounds(
					bounds=node.original_node.snapshot_node.bounds,
					depth=depth,
					node_id=node.original_node.node_id,
					tag=tag,
				)

		# Распространить на ВСЕХ детей
		# Использовать new_bounds, если этот узел начинает распространение, иначе продолжить с active_bounds
		propagate_bounds = new_bounds if new_bounds else active_bounds

		for child in node.children:
			self._filter_tree_recursive(active_bounds=propagate_bounds, depth=depth + 1, node=child)

	def _should_exclude_child(self, active_bounds: PropagatingBounds, node: SimplifiedNode) -> bool:
		"""
		Определить, должен ли ребёнок быть исключён на основе распространяющихся границ.
		"""

		# Никогда не исключать текстовые узлы - мы всегда хотим сохранить текстовое содержимое
		if node.original_node.node_type == NodeType.TEXT_NODE:
			return False
		
		# КРИТИЧНО: Никогда не исключать элементы, которые Chrome считает кликабельными
		# Это гарантирует, что кликабельные кнопки всегда видимы
		if node.original_node.snapshot_node and node.original_node.snapshot_node.is_clickable:
			return False
		
		# КРИТИЧНО: Никогда не исключать интерактивные элементы (ссылки, кнопки)
		# Они нужны агенту для клика по ним
		role = node.original_node.attributes.get('role', '') if node.original_node.attributes else ''
		tag = node.original_node.tag_name.lower() if node.original_node.tag_name else ''
		if role == 'button' or tag in ('a', 'button'):
			return False

		# Получить границы ребёнка
		if not node.original_node.snapshot_node or not node.original_node.snapshot_node.bounds:
			return False  # Нет границ = нельзя определить содержательность

		child_bounds = node.original_node.snapshot_node.bounds

		# Проверить содержательность с настроенным порогом
		if not self._is_contained(child_bounds, active_bounds.bounds, self.containment_threshold):
			return False  # Недостаточно содержится

		# ПРАВИЛА ИСКЛЮЧЕНИЙ - Сохранить эти, даже если содержатся:

		child_role = node.original_node.attributes.get('role') if node.original_node.attributes else None
		child_tag = node.original_node.tag_name.lower()
		child_attributes = {
			'role': child_role,
			'tag': child_tag,
		}

		# 1. Никогда не исключать элементы форм (им нужны индивидуальные взаимодействия)
		if child_tag in ['input', 'label', 'select', 'textarea']:
			return False

		# 2. Сохранить, если ребёнок также является распространяющимся элементом
		# (может иметь stopPropagation, например, button в button)
		if self._is_propagating_element(child_attributes):
			return False

		# 3. Сохранить, если имеет явный обработчик onclick
		if node.original_node.attributes and 'onclick' in node.original_node.attributes:
			return False

		# 4. Сохранить, если имеет aria-label, предполагающий независимую интерактивность
		if node.original_node.attributes:
			aria_label = node.original_node.attributes.get('aria-label')
			if aria_label and aria_label.strip():
				# Имеет значимый aria-label, вероятно интерактивен
				return False

		# 5. Сохранить, если имеет роль, предполагающую интерактивность
		if node.original_node.attributes:
			role = node.original_node.attributes.get('role')
			if role in ['checkbox', 'link', 'menuitem', 'option', 'radio', 'button', 'tab']:
				return False

		# По умолчанию: исключить этого ребёнка
		return True

	def _is_contained(self, child: DOMRect, parent: DOMRect, threshold: float) -> bool:
		"""
		Проверить, содержится ли ребёнок в пределах границ родителя.

		Args:
			threshold: Процент (0.0-1.0) ребёнка, который должен быть в пределах родителя
		"""
		# Вычислить пересечение
		x_overlap = max(0, min(parent.x + parent.width, child.x + child.width) - max(parent.x, child.x))
		y_overlap = max(0, min(parent.y + parent.height, child.y + child.height) - max(parent.y, child.y))

		child_area = child.width * child.height
		intersection_area = x_overlap * y_overlap

		if child_area == 0:
			return False  # Элемент с нулевой площадью

		containment_ratio = intersection_area / child_area
		return containment_ratio >= threshold

	def _count_excluded_nodes(self, node: SimplifiedNode, count: int = 0) -> int:
		"""Count how many nodes were excluded (for debugging)."""
		if hasattr(node, 'excluded_by_parent') and node.excluded_by_parent:
			count += 1
		for child in node.children:
			count = self._count_excluded_nodes(child, count)
		return count

	def _is_propagating_element(self, attributes: dict[str, str | None]) -> bool:
		"""
		Check if an element should propagate bounds based on attributes.
		If the element satisfies one of the patterns, it propagates bounds to all its children.
		"""
		keys_to_check = ['tag', 'role']
		for pattern in self.PROPAGATING_ELEMENTS:
			# Check if the element satisfies the pattern
			check = [pattern.get(key) is None or pattern.get(key) == attributes.get(key) for key in keys_to_check]
			if all(check):
				return True

		return False

	@staticmethod
	def serialize_tree(node: SimplifiedNode | None, include_attributes: list[str], depth: int = 0) -> str:
		"""Serialize the optimized tree to string format."""
		if not node:
			return ''

		# Skip rendering excluded nodes, but process their children
		if hasattr(node, 'excluded_by_parent') and node.excluded_by_parent:
			formatted_text = []
			for child in node.children:
				child_text = DOMTreeSerializer.serialize_tree(child, include_attributes, depth)
				if child_text:
					formatted_text.append(child_text)
			return '\n'.join(formatted_text)

		formatted_text = []
		depth_str = depth * '\t'
		next_depth = depth

		if node.original_node.node_type == NodeType.ELEMENT_NODE:
			# Skip displaying nodes marked as should_display=False
			if not node.should_display:
				for child in node.children:
					child_text = DOMTreeSerializer.serialize_tree(child, include_attributes, depth)
					if child_text:
						formatted_text.append(child_text)
				return '\n'.join(formatted_text)

			# Special handling for SVG elements - show the tag but collapse children
			if node.original_node.tag_name.lower() == 'svg':
				shadow_prefix = ''
				if node.is_shadow_host:
					has_closed_shadow = any(
						child.original_node.node_type == NodeType.DOCUMENT_FRAGMENT_NODE
						and child.original_node.shadow_root_type
						and child.original_node.shadow_root_type.lower() == 'closed'
						for child in node.children
					)
					shadow_prefix = '|SHADOW(closed)|' if has_closed_shadow else '|SHADOW(open)|'

				line = f'{depth_str}{shadow_prefix}'
				# Add interactive marker if clickable
				if node.is_interactive:
					new_prefix = '*' if node.is_new else ''
					line += f'{new_prefix}[{node.original_node.backend_node_id}]'
				line += '<svg'
				attributes_html_str = DOMTreeSerializer._build_attributes_string(node.original_node, include_attributes, '')
				if attributes_html_str:
					line += f' {attributes_html_str}'
				line += ' /> <!-- SVG content collapsed -->'
				formatted_text.append(line)
				# Don't process children for SVG
				return '\n'.join(formatted_text)

			# Add element if clickable, scrollable, or iframe
			is_any_scrollable = node.original_node.is_actually_scrollable or node.original_node.is_scrollable
			should_show_scroll = node.original_node.should_show_scroll_info
			
			
			if (
				node.is_interactive
				or is_any_scrollable
				or node.original_node.tag_name.upper() == 'IFRAME'
				or node.original_node.tag_name.upper() == 'FRAME'
			):
				next_depth += 1

				# Build attributes string with compound component info
				text_content = ''
				attributes_html_str = DOMTreeSerializer._build_attributes_string(
					node.original_node, include_attributes, text_content
				)

				# Add compound component information to attributes if present
				if node.original_node._compound_children:
					compound_info = []
					for child_info in node.original_node._compound_children:
						parts = []
						if child_info['name']:
							parts.append(f'name={child_info["name"]}')
						if child_info['role']:
							parts.append(f'role={child_info["role"]}')
						if child_info['valuemin'] is not None:
							parts.append(f'min={child_info["valuemin"]}')
						if child_info['valuemax'] is not None:
							parts.append(f'max={child_info["valuemax"]}')
						if child_info['valuenow'] is not None:
							parts.append(f'current={child_info["valuenow"]}')

						# Add select-specific information
						if 'options_count' in child_info and child_info['options_count'] is not None:
							parts.append(f'count={child_info["options_count"]}')
						if 'first_options' in child_info and child_info['first_options']:
							options_str = '|'.join(child_info['first_options'][:4])  # Limit to 4 options
							parts.append(f'options={options_str}')
						if 'format_hint' in child_info and child_info['format_hint']:
							parts.append(f'format={child_info["format_hint"]}')

						if parts:
							compound_info.append(f'({",".join(parts)})')

					if compound_info:
						compound_attr = f'compound_components={",".join(compound_info)}'
						if attributes_html_str:
							attributes_html_str += f' {compound_attr}'
						else:
							attributes_html_str = compound_attr

				# Build the line with shadow host indicator
				shadow_prefix = ''
				if node.is_shadow_host:
					# Check if any shadow children are closed
					has_closed_shadow = any(
						child.original_node.node_type == NodeType.DOCUMENT_FRAGMENT_NODE
						and child.original_node.shadow_root_type
						and child.original_node.shadow_root_type.lower() == 'closed'
						for child in node.children
					)
					shadow_prefix = '|SHADOW(closed)|' if has_closed_shadow else '|SHADOW(open)|'

				if should_show_scroll and not node.is_interactive:
					# Scrollable container but not clickable
					line = f'{depth_str}{shadow_prefix}|SCROLL|<{node.original_node.tag_name}'
				elif node.is_interactive:
					# Clickable (and possibly scrollable) - show backend_node_id
					new_prefix = '*' if node.is_new else ''
					scroll_prefix = '|SCROLL[' if should_show_scroll else '['
					# Add visibility indicator for non-visible but clickable elements
					is_visible_for_serialization = node.original_node.snapshot_node and node.original_node.is_visible if node.original_node.snapshot_node else False
					visibility_suffix = '|HIDDEN' if not is_visible_for_serialization else ''
					line = f'{depth_str}{shadow_prefix}{new_prefix}{scroll_prefix}{node.original_node.backend_node_id}]{visibility_suffix}<{node.original_node.tag_name}'
				elif node.original_node.tag_name.upper() == 'IFRAME':
					# Iframe element (not interactive)
					line = f'{depth_str}{shadow_prefix}|IFRAME|<{node.original_node.tag_name}'
				elif node.original_node.tag_name.upper() == 'FRAME':
					# Frame element (not interactive)
					line = f'{depth_str}{shadow_prefix}|FRAME|<{node.original_node.tag_name}'
				else:
					line = f'{depth_str}{shadow_prefix}<{node.original_node.tag_name}'

				if attributes_html_str:
					line += f' {attributes_html_str}'

				line += ' />'

				# Add scroll information only when we should show it
				if should_show_scroll:
					scroll_info_text = node.original_node.get_scroll_info_text()
					if scroll_info_text:
						line += f' ({scroll_info_text})'

				formatted_text.append(line)
			else:
				# Element is NOT interactive, not scrollable, not iframe - will NOT be serialized
				# Skip logging for non-serialized elements (too verbose)
				pass

		elif node.original_node.node_type == NodeType.DOCUMENT_FRAGMENT_NODE:
			# Shadow DOM representation - show clearly to LLM
			if node.original_node.shadow_root_type and node.original_node.shadow_root_type.lower() == 'closed':
				formatted_text.append(f'{depth_str}Closed Shadow')
			else:
				formatted_text.append(f'{depth_str}Open Shadow')

			next_depth += 1

			# Process shadow DOM children
			for child in node.children:
				child_text = DOMTreeSerializer.serialize_tree(child, include_attributes, next_depth)
				if child_text:
					formatted_text.append(child_text)

			# Close shadow DOM indicator
			if node.children:  # Only show close if we had content
				formatted_text.append(f'{depth_str}Shadow End')

		elif node.original_node.node_type == NodeType.TEXT_NODE:
			# Include visible text
			is_visible = node.original_node.snapshot_node and node.original_node.is_visible
			if (
				is_visible
				and node.original_node.node_value
				and node.original_node.node_value.strip()
				and len(node.original_node.node_value.strip()) > 1
			):
				clean_text = node.original_node.node_value.strip()
				formatted_text.append(f'{depth_str}{clean_text}')

		# Process children (for non-shadow elements)
		if node.original_node.node_type != NodeType.DOCUMENT_FRAGMENT_NODE:
			for child in node.children:
				child_text = DOMTreeSerializer.serialize_tree(child, include_attributes, next_depth)
				if child_text:
					formatted_text.append(child_text)

		return '\n'.join(formatted_text)

	@staticmethod
	def _build_attributes_string(node: EnhancedDOMTreeNode, include_attributes: list[str], text: str) -> str:
		"""Build the attributes string for an element."""
		attributes_to_include = {}

		# Include HTML attributes
		if node.attributes:
			attributes_to_include.update(
				{
					key: str(value).strip()
					for key, value in node.attributes.items()
					if key in include_attributes and str(value).strip() != ''
				}
			)

		if node.tag_name and node.tag_name.lower() == 'input' and node.attributes:
			input_type = node.attributes.get('type', '').lower()

			# For HTML5 date/time inputs, add a highly visible "format" attribute
			# This makes it IMPOSSIBLE for the model to miss the required format
			if input_type in ['date', 'time', 'datetime-local', 'month', 'week']:
				format_map = {
					'date': 'YYYY-MM-DD',
					'time': 'HH:MM',
					'datetime-local': 'YYYY-MM-DDTHH:MM',
					'month': 'YYYY-MM',
					'week': 'YYYY-W##',
				}
				# Add format as a special attribute that appears prominently
				# This appears BEFORE placeholder in the serialized output
				attributes_to_include['format'] = format_map[input_type]

			# Only add placeholder if it doesn't already exist
			if 'placeholder' in include_attributes and 'placeholder' not in attributes_to_include:
				# Native HTML5 date/time inputs - ISO format required
				if input_type == 'date':
					attributes_to_include['placeholder'] = 'YYYY-MM-DD'
				elif input_type == 'time':
					attributes_to_include['placeholder'] = 'HH:MM'
				elif input_type == 'datetime-local':
					attributes_to_include['placeholder'] = 'YYYY-MM-DDTHH:MM'
				elif input_type == 'month':
					attributes_to_include['placeholder'] = 'YYYY-MM'
				elif input_type == 'week':
					attributes_to_include['placeholder'] = 'YYYY-W##'
				# Tel - suggest format if no pattern attribute
				elif input_type == 'tel' and 'pattern' not in attributes_to_include:
					attributes_to_include['placeholder'] = '123-456-7890'
				# jQuery/Bootstrap/AngularJS datepickers (text inputs with datepicker classes/attributes)
				elif input_type in {'text', ''}:
					class_attr = node.attributes.get('class', '').lower()

					# Check for AngularJS UI Bootstrap datepicker (uib-datepicker-popup attribute)
					# This takes precedence as it's the most specific indicator
					if 'uib-datepicker-popup' in node.attributes:
						# Extract format from uib-datepicker-popup="MM/dd/yyyy"
						date_format = node.attributes.get('uib-datepicker-popup', '')
						if date_format:
							# Use 'expected_format' for clarity - this is the required input format
							attributes_to_include['expected_format'] = date_format
							# Also keep format for consistency with HTML5 date inputs
							attributes_to_include['format'] = date_format
					# Detect jQuery/Bootstrap datepickers by class names
					elif any(indicator in class_attr for indicator in ['datepicker', 'datetimepicker', 'daterangepicker']):
						# Try to get format from data-date-format attribute
						date_format = node.attributes.get('data-date-format', '')
						if date_format:
							attributes_to_include['placeholder'] = date_format
							attributes_to_include['format'] = date_format  # Also add format for jQuery datepickers
						else:
							# Default to common US format for jQuery datepickers
							attributes_to_include['placeholder'] = 'mm/dd/yyyy'
							attributes_to_include['format'] = 'mm/dd/yyyy'
					# Also detect by data-* attributes
					elif any(attr in node.attributes for attr in ['data-datepicker']):
						date_format = node.attributes.get('data-date-format', '')
						if date_format:
							attributes_to_include['placeholder'] = date_format
							attributes_to_include['format'] = date_format
						else:
							attributes_to_include['placeholder'] = 'mm/dd/yyyy'
							attributes_to_include['format'] = 'mm/dd/yyyy'

		# Include accessibility properties
		if node.ax_node and node.ax_node.properties:
			for prop in node.ax_node.properties:
				try:
					if prop.name in include_attributes and prop.value is not None:
						# Convert boolean to lowercase string, keep others as-is
						if isinstance(prop.value, bool):
							attributes_to_include[prop.name] = str(prop.value).lower()
						else:
							prop_value_str = str(prop.value).strip()
							if prop_value_str:
								attributes_to_include[prop.name] = prop_value_str
				except (AttributeError, ValueError):
					continue

		# Special handling for form elements - ensure current value is shown
		# For text inputs, textareas, and selects, prioritize showing the current value from AX tree
		if node.tag_name and node.tag_name.lower() in ['input', 'textarea', 'select']:
			# ALWAYS check AX tree - it reflects actual typed value, DOM attribute may not update
			if node.ax_node and node.ax_node.properties:
				for prop in node.ax_node.properties:
					# Try valuetext first (human-readable display value)
					if prop.name == 'valuetext' and prop.value:
						value_str = str(prop.value).strip()
						if value_str:
							attributes_to_include['value'] = value_str
							break
					# Also try 'value' property directly
					elif prop.name == 'value' and prop.value:
						value_str = str(prop.value).strip()
						if value_str:
							attributes_to_include['value'] = value_str
							break

		if not attributes_to_include:
			return ''

		# Remove duplicate values
		ordered_keys = [key for key in include_attributes if key in attributes_to_include]

		if len(ordered_keys) > 1:
			keys_to_remove = set()
			seen_values = {}

			# Attributes that should never be removed as duplicates (they serve distinct purposes)
			protected_attrs = {'format', 'expected_format', 'placeholder', 'value', 'aria-label', 'title'}

			for key in ordered_keys:
				value = attributes_to_include[key]
				if len(value) > 5:
					if value in seen_values and key not in protected_attrs:
						keys_to_remove.add(key)
					else:
						seen_values[value] = key

			for key in keys_to_remove:
				del attributes_to_include[key]

		# Remove attributes that duplicate accessibility data
		role = node.ax_node.role if node.ax_node else None
		if role and node.node_name == role:
			attributes_to_include.pop('role', None)

		# Remove type attribute if it matches the tag name (e.g. <button type="button">)
		if 'type' in attributes_to_include and attributes_to_include['type'].lower() == node.node_name.lower():
			del attributes_to_include['type']

		# Remove invalid attribute if it's false (only show when true)
		if 'invalid' in attributes_to_include and attributes_to_include['invalid'].lower() == 'false':
			del attributes_to_include['invalid']

		boolean_attrs = {'required'}
		for attr in boolean_attrs:
			if attr in attributes_to_include and attributes_to_include[attr].lower() in {'false', '0', 'no'}:
				del attributes_to_include[attr]

		# Remove aria-expanded if we have expanded (prefer AX tree over HTML attribute)
		if 'expanded' in attributes_to_include and 'aria-expanded' in attributes_to_include:
			del attributes_to_include['aria-expanded']

		attrs_to_remove_if_text_matches = ['aria-label', 'placeholder', 'title']
		for attr in attrs_to_remove_if_text_matches:
			if attributes_to_include.get(attr) and attributes_to_include.get(attr, '').strip().lower() == text.strip().lower():
				del attributes_to_include[attr]

		if attributes_to_include:
			# Format attributes, wrapping empty values in quotes for clarity
			formatted_attrs = []
			for key, value in attributes_to_include.items():
				capped_value = cap_text_length(value, 100)
				# Show empty values as key='' instead of key=
				if not capped_value:
					formatted_attrs.append(f"{key}=''")
				else:
					formatted_attrs.append(f'{key}={capped_value}')
			return ' '.join(formatted_attrs)

		return ''
