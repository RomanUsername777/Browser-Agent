from core.dom_processing.models import EnhancedDOMTreeNode, NodeType


class ClickableElementDetector:
	@staticmethod
	def is_interactive(node: EnhancedDOMTreeNode) -> bool:
		"""Проверить, является ли этот узел кликабельным/интерактивным, используя улучшенную оценку."""

		# Пропустить узлы, не являющиеся элементами
		if node.node_type != NodeType.ELEMENT_NODE:
			return False

		# # если ax игнорируется, пропустить
		# if node.ax_node and node.ax_node.ignored:
		# 	return False

		# удалить узлы html и body
		if node.tag_name in {'body', 'html'}:
			return False

		# КРИТИЧЕСКИ ВАЖНО: Проверка isClickable из DOMSnapshot ПЕРВОЙ
		# Это захватывает элементы, которые Chrome считает кликабельными, даже без явных ролей/атрибутов
		# Это ключ к обнаружению кликабельных кнопок на веб-страницах
		if node.snapshot_node and node.snapshot_node.is_clickable:
			return True

		# Элементы IFRAME должны быть интерактивными, если они достаточно большие, чтобы потенциально нуждаться в прокрутке
		# Маленькие iframe (< 100px ширина или высота) вряд ли имеют прокручиваемое содержимое
		if node.tag_name and (node.tag_name.upper() == 'FRAME' or node.tag_name.upper() == 'IFRAME'):
			if node.snapshot_node and node.snapshot_node.bounds:
				height = node.snapshot_node.bounds.height
				width = node.snapshot_node.bounds.width
				# Включать только iframe больше 100x100px
				if height > 100 and width > 100:
					return True

		# РАСШИРЕННАЯ ПРОВЕРКА РАЗМЕРА: Разрешить все элементы, включая размер 0 (они могут быть интерактивными оверлеями и т.д.)
		# Примечание: Элементы размера 0 все еще могут быть интерактивными (например, невидимые кликабельные оверлеи)
		# Видимость определяется отдельно стилями CSS, а не только размером ограничивающей рамки

		# ОБНАРУЖЕНИЕ ЭЛЕМЕНТОВ ПОИСКА: Проверить классы и атрибуты, связанные с поиском
		if node.attributes:
			search_indicators = {
				'find',
				'glass',
				'lookup',
				'magnify',
				'query',
				'search',
				'search-btn',
				'search-button',
				'search-icon',
				'searchbox',
			}

			# Проверить имена классов на индикаторы поиска
			class_list = node.attributes.get('class', '').lower().split()
			if any(indicator in ' '.join(class_list) for indicator in search_indicators):
				return True

			# Проверить id на индикаторы поиска
			element_id = node.attributes.get('id', '').lower()
			if any(indicator in element_id for indicator in search_indicators):
				return True

			# Проверить data-атрибуты на функциональность поиска
			for attr_name, attr_value in node.attributes.items():
				if attr_name.startswith('data-') and any(indicator in attr_value.lower() for indicator in search_indicators):
					return True

		# Улучшенные проверки свойств доступности - только прямые четкие индикаторы
		if node.ax_node and node.ax_node.properties:
			for prop in node.ax_node.properties:
				try:
					# aria hidden
					if prop.name == 'hidden' and prop.value:
						return False

					# aria disabled
					if prop.name == 'disabled' and prop.value:
						return False

					# Прямые индикаторы интерактивности
					if prop.name in ['editable', 'focusable', 'settable'] and prop.value:
						return True

					# Свойства интерактивного состояния (наличие указывает на интерактивный виджет)
					if prop.name in ['checked', 'expanded', 'pressed', 'selected']:
						# Эти свойства существуют только на интерактивных элементах
						return True

					# Интерактивность, связанная с формами
					if prop.name in ['autocomplete', 'required'] and prop.value:
						return True

					# Элементы с клавиатурными сокращениями являются интерактивными
					if prop.name == 'keyshortcuts' and prop.value:
						return True
				except (AttributeError, ValueError):
					# Пропустить свойства, которые мы не можем обработать
					continue

		# УЛУЧШЕННАЯ ПРОВЕРКА ТЕГОВ: Включить действительно интерактивные элементы
		# Примечание: 'label' удален - метки обрабатываются другими проверками атрибутов ниже - иначе метки с атрибутом "for" могут уничтожить реальный кликабельный элемент на apartments.com
		interactive_tags = {
			'a',
			'button',
			'details',
			'input',
			'optgroup',
			'option',
			'select',
			'summary',
			'textarea',
		}
		# Проверить с регистронезависимым сравнением
		if node.tag_name and node.tag_name.lower() in interactive_tags:
			return True


		# Третичная проверка: элементы с интерактивными атрибутами
		if node.attributes:
			# Проверить обработчики событий или интерактивные атрибуты
			interactive_attributes = {'onclick', 'onkeydown', 'onkeyup', 'onmousedown', 'onmouseup', 'tabindex'}
			if any(attr in node.attributes for attr in interactive_attributes):
				return True

			# Проверить интерактивные ARIA роли
			if 'role' in node.attributes:
				interactive_roles = {
					'button',
					'checkbox',
					'combobox',
					'link',
					'menuitem',
					'option',
					'radio',
					'search',
					'searchbox',
					'slider',
					'spinbutton',
					'tab',
					'textbox',
				}
				if node.attributes['role'] in interactive_roles:
					return True

		# Четвертичная проверка: роли дерева доступности
		if node.ax_node and node.ax_node.role:
			interactive_ax_roles = {
				'button',
				'checkbox',
				'combobox',
				'link',
				'listbox',
				'menuitem',
				'option',
				'radio',
				'search',
				'searchbox',
				'slider',
				'spinbutton',
				'tab',
				'textbox',
			}
			if node.ax_node.role in interactive_ax_roles:
				return True

		# ПРОВЕРКА ИКОНОК И МАЛЕНЬКИХ ЭЛЕМЕНТОВ: Элементы, которые могут быть иконками
		if (
			node.snapshot_node
			and node.snapshot_node.bounds
			and 10 <= node.snapshot_node.bounds.height <= 50  # Элементы размера иконки
			and 10 <= node.snapshot_node.bounds.width <= 50
		):
			# Проверить, имеет ли этот маленький элемент интерактивные свойства
			if node.attributes:
				# Маленькие элементы с этими атрибутами, вероятно, являются интерактивными иконками
				icon_attributes = {'aria-label', 'class', 'data-action', 'onclick', 'role'}
				if any(attr in node.attributes for attr in icon_attributes):
					return True

		# Финальный запасной вариант: стиль курсора указывает на интерактивность (для случаев, которые Chrome пропустил)
		if node.snapshot_node and node.snapshot_node.cursor_style and node.snapshot_node.cursor_style == 'pointer':
			return True

		return False
