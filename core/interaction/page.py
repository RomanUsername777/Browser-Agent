"""Класс Page для операций уровня страницы."""

from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel

from core.interaction.helpers import get_key_info
from core.dom_processing.serializer.serializer import DOMTreeSerializer
from core.dom_processing.manager import DomService
from core.ai_models.messages import SystemMessage, UserMessage

T = TypeVar('T', bound=BaseModel)

if TYPE_CHECKING:
	from cdp_use.cdp.dom.commands import (
		DescribeNodeParameters,
		QuerySelectorAllParameters,
	)
	from cdp_use.cdp.emulation.commands import SetDeviceMetricsOverrideParameters
	from cdp_use.cdp.input.commands import (
		DispatchKeyEventParameters,
	)
	from cdp_use.cdp.page.commands import CaptureScreenshotParameters, NavigateParameters, NavigateToHistoryEntryParameters
	from cdp_use.cdp.runtime.commands import EvaluateParameters
	from cdp_use.cdp.target.commands import (
		AttachToTargetParameters,
		GetTargetInfoParameters,
	)
	from cdp_use.cdp.target.types import TargetInfo

	from core.session.session import ChromeSession
	from core.ai_models.models import BaseChatModel

	from .element import Element
	from .mouse import Mouse


class Page:
	"""Операции со страницей (вкладка или iframe)."""

	def __init__(
		self, browser_session: 'ChromeSession', target_id: str, session_id: str | None = None, llm: 'BaseChatModel | None' = None
	):
		self._browser_session = browser_session
		self._client = browser_session.cdp_client
		self._target_id = target_id
		self._session_id: str | None = session_id
		self._mouse: 'Mouse | None' = None

		self._llm = llm

	async def _ensure_session(self) -> str:
		"""Обеспечить наличие session ID для этого target."""
		if not self._session_id:
			attach_params: 'AttachToTargetParameters' = {'targetId': self._target_id, 'flatten': True}
			attach_result = await self._client.send.Target.attachToTarget(attach_params)
			self._session_id = attach_result['sessionId']

			# Включить необходимые домены
			import asyncio

			await asyncio.gather(
				self._client.send.Page.enable(session_id=self._session_id),
				self._client.send.DOM.enable(session_id=self._session_id),
				self._client.send.Runtime.enable(session_id=self._session_id),
				self._client.send.Network.enable(session_id=self._session_id),
			)

		return self._session_id

	@property
	async def session_id(self) -> str:
		"""Получить session ID для этого target.

		@dev Передать это в произвольный CDP вызов
		"""
		return await self._ensure_session()

	@property
	async def mouse(self) -> 'Mouse':
		"""Получить интерфейс мыши для этого target."""
		if not self._mouse:
			session_id = await self._ensure_session()
			from .mouse import Mouse

			self._mouse = Mouse(self._browser_session, session_id, self._target_id)
		return self._mouse

	async def reload(self) -> None:
		"""Перезагрузить target."""
		session_id = await self._ensure_session()
		await self._client.send.Page.reload(session_id=session_id)

	async def get_element(self, backend_node_id: int) -> 'Element':
		"""Получить элемент по его backend node ID."""
		session_id = await self._ensure_session()

		from .element import Element as Element_

		return Element_(self._browser_session, backend_node_id, session_id)

	async def evaluate(self, page_function: str, *args) -> str:
		"""Выполнить JavaScript в target.

		Args:
			page_function: JavaScript код, который ДОЛЖЕН начинаться с формата (...args) =>
			*args: Аргументы для передачи в функцию

		Returns:
			Строковое представление результата выполнения JavaScript.
			Объекты и массивы преобразуются в JSON-строку.
		"""
		session_id = await self._ensure_session()

		# Очистить и исправить общие проблемы парсинга JavaScript строк
		js_function = self._fix_javascript_string(page_function)

		# Принудительно установить формат стрелочной функции
		if not (js_function.startswith('(') and '=>' in js_function):
			raise ValueError(f'JavaScript code must start with (...args) => format. Got: {js_function[:50]}...')

		# Построить выражение - вызвать стрелочную функцию с предоставленными args
		if args:
			# Преобразовать args в JSON представление для безопасной передачи
			import json

			json_args = [json.dumps(arg) for arg in args]
			js_expression = f'({js_function})({", ".join(json_args)})'
		else:
			js_expression = f'({js_function})()'


		eval_params: 'EvaluateParameters' = {'expression': js_expression, 'returnByValue': True, 'awaitPromise': True}
		eval_result = await self._client.send.Runtime.evaluate(
			eval_params,
			session_id=session_id,
		)

		if 'exceptionDetails' in eval_result:
			raise RuntimeError(f'JavaScript evaluation failed: {eval_result["exceptionDetails"]}')

		result_value = eval_result.get('result', {}).get('value')

		# Всегда возвращать строковое представление
		if result_value is None:
			return ''
		elif isinstance(result_value, str):
			return result_value
		else:
			# Преобразовать объекты, числа, булевы значения в строку
			import json

			try:
				return json.dumps(result_value) if isinstance(result_value, (dict, list)) else str(result_value)
			except (TypeError, ValueError):
				return str(result_value)

	def _fix_javascript_string(self, js_code: str) -> str:
		"""Исправить общие проблемы парсинга JavaScript строк при записи как Python строки."""

		# Выполнить только минимальную, безопасную очистку
		cleaned_code = js_code.strip()

		# Исправить только самые распространенные и безопасные проблемы:

		# 1. Удалить очевидные Python строковые обертки кавычек, если они есть
		if (cleaned_code.startswith('"') and cleaned_code.endswith('"')) or (cleaned_code.startswith("'") and cleaned_code.endswith("'")):
			# Проверить, является ли это обернутой строкой (не частью JS синтаксиса)
			unwrapped = cleaned_code[1:-1]
			if unwrapped.count('"') + unwrapped.count("'") == 0 or '() =>' in unwrapped:
				cleaned_code = unwrapped

		# 2. Исправить только явно экранированные кавычки, которые не должны быть
		# Но быть очень консервативным - только если мы уверены, что это артефакт Python строки
		if '\\"' in cleaned_code and cleaned_code.count('\\"') > cleaned_code.count('"'):
			cleaned_code = cleaned_code.replace('\\"', '"')
		if "\\'" in cleaned_code and cleaned_code.count("\\'") > cleaned_code.count("'"):
			cleaned_code = cleaned_code.replace("\\'", "'")

		# 3. Только базовая нормализация пробелов
		cleaned_code = cleaned_code.strip()

		# Финальная проверка - убедиться, что не пусто
		if not cleaned_code:
			raise ValueError('JavaScript code is empty after cleaning')

		return cleaned_code

	async def screenshot(self, format: str = 'png', quality: int | None = None) -> str:
		"""Сделать скриншот и вернуть base64-закодированное изображение.

		Args:
		    format: Формат изображения ('jpeg', 'png', 'webp')
		    quality: Качество 0-100 для формата JPEG

		Returns:
		    Base64-закодированные данные изображения
		"""
		session_id = await self._ensure_session()

		screenshot_params: 'CaptureScreenshotParameters' = {'format': format}

		if quality is not None and format.lower() == 'jpeg':
			screenshot_params['quality'] = quality

		screenshot_result = await self._client.send.Page.captureScreenshot(screenshot_params, session_id=session_id)

		return screenshot_result['data']

	async def press(self, key: str) -> None:
		"""Нажать клавишу на странице (отправляет ввод с клавиатуры в фокусированный элемент или страницу)."""
		session_id = await self._ensure_session()

		# Обработать комбинации клавиш, такие как "Control+A"
		if '+' in key:
			key_parts = key.split('+')
			modifier_keys = key_parts[:-1]
			primary_key = key_parts[-1]

			# Вычислить битовую маску модификатора
			modifier_bitmask = 0
			modifier_mapping = {'Alt': 1, 'Control': 2, 'Meta': 4, 'Shift': 8}
			for modifier_key in modifier_keys:
				modifier_bitmask |= modifier_mapping.get(modifier_key, 0)

			# Нажать клавиши модификаторов
			for modifier_key in modifier_keys:
				key_code, virtual_key_code = get_key_info(modifier_key)
				mod_down_params: 'DispatchKeyEventParameters' = {'type': 'keyDown', 'key': modifier_key, 'code': key_code}
				if virtual_key_code is not None:
					mod_down_params['windowsVirtualKeyCode'] = virtual_key_code
				await self._client.send.Input.dispatchKeyEvent(mod_down_params, session_id=session_id)

			# Нажать основную клавишу с битовой маской модификаторов
			primary_code, primary_vk_code = get_key_info(primary_key)
			primary_key_down: 'DispatchKeyEventParameters' = {
				'type': 'keyDown',
				'key': primary_key,
				'code': primary_code,
				'modifiers': modifier_bitmask,
			}
			if primary_vk_code is not None:
				primary_key_down['windowsVirtualKeyCode'] = primary_vk_code
			await self._client.send.Input.dispatchKeyEvent(primary_key_down, session_id=session_id)

			primary_key_up: 'DispatchKeyEventParameters' = {
				'type': 'keyUp',
				'key': primary_key,
				'code': primary_code,
				'modifiers': modifier_bitmask,
			}
			if primary_vk_code is not None:
				primary_key_up['windowsVirtualKeyCode'] = primary_vk_code
			await self._client.send.Input.dispatchKeyEvent(primary_key_up, session_id=session_id)

			# Отпустить клавиши модификаторов
			for modifier_key in reversed(modifier_keys):
				key_code, virtual_key_code = get_key_info(modifier_key)
				mod_up_params: 'DispatchKeyEventParameters' = {'type': 'keyUp', 'key': modifier_key, 'code': key_code}
				if virtual_key_code is not None:
					mod_up_params['windowsVirtualKeyCode'] = virtual_key_code
				await self._client.send.Input.dispatchKeyEvent(mod_up_params, session_id=session_id)
		else:
			# Простое нажатие клавиши
			key_code, virtual_key_code = get_key_info(key)
			single_key_down: 'DispatchKeyEventParameters' = {'type': 'keyDown', 'key': key, 'code': key_code}
			if virtual_key_code is not None:
				single_key_down['windowsVirtualKeyCode'] = virtual_key_code
			await self._client.send.Input.dispatchKeyEvent(single_key_down, session_id=session_id)

			single_key_up: 'DispatchKeyEventParameters' = {'type': 'keyUp', 'key': key, 'code': key_code}
			if virtual_key_code is not None:
				single_key_up['windowsVirtualKeyCode'] = virtual_key_code
			await self._client.send.Input.dispatchKeyEvent(single_key_up, session_id=session_id)

	async def set_viewport_size(self, width: int, height: int) -> None:
		"""Установить размер viewport."""
		session_id = await self._ensure_session()

		viewport_params: 'SetDeviceMetricsOverrideParameters' = {
			'width': width,
			'height': height,
			'deviceScaleFactor': 1.0,
			'mobile': False,
		}
		await self._client.send.Emulation.setDeviceMetricsOverride(
			viewport_params,
			session_id=session_id,
		)

	# Свойства target (из CDP getTargetInfo)
	async def get_target_info(self) -> 'TargetInfo':
		"""Получить информацию о target."""
		target_params: 'GetTargetInfoParameters' = {'targetId': self._target_id}
		target_result = await self._client.send.Target.getTargetInfo(target_params)
		return target_result['targetInfo']

	async def get_url(self) -> str:
		"""Получить текущий URL."""
		target_info = await self.get_target_info()
		return target_info.get('url', '')

	async def get_title(self) -> str:
		"""Получить текущий заголовок."""
		target_info = await self.get_target_info()
		return target_info.get('title', '')

	async def goto(self, url: str) -> None:
		"""Навигировать этот target к URL."""
		session_id = await self._ensure_session()

		navigate_params: 'NavigateParameters' = {'url': url}
		await self._client.send.Page.navigate(navigate_params, session_id=session_id)

	async def navigate(self, url: str) -> None:
		"""Псевдоним для goto."""
		await self.goto(url)

	async def go_back(self) -> None:
		"""Навигировать назад в истории."""
		session_id = await self._ensure_session()

		try:
			# Получить историю навигации
			nav_history = await self._client.send.Page.getNavigationHistory(session_id=session_id)
			history_index = nav_history['currentIndex']
			history_entries = nav_history['entries']

			# Проверить, можем ли мы идти назад
			if history_index <= 0:
				raise RuntimeError('Cannot go back - no previous entry in history')

			# Навигировать к предыдущей записи
			prev_entry_id = history_entries[history_index - 1]['id']
			back_params: 'NavigateToHistoryEntryParameters' = {'entryId': prev_entry_id}
			await self._client.send.Page.navigateToHistoryEntry(back_params, session_id=session_id)

		except Exception as back_error:
			raise RuntimeError(f'Failed to navigate back: {back_error}')

	async def go_forward(self) -> None:
		"""Навигировать вперед в истории."""
		session_id = await self._ensure_session()

		try:
			# Получить историю навигации
			nav_history = await self._client.send.Page.getNavigationHistory(session_id=session_id)
			history_index = nav_history['currentIndex']
			history_entries = nav_history['entries']

			# Проверить, можем ли мы идти вперед
			if history_index >= len(history_entries) - 1:
				raise RuntimeError('Cannot go forward - no next entry in history')

			# Навигировать к следующей записи
			next_entry_id = history_entries[history_index + 1]['id']
			forward_params: 'NavigateToHistoryEntryParameters' = {'entryId': next_entry_id}
			await self._client.send.Page.navigateToHistoryEntry(forward_params, session_id=session_id)

		except Exception as forward_error:
			raise RuntimeError(f'Failed to navigate forward: {forward_error}')

	# Методы поиска элементов (должны быть реализованы на основе DOM запросов)
	async def get_elements_by_css_selector(self, selector: str) -> list['Element']:
		"""Получить элементы по CSS селектору."""
		session_id = await self._ensure_session()

		# Получить документ сначала
		document_result = await self._client.send.DOM.getDocument(session_id=session_id)
		doc_node_id = document_result['root']['nodeId']

		# Запрос селектора всех
		selector_params: 'QuerySelectorAllParameters' = {'nodeId': doc_node_id, 'selector': selector}
		selector_result = await self._client.send.DOM.querySelectorAll(selector_params, session_id=session_id)

		found_elements = []
		from .element import Element as Element_

		# Преобразовать node IDs в backend node IDs
		for dom_node_id in selector_result['nodeIds']:
			# Получить backend node ID
			describe_params: 'DescribeNodeParameters' = {'nodeId': dom_node_id}
			describe_result = await self._client.send.DOM.describeNode(describe_params, session_id=session_id)
			backend_node = describe_result['node']['backendNodeId']
			found_elements.append(Element_(self._browser_session, backend_node, session_id))

		return found_elements

	# AI МЕТОДЫ

	@property
	def dom_service(self) -> 'DomService':
		"""Получить DOM сервис для этого target."""
		return DomService(self._browser_session)

	async def get_element_by_prompt(self, prompt: str, llm: 'BaseChatModel | None' = None) -> 'Element | None':
		"""Получить элемент по промпту."""
		await self._ensure_session()
		llm = llm or self._llm

		if not llm:
			raise ValueError('LLM not provided')

		dom_service = self.dom_service

		# Lazy fetch all_frames inside get_dom_tree if needed (for cross-origin iframes)
		enhanced_dom_tree, _ = await dom_service.get_dom_tree(target_id=self._target_id, all_frames=None)

		session_id = self._browser_session.id
		serialized_dom_state, _ = DOMTreeSerializer(
			enhanced_dom_tree, None, paint_order_filtering=True, session_id=session_id
		).serialize_accessible_elements()

		llm_representation = serialized_dom_state.llm_representation()

		system_message = SystemMessage(
			content="""You are an AI created to find an element on a page by a prompt.

<browser_state>
Interactive Elements: All interactive elements will be provided in format as [index]<type>text</type> where
- index: Numeric identifier for interaction
- type: HTML element type (button, input, etc.)
- text: Element description

Examples:
[33]<div>User form</div>
[35]<button aria-label='Submit form'>Submit</button>

Note that:
- Only elements with numeric indexes in [] are interactive
- (stacked) indentation (with \t) is important and means that the element is a (html) child of the element above (with a lower index)
- Pure text elements without [] are not interactive.
</browser_state>

Your task is to find an element index (if any) that matches the prompt (written in <prompt> tag).

If non of the elements matches the, return None.

Before you return the element index, reason about the state and elements for a sentence or two."""
		)

		state_message = UserMessage(
			content=f"""
			<browser_state>
			{llm_representation}
			</browser_state>

			<prompt>
			{prompt}
			</prompt>
			"""
		)

		class ElementResponse(BaseModel):
			# thinking: str
			element_highlight_index: int | None

		llm_response = await llm.ainvoke(
			[
				system_message,
				state_message,
			],
			output_format=ElementResponse,
		)

		element_highlight_index = llm_response.completion.element_highlight_index

		if element_highlight_index is None or element_highlight_index not in serialized_dom_state.selector_map:
			return None

		element = serialized_dom_state.selector_map[element_highlight_index]

		from .element import Element as Element_

		return Element_(self._browser_session, element.backend_node_id, self._session_id)

	async def must_get_element_by_prompt(self, prompt: str, llm: 'BaseChatModel | None' = None) -> 'Element':
		"""Get an element by a prompt.

		@dev LLM can still return None, this just raises an error if the element is not found.
		"""
		element = await self.get_element_by_prompt(prompt, llm)
		if element is None:
			raise ValueError(f'No element found for prompt: {prompt}')

		return element

	async def extract_content(self, prompt: str, structured_output: type[T], llm: 'BaseChatModel | None' = None) -> T:
		"""Extract structured content from the current page using LLM.

		Extracts clean markdown from the page and sends it to LLM for structured data extraction.

		Args:
			prompt: Description of what content to extract
			structured_output: Pydantic BaseModel class defining the expected output structure
			llm: Language model to use for extraction

		Returns:
			The structured BaseModel instance with extracted content
		"""
		llm = llm or self._llm

		if not llm:
			raise ValueError('LLM not provided')

		# Extract clean markdown using the same method as in tools/service.py
		try:
			content, content_stats = await self._extract_clean_markdown()
		except Exception as e:
			raise RuntimeError(f'Could not extract clean markdown: {type(e).__name__}')

		# System prompt for structured extraction
		system_prompt = """
You are an expert at extracting structured data from the markdown of a webpage.

<input>
You will be given a query and the markdown of a webpage that has been filtered to remove noise and advertising content.
</input>

<instructions>
- You are tasked to extract information from the webpage that is relevant to the query.
- You should ONLY use the information available in the webpage to answer the query. Do not make up information or provide guess from your own knowledge.
- If the information relevant to the query is not available in the page, your response should mention that.
- If the query asks for all items, products, etc., make sure to directly list all of them.
- Return the extracted content in the exact structured format specified.
</instructions>

<output>
- Your output should present ALL the information relevant to the query in the specified structured format.
- Do not answer in conversational format - directly output the relevant information in the structured format.
</output>
""".strip()

		# Build prompt with just query and content
		prompt_content = f'<query>\n{prompt}\n</query>\n\n<webpage_content>\n{content}\n</webpage_content>'

		# Send to LLM with structured output
		import asyncio

		try:
			response = await asyncio.wait_for(
				llm.ainvoke(
					[SystemMessage(content=system_prompt), UserMessage(content=prompt_content)], output_format=structured_output
				),
				timeout=120.0,
			)

			# Return the structured output BaseModel instance
			return response.completion
		except Exception as e:
			raise RuntimeError(str(e))

	async def _extract_clean_markdown(self, extract_links: bool = False) -> tuple[str, dict]:
		"""Extract clean markdown from the current page using enhanced DOM tree.

		Uses the shared markdown extractor for consistency with tools/service.py.
		"""
		from core.dom_processing.markdown_extractor import extract_clean_markdown

		dom_service = self.dom_service
		return await extract_clean_markdown(dom_service=dom_service, target_id=self._target_id, extract_links=extract_links)
