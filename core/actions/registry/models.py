from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from core.session.session import BrowserSession
from core.ai_models.models import BaseChatModel

if TYPE_CHECKING:
	pass


class RegisteredAction(BaseModel):
	"""Model for a registered action"""

	name: str
	description: str
	function: Callable
	param_model: type[BaseModel]

	# filters: provide specific domains to determine whether the action should be available on the given URL or not
	domains: list[str] | None = None  # e.g. ['*.google.com', 'www.bing.com', 'yahoo.*]

	model_config = ConfigDict(arbitrary_types_allowed=True)

	def prompt_description(self) -> str:
		"""Get a description of the action for the prompt in unstructured format"""
		action_schema = self.param_model.model_json_schema()
		parameter_descriptions = []

		if 'properties' in action_schema:
			for property_name, property_schema in action_schema['properties'].items():
				# Build parameter description
				description_text = property_name

				# Add type information if available
				if 'type' in property_schema:
					type_str = property_schema['type']
					description_text += f'={type_str}'

				# Add description as comment if available
				if 'description' in property_schema:
					description_text += f' ({property_schema["description"]})'

				parameter_descriptions.append(description_text)

		# Format: action_name: Description. (param1=type, param2=type, ...)
		if parameter_descriptions:
			return f'{self.name}: {self.description}. ({", ".join(parameter_descriptions)})'
		else:
			return f'{self.name}: {self.description}'


class ActionModel(BaseModel):
	"""Base model for dynamically created action models"""

	# this will have all the registered actions, e.g.
	# click_element = param_model = ClickElementParams
	# done = param_model = None
	#
	model_config = ConfigDict(arbitrary_types_allowed=True, extra='forbid')

	def get_index(self) -> int | None:
		"""Get the index of the action"""
		# {'clicked_element': {'index':5}}
		action_parameters = self.model_dump(exclude_unset=True).values()
		if not action_parameters:
			return None
		for action_param in action_parameters:
			if action_param is not None and 'index' in action_param:
				return action_param['index']
		return None

	def set_index(self, index: int):
		"""Overwrite the index of the action"""
		# Get the action name and params
		dumped_action_data = self.model_dump(exclude_unset=True)
		action_key = next(iter(dumped_action_data.keys()))
		action_parameter_model = getattr(self, action_key)

		# Update the index directly on the model
		if hasattr(action_parameter_model, 'index'):
			action_parameter_model.index = index


class ActionRegistry(BaseModel):
	"""Model representing the action registry"""

	actions: dict[str, RegisteredAction] = {}

	@staticmethod
	def _match_domains(domains: list[str] | None, url: str) -> bool:
		"""
		Match a list of domain glob patterns against a URL.

		Args:
			domains: A list of domain patterns that can include glob patterns (* wildcard)
			url: The URL to match against

		Returns:
			True if the URL's domain matches the pattern, False otherwise
		"""

		if domains is None or not url:
			return True

		# Use the centralized URL matching logic from utils
		from core.helpers import match_url_with_domain_pattern

		for pattern in domains:
			if match_url_with_domain_pattern(url, pattern):
				return True
		return False

	def get_prompt_description(self, page_url: str | None = None) -> str:
		"""Get a description of all actions for the prompt

		Args:
			page_url: If provided, filter actions by URL using domain filters.

		Returns:
			A string description of available actions.
			- If page is None: return only actions with no page_filter and no domains (for system prompt)
			- If page is provided: return only filtered actions that match the current page (excluding unfiltered actions)
		"""
		if page_url is None:
			# For system prompt (no URL provided), include only actions with no filters
			unfiltered_actions = [action for action in self.actions.values() if action.domains is None]
			return '\n'.join(action.prompt_description() for action in unfiltered_actions)

		# Only include filtered actions for the current page URL
		url_filtered_actions = []
		for registered_action in self.actions.values():
			if not registered_action.domains:
				# Skip actions with no filters, they are already included in the system prompt
				continue

			# Check domain filter
			if self._match_domains(registered_action.domains, page_url):
				url_filtered_actions.append(registered_action)

		return '\n'.join(action.prompt_description() for action in url_filtered_actions)


class SpecialActionParameters(BaseModel):
	"""Model defining all special parameters that can be injected into actions"""

	model_config = ConfigDict(arbitrary_types_allowed=True)

	# optional user-provided context object passed down from Agent(context=...)
	# e.g. can contain anything, external db connections, file handles, queues, runtime config objects, etc.
	# that you might want to be able to access quickly from within many of your actions
	# Поле context напрямую не используется в ядре, передаётся в действия для удобства
	context: Any | None = None

	# Сессия браузера, может использоваться для открытия вкладок, навигации и доступа к CDP
	browser_session: BrowserSession | None = None

	# Current page URL for filtering and context
	page_url: str | None = None

	# CDP client for direct Chrome DevTools Protocol access
	cdp_client: Any | None = None  # CDPClient type from cdp_use

	# extra injected config if the action asks for these arg names
	page_extraction_llm: BaseChatModel | None = None
	file_system: Any | None = None
	available_file_paths: list[str] | None = None
	has_sensitive_data: bool = False

	@classmethod
	def get_browser_requiring_params(cls) -> set[str]:
		"""Get parameter names that require browser_session"""
		return {'browser_session', 'cdp_client', 'page_url'}
