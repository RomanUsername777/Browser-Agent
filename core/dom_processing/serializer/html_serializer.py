# @file purpose: Сериализует улучшенные DOM-деревья в формат HTML, включая shadow roots

from core.dom_processing.models import EnhancedDOMTreeNode, NodeType


class HTMLSerializer:
	"""Сериализует улучшенные DOM-деревья обратно в формат HTML.

	Этот сериализатор восстанавливает HTML из улучшенного DOM-дерева, включая:
	- Содержимое Shadow DOM (как открытое, так и закрытое)
	- Документы содержимого iframe
	- Все атрибуты и текстовые узлы
	- Правильную структуру HTML

	В отличие от getOuterHTML, который захватывает только light DOM, это захватывает полное
	улучшенное дерево, включая shadow roots, которые критичны для современных SPA.
	"""

	def __init__(self, extract_links: bool = False):
		"""Инициализировать HTML-сериализатор.

		Args:
			extract_links: Если True, сохраняет все ссылки. Если False, удаляет атрибуты href.
		"""
		self.extract_links = extract_links

	def serialize(self, node: EnhancedDOMTreeNode, depth: int = 0) -> str:
		"""Сериализовать узел улучшенного DOM-дерева в HTML.

		Args:
			depth: Текущая глубина для отступов (внутреннее использование)
			node: Узел улучшенного DOM-дерева для сериализации

		Returns:
			Строковое представление HTML узла и его потомков
		"""
		if node.node_type == NodeType.DOCUMENT_NODE:
			# Обработать корень документа - сериализовать всех детей
			parts = []
			for child in node.children_and_shadow_roots:
				child_html = self.serialize(child, depth)
				if child_html:
					parts.append(child_html)
			return ''.join(parts)

		elif node.node_type == NodeType.DOCUMENT_FRAGMENT_NODE:
			# Корень Shadow DOM - обернуть в template с атрибутом shadowrootmode
			parts = []

			# Добавить открытие shadow root
			shadow_type = node.shadow_root_type or 'open'
			parts.append(f'<template shadowroot="{shadow_type.lower()}">')

			# Сериализовать детей shadow
			for child in node.children:
				child_html = self.serialize(child, depth + 1)
				if child_html:
					parts.append(child_html)

			# Закрыть shadow root
			parts.append('</template>')

			return ''.join(parts)

		elif node.node_type == NodeType.ELEMENT_NODE:
			parts = []
			tag_name = node.tag_name.lower()

			# Пропустить элементы без содержимого
			if tag_name in {'head', 'link', 'meta', 'script', 'style', 'title'}:
				return ''

			# Пропустить теги code с display:none - они часто содержат JSON-состояние для SPA
			if tag_name == 'code' and node.attributes:
				style = node.attributes.get('style', '')
				# Проверить, скрыт ли элемент (display:none) - вероятно JSON-данные
				if 'display: none' in style or 'display:none' in style.replace(' ', ''):
					return ''
				# Также проверить ID bpr-guid (паттерн JSON-данных LinkedIn)
				element_id = node.attributes.get('id', '')
				if 'bpr-guid' in element_id or 'data' in element_id or 'state' in element_id:
					return ''

			# Пропустить встроенные изображения base64 - обычно это плейсхолдеры или пиксели отслеживания
			if tag_name == 'img' and node.attributes:
				src = node.attributes.get('src', '')
				if src.startswith('data:image/'):
					return ''

			# Открывающий тег
			parts.append(f'<{tag_name}')

			# Добавить атрибуты
			if node.attributes:
				attrs = self._serialize_attributes(node.attributes)
				if attrs:
					parts.append(' ' + attrs)

			# Обработать void-элементы (самозакрывающиеся)
			void_elements = {
				'area',
				'base',
				'br',
				'col',
				'embed',
				'hr',
				'img',
				'input',
				'link',
				'meta',
				'param',
				'source',
				'track',
				'wbr',
			}
			if tag_name in void_elements:
				parts.append(' />')
				return ''.join(parts)

			parts.append('>')

			# Обработать документ содержимого iframe
			if tag_name in {'frame', 'iframe'} and node.content_document:
				# Сериализовать содержимое iframe
				for child in node.content_document.children_nodes or []:
					child_html = self.serialize(child, depth + 1)
					if child_html:
						parts.append(child_html)
			else:
				# Сериализовать shadow roots ПЕРВЫМИ (для декларативного shadow DOM)
				if node.shadow_roots:
					for shadow_root in node.shadow_roots:
						child_html = self.serialize(shadow_root, depth + 1)
						if child_html:
							parts.append(child_html)

				# Затем сериализовать детей light DOM (для проекции слотов)
				for child in node.children:
					child_html = self.serialize(child, depth + 1)
					if child_html:
						parts.append(child_html)

			# Закрывающий тег
			parts.append(f'</{tag_name}>')

			return ''.join(parts)

		elif node.node_type == NodeType.TEXT_NODE:
			# Вернуть текстовое содержимое с базовым экранированием HTML
			if node.node_value:
				return self._escape_html(node.node_value)
			return ''

		elif node.node_type == NodeType.COMMENT_NODE:
			# Пропустить комментарии для уменьшения шума
			return ''

		else:
			# Неизвестный тип узла - пропустить
			return ''

	def _serialize_attributes(self, attributes: dict[str, str]) -> str:
		"""Сериализовать атрибуты элемента в строку атрибутов HTML.

		Args:
			attributes: Словарь имен атрибутов к значениям

		Returns:
			Строка атрибутов HTML (например, 'class="foo" id="bar"')
		"""
		parts = []
		for key, value in attributes.items():
			# Пропустить href, если не извлекаем ссылки
			if not self.extract_links and key == 'href':
				continue

			# Пропустить data-* атрибуты, так как они часто содержат JSON-полезные нагрузки
			# Они используются современными SPA (React, Vue, Angular) для управления состоянием
			if key.startswith('data-'):
				continue

			# Обработать булевы атрибуты
			if value is None or value == '':
				parts.append(key)
			else:
				# Экранировать значение атрибута
				escaped_value = self._escape_attribute(value)
				parts.append(f'{key}="{escaped_value}"')

		return ' '.join(parts)

	def _escape_html(self, text: str) -> str:
		"""Экранировать специальные символы HTML в текстовом содержимом.

		Args:
			text: Сырое текстовое содержимое

		Returns:
			HTML-экранированный текст
		"""
		return text.replace('&', '&amp;').replace('>', '&gt;').replace('<', '&lt;')

	def _escape_attribute(self, value: str) -> str:
		"""Экранировать специальные символы HTML в значениях атрибутов.

		Args:
			value: Сырое значение атрибута

		Returns:
			HTML-экранированное значение атрибута
		"""
		return value.replace('&', '&amp;').replace('>', '&gt;').replace('<', '&lt;').replace("'", '&#x27;').replace('"', '&quot;')
