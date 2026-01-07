import json
from typing import overload

from anthropic.types import (
	CacheControlEphemeralParam,
	ImageBlockParam,
	MessageParam,
	TextBlockParam,
	ToolUseBlockParam,
	Base64ImageSourceParam,
	URLImageSourceParam,
)

from core.ai_models.messages import (
	BaseMessage,
	ContentPartImageParam,
	ContentPartTextParam,
	SupportedImageMediaType,
	AssistantMessage,
	SystemMessage,
	UserMessage,
)

NonSystemMessage = UserMessage | AssistantMessage


class AnthropicMessageSerializer:
	"""Сериализатор для преобразования между пользовательскими типами сообщений и типами параметров сообщений Anthropic."""

	@staticmethod
	def _is_base64_image(url: str) -> bool:
		"""Проверить, является ли URL изображением в кодировке base64."""
		return url.startswith('data:image/')

	@staticmethod
	def _parse_base64_url(url: str) -> tuple[SupportedImageMediaType, str]:
		"""Распарсить data URL base64 для извлечения типа медиа и данных."""
		# Формат: data:image/jpeg;base64,<data>
		if not url.startswith('data:'):
			raise ValueError(f'Неверный base64 URL: {url}')

		data = url.split(',', 1)[1]
		header = url.split(',', 1)[0]
		media_type = header.split(';')[0].replace('data:', '')

		# Убедиться, что это поддерживаемый тип медиа
		supported_types = ['image/gif', 'image/jpeg', 'image/png', 'image/webp']
		if media_type not in supported_types:
			# По умолчанию jpeg, если не распознан
			media_type = 'image/jpeg'

		return media_type, data  # type: ignore

	@staticmethod
	def _serialize_cache_control(use_cache: bool) -> CacheControlEphemeralParam | None:
		"""Сериализовать управление кэшем."""
		if use_cache:
			return CacheControlEphemeralParam(type='ephemeral')
		return None

	@staticmethod
	def _serialize_content_part_text(part: ContentPartTextParam, use_cache: bool) -> TextBlockParam:
		"""Преобразовать текстовую часть содержимого в TextBlockParam Anthropic."""
		return TextBlockParam(
			cache_control=AnthropicMessageSerializer._serialize_cache_control(use_cache),
			text=part.text,
			type='text',
		)

	@staticmethod
	def _serialize_content_part_image(part: ContentPartImageParam) -> ImageBlockParam:
		"""Преобразовать часть содержимого изображения в ImageBlockParam Anthropic."""
		url = part.image_url.url

		if AnthropicMessageSerializer._is_base64_image(url):
			# Обработать изображения в кодировке base64
			media_type, data = AnthropicMessageSerializer._parse_base64_url(url)
			return ImageBlockParam(
				source=Base64ImageSourceParam(
					data=data,
					media_type=media_type,
					type='base64',
				),
				type='image',
			)
		else:
			# Обработать изображения по URL
			return ImageBlockParam(source=URLImageSourceParam(type='url', url=url), type='image')

	@staticmethod
	def _serialize_content_to_str(
		content: str | list[ContentPartTextParam], use_cache: bool = False
	) -> list[TextBlockParam] | str:
		"""Сериализовать содержимое в строку."""
		cache_control = AnthropicMessageSerializer._serialize_cache_control(use_cache)

		if isinstance(content, str):
			if cache_control:
				return [TextBlockParam(cache_control=cache_control, text=content, type='text')]
			else:
				return content

		serialized_blocks: list[TextBlockParam] = []
		for i, part in enumerate(content):
			is_last = i == len(content) - 1
			if part.type == 'text':
				serialized_blocks.append(
					AnthropicMessageSerializer._serialize_content_part_text(part, use_cache=use_cache and is_last)
				)

		return serialized_blocks

	@staticmethod
	def _serialize_content(
		content: str | list[ContentPartTextParam | ContentPartImageParam],
		use_cache: bool = False,
	) -> str | list[TextBlockParam | ImageBlockParam]:
		"""Сериализовать содержимое в формат Anthropic."""
		if isinstance(content, str):
			if use_cache:
				return [TextBlockParam(cache_control=CacheControlEphemeralParam(type='ephemeral'), text=content, type='text')]
			else:
				return content

		serialized_blocks: list[TextBlockParam | ImageBlockParam] = []
		for i, part in enumerate(content):
			is_last = i == len(content) - 1
			if part.type == 'image_url':
				serialized_blocks.append(AnthropicMessageSerializer._serialize_content_part_image(part))
			elif part.type == 'text':
				serialized_blocks.append(
					AnthropicMessageSerializer._serialize_content_part_text(part, use_cache=use_cache and is_last)
				)

		return serialized_blocks

	@staticmethod
	def _serialize_tool_calls_to_content(tool_calls, use_cache: bool = False) -> list[ToolUseBlockParam]:
		"""Преобразовать вызовы инструментов в формат ToolUseBlockParam Anthropic."""
		blocks: list[ToolUseBlockParam] = []
		for i, tool_call in enumerate(tool_calls):
			# Распарсить строку аргументов JSON в объект

			try:
				input_obj = json.loads(tool_call.function.arguments)
			except json.JSONDecodeError:
				# Если аргументы не являются валидным JSON, использовать как строку
				input_obj = {'arguments': tool_call.function.arguments}

			is_last = i == len(tool_calls) - 1
			blocks.append(
				ToolUseBlockParam(
					cache_control=AnthropicMessageSerializer._serialize_cache_control(use_cache and is_last),
					id=tool_call.id,
					input=input_obj,
					name=tool_call.function.name,
					type='tool_use',
				)
			)
		return blocks

	# region - Serialize overloads
	@overload
	@staticmethod
	def serialize(message: UserMessage) -> MessageParam: ...

	@overload
	@staticmethod
	def serialize(message: SystemMessage) -> SystemMessage: ...

	@overload
	@staticmethod
	def serialize(message: AssistantMessage) -> MessageParam: ...

	@staticmethod
	def serialize(message: BaseMessage) -> MessageParam | SystemMessage:
		"""Сериализовать пользовательское сообщение в MessageParam Anthropic.

		Примечание: Anthropic не имеет роли 'system'. Системные сообщения должны быть
		обработаны отдельно как параметр system в вызове API, а не как сообщение.
		Если здесь передан SystemMessage, он будет преобразован в сообщение пользователя.
		"""
		if isinstance(message, UserMessage):
			content = AnthropicMessageSerializer._serialize_content(message.content, use_cache=message.cache)
			return MessageParam(content=content, role='user')

		elif isinstance(message, SystemMessage):
			# Anthropic не имеет системных сообщений в массиве messages
			# Системные промпты передаются отдельно. Преобразовать в сообщение пользователя.
			return message

		elif isinstance(message, AssistantMessage):
			# Обработать содержимое и вызовы инструментов
			blocks: list[TextBlockParam | ToolUseBlockParam] = []

			# Добавить блоки содержимого, если присутствуют
			if message.content is not None:
				if isinstance(message.content, str):
					# Строковое содержимое: кэшировать только если это единственный/последний блок (нет вызовов инструментов)
					blocks.append(
						TextBlockParam(
							cache_control=AnthropicMessageSerializer._serialize_cache_control(
								message.cache and not message.tool_calls
							),
							text=message.content,
							type='text',
						)
					)
				else:
					# Обработать части содержимого (текст и отказ)
					for i, part in enumerate(message.content):
						# Только последний блок содержимого получает кэш, если нет вызовов инструментов
						is_last_content = (i == len(message.content) - 1) and not message.tool_calls
						if part.type == 'text':
							blocks.append(
								AnthropicMessageSerializer._serialize_content_part_text(
									part, use_cache=message.cache and is_last_content
								)
							)
							# # Примечание: Anthropic не имеет специального типа блока отказа,
							# # поэтому мы преобразуем отказы в текстовые блоки
							# elif part.type == 'refusal':
							# 	blocks.append(TextBlockParam(text=f'[Refusal] {part.refusal}', type='text'))

			# Добавить блоки использования инструментов, если присутствуют
			if message.tool_calls:
				tool_blocks = AnthropicMessageSerializer._serialize_tool_calls_to_content(
					message.tool_calls, use_cache=message.cache
				)
				blocks.extend(tool_blocks)

			# Если нет содержимого или вызовов инструментов, добавить пустой текстовый блок
			# (Anthropic требует хотя бы один блок содержимого)
			if not blocks:
				blocks.append(
					TextBlockParam(
						cache_control=AnthropicMessageSerializer._serialize_cache_control(message.cache),
						text='',
						type='text',
					)
				)

			# Если кэширование включено или у нас несколько блоков, вернуть блоки как есть
			# Иначе упростить одиночные текстовые блоки до простой строки
			if len(blocks) > 1 or message.cache:
				content = blocks
			else:
				# Упрощать только когда нет кэширования и один блок
				single_block = blocks[0]
				if single_block['type'] == 'text' and not single_block.get('cache_control'):
					content = single_block['text']
				else:
					content = blocks

			return MessageParam(
				content=content,
				role='assistant',
			)

		else:
			raise ValueError(f'Unknown message type: {type(message)}')

	@staticmethod
	def _clean_cache_messages(messages: list[NonSystemMessage]) -> list[NonSystemMessage]:
		"""Очистить настройки кэша, чтобы только последнее сообщение с cache=True оставалось закэшированным.

		Из-за того, как работает кэширование Claude, имеет значение только последнее кэшированное сообщение.
		Этот метод автоматически удаляет cache=True из всех сообщений, кроме последнего.

		Args:
			messages: Список несистемных сообщений для очистки

		Returns:
			Список сообщений с очищенными настройками кэша
		"""
		if not messages:
			return messages

		# Создать копию, чтобы не изменять оригинал
		cleaned_messages = [msg.model_copy(deep=True) for msg in messages]

		# Найти последнее сообщение с cache=True
		last_cache_index = -1
		for i in range(len(cleaned_messages) - 1, -1, -1):
			if cleaned_messages[i].cache:
				last_cache_index = i
				break

		# Если нашли закэшированное сообщение, отключить кэш для всех остальных
		if last_cache_index != -1:
			for i, msg in enumerate(cleaned_messages):
				if i != last_cache_index and msg.cache:
					# Установить cache в False для всех сообщений, кроме последнего закэшированного
					msg.cache = False

		return cleaned_messages

	@staticmethod
	def serialize_messages(messages: list[BaseMessage]) -> tuple[list[MessageParam], list[TextBlockParam] | str | None]:
		"""Сериализовать список сообщений, извлекая любое системное сообщение.

		Returns:
		    Кортеж (messages, system_message), где system_message извлечено
		    из любого SystemMessage в списке.
		"""
		messages = [m.model_copy(deep=True) for m in messages]

		# Отделить системные сообщения от обычных сообщений
		normal_messages: list[NonSystemMessage] = []
		system_message: SystemMessage | None = None

		for message in messages:
			if isinstance(message, SystemMessage):
				system_message = message
			else:
				normal_messages.append(message)

		# Очистить кэшированные сообщения, чтобы только последнее сообщение с cache=True оставалось закэшированным
		normal_messages = AnthropicMessageSerializer._clean_cache_messages(normal_messages)

		# Сериализовать обычные сообщения
		serialized_messages: list[MessageParam] = []
		for message in normal_messages:
			serialized_messages.append(AnthropicMessageSerializer.serialize(message))

		# Сериализовать системное сообщение
		serialized_system_message: list[TextBlockParam] | str | None = None
		if system_message:
			serialized_system_message = AnthropicMessageSerializer._serialize_content_to_str(
				system_message.content, use_cache=system_message.cache
			)

		return serialized_messages, serialized_system_message
