from collections import defaultdict
from dataclasses import dataclass

from core.dom_processing.models import SimplifiedNode

"""
Вспомогательный класс для поддержания объединения прямоугольников (используется для расчета порядка элементов)
"""


@dataclass(frozen=True, slots=True)
class Rect:
	"""Замкнутый выровненный по осям прямоугольник с (x1,y1) нижний-левый, (x2,y2) верхний-правый."""

	x1: float
	x2: float
	y1: float
	y2: float

	def __post_init__(self):
		if not (self.y1 <= self.y2 and self.x1 <= self.x2):
			return False

	# --- быстрые отношения ----------------------------------------------------
	def area(self) -> float:
		return (self.y2 - self.y1) * (self.x2 - self.x1)

	def intersects(self, other: 'Rect') -> bool:
		return not (other.x2 <= self.x1 or self.x2 <= other.x1 or other.y2 <= self.y1 or self.y2 <= other.y1)

	def contains(self, other: 'Rect') -> bool:
		return self.y1 <= other.y1 and self.x1 <= other.x1 and self.y2 >= other.y2 and self.x2 >= other.x2


class RectUnionPure:
	"""
	Поддерживает *непересекающееся* множество прямоугольников.
	Без внешних зависимостей - подходит для нескольких тысяч прямоугольников.
	"""

	__slots__ = ('_rects',)

	def __init__(self):
		self._rects: list[Rect] = []

	# -----------------------------------------------------------------
	def _split_diff(self, a: Rect, b: Rect) -> list[Rect]:
		r"""
		Вернуть список до 4 прямоугольников = a \ b.
		Предполагает, что a пересекается с b.
		"""
		parts = []

		# Верхний срез
		if a.y2 > b.y2:
			parts.append(Rect(a.x1, a.x2, b.y2, a.y2))
		# Нижний срез
		if a.y1 < b.y1:
			parts.append(Rect(a.x1, a.x2, a.y1, b.y1))

		# Средняя (вертикальная) полоса: перекрытие y это [max(a.y1,b.y1), min(a.y2,b.y2)]
		y_hi = min(a.y2, b.y2)
		y_lo = max(a.y1, b.y1)

		# Правый срез
		if a.x2 > b.x2:
			parts.append(Rect(b.x2, a.x2, y_lo, y_hi))
		# Левый срез
		if a.x1 < b.x1:
			parts.append(Rect(a.x1, b.x1, y_lo, y_hi))

		return parts

	# -----------------------------------------------------------------
	def contains(self, r: Rect) -> bool:
		"""
		True тогда и только тогда, когда r полностью покрыт текущим объединением.
		"""
		if not self._rects:
			return False

		stack = [r]
		for s in self._rects:
			new_stack = []
			for piece in stack:
				if s.contains(piece):
					# кусок полностью исчез
					continue
				if piece.intersects(s):
					new_stack.extend(self._split_diff(piece, s))
				else:
					new_stack.append(piece)
			if not new_stack:  # всё съедено – покрыто
				return True
			stack = new_stack
		return False  # что-то выжило

	# -----------------------------------------------------------------
	def add(self, r: Rect) -> bool:
		"""
		Вставить r, если он еще не покрыт.
		Возвращает True, если объединение выросло.
		"""
		if self.contains(r):
			return False

		pending = [r]
		i = 0
		while i < len(self._rects):
			s = self._rects[i]
			changed = False
			new_pending = []
			for piece in pending:
				if piece.intersects(s):
					new_pending.extend(self._split_diff(piece, s))
					changed = True
				else:
					new_pending.append(piece)
			pending = new_pending
			if changed:
				# s не изменен; продолжить со следующим существующим прямоугольником
				i += 1
			else:
				i += 1

		# Любые оставшиеся куски - новые непересекающиеся области
		self._rects.extend(pending)
		return True


class PaintOrderRemover:
	"""
	Вычисляет, какие элементы должны быть удалены на основе параметра порядка отрисовки.
	"""

	def __init__(self, root: SimplifiedNode):
		self.root = root

	def calculate_paint_order(self) -> None:
		all_simplified_nodes_with_paint_order: list[SimplifiedNode] = []

		def collect_paint_order(node: SimplifiedNode) -> None:
			if (
				node.original_node.snapshot_node
				and node.original_node.snapshot_node.bounds is not None
				and node.original_node.snapshot_node.paint_order is not None
			):
				all_simplified_nodes_with_paint_order.append(node)

			for child in node.children:
				collect_paint_order(child)

		collect_paint_order(self.root)

		grouped_by_paint_order: defaultdict[int, list[SimplifiedNode]] = defaultdict(list)

		for node in all_simplified_nodes_with_paint_order:
			if node.original_node.snapshot_node and node.original_node.snapshot_node.paint_order is not None:
				grouped_by_paint_order[node.original_node.snapshot_node.paint_order].append(node)

		rect_union = RectUnionPure()

		for paint_order, nodes in sorted(grouped_by_paint_order.items(), key=lambda x: -x[0]):
			rects_to_add = []

			for node in nodes:
				if not node.original_node.snapshot_node or not node.original_node.snapshot_node.bounds:
					continue  # не должно произойти по тому, как мы их отфильтровали в первую очередь

				rect = Rect(
					x1=node.original_node.snapshot_node.bounds.x,
					x2=node.original_node.snapshot_node.bounds.x + node.original_node.snapshot_node.bounds.width,
					y1=node.original_node.snapshot_node.bounds.y,
					y2=node.original_node.snapshot_node.bounds.y + node.original_node.snapshot_node.bounds.height,
				)

				if rect_union.contains(rect):
					node.ignored_by_paint_order = True

				# не добавлять к узлам, если opacity меньше 0.95 или background-color прозрачный
				if (
					node.original_node.snapshot_node.computed_styles
					and float(node.original_node.snapshot_node.computed_styles.get('opacity', '1'))
					< 0.8  # это очень основанное на вибрациях число
				) or (
					node.original_node.snapshot_node.computed_styles
					and node.original_node.snapshot_node.computed_styles.get('background-color', 'rgba(0, 0, 0, 0)')
					== 'rgba(0, 0, 0, 0)'
				):
					continue

				rects_to_add.append(rect)

			for rect in rects_to_add:
				rect_union.add(rect)

		return None
