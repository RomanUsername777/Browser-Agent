"""Модели и утилиты для управления сообщениями агента."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
from pydantic import BaseModel, ConfigDict, Field

from core.ai_models.messages import (
	BaseMessage,
)

if TYPE_CHECKING:
	pass

logger = logging.getLogger(__name__)


# ========== Models ==========

class HistoryItem(BaseModel):
	"""Представляет один элемент истории агента с его данными и строковым представлением"""

	action_results: str | None = None
	error: str | None = None
	evaluation_previous_goal: str | None = None
	memory: str | None = None
	next_goal: str | None = None
	step_number: int | None = None
	system_message: str | None = None

	model_config = ConfigDict(arbitrary_types_allowed=True)

	def model_post_init(self, __context) -> None:
		"""Проверить, что error и system_message не предоставлены одновременно"""
		if self.system_message is not None and self.error is not None:
			raise ValueError('Нельзя иметь одновременно error и system_message')

	def to_string(self) -> str:
		"""Получить строковое представление элемента истории"""
		step_str = 'step_unknown' if self.step_number is None else 'step'

		if self.system_message:
			return self.system_message
		elif self.error:
			return f"""<{step_str}>
{self.error}"""
		else:
			content_parts = []

			# Всегда включать memory
			if self.memory:
				content_parts.append(f'{self.memory}')

			# Включать evaluation_previous_goal только если он не None/пустой
			if self.evaluation_previous_goal:
				content_parts.append(f'{self.evaluation_previous_goal}')

			# Включать next_goal только если он не None/пустой
			if self.next_goal:
				content_parts.append(f'{self.next_goal}')

			if self.action_results:
				content_parts.append(self.action_results)

			content = '\n'.join(content_parts)

			return f"""<{step_str}>
{content}"""


class MessageHistory(BaseModel):
	"""История сообщений"""

	context_messages: list[BaseMessage] = Field(default_factory=list)
	state_message: BaseMessage | None = None
	system_message: BaseMessage | None = None
	model_config = ConfigDict(arbitrary_types_allowed=True)

	def get_messages(self) -> list[BaseMessage]:
		"""Получить все сообщения в правильном порядке: system -> state -> contextual"""
		messages = []
		if self.system_message:
			messages.append(self.system_message)
		if self.state_message:
			messages.append(self.state_message)
		messages.extend(self.context_messages)

		return messages


class MessageManagerState(BaseModel):
	"""Хранит состояние для MessageManager"""

	agent_history_items: list[HistoryItem] = Field(
		default_factory=lambda: [HistoryItem(step_number=0, system_message='Agent initialized')]
	)
	history: MessageHistory = Field(default_factory=MessageHistory)
	read_state_description: str = ''
	# Изображения для включения в следующее сообщение состояния (очищается после каждого шага)
	read_state_images: list[dict[str, Any]] = Field(default_factory=list)
	tool_id: int = 1

	model_config = ConfigDict(arbitrary_types_allowed=True)


# ========== Helper Functions ==========

async def save_conversation(
	input_messages: list[BaseMessage],
	response: Any,
	target: str | Path,
	encoding: str | None = None,
) -> None:
	"""Сохранить историю разговора в файл асинхронно."""
	target_path = Path(target)
	# создать папки, если не существуют
	if target_path.parent:
		await anyio.Path(target_path.parent).mkdir(exist_ok=True, parents=True)

	formatted_text = await _format_conversation(input_messages, response)
	await anyio.Path(target_path).write_text(
		formatted_text,
		encoding=encoding or 'utf-8',
	)


async def _format_conversation(messages: list[BaseMessage], response: Any) -> str:
	"""Отформатировать разговор, включая сообщения и ответ."""
	lines = []

	# Отформатировать сообщения
	for message in messages:
		lines.append(f' {message.role} ')

		lines.append(message.text)
		lines.append('')  # Пустая строка после каждого сообщения

	# Отформатировать ответ
	response_json = response.model_dump_json(exclude_unset=True)
	response_dict = json.loads(response_json)
	lines.append(json.dumps(response_dict, ensure_ascii=False, indent=2))

	return '\n'.join(lines)


# Примечание: _write_messages_to_file и _write_response_to_file объединены в _format_conversation
# Это более эффективно для асинхронных операций и уменьшает файловый ввод-вывод
