from typing import TYPE_CHECKING

# Заглушки типов для ленивых импортов
if TYPE_CHECKING:
	from .session import ChromeSession
	from .profile import BrowserProfile, ProxySettings


# Словарь для ленивой загрузки тяжёлых компонентов браузера
_LAZY_IMPORTS = {
	'ChromeSession': ('.session', 'ChromeSession'),
	'BrowserProfile': ('.profile', 'BrowserProfile'),
	'ProxySettings': ('.profile', 'ProxySettings'),
}


def __getattr__(name: str):
	"""Механизм ленивой загрузки для тяжёлых компонентов браузера."""
	if name not in _LAZY_IMPORTS:
		raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
	
	module_path, attr_name = _LAZY_IMPORTS[name]
	try:
		from importlib import import_module

		# Используем относительный импорт для текущего пакета
		full_module_path = f'core.session{module_path}'
		module = import_module(full_module_path)
		attr = getattr(module, attr_name)
		# Кешируем импортированный атрибут в глобальных переменных модуля
		globals()[name] = attr
		return attr
	except ImportError as e:
		raise ImportError(f'Failed to import {name} from {full_module_path}: {e}') from e


__all__ = [
	'BrowserProfile',
	'ChromeSession',
	'ProxySettings',
]
