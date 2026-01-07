# @file purpose: Краткий сериализатор для оценки DOM-деревьев - оптимизирован для написания LLM-запросов

import logging

from core.dom_processing.models import cap_text_length
from core.dom_processing.models import (
	EnhancedDOMTreeNode,
	NodeType,
	SimplifiedNode,
)

logger = logging.getLogger(__name__)

# Критичные атрибуты для написания запросов и взаимодействия с формами
# Примечание: удалены 'id' и 'class' для принудительного использования более надежных структурных селекторов
EVAL_KEY_ATTRIBUTES = [
	'alt',  # для изображений
	'aria-checked',
	'aria-expanded',
	'aria-invalid',
	'aria-label',
	'aria-pressed',
	'aria-selected',
	'aria-valuemax',
	'aria-valuemin',
	'aria-valuenow',
	'checked',
	'class',  # Удалено - может содержать специальные символы, принуждает использовать структурные селекторы
	'data-qa',  # data-qa важен для идентификации интерактивных элементов
	'data-testid',
	'disabled',
	'id',  # Удалено - может содержать специальные символы, принуждает использовать структурные селекторы
	'max',
	'maxlength',
	'min',
	'minlength',
	'name',
	'pattern',
	'placeholder',
	'readonly',
	'required',
	'role',
	'selected',
	'step',
	'title',  # полезно для подсказок/контекста ссылок
	'value',
]

# Семантические элементы, которые всегда должны отображаться
SEMANTIC_ELEMENTS = {
	'a',
	'article',
	'audio',
	'body',  # Всегда показывать body
	'button',
	'footer',
	'form',
	'h1',
	'h2',
	'h3',
	'h4',
	'h5',
	'h6',
	'header',
	'html',  # Всегда показывать корень документа
	'iframe',
	'img',
	'input',
	'label',
	'li',
	'main',
	'nav',
	'ol',
	'section',
	'select',
	'table',
	'tbody',
	'td',
	'textarea',
	'th',
	'thead',
	'tr',
	'ul',
	'video',
}

# Элементы-контейнеры, которые могут быть свернуты, если оборачивают только один дочерний элемент
COLLAPSIBLE_CONTAINERS = {'article', 'div', 'section', 'span'}

# Дочерние SVG-элементы для пропуска (только декоративные, без интерактивной ценности)
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


class DOMEvalSerializer:
	"""Ультра-краткий сериализатор DOM для быстрого написания LLM-запросов."""

	@staticmethod
	def serialize_tree(node: SimplifiedNode | None, include_attributes: list[str], depth: int = 0) -> str:
		"""
		Сериализовать полную структуру DOM-дерева для понимания LLM.

		Стратегия:
		- Показывать ВСЕ элементы для сохранения структуры DOM
		- Неинтерактивные элементы показывают только имя тега
		- Интерактивные элементы показывают полные атрибуты + [index]
		- Только самозакрывающиеся теги (без закрывающих тегов)
		"""
		if not node:
			return ''

		# Пропустить исключенные узлы, но обработать дочерние элементы
		if hasattr(node, 'excluded_by_parent') and node.excluded_by_parent:
			return DOMEvalSerializer._serialize_children(node, include_attributes, depth)

		# Пропустить узлы, помеченные как should_display=False
		if not node.should_display:
			return DOMEvalSerializer._serialize_children(node, include_attributes, depth)

		formatted_text = []
		depth_str = depth * '\t'

		if node.original_node.node_type == NodeType.ELEMENT_NODE:
			tag = node.original_node.tag_name.lower()
			is_visible = node.original_node.snapshot_node and node.original_node.is_visible

			# Элементы-контейнеры, которые должны показываться, даже если невидимы (могут иметь видимые дочерние элементы)
			container_tags = {'article', 'aside', 'body', 'div', 'footer', 'header', 'html', 'main', 'nav', 'section'}

			# КРИТИЧНО: Всегда сериализовать интерактивные элементы (в selector_map), даже если невидимы
			# Это необходимо для кнопок в модальных окнах/оверлеях, которые Chrome считает кликабельными
			# но могут не пройти проверки видимости
			
			if not is_visible and tag not in container_tags and tag not in ['frame', 'iframe'] and not node.is_interactive:
				return DOMEvalSerializer._serialize_children(node, include_attributes, depth)

			# Специальная обработка для iframe - показать их с содержимым
			if tag in ['frame', 'iframe']:
				return DOMEvalSerializer._serialize_iframe(node, include_attributes, depth)

			# Пропустить SVG-элементы полностью - это просто декоративная графика без интерактивной ценности
			# Показать сам тег <svg>, чтобы указать на графику, но не рекурсировать в дочерние элементы
			if tag == 'svg':
				line = f'{depth_str}'
				# Добавить [i_X] только для интерактивных SVG-элементов
				if node.is_interactive:
					line += f'[i_{node.original_node.backend_node_id}] '
				line += '<svg'
				attributes_str = DOMEvalSerializer._build_compact_attributes(node.original_node)
				if attributes_str:
					line += f' {attributes_str}'
				line += ' /> <!-- SVG content collapsed -->'
				return line

			# Пропустить дочерние SVG-элементы полностью (path, rect, g, circle и т.д.)
			if tag in SVG_ELEMENTS:
				return ''

			# Построить компактную строку атрибутов
			attributes_str = DOMEvalSerializer._build_compact_attributes(node.original_node)

			# Решить, должен ли этот элемент быть показан
			has_children = len(node.children) > 0
			has_text_content = DOMEvalSerializer._has_direct_text(node)
			has_useful_attrs = bool(attributes_str)
			is_semantic = tag in SEMANTIC_ELEMENTS

			# Построить компактное представление элемента
			line = f'{depth_str}'
			# Добавить обозначение backend node ID - [i_X] только для интерактивных элементов
			if node.is_interactive:
				line += f'[i_{node.original_node.backend_node_id}] '
			# Неинтерактивные элементы не получают обозначение индекса
			line += f'<{tag}'

			if attributes_str:
				line += f' {attributes_str}'

			# Добавить информацию о прокрутке, если элемент прокручиваемый
			if node.original_node.should_show_scroll_info:
				scroll_text = node.original_node.get_scroll_info_text()
				if scroll_text:
					line += f' scroll="{scroll_text}"'

			# Добавить встроенный текст, если присутствует (оставить на той же строке для компактности)
			inline_text = DOMEvalSerializer._get_inline_text(node)

			# ВАЖНО: для кнопок и ссылок всегда показываем текст, даже если это контейнер
			# Это критично для кнопок, которые могут быть вложены в span
			is_button_or_link = tag in ('a', 'button') or (node.original_node.attributes and node.original_node.attributes.get('role') == 'button')
			
			# Для контейнеров (html, body, div и т.д.) всегда показывать дочерние элементы, даже если есть встроенный текст
			# Для других элементов встроенный текст заменяет дочерние элементы (более компактно)
			is_container = tag in container_tags

			if inline_text and (not is_container or is_button_or_link):
				line += f'>{inline_text}'
			else:
				line += ' />'

			formatted_text.append(line)

			# Обработать дочерние элементы (всегда для контейнеров, только если нет inline_text для других)
			if has_children and (is_container or not inline_text):
				children_text = DOMEvalSerializer._serialize_children(node, include_attributes, depth + 1)
				if children_text:
					formatted_text.append(children_text)

		elif node.original_node.node_type == NodeType.TEXT_NODE:
			# Текстовые узлы обрабатываются встроенно со своим родителем
			pass

		elif node.original_node.node_type == NodeType.DOCUMENT_FRAGMENT_NODE:
			# Shadow DOM - просто показать дочерние элементы напрямую с минимальным маркером
			if node.children:
				formatted_text.append(f'{depth_str}#shadow')
				children_text = DOMEvalSerializer._serialize_children(node, include_attributes, depth + 1)
				if children_text:
					formatted_text.append(children_text)

		return '\n'.join(formatted_text)

	@staticmethod
	def _serialize_children(node: SimplifiedNode, include_attributes: list[str], depth: int) -> str:
		"""Вспомогательная функция для сериализации всех дочерних элементов узла."""
		children_output = []

		# Проверить, является ли родитель контейнером списка (ul, ol)
		is_list_container = node.original_node.node_type == NodeType.ELEMENT_NODE and node.original_node.tag_name.lower() in [
			'ol',
			'ul',
		]

		# Отслеживать элементы списка и последовательные ссылки
		consecutive_link_count = 0
		li_count = 0
		max_consecutive_links = 50
		max_list_items = 50
		total_links_skipped = 0

		for child in node.children:
			# Получить имя тега для этого дочернего элемента
			current_tag = None
			if child.original_node.node_type == NodeType.ELEMENT_NODE:
				current_tag = child.original_node.tag_name.lower()

			# Если мы в контейнере списка и этот дочерний элемент - элемент li
			if is_list_container and current_tag == 'li':
				li_count += 1
				# Пропустить элементы li после 50-го
				if li_count > max_list_items:
					continue

			# Отслеживать последовательные теги якорей (ссылки)
			if current_tag == 'a':
				consecutive_link_count += 1
				# Пропустить ссылки после 50-й последовательной
				if consecutive_link_count > max_consecutive_links:
					total_links_skipped += 1
					continue
			else:
				# Сбросить счетчик при встрече с элементом, не являющимся ссылкой
				# Но сначала добавить сообщение об обрезке, если мы пропустили ссылки
				if total_links_skipped > 0:
					depth_str = depth * '\t'
					children_output.append(f'{depth_str}... ({total_links_skipped} more links in this list)')
					total_links_skipped = 0
				consecutive_link_count = 0

			child_text = DOMEvalSerializer.serialize_tree(child, include_attributes, depth)
			if child_text:
				children_output.append(child_text)

		# Добавить сообщение об обрезке, если мы пропустили элементы в конце
		if is_list_container and li_count > max_list_items:
			depth_str = depth * '\t'
			children_output.append(
				f'{depth_str}... ({li_count - max_list_items} more items in this list (truncated) use evaluate to get more.'
			)

		# Добавить сообщение об обрезке для ссылок, если мы пропустили какие-либо в конце
		if total_links_skipped > 0:
			depth_str = depth * '\t'
			children_output.append(
				f'{depth_str}... ({total_links_skipped} more links in this list) (truncated) use evaluate to get more.'
			)

		return '\n'.join(children_output)

	@staticmethod
	def _build_compact_attributes(node: EnhancedDOMTreeNode) -> str:
		"""Build ultra-compact attributes string with only key attributes."""
		attrs = []

		# Prioritize attributes that help with query writing
		if node.attributes:
			for attr in EVAL_KEY_ATTRIBUTES:
				if attr in node.attributes:
					value = str(node.attributes[attr]).strip()
					if not value:
						continue

					# Special handling for different attributes
					if attr == 'class':
						# For class, limit to first 2 classes to save space
						classes = value.split()[:3]
						value = ' '.join(classes)
					elif attr == 'href':
						# For href, cap at 20 chars to save space
						value = cap_text_length(value, 80)
					else:
						# Cap at 25 chars for other attributes
						value = cap_text_length(value, 80)

					attrs.append(f'{attr}="{value}"')

		# Note: We intentionally don't add role from ax_node here because:
		# 1. If role is explicitly set in HTML, it's already captured above via EVAL_KEY_ATTRIBUTES
		# 2. Inferred roles from AX tree (like link, listitem, LineBreak) are redundant with the tag name
		# 3. This reduces noise - <a href="..." role="link"> is redundant, we already know <a> is a link

		return ' '.join(attrs)

	@staticmethod
	def _has_direct_text(node: SimplifiedNode) -> bool:
		"""Check if node has direct text children (not nested in other elements)."""
		for child in node.children:
			if child.original_node.node_type == NodeType.TEXT_NODE:
				text = child.original_node.node_value.strip() if child.original_node.node_value else ''
				if len(text) > 1:
					return True
		return False

	@staticmethod
	def _get_inline_text(node: SimplifiedNode) -> str:
		"""Get text content to display inline (max 80 chars).
		
		Uses original_node.get_all_children_text() to get text from the ORIGINAL DOM,
		not from SimplifiedNode.children which may have filtered out nested spans.
		This is critical for buttons with nested spans like <button><span><span><span>Button Text</span></span></span></button>
		"""
		# Use the original DOM node to get ALL nested text, bypassing SimplifiedNode filtering
		text = node.original_node.get_all_children_text().strip() if node.original_node else ''
		if not text or len(text) <= 1:
			return ''
		return cap_text_length(text, 80)

	@staticmethod
	def _serialize_iframe(node: SimplifiedNode, include_attributes: list[str], depth: int) -> str:
		"""Handle iframe serialization with content document."""
		formatted_text = []
		depth_str = depth * '\t'
		tag = node.original_node.tag_name.lower()

		# Build minimal iframe marker with key attributes
		attributes_str = DOMEvalSerializer._build_compact_attributes(node.original_node)
		line = f'{depth_str}<{tag}'
		if attributes_str:
			line += f' {attributes_str}'

		# Add scroll info for iframe content
		if node.original_node.should_show_scroll_info:
			scroll_text = node.original_node.get_scroll_info_text()
			if scroll_text:
				line += f' scroll="{scroll_text}"'

		line += ' />'
		formatted_text.append(line)

		# If iframe has content document, serialize its content
		if node.original_node.content_document:
			# Add marker for iframe content
			formatted_text.append(f'{depth_str}\t#iframe-content')

			# Process content document children
			for child_node in node.original_node.content_document.children_nodes or []:
				# Process html documents
				if child_node.tag_name.lower() == 'html':
					# Find and serialize body content only (skip head)
					for html_child in child_node.children:
						if html_child.tag_name.lower() == 'body':
							for body_child in html_child.children:
								# Recursively process body children (iframe content)
								DOMEvalSerializer._serialize_document_node(
									body_child, formatted_text, include_attributes, depth + 2, is_iframe_content=True
								)
							break  # Stop after processing body
				else:
					# Not an html element - serialize directly
					DOMEvalSerializer._serialize_document_node(
						child_node, formatted_text, include_attributes, depth + 1, is_iframe_content=True
					)

		return '\n'.join(formatted_text)

	@staticmethod
	def _serialize_document_node(
		dom_node: EnhancedDOMTreeNode,
		output: list[str],
		include_attributes: list[str],
		depth: int,
		is_iframe_content: bool = True,
	) -> None:
		"""Helper to serialize a document node without SimplifiedNode wrapper.

		Args:
			is_iframe_content: If True, be more permissive with visibility checks since
				iframe content might not have snapshot data from parent page.
		"""
		depth_str = depth * '\t'

		if dom_node.node_type == NodeType.ELEMENT_NODE:
			tag = dom_node.tag_name.lower()

			# For iframe content, be permissive - show all semantic elements even without snapshot data
			# For regular content, skip invisible elements
			if is_iframe_content:
				# Only skip if we have snapshot data AND it's explicitly invisible
				# If no snapshot data, assume visible (cross-origin iframe content)
				is_visible = (not dom_node.snapshot_node) or dom_node.is_visible
			else:
				# Regular strict visibility check
				is_visible = dom_node.snapshot_node and dom_node.is_visible

			if not is_visible:
				return

			# Check if semantic or has useful attributes
			is_semantic = tag in SEMANTIC_ELEMENTS
			attributes_str = DOMEvalSerializer._build_compact_attributes(dom_node)

			if not is_semantic and not attributes_str:
				# Skip but process children
				for child in dom_node.children:
					DOMEvalSerializer._serialize_document_node(
						child, output, include_attributes, depth, is_iframe_content=is_iframe_content
					)
				return

			# Build element line
			line = f'{depth_str}<{tag}'
			if attributes_str:
				line += f' {attributes_str}'

			# Get direct text content
			text_parts = []
			for child in dom_node.children:
				if child.node_type == NodeType.TEXT_NODE and child.node_value:
					text = child.node_value.strip()
					if text and len(text) > 1:
						text_parts.append(text)

			if text_parts:
				combined = ' '.join(text_parts)
				line += f'>{cap_text_length(combined, 100)}'
			else:
				line += ' />'

			output.append(line)

			# Process non-text children
			for child in dom_node.children:
				if child.node_type != NodeType.TEXT_NODE:
					DOMEvalSerializer._serialize_document_node(
						child, output, include_attributes, depth + 1, is_iframe_content=is_iframe_content
					)
