"""Обработчик действий браузера - dropdown."""

import asyncio
import json
from typing import TYPE_CHECKING

from core.dom_processing.manager import EnhancedDOMTreeNode
from core.session.events import DropdownOptionsRequest, DropdownSelectRequest
from core.session.models import BrowserError, URLNotAllowedError
from core.observability import observe_debug

if TYPE_CHECKING:
	from core.session.monitors.watchdogs.default_action_watchdog import DefaultActionWatchdog


class DropdownHandler:
	"""Обработчик dropdown для DefaultActionWatchdog."""

	def __init__(self, watchdog: "DefaultActionWatchdog"):
		"""Инициализация обработчика с ссылкой на watchdog."""
		self.watchdog = watchdog
		self.browser_session = watchdog.browser_session
		self.browser_controller = watchdog.browser_controller
		self.logger = watchdog.logger

	async def on_DropdownOptionsRequest(self, event: DropdownOptionsRequest) -> dict[str, str]:
		"""Обработать запрос получения опций dropdown с CDP."""
		try:
			# Использовать предоставленный узел
			dom_node = event.node
			log_index = dom_node.backend_node_id or 'unknown'

			# Получить CDP сессию для этого узла
			cdp_connection = await self.browser_session.cdp_client_for_node(dom_node)

			# Преобразовать узел в object ID для CDP операций
			try:
				resolve_result = await cdp_connection.cdp_client.send.DOM.resolveNode(
					params={'backendNodeId': dom_node.backend_node_id}, session_id=cdp_connection.session_id
				)
				resolved_object = resolve_result.get('object', {})
				js_object_id = resolved_object.get('objectId')
				if not js_object_id:
					raise ValueError('Could not get object ID from resolved node')
			except Exception as resolve_error:
				raise ValueError(f'Failed to resolve node to object: {resolve_error}') from resolve_error

			# Использовать JavaScript для извлечения опций dropdown
			extract_script = """
			function() {
				const startElement = this;

				// Function to check if an element is a dropdown and extract options
				function checkDropdownElement(element) {
					// Check if it's a native select element
					if (element.tagName.toLowerCase() === 'select') {
						return {
							type: 'select',
							options: Array.from(element.options).map((opt, idx) => ({
								text: opt.text.trim(),
								value: opt.value,
								index: idx,
								selected: opt.selected
							})),
							id: element.id || '',
							name: element.name || '',
							source: 'target'
						};
					}

					// Check if it's an ARIA dropdown/menu
					const role = element.getAttribute('role');
					if (role === 'menu' || role === 'listbox' || role === 'combobox') {
						// Find all menu items/options
						const menuItems = element.querySelectorAll('[role="menuitem"], [role="option"]');
						const options = [];

						menuItems.forEach((item, idx) => {
							const text = item.textContent ? item.textContent.trim() : '';
							if (text) {
								options.push({
									text: text,
									value: item.getAttribute('data-value') || text,
									index: idx,
									selected: item.getAttribute('aria-selected') === 'true' || item.classList.contains('selected')
								});
							}
						});

						return {
							type: 'aria',
							options: options,
							id: element.id || '',
							name: element.getAttribute('aria-label') || '',
							source: 'target'
						};
					}

					// Check if it's a Semantic UI dropdown or similar
					if (element.classList.contains('dropdown') || element.classList.contains('ui')) {
						const menuItems = element.querySelectorAll('.item, .option, [data-value]');
						const options = [];

						menuItems.forEach((item, idx) => {
							const text = item.textContent ? item.textContent.trim() : '';
							if (text) {
								options.push({
									text: text,
									value: item.getAttribute('data-value') || text,
									index: idx,
									selected: item.classList.contains('selected') || item.classList.contains('active')
								});
							}
						});

						if (options.length > 0) {
							return {
								type: 'custom',
								options: options,
								id: element.id || '',
								name: element.getAttribute('aria-label') || '',
								source: 'target'
							};
						}
					}

					return null;
				}

				// Function to recursively search children up to specified depth
				function searchChildrenForDropdowns(element, maxDepth, currentDepth = 0) {
					if (currentDepth >= maxDepth) return null;

					// Check all direct children
					for (let child of element.children) {
						// Check if this child is a dropdown
						const result = checkDropdownElement(child);
						if (result) {
							result.source = `child-depth-${currentDepth + 1}`;
							return result;
						}

						// Recursively check this child's children
						const childResult = searchChildrenForDropdowns(child, maxDepth, currentDepth + 1);
						if (childResult) {
							return childResult;
						}
					}

					return null;
				}

				// First check the target element itself
				let dropdownResult = checkDropdownElement(startElement);
				if (dropdownResult) {
					return dropdownResult;
				}

				// If target element is not a dropdown, search children up to depth 4
				dropdownResult = searchChildrenForDropdowns(startElement, 4);
				if (dropdownResult) {
					return dropdownResult;
				}

				return {
					error: `Element and its children (depth 4) are not recognizable dropdown types (tag: ${startElement.tagName}, role: ${startElement.getAttribute('role')}, classes: ${startElement.className})`
				};
			}
			"""

			execution_result = await cdp_connection.cdp_client.send.Runtime.callFunctionOn(
				params={
					'functionDeclaration': extract_script,
					'objectId': js_object_id,
					'returnByValue': True,
				},
				session_id=cdp_connection.session_id,
			)

			extracted_data = execution_result.get('result', {}).get('value', {})

			if extracted_data.get('error'):
				raise BrowserError(message=extracted_data['error'], long_term_memory=extracted_data['error'])

			if not extracted_data.get('options'):
				error_message = f'No options found in dropdown at index {log_index}'
				return {
					'error': error_message,
					'short_term_memory': error_message,
					'long_term_memory': error_message,
					'backend_node_id': str(log_index),
				}

			# Форматировать опции для отображения
			option_list = []
			for option_item in extracted_data['options']:
				# Использовать JSON кодирование для точного совпадения строк
				text_json = json.dumps(option_item['text'])
				selection_status = ' (selected)' if option_item.get('selected') else ''
				option_list.append(f'{option_item["index"]}: text={text_json}, value={json.dumps(option_item["value"])}{selection_status}')

			type_name = extracted_data.get('type', 'select')
			element_description = f'Index: {log_index}, Type: {type_name}, ID: {extracted_data.get("id", "none")}, Name: {extracted_data.get("name", "none")}'
			source_location = extracted_data.get('source', 'unknown')

			if source_location == 'target':
				result_message = f'Found {type_name} dropdown ({element_description}):\n' + '\n'.join(option_list)
			else:
				result_message = f'Found {type_name} dropdown in {source_location} ({element_description}):\n' + '\n'.join(option_list)
			result_message += (
				f'\n\nUse the exact text or value string (without quotes) in select_dropdown(index={log_index}, text=...)'
			)

			if source_location == 'target':
				self.logger.info(f'📋 Found {len(extracted_data["options"])} dropdown options for index {log_index}')
			else:
				self.logger.info(
					f'📋 Found {len(extracted_data["options"])} dropdown options for index {log_index} in {source_location}'
				)

			# Создать структурированную память для ответа
			short_memory = result_message
			long_memory = f'Got dropdown options for index {log_index}'

			# Вернуть данные dropdown как dict со структурированной памятью
			return {
				'type': type_name,
				'options': json.dumps(extracted_data['options']),  # Преобразовать список в JSON строку для dict[str, str] типа
				'element_info': element_description,
				'source': source_location,
				'formatted_options': '\n'.join(option_list),
				'message': result_message,
				'short_term_memory': short_memory,
				'long_term_memory': long_memory,
				'backend_node_id': str(log_index),
			}

		except BrowserError:
			# Повторно выбросить BrowserError как есть, чтобы сохранить структурированную память
			raise
		except TimeoutError:
			timeout_message = f'Failed to get dropdown options for index {log_index} due to timeout.'
			self.logger.error(timeout_message)
			raise BrowserError(message=timeout_message, long_term_memory=timeout_message)
		except Exception as dropdown_error:
			base_message = 'Failed to get dropdown options'
			full_error = f'{base_message}: {str(dropdown_error)}'
			self.logger.error(full_error)
			raise BrowserError(
				message=full_error, long_term_memory=f'Failed to get dropdown options for index {log_index}.'
			)


	async def on_DropdownSelectRequest(self, event: DropdownSelectRequest) -> dict[str, str]:
		"""Обработать запрос выбора опции dropdown с CDP."""
		try:
			# Использовать предоставленный узел
			dom_node = event.node
			log_index = dom_node.backend_node_id or 'unknown'
			option_text = event.text

			# Получить CDP сессию для этого узла
			cdp_connection = await self.browser_session.cdp_client_for_node(dom_node)

			# Преобразовать узел в object ID для CDP операций
			try:
				resolve_result = await cdp_connection.cdp_client.send.DOM.resolveNode(
					params={'backendNodeId': dom_node.backend_node_id}, session_id=cdp_connection.session_id
				)
				resolved_object = resolve_result.get('object', {})
				js_object_id = resolved_object.get('objectId')
				if not js_object_id:
					raise ValueError('Could not get object ID from resolved node')
			except Exception as resolve_error:
				raise ValueError(f'Failed to resolve node to object: {resolve_error}') from resolve_error
			try:
				# Использовать JavaScript для выбора опции
				select_script = """
				function(targetText) {
					const startElement = this;
					// Function to attempt selection on a dropdown element
					function attemptSelection(element) {
						// Handle native select elements
						if (element.tagName.toLowerCase() === 'select') {
							const options = Array.from(element.options);
							const targetTextLower = targetText.toLowerCase();
							for (const option of options) {
								const optionTextLower = option.text.trim().toLowerCase();
								const optionValueLower = option.value.toLowerCase();
								// Match against both text and value (case-insensitive)
								if (optionTextLower === targetTextLower || optionValueLower === targetTextLower) {
									// Focus the element FIRST (important for Svelte/Vue/React and other reactive frameworks)
									// This simulates the user focusing on the dropdown before changing it
									element.focus();
									// Then set the value
									element.value = option.value;
									option.selected = true;
									// Trigger all necessary events for reactive frameworks
									// 1. input event - critical for Vue's v-model and Svelte's bind:value
									const inputEvent = new Event('input', { bubbles: true, cancelable: true });
									element.dispatchEvent(inputEvent);
									// 2. change event - traditional form validation and framework reactivity
									const changeEvent = new Event('change', { bubbles: true, cancelable: true });
									element.dispatchEvent(changeEvent);
									// 3. blur event - completes the interaction, triggers validation
									element.blur();
									return {
										success: true,
										message: `Selected option: ${option.text.trim()} (value: ${option.value})`,
										value: option.value
									};
								}
							}
							// Return available options as separate field
							const availableOptions = options.map(opt => ({
								text: opt.text.trim(),
								value: opt.value
							}));
							return {
								success: false,
								error: `Option with text or value '${targetText}' not found in select element`,
								availableOptions: availableOptions
							};
						}
						// Handle ARIA dropdowns/menus
						const role = element.getAttribute('role');
						if (role === 'menu' || role === 'listbox' || role === 'combobox') {
							const menuItems = element.querySelectorAll('[role="menuitem"], [role="option"]');
							const targetTextLower = targetText.toLowerCase();
							for (const item of menuItems) {
								if (item.textContent) {
									const itemTextLower = item.textContent.trim().toLowerCase();
									const itemValueLower = (item.getAttribute('data-value') || '').toLowerCase();
									// Match against both text and data-value (case-insensitive)
									if (itemTextLower === targetTextLower || itemValueLower === targetTextLower) {
										// Clear previous selections
										menuItems.forEach(mi => {
											mi.setAttribute('aria-selected', 'false');
											mi.classList.remove('selected');
										});
										// Select this item
										item.setAttribute('aria-selected', 'true');
										item.classList.add('selected');
										// Trigger click and change events
										item.click();
										const clickEvent = new MouseEvent('click', { view: window, bubbles: true, cancelable: true });
										item.dispatchEvent(clickEvent);
										return {
											success: true,
											message: `Selected ARIA menu item: ${item.textContent.trim()}`
										};
									}
								}
							}
							// Return available options as separate field
							const availableOptions = Array.from(menuItems).map(item => ({
								text: item.textContent ? item.textContent.trim() : '',
								value: item.getAttribute('data-value') || ''
							})).filter(opt => opt.text || opt.value);
							return {
								success: false,
								error: `Menu item with text or value '${targetText}' not found`,
								availableOptions: availableOptions
							};
						}
						// Handle Semantic UI or custom dropdowns
						if (element.classList.contains('dropdown') || element.classList.contains('ui')) {
							const menuItems = element.querySelectorAll('.item, .option, [data-value]');
							const targetTextLower = targetText.toLowerCase();
							for (const item of menuItems) {
								if (item.textContent) {
									const itemTextLower = item.textContent.trim().toLowerCase();
									const itemValueLower = (item.getAttribute('data-value') || '').toLowerCase();
									// Match against both text and data-value (case-insensitive)
									if (itemTextLower === targetTextLower || itemValueLower === targetTextLower) {
										// Clear previous selections
										menuItems.forEach(mi => {
											mi.classList.remove('selected', 'active');
										});
										// Select this item
										item.classList.add('selected', 'active');
										// Update dropdown text if there's a text element
										const textElement = element.querySelector('.text');
										if (textElement) {
											textElement.textContent = item.textContent.trim();
										}
										// Trigger click and change events
										item.click();
										const clickEvent = new MouseEvent('click', { view: window, bubbles: true, cancelable: true });
										item.dispatchEvent(clickEvent);
										// Also dispatch on the main dropdown element
										const dropdownChangeEvent = new Event('change', { bubbles: true });
										element.dispatchEvent(dropdownChangeEvent);
										return {
											success: true,
											message: `Selected custom dropdown item: ${item.textContent.trim()}`
										};
									}
								}
							}
							// Return available options as separate field
							const availableOptions = Array.from(menuItems).map(item => ({
								text: item.textContent ? item.textContent.trim() : '',
								value: item.getAttribute('data-value') || ''
							})).filter(opt => opt.text || opt.value);
							return {
								success: false,
								error: `Custom dropdown item with text or value '${targetText}' not found`,
								availableOptions: availableOptions
							};
						}
						return null; // Not a dropdown element
					}
					// Function to recursively search children for dropdowns
					function searchChildrenForSelection(element, maxDepth, currentDepth = 0) {
						if (currentDepth >= maxDepth) return null;
						// Check all direct children
						for (let child of element.children) {
							// Try selection on this child
							const result = attemptSelection(child);
							if (result && result.success) {
								return result;
							}
							// Recursively check this child's children
							const childResult = searchChildrenForSelection(child, maxDepth, currentDepth + 1);
							if (childResult && childResult.success) {
								return childResult;
							}
						}
						return null;
					}
					// First try the target element itself
					let selectionResult = attemptSelection(startElement);
					if (selectionResult) {
						// If attemptSelection returned a result (success or failure), use it
						// Don't search children if we found a dropdown element but selection failed
						return selectionResult;
					}
					// Only search children if target element is not a dropdown element
					selectionResult = searchChildrenForSelection(startElement, 4);
					if (selectionResult && selectionResult.success) {
						return selectionResult;
					}
					return {
						success: false,
						error: `Element and its children (depth 4) do not contain a dropdown with option '${targetText}' (tag: ${startElement.tagName}, role: ${startElement.getAttribute('role')}, classes: ${startElement.className})`
					};
				}
				"""
				execution_result = await cdp_connection.cdp_client.send.Runtime.callFunctionOn(
					params={
						'functionDeclaration': select_script,
						'arguments': [{'value': option_text}],
						'objectId': js_object_id,
						'returnByValue': True,
					},
					session_id=cdp_connection.session_id,
				)
				select_data = execution_result.get('result', {}).get('value', {})
				if select_data.get('success'):
					success_message = select_data.get('message', f'Selected option: {option_text}')
					self.logger.debug(f'{success_message}')
					# Вернуть результат как dict
					return {
						'success': 'true',
						'message': success_message,
						'value': select_data.get('value', option_text),
						'backend_node_id': str(log_index),
					}
				else:
					error_message = select_data.get('error', f'Failed to select option: {option_text}')
					options_list = select_data.get('availableOptions', [])
					self.logger.error(f'❌ {error_message}')
					self.logger.debug(f'Available options from JavaScript: {options_list}')
					# Если есть доступные опции, вернуть структурированные данные об ошибке
					if options_list:
						# Форматировать опции для short_term_memory (простой список с маркерами)
						formatted_list = []
						for option_item in options_list:
							if isinstance(option_item, dict):
								item_text = option_item.get('text', '').strip()
								item_value = option_item.get('value', '').strip()
								if item_text:
									formatted_list.append(f'- {item_text}')
								elif item_value:
									formatted_list.append(f'- {item_value}')
							elif isinstance(option_item, str):
								formatted_list.append(f'- {option_item}')
						if formatted_list:
							short_memory = 'Available dropdown options  are:\n' + '\n'.join(formatted_list)
							long_memory = (
								f"Couldn't select the dropdown option as '{option_text}' is not one of the available options."
							)
							# Вернуть результат ошибки со структурированной памятью вместо выброса исключения
							return {
								'success': 'false',
								'error': error_message,
								'short_term_memory': short_memory,
								'long_term_memory': long_memory,
								'backend_node_id': str(log_index),
							}
					# Запасной вариант: обычный результат ошибки, если нет доступных опций
					return {
						'success': 'false',
						'error': error_message,
						'backend_node_id': str(log_index),
					}
			except Exception as select_error:
				error_message = f'Failed to select dropdown option: {str(select_error)}'
				self.logger.error(error_message)
				raise ValueError(error_message) from select_error
		except Exception as select_exception:
			error_message = f'Failed to select dropdown option "{option_text}" for element {log_index}: {str(select_exception)}'
			self.logger.error(error_message)
			raise ValueError(error_message) from select_exception
