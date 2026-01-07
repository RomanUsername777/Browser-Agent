import asyncio
import functools
import inspect
import logging
import re
from collections.abc import Callable
from inspect import Parameter, iscoroutinefunction, signature
from types import UnionType
from typing import Any, Generic, Optional, TypeVar, Union, get_args, get_origin

import pyotp
from pydantic import BaseModel, Field, RootModel, create_model

from core.session.session import ChromeSession
from core.ai_models.models import BaseChatModel
from core.observability import observe_debug
from core.actions.registry.models import (
	CommandModel,
	ActionRegistry,
	RegisteredAction,
	SpecialActionParameters,
)
from core.helpers import is_new_tab_page, match_url_with_domain_pattern, time_execution_async

Context = TypeVar('Context')

logger = logging.getLogger(__name__)


class Registry(Generic[Context]):
	"""Service for registering and managing actions"""

	def __init__(self, exclude_actions: list[str] | None = None):
		self.registry = ActionRegistry()
		# Initialize exclude list to avoid mutable default argument issues
		self.exclude_actions = list(exclude_actions) if exclude_actions is not None else []

	def exclude_action(self, action_name: str) -> None:
		"""Exclude an action from the registry after initialization.

		If the action is already registered, it will be removed from the registry.
		The action is also added to the exclude_actions list to prevent re-registration.
		"""
		# Add to exclude list to prevent future registration
		if action_name not in self.exclude_actions:
			self.exclude_actions.append(action_name)

		# Remove from registry if already registered
		if action_name in self.registry.actions:
			del self.registry.actions[action_name]
			logger.debug(f'Excluded action "{action_name}" from registry')

	def _get_special_param_types(self) -> dict[str, type | UnionType | None]:
		"""Get the expected types for special parameters from SpecialActionParameters"""
		# Manually define the expected types to avoid issues with Optional handling.
		# we should try to reduce this list to 0 if possible, give as few standardized objects to all the actions
		# but each driver should decide what is relevant to expose the action methods,
		# e.g. CDP client, 2fa code getters, sensitive_data wrappers, other context, etc.
		return {
			'context': None,  # Context is a TypeVar, so we can't validate type
			'browser_session': ChromeSession,
			'page_url': str,
			'cdp_client': None,  # CDPClient type from cdp_use, but we don't import it here
			'page_extraction_llm': BaseChatModel,
			'available_file_paths': list,
			'has_sensitive_data': bool,
			'file_system': Any,
		}

	def _normalize_action_function_signature(
		self,
		func: Callable,
		description: str,
		param_model: type[BaseModel] | None = None,
	) -> tuple[Callable, type[BaseModel]]:
		"""
		Normalize action function to accept only kwargs.

		Returns:
			- Normalized function that accepts (*_, params: ParamModel, **special_params)
			- The param model to use for registration
		"""
		sig = signature(func)
		parameters = list(sig.parameters.values())
		special_param_types = self._get_special_param_types()
		special_param_names = set(special_param_types.keys())

		# Validate that original function signature doesn't contain **kwargs
		# Default values should be provided via dedicated param_model: BaseModel instead
		for parameter in parameters:
			if parameter.kind == Parameter.VAR_KEYWORD:
				raise ValueError(
					f"Action '{func.__name__}' has **{parameter.name} which is not allowed. "
					f'Actions must have explicit positional parameters only.'
				)

		# Categorize parameters into special and action parameters
		action_parameters = []
		special_parameters = []
		has_param_model = param_model is not None

		for param_index, parameter in enumerate(parameters):
			# Detect Type 1 pattern (first parameter is BaseModel)
			if param_index == 0 and has_param_model and parameter.name not in special_param_names:
				# Type 1 pattern detected - skip the params argument
				continue

			if parameter.name in special_param_names:
				# Validate special parameter type compatibility
				required_type = special_param_types.get(parameter.name)
				if parameter.annotation != Parameter.empty and required_type is not None:
					# Normalize Optional types on both sides
					actual_param_type = parameter.annotation
					type_origin = get_origin(actual_param_type)
					if type_origin is Union:
						type_args = get_args(actual_param_type)
						# Extract non-None type from Union
						actual_param_type = next((arg for arg in type_args if arg is not type(None)), actual_param_type)

					# Verify type compatibility (exact match, subclass relationship, or generic list)
					is_compatible = (
						actual_param_type == required_type
						or (
							inspect.isclass(actual_param_type)
							and inspect.isclass(required_type)
							and issubclass(actual_param_type, required_type)
						)
						or
						# Handle list[T] vs list comparison
						(required_type is list and (actual_param_type is list or get_origin(actual_param_type) is list))
					)

					if not is_compatible:
						required_type_str = getattr(required_type, '__name__', str(required_type))
						actual_type_str = getattr(actual_param_type, '__name__', str(actual_param_type))
						raise ValueError(
							f"Action '{func.__name__}' parameter '{parameter.name}: {actual_type_str}' "
							f"conflicts with special argument injected by tools: '{parameter.name}: {required_type_str}'"
						)
				special_parameters.append(parameter)
			else:
				action_parameters.append(parameter)

		# Generate or validate parameter model
		if not has_param_model:
			# Type 2: Build param model from action parameters
			if action_parameters:
				model_fields = {}
				for action_param in action_parameters:
					param_annotation = action_param.annotation if action_param.annotation != Parameter.empty else str
					param_default = ... if action_param.default == Parameter.empty else action_param.default
					model_fields[action_param.name] = (param_annotation, param_default)

				param_model = create_model(f'{func.__name__}_Params', __base__=CommandModel, **model_fields)
			else:
				# No action parameters, create empty model
				param_model = create_model(
					f'{func.__name__}_Params',
					__base__=CommandModel,
				)
		assert param_model is not None, f'param_model is None for {func.__name__}'

		# Step 4: Create normalized wrapper function
		@functools.wraps(func)
		async def normalized_wrapper(*args, params: BaseModel | None = None, **kwargs):
			"""Normalized action that only accepts kwargs"""
			# Validate no positional args
			if args:
				raise TypeError(f'{func.__name__}() does not accept positional arguments, only keyword arguments are allowed')

			# Prepare arguments for original function
			call_args = []
			call_kwargs = {}

			# Process Type 1 pattern (first argument is the param model)
			if has_param_model and parameters and parameters[0].name not in special_param_names:
				if params is None:
					raise ValueError(f"{func.__name__}() missing required 'params' argument")
				# For Type 1, params object will be used as first argument
				pass
			else:
				# Type 2 pattern - unpack params from kwargs if needed
				# If params is None, attempt to construct it from kwargs
				if params is None and action_parameters:
					# Collect action parameters from kwargs
					extracted_action_kwargs = {}
					for action_param in action_parameters:
						if action_param.name in kwargs:
							extracted_action_kwargs[action_param.name] = kwargs[action_param.name]
					if extracted_action_kwargs:
						# Use param_model which has correct type definitions
						params = param_model(**extracted_action_kwargs)

			# Construct call arguments by iterating through original function parameters in order
			params_data = params.model_dump() if params is not None else {}

			for param_index, parameter in enumerate(parameters):
				# Skip first parameter for Type 1 pattern (it's the model itself)
				if has_param_model and param_index == 0 and parameter.name not in special_param_names:
					call_args.append(params)
				elif parameter.name in special_param_names:
					# Process special parameter
					if parameter.name in kwargs:
						param_value = kwargs[parameter.name]
						# Validate required special parameter is not None
						if param_value is None and parameter.default == Parameter.empty:
							if parameter.name == 'browser_session':
								raise ValueError(f'Action {func.__name__} requires browser_session but none provided.')
							elif parameter.name == 'page_extraction_llm':
								raise ValueError(f'Action {func.__name__} requires page_extraction_llm but none provided.')
							elif parameter.name == 'file_system':
								raise ValueError(f'Action {func.__name__} requires file_system but none provided.')
							elif parameter.name == 'page':
								raise ValueError(f'Action {func.__name__} requires page but none provided.')
							elif parameter.name == 'available_file_paths':
								raise ValueError(f'Action {func.__name__} requires available_file_paths but none provided.')
							else:
								raise ValueError(f"{func.__name__}() missing required special parameter '{parameter.name}'")
						call_args.append(param_value)
					elif parameter.default != Parameter.empty:
						call_args.append(parameter.default)
					else:
						# Special parameter is required but not provided
						if parameter.name == 'browser_session':
							raise ValueError(f'Action {func.__name__} requires browser_session but none provided.')
						elif parameter.name == 'page_extraction_llm':
							raise ValueError(f'Action {func.__name__} requires page_extraction_llm but none provided.')
						elif parameter.name == 'file_system':
							raise ValueError(f'Action {func.__name__} requires file_system but none provided.')
						elif parameter.name == 'page':
							raise ValueError(f'Action {func.__name__} requires page but none provided.')
						elif parameter.name == 'available_file_paths':
							raise ValueError(f'Action {func.__name__} requires available_file_paths but none provided.')
						else:
							raise ValueError(f"{func.__name__}() missing required special parameter '{parameter.name}'")
				else:
					# Process action parameter
					if parameter.name in params_data:
						call_args.append(params_data[parameter.name])
					elif parameter.default != Parameter.empty:
						call_args.append(parameter.default)
					else:
						raise ValueError(f"{func.__name__}() missing required parameter '{parameter.name}'")

			# Call original function with positional args
			if iscoroutinefunction(func):
				return await func(*call_args)
			else:
				return await asyncio.to_thread(func, *call_args)

		# Update wrapper signature to accept only keyword arguments
		updated_parameters = [Parameter('params', Parameter.KEYWORD_ONLY, default=None, annotation=Optional[param_model])]

		# Append special parameters as keyword-only
		for special_param in special_parameters:
			updated_parameters.append(Parameter(special_param.name, Parameter.KEYWORD_ONLY, default=special_param.default, annotation=special_param.annotation))

		# Add **kwargs to accept and ignore extra parameters
		updated_parameters.append(Parameter('kwargs', Parameter.VAR_KEYWORD))

		normalized_wrapper.__signature__ = sig.replace(parameters=updated_parameters)  # type: ignore[attr-defined]

		return normalized_wrapper, param_model

	# @time_execution_sync('--create_param_model')
	def _create_param_model(self, function: Callable) -> type[BaseModel]:
		"""Creates a Pydantic model from function signature"""
		sig = signature(function)
		special_param_names = set(SpecialActionParameters.model_fields.keys())
		params = {
			name: (param.annotation, ... if param.default == param.empty else param.default)
			for name, param in sig.parameters.items()
			if name not in special_param_names
		}
		# Примечание: типы требуют доработки
		return create_model(
			f'{function.__name__}_parameters',
			__base__=CommandModel,
			**params,  # type: ignore
		)

	def action(
		self,
		description: str,
		param_model: type[BaseModel] | None = None,
		domains: list[str] | None = None,
		allowed_domains: list[str] | None = None,
	):
		"""Decorator for registering actions"""
		# Handle aliases: domains and allowed_domains are the same parameter
		if allowed_domains is not None and domains is not None:
			raise ValueError("Cannot specify both 'domains' and 'allowed_domains' - they are aliases for the same parameter")

		final_domains = allowed_domains if allowed_domains is not None else domains

		def decorator(func: Callable):
			# Skip registration if action is in exclude_actions
			if func.__name__ in self.exclude_actions:
				return func

			# Normalize the function signature
			normalized_func, actual_param_model = self._normalize_action_function_signature(func, description, param_model)

			action = RegisteredAction(
				name=func.__name__,
				description=description,
				function=normalized_func,
				param_model=actual_param_model,
				domains=final_domains,
			)
			self.registry.actions[func.__name__] = action

			# Return the normalized function so it can be called with kwargs
			return normalized_func

		return decorator

	@observe_debug(ignore_input=True, ignore_output=True, name='execute_action')
	@time_execution_async('--execute_action')
	async def execute_action(
		self,
		action_name: str,
		params: dict,
		browser_session: ChromeSession | None = None,
		page_extraction_llm: BaseChatModel | None = None,
		file_system: Any | None = None,
		sensitive_data: dict[str, str | dict[str, str]] | None = None,
		available_file_paths: list[str] | None = None,
	) -> Any:
		"""Execute a registered action with simplified parameter handling"""
		if action_name not in self.registry.actions:
			raise ValueError(f'Action {action_name} not found')

		registered_action = self.registry.actions[action_name]
		try:
			# Validate and create Pydantic model
			try:
				validated_parameters = registered_action.param_model(**params)
			except Exception as e:
				raise ValueError(f'Invalid parameters {params} for action {action_name}: {type(e)}: {e}') from e

			if sensitive_data:
				# Retrieve current URL if browser_session is available
				page_url = None
				if browser_session and browser_session.agent_focus_target_id:
					try:
						# Retrieve current page information from session_manager
						current_target = browser_session.session_manager.get_target(browser_session.agent_focus_target_id)
						if current_target:
							page_url = current_target.url
					except Exception:
						pass
				validated_parameters = self._replace_sensitive_data(validated_parameters, sensitive_data, page_url)

			# Construct special context dictionary
			action_context = {
				'browser_session': browser_session,
				'page_extraction_llm': page_extraction_llm,
				'available_file_paths': available_file_paths,
				'has_sensitive_data': action_name == 'input' and bool(sensitive_data),
				'file_system': file_system,
			}

			# Only pass sensitive_data to actions that explicitly require it (input)
			if action_name == 'input':
				action_context['sensitive_data'] = sensitive_data

			# Append CDP-related parameters if browser_session is available
			if browser_session:
				# Append page_url
				try:
					action_context['page_url'] = await browser_session.get_current_page_url()
				except Exception:
					action_context['page_url'] = None

				# Append cdp_client
				action_context['cdp_client'] = browser_session.cdp_client

			# All functions are normalized to accept kwargs only
			# Invoke with params and unpacked special context
			try:
				return await registered_action.function(params=validated_parameters, **action_context)
			except Exception as e:
				raise

		except ValueError as e:
			# Preserve ValueError messages from validation
			if 'requires browser_session but none provided' in str(e) or 'requires page_extraction_llm but none provided' in str(
				e
			):
				raise RuntimeError(str(e)) from e
			else:
				raise RuntimeError(f'Error executing action {action_name}: {str(e)}') from e
		except TimeoutError as e:
			raise RuntimeError(f'Error executing action {action_name} due to timeout.') from e
		except Exception as e:
			raise RuntimeError(f'Error executing action {action_name}: {str(e)}') from e

	def _log_sensitive_data_usage(self, placeholders_used: set[str], current_url: str | None) -> None:
		"""Log when sensitive data is being used on a page"""
		if placeholders_used:
			url_info = f' on {current_url}' if current_url and not is_new_tab_page(current_url) else ''
			logger.info(f'🔒 Using sensitive data placeholders: {", ".join(sorted(placeholders_used))}{url_info}')

	def _replace_sensitive_data(
		self, params: BaseModel, sensitive_data: dict[str, Any], current_url: str | None = None
	) -> BaseModel:
		"""
		Replaces sensitive data placeholders in params with actual values.

		Args:
			params: The parameter object containing <secret>placeholder</secret> tags
			sensitive_data: Словарь чувствительных данных в старом формате {key: value} или новом
						   or new format {domain_pattern: {key: value}}
			current_url: Optional current URL for domain matching

		Returns:
			BaseModel: The parameter object with placeholders replaced by actual values
		"""
		secret_pattern = re.compile(r'<secret>(.*?)</secret>')

		# Set to track all missing placeholders across the full object
		all_missing_placeholders = set()
		# Set to track successfully replaced placeholders
		replaced_placeholders = set()

		# Process sensitive data according to format and current URL
		relevant_secrets = {}

		for domain_pattern_or_key, secret_content in sensitive_data.items():
			if isinstance(secret_content, dict):
				# New format: {domain_pattern: {key: value}}
				# Only include secrets for domains matching the current URL
				if current_url and not is_new_tab_page(current_url):
					# Real URL detected, validate using custom allowed_domains scheme://*.example.com glob matching
					if match_url_with_domain_pattern(current_url, domain_pattern_or_key):
						relevant_secrets.update(secret_content)
			else:
				# Упрощенный формат: {key: value}, доступен для всех доменов
				relevant_secrets[domain_pattern_or_key] = secret_content

		# Remove empty values
		relevant_secrets = {key: value for key, value in relevant_secrets.items() if value}

		def replace_secrets_recursively(data_value: str | dict | list) -> str | dict | list:
			if isinstance(data_value, str):
				found_placeholders = secret_pattern.findall(data_value)
				# Check if placeholder key (e.g., x_password) is in LLM output parameters and replace with sensitive data
				for placeholder_key in found_placeholders:
					if placeholder_key in relevant_secrets:
						# Generate TOTP code if secret is a 2FA secret
						if 'bu_2fa_code' in placeholder_key:
							totp_generator = pyotp.TOTP(relevant_secrets[placeholder_key], digits=6)
							secret_value = totp_generator.now()
						else:
							secret_value = relevant_secrets[placeholder_key]

						data_value = data_value.replace(f'<secret>{placeholder_key}</secret>', secret_value)
						replaced_placeholders.add(placeholder_key)
					else:
						# Track missing placeholders
						all_missing_placeholders.add(placeholder_key)
						# Keep the tag unchanged

				return data_value
			elif isinstance(data_value, dict):
				return {dict_key: replace_secrets_recursively(dict_value) for dict_key, dict_value in data_value.items()}
			elif isinstance(data_value, list):
				return [replace_secrets_recursively(list_item) for list_item in data_value]
			return data_value

		params_serialized = params.model_dump()
		params_with_secrets_replaced = replace_secrets_recursively(params_serialized)

		# Log sensitive data usage
		self._log_sensitive_data_usage(replaced_placeholders, current_url)

		# Log warning if any placeholders are missing
		if all_missing_placeholders:
			logger.warning(f'Missing or empty keys in sensitive_data dictionary: {", ".join(all_missing_placeholders)}')

		return type(params).model_validate(params_with_secrets_replaced)

	# @time_execution_sync('--create_action_model')
	def create_action_model(self, include_actions: list[str] | None = None, page_url: str | None = None) -> type[CommandModel]:
		"""Creates a Union of individual action models from registered actions,
		used by LLM APIs that support tool calling & enforce a schema.

		Each action model contains only the specific action being used,
		rather than all actions with most set to None.
		"""
		from typing import Union

		# Filter actions based on page_url if provided:
		#   if page_url is None, only include actions with no filters
		#   if page_url is provided, only include actions that match the URL

		filtered_actions: dict[str, RegisteredAction] = {}
		for action_name, registered_action in self.registry.actions.items():
			if include_actions is not None and action_name not in include_actions:
				continue

			# If no page_url provided, only include actions with no domain filters
			if page_url is None:
				if registered_action.domains is None:
					filtered_actions[action_name] = registered_action
				continue

			# Validate domain filter if present
			matches_domain = self.registry._match_domains(registered_action.domains, page_url)

			# Include action if domain filter matches
			if matches_domain:
				filtered_actions[action_name] = registered_action

		# Generate individual action models for each action
		per_action_models: list[type[BaseModel]] = []

		for action_name, registered_action in filtered_actions.items():
			# Generate individual model for each action containing only one field
			single_action_model = create_model(
				f'{action_name.title().replace("_", "")}CommandModel',
				__base__=CommandModel,
				**{
					action_name: (
						registered_action.param_model,
						Field(description=registered_action.description),
					)  # type: ignore
				},
			)
			per_action_models.append(single_action_model)

		# If no actions available, return empty CommandModel
		if not per_action_models:
			return create_model('EmptyCommandModel', __base__=CommandModel)

		# Create proper Union type that maintains CommandModel interface
		if len(per_action_models) == 1:
			# If only one action, return it directly (no Union needed)
			result_model = per_action_models[0]

		# Length is greater than 1
		else:
			# Create Union type using RootModel that properly delegates CommandModel methods
			action_union_type = Union[tuple(per_action_models)]  # type: ignore : Typing doesn't understand that the length is >= 2 (by design)

			class CommandModelUnion(RootModel[action_union_type]):  # type: ignore
				@classmethod
				def model_validate(cls, obj, *, strict=None, from_attributes=None, context=None):
					"""Custom validation: try each type in Union sequentially"""
					# Try each type in Union
					validation_errors = []
					for action_model_type in per_action_models:
						try:
							# Attempt to parse as this type
							validated_obj = action_model_type.model_validate(obj, strict=strict, from_attributes=from_attributes, context=context)
							# If successful, create RootModel with this value
							return cls(root=validated_obj)
						except Exception as e:
							validation_errors.append((action_model_type.__name__, str(e)))
							continue
					
					# If no type matched, raise first error
					if validation_errors:
						first_validation_error = validation_errors[0]
						error_message = f'Failed to parse as any action type. First error ({first_validation_error[0]}): {first_validation_error[1][:200]}'
						raise ValueError(error_message)
					raise ValueError('No action types available')
				
				def get_index(self) -> int | None:
					"""Delegate get_index to the underlying action model"""
					if hasattr(self.root, 'get_index'):
						return self.root.get_index()  # type: ignore
					return None

				def set_index(self, index: int):
					"""Delegate set_index to the underlying action model"""
					if hasattr(self.root, 'set_index'):
						self.root.set_index(index)  # type: ignore

				def model_dump(self, **kwargs):
					"""Delegate model_dump to the underlying action model"""
					if hasattr(self.root, 'model_dump'):
						return self.root.model_dump(**kwargs)  # type: ignore
					return super().model_dump(**kwargs)

			# Set the name for better debugging
			CommandModelUnion.__name__ = 'CommandModel'
			CommandModelUnion.__qualname__ = 'CommandModel'

			result_model = CommandModelUnion

		return result_model  # type:ignore

	def get_prompt_description(self, page_url: str | None = None) -> str:
		"""Get a description of all actions for the prompt

		If page_url is provided, only include actions that are available for that URL
		based on their domain filters
		"""
		return self.registry.get_prompt_description(page_url=page_url)
