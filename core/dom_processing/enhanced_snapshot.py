"""
Расширенная обработка DOM-снимков для извлечения структуры и интерактивности страницы.

Модуль содержит функции для разбора DOMSnapshot (Chrome DevTools Protocol)
и получения видимости, кликабельности, стилей курсора и других параметров макета.
"""

from cdp_use.cdp.domsnapshot.commands import CaptureSnapshotReturns
from cdp_use.cdp.domsnapshot.types import (
	LayoutTreeSnapshot,
	NodeTreeSnapshot,
	RareBooleanData,
)

from core.dom_processing.models import DOMRect, EnhancedSnapshotNode

# Only the ESSENTIAL computed styles for interactivity and visibility detection
REQUIRED_COMPUTED_STYLES = [
	# Only styles actually accessed in the codebase (prevents Chrome crashes on heavy sites)
	'display',  # Used in service.py visibility detection
	'visibility',  # Used in service.py visibility detection
	'opacity',  # Used in service.py visibility detection
	'overflow',  # Used in views.py scrollability detection
	'overflow-x',  # Used in views.py scrollability detection
	'overflow-y',  # Used in views.py scrollability detection
	'cursor',  # Used in enhanced_snapshot.py cursor extraction
	'pointer-events',  # Used for clickability logic
	'position',  # Used for visibility logic
	'background-color',  # Used for visibility logic
]


def _parse_rare_boolean_data(rare_boolean_data: RareBooleanData, node_index: int) -> bool | None:
	"""Разобрать редкие булевы данные из снимка - возвращает True, если node_index находится в редких данных."""
	return node_index in rare_boolean_data['index']


def _parse_computed_styles(string_array: list[str], style_index_list: list[int]) -> dict[str, str]:
	"""Разобрать вычисленные стили из дерева макета используя индексы строк."""
	computed_styles_dict = {}
	for style_idx, string_index in enumerate(style_index_list):
		if style_idx < len(REQUIRED_COMPUTED_STYLES) and 0 <= string_index < len(string_array):
			computed_styles_dict[REQUIRED_COMPUTED_STYLES[style_idx]] = string_array[string_index]
	return computed_styles_dict


def build_snapshot_lookup(
	dom_snapshot: CaptureSnapshotReturns,
	pixel_ratio: float = 1.0,
) -> dict[int, EnhancedSnapshotNode]:
	"""Построить таблицу поиска от backend node ID к расширенным данным снимка со всем вычисленным заранее."""
	lookup_table: dict[int, EnhancedSnapshotNode] = {}

	if not dom_snapshot['documents']:
		return lookup_table

	string_list = dom_snapshot['strings']

	for doc in dom_snapshot['documents']:
		node_tree: NodeTreeSnapshot = doc['nodes']
		layout_tree: LayoutTreeSnapshot = doc['layout']

		# Построить поиск от backend node id к индексу снимка
		node_id_to_index_map = {}
		if 'backendNodeId' in node_tree:
			for idx, node_id in enumerate(node_tree['backendNodeId']):
				node_id_to_index_map[node_id] = idx

		# ПРОИЗВОДИТЕЛЬНОСТЬ: Предварительно построить карту индексов макета для устранения O(n²) двойных поисков
		# Сохранить исходное поведение: использовать ПЕРВОЕ вхождение для дубликатов
		layout_idx_map = {}
		if layout_tree and 'nodeIndex' in layout_tree:
			for layout_index, snapshot_node_index in enumerate(layout_tree['nodeIndex']):
				if snapshot_node_index not in layout_idx_map:  # Сохранить только первое вхождение
					layout_idx_map[snapshot_node_index] = layout_index

		# Построить поиск снимка для каждого backend node id
		for node_id, snapshot_idx in node_id_to_index_map.items():
			clickable_flag = None
			if 'isClickable' in node_tree:
				clickable_flag = _parse_rare_boolean_data(node_tree['isClickable'], snapshot_idx)

			# Найти соответствующий узел макета
			cursor_value = None
			visibility_flag = None
			bbox = None
			styles_dict = {}

			# Искать узел дерева макета, который соответствует этому узлу снимка
			paint_order_value = None
			client_rectangles = None
			scroll_rectangles = None
			stacking_context_list = None
			if snapshot_idx in layout_idx_map:
				layout_index = layout_idx_map[snapshot_idx]
				if layout_index < len(layout_tree.get('bounds', [])):
					# Разобрать ограничивающий прямоугольник
					bounding_data = layout_tree['bounds'][layout_index]
					if len(bounding_data) >= 4:
						# ВАЖНО: координаты CDP в пикселях устройства, преобразовать в CSS пиксели
						# путем деления на коэффициент пикселей устройства
						device_x, device_y, device_w, device_h = bounding_data[0], bounding_data[1], bounding_data[2], bounding_data[3]

						# Применить масштабирование коэффициента пикселей устройства для преобразования пикселей устройства в CSS пиксели
						bbox = DOMRect(
							x=device_x / pixel_ratio,
							y=device_y / pixel_ratio,
							width=device_w / pixel_ratio,
							height=device_h / pixel_ratio,
						)

					# Разобрать вычисленные стили для этого узла макета
					if layout_index < len(layout_tree.get('styles', [])):
						style_index_array = layout_tree['styles'][layout_index]
						styles_dict = _parse_computed_styles(string_list, style_index_array)
						cursor_value = styles_dict.get('cursor')

					# Извлечь порядок отрисовки, если доступен
					if layout_index < len(layout_tree.get('paintOrders', [])):
						paint_order_value = layout_tree.get('paintOrders', [])[layout_index]

					# Извлечь client rects, если доступны
					client_rects_array = layout_tree.get('clientRects', [])
					if layout_index < len(client_rects_array):
						client_rect_item = client_rects_array[layout_index]
						if client_rect_item and len(client_rect_item) >= 4:
							client_rectangles = DOMRect(
								x=client_rect_item[0],
								y=client_rect_item[1],
								width=client_rect_item[2],
								height=client_rect_item[3],
							)

					# Извлечь scroll rects, если доступны
					scroll_rects_array = layout_tree.get('scrollRects', [])
					if layout_index < len(scroll_rects_array):
						scroll_rect_item = scroll_rects_array[layout_index]
						if scroll_rect_item and len(scroll_rect_item) >= 4:
							scroll_rectangles = DOMRect(
								x=scroll_rect_item[0],
								y=scroll_rect_item[1],
								width=scroll_rect_item[2],
								height=scroll_rect_item[3],
							)

					# Извлечь stacking contexts, если доступны
					if layout_index < len(layout_tree.get('stackingContexts', [])):
						stacking_context_list = layout_tree.get('stackingContexts', {}).get('index', [])[layout_index]

			lookup_table[node_id] = EnhancedSnapshotNode(
				is_clickable=clickable_flag,
				cursor_style=cursor_value,
				bounds=bbox,
				clientRects=client_rectangles,
				scrollRects=scroll_rectangles,
				computed_styles=styles_dict if styles_dict else None,
				paint_order=paint_order_value,
				stacking_contexts=stacking_context_list,
			)

	return lookup_table
