from typing import overload

from openai.types.chat import (
	ChatCompletionContentPartImageParam,
	ChatCompletionContentPartRefusalParam,
	ChatCompletionContentPartTextParam,
	ChatCompletionMessageFunctionToolCallParam,
	ChatCompletionMessageParam,
	ChatCompletionAssistantMessageParam,
	ChatCompletionSystemMessageParam,
	ChatCompletionUserMessageParam,
)
from openai.types.chat.chat_completion_content_part_image_param import ImageURL
from openai.types.chat.chat_completion_message_function_tool_call_param import Function

from core.ai_models.messages import (
	BaseMessage,
	ContentPartImageParam,
	ContentPartRefusalParam,
	ContentPartTextParam,
	AssistantMessage,
	SystemMessage,
	ToolCall,
	UserMessage,
)


class OpenAIMessageSerializer:
	"""Сериализатор для преобразования между пользовательскими типами сообщений и типами параметров сообщений OpenAI."""

	@staticmethod
	def _serialize_content_part_text(part: ContentPartTextParam) -> ChatCompletionContentPartTextParam:
		return ChatCompletionContentPartTextParam(type='text', text=part.text)

	@staticmethod
	def _serialize_content_part_image(part: ContentPartImageParam) -> ChatCompletionContentPartImageParam:
		return ChatCompletionContentPartImageParam(
			image_url=ImageURL(detail=part.image_url.detail, url=part.image_url.url),
			type='image_url',
		)

	@staticmethod
	def _serialize_content_part_refusal(part: ContentPartRefusalParam) -> ChatCompletionContentPartRefusalParam:
		return ChatCompletionContentPartRefusalParam(type='refusal', refusal=part.refusal)

	@staticmethod
	def _serialize_user_content(
		content: str | list[ContentPartTextParam | ContentPartImageParam],
	) -> str | list[ChatCompletionContentPartTextParam | ChatCompletionContentPartImageParam]:
		"""Сериализовать содержимое для сообщений пользователя (разрешены текст и изображения)."""
		if isinstance(content, str):
			return content

		serialized_parts: list[ChatCompletionContentPartTextParam | ChatCompletionContentPartImageParam] = []
		for part in content:
			if part.type == 'text':
				serialized_parts.append(OpenAIMessageSerializer._serialize_content_part_text(part))
			if part.type == 'image_url':
				serialized_parts.append(OpenAIMessageSerializer._serialize_content_part_image(part))
		return serialized_parts

	@staticmethod
	def _serialize_system_content(
		content: str | list[ContentPartTextParam],
	) -> str | list[ChatCompletionContentPartTextParam]:
		"""Сериализовать содержимое для системных сообщений (только текст)."""
		if isinstance(content, str):
			return content

		serialized_parts: list[ChatCompletionContentPartTextParam] = []
		for part in content:
			if part.type == 'text':
				serialized_parts.append(OpenAIMessageSerializer._serialize_content_part_text(part))
		return serialized_parts

	@staticmethod
	def _serialize_assistant_content(
		content: str | list[ContentPartTextParam | ContentPartRefusalParam] | None,
	) -> str | list[ChatCompletionContentPartTextParam | ChatCompletionContentPartRefusalParam] | None:
		"""Сериализовать содержимое для сообщений ассистента (разрешены текст и отказ)."""
		if content is None:
			return None
		if isinstance(content, str):
			return content

		serialized_parts: list[ChatCompletionContentPartTextParam | ChatCompletionContentPartRefusalParam] = []
		for part in content:
			if part.type == 'text':
				serialized_parts.append(OpenAIMessageSerializer._serialize_content_part_text(part))
			if part.type == 'refusal':
				serialized_parts.append(OpenAIMessageSerializer._serialize_content_part_refusal(part))
		return serialized_parts

	@staticmethod
	def _serialize_tool_call(tool_call: ToolCall) -> ChatCompletionMessageFunctionToolCallParam:
		return ChatCompletionMessageFunctionToolCallParam(
			function=Function(arguments=tool_call.function.arguments, name=tool_call.function.name),
			id=tool_call.id,
			type='function',
		)

	# endregion

	# region - Serialize overloads
	@overload
	@staticmethod
	def serialize(message: UserMessage) -> ChatCompletionUserMessageParam: ...

	@overload
	@staticmethod
	def serialize(message: SystemMessage) -> ChatCompletionSystemMessageParam: ...

	@overload
	@staticmethod
	def serialize(message: AssistantMessage) -> ChatCompletionAssistantMessageParam: ...

	@staticmethod
	def serialize(message: BaseMessage) -> ChatCompletionMessageParam:
		"""Сериализовать пользовательское сообщение в параметр сообщения OpenAI."""

		if isinstance(message, SystemMessage):
			system_result: ChatCompletionSystemMessageParam = {
				'content': OpenAIMessageSerializer._serialize_system_content(message.content),
				'role': 'system',
			}
			if message.name is not None:
				system_result['name'] = message.name
			return system_result

		elif isinstance(message, UserMessage):
			user_result: ChatCompletionUserMessageParam = {
				'content': OpenAIMessageSerializer._serialize_user_content(message.content),
				'role': 'user',
			}
			if message.name is not None:
				user_result['name'] = message.name
			return user_result

		elif isinstance(message, AssistantMessage):
			# Обработать сериализацию содержимого
			content = None
			if message.content is not None:
				content = OpenAIMessageSerializer._serialize_assistant_content(message.content)

			assistant_result: ChatCompletionAssistantMessageParam = {'role': 'assistant'}

			# Добавить содержимое только если оно не None
			if content is not None:
				assistant_result['content'] = content

			if message.tool_calls:
				assistant_result['tool_calls'] = [OpenAIMessageSerializer._serialize_tool_call(tc) for tc in message.tool_calls]
			if message.refusal is not None:
				assistant_result['refusal'] = message.refusal
			if message.name is not None:
				assistant_result['name'] = message.name

			return assistant_result

		else:
			raise ValueError(f'Неизвестный тип сообщения: {type(message)}')

	@staticmethod
	def serialize_messages(messages: list[BaseMessage]) -> list[ChatCompletionMessageParam]:
		"""Сериализовать список сообщений."""
		return [OpenAIMessageSerializer.serialize(m) for m in messages]
