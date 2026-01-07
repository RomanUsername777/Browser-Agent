"""
Утилиты для создания оптимизированных Pydantic-схем для использования с LLM.
"""

from typing import Any

from pydantic import BaseModel


class SchemaOptimizer:
	@staticmethod
	def create_optimized_json_schema(
		model: type[BaseModel],
		*,
		remove_min_items: bool = False,
		remove_defaults: bool = False,
	) -> dict[str, Any]:
		"""
		Создать максимально оптимизированную схему, уплощая все $ref/$defs, сохраняя
		ПОЛНЫЕ описания и ВСЕ определения действий. Также обеспечивает совместимость с OpenAI strict mode.

		Args:
			model: Pydantic-модель для оптимизации
			remove_min_items: Если True, удалить minItems из схемы
			remove_defaults: Если True, удалить значения по умолчанию из схемы

		Returns:
			Оптимизированная схема со всеми разрешёнными $refs и совместимостью со strict mode
		"""
		# Генерируем исходную схему
		original_schema = model.model_json_schema()

		# Извлекаем $defs для разрешения ссылок, затем уплощаем всё
		defs_lookup = original_schema.get('$defs', {})

		# Создаём оптимизированную схему с уплощением
		# Передаём флаги в optimize_schema через замыкание
		def optimize_schema(obj: Any, defs_lookup: dict[str, Any] | None = None, *, in_properties: bool = False) -> Any:
			"""Применить все техники оптимизации, включая уплощение всех $ref/$defs"""
			if isinstance(obj, dict):
				optimized: dict[str, Any] = {}
				flattened_ref: dict[str, Any] | None = None

				# Пропускаем ненужные поля И $defs (мы встроим всё)
				skip_fields = ['$defs', 'additionalProperties']

				for key, value in obj.items():
					if key in skip_fields:
						continue

					# Пропускаем метаданные "title", если не итерируемся внутри реальной карты `properties`
					if key == 'title' and not in_properties:
						continue

					# Сохраняем ПОЛНЫЕ описания без обрезки, пропускаем пустые
					elif key == 'description':
						if value:  # Включаем только непустые описания
							optimized[key] = value

					# УПЛОЩЕНИЕ: Разрешаем $ref, встраивая фактическое определение
					elif key == '$ref' and defs_lookup:
						ref_path = value.split('/')[-1]  # Получаем имя определения из "#/$defs/SomeName"
						if ref_path in defs_lookup:
							# Получаем ссылающееся определение и уплощаем его
							referenced_def = defs_lookup[ref_path]
							flattened_ref = optimize_schema(referenced_def, defs_lookup)

					# Обрабатываем поле type - должно рекурсивно обрабатываться, если значение содержит $ref
					elif key == 'type':
						optimized[key] = value if not isinstance(value, (dict, list)) else optimize_schema(value, defs_lookup)

					# Пропускаем minItems/min_items и default, если запрошено (проверяем ПЕРЕД обработкой)
					elif key in ('minItems', 'min_items') and remove_min_items:
						continue  # Пропускаем minItems/min_items
					elif key == 'default' and remove_defaults:
						continue  # Пропускаем значения по умолчанию

					# Рекурсивно оптимизируем вложенные структуры
					elif key in ['items', 'properties']:
						optimized[key] = optimize_schema(
							value,
							defs_lookup,
							in_properties=(key == 'properties'),
						)

					# Сохраняем все структуры anyOf (объединения действий) и разрешаем любые $refs внутри
					elif key == 'anyOf' and isinstance(value, list):
						optimized[key] = [optimize_schema(item, defs_lookup) for item in value]

					# Сохраняем важные поля валидации
					elif key in [
						'default',
						'maxItems',
						'maximum',
						'minItems',
						'min_items',
						'minimum',
						'pattern',
						'required',
						'type',
					]:
						optimized[key] = value if not isinstance(value, (dict, list)) else optimize_schema(value, defs_lookup)

					# Рекурсивно обрабатываем все остальные поля
					else:
						optimized[key] = optimize_schema(value, defs_lookup) if isinstance(value, (dict, list)) else value

				# Если есть уплощённая ссылка, объединяем её с оптимизированными свойствами
				if flattened_ref is not None and isinstance(flattened_ref, dict):
					# Начинаем с уплощённой ссылки как основы
					result = flattened_ref.copy()

					# Объединяем любые обработанные свойства-соседи
					for key, value in optimized.items():
						# Сохраняем описания из исходного объекта, если они существуют
						if key == 'description' and 'description' not in result:
							result[key] = value
						elif key != 'description':  # Не перезаписываем описание из уплощённой ссылки
							result[key] = value

					return result
				else:
					# Нет $ref, просто возвращаем оптимизированный объект
					# КРИТИЧЕСКИ ВАЖНО: Добавляем additionalProperties: false ко ВСЕМ объектам для OpenAI strict mode
					if optimized.get('type') == 'object':
						optimized['additionalProperties'] = False

					return optimized

			elif isinstance(obj, list):
				return [optimize_schema(item, defs_lookup, in_properties=in_properties) for item in obj]
			return obj

		optimized_result = optimize_schema(original_schema, defs_lookup)

		# Убеждаемся, что у нас словарь (должно быть так для корня схемы)
		if not isinstance(optimized_result, dict):
			raise ValueError('Optimized schema result is not a dictionary')

		optimized_schema: dict[str, Any] = optimized_result

		# Дополнительный проход, чтобы убедиться, что ВСЕ объекты имеют additionalProperties: false
		def ensure_additional_properties_false(obj: Any) -> None:
			"""Убедиться, что все объекты имеют additionalProperties: false"""
			if isinstance(obj, dict):
				# Если это тип object, убеждаемся, что additionalProperties = false
				if obj.get('type') == 'object':
					obj['additionalProperties'] = False

				# Рекурсивно применяем ко всем значениям
				for value in obj.values():
					if isinstance(value, (dict, list)):
						ensure_additional_properties_false(value)
			elif isinstance(obj, list):
				for item in obj:
					if isinstance(item, (dict, list)):
						ensure_additional_properties_false(item)

		ensure_additional_properties_false(optimized_schema)
		SchemaOptimizer._make_strict_compatible(optimized_schema)

		# Финальный проход для удаления minItems/min_items и значений по умолчанию, если запрошено
		if remove_min_items or remove_defaults:

			def remove_forbidden_fields(obj: Any) -> None:
				"""Рекурсивно удалить minItems/min_items и значения по умолчанию"""
				if isinstance(obj, dict):
					# Удаляем запрещённые ключи
					if remove_min_items:
						obj.pop('min_items', None)
						obj.pop('minItems', None)
					if remove_defaults:
						obj.pop('default', None)
					# Рекурсивно обрабатываем все значения
					for value in obj.values():
						if isinstance(value, (dict, list)):
							remove_forbidden_fields(value)
				elif isinstance(obj, list):
					for item in obj:
						if isinstance(item, (dict, list)):
							remove_forbidden_fields(item)

			remove_forbidden_fields(optimized_schema)

		return optimized_schema

	@staticmethod
	def _make_strict_compatible(schema: dict[str, Any] | list[Any]) -> None:
		"""Убедиться, что все свойства обязательны для OpenAI strict mode"""
		if isinstance(schema, dict):
			# Сначала рекурсивно применяем к вложенным объектам
			for key, value in schema.items():
				if isinstance(value, (dict, list)) and key != 'required':
					SchemaOptimizer._make_strict_compatible(value)

			# Затем обновляем required для этого уровня
			if 'properties' in schema and 'type' in schema and schema['type'] == 'object':
				# Добавляем все свойства в массив required
				all_props = list(schema['properties'].keys())
				schema['required'] = all_props  # Устанавливаем все свойства как обязательные

		elif isinstance(schema, list):
			for item in schema:
				SchemaOptimizer._make_strict_compatible(item)

	@staticmethod
	def create_gemini_optimized_schema(model: type[BaseModel]) -> dict[str, Any]:
		"""
		Создать оптимизированную для Gemini схему, сохраняя явные массивы `required`, чтобы Gemini
		уважал обязательные поля, определённые вызывающей стороной.

		Args:
			model: Pydantic-модель для оптимизации

		Returns:
			Оптимизированная схема, подходящая для структурированного вывода Gemini
		"""
		return SchemaOptimizer.create_optimized_json_schema(model)
