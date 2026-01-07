"""Минимальный агент автоматизации браузера"""

import os
from typing import TYPE_CHECKING

from core.logging_config import setup_logging

# Setup logging
if os.environ.get('AGENT_SETUP_LOGGING', 'true').lower() != 'false':
    from core.config import CONFIG
    debug_log_file = getattr(CONFIG, 'DEBUG_LOG_FILE', None)
    info_log_file = getattr(CONFIG, 'INFO_LOG_FILE', None)
    logger = setup_logging(debug_log_file=debug_log_file, info_log_file=info_log_file)
else:
    import logging
    logger = logging.getLogger('agent')

# Monkeypatch BaseSubprocessTransport.__del__ to handle closed event loops gracefully
from asyncio import base_subprocess

_original_del = base_subprocess.BaseSubprocessTransport.__del__

def _patched_del(self):
    """Patched __del__ that handles closed event loops without throwing noisy errors."""
    try:
        if hasattr(self, '_loop') and self._loop and self._loop.is_closed():
            return
        _original_del(self)
    except RuntimeError as e:
        if 'Event loop is closed' in str(e):
            pass
        else:
            raise

base_subprocess.BaseSubprocessTransport.__del__ = _patched_del

# Типы для lazy imports
if TYPE_CHECKING:
    from core.orchestrator.prompts import SystemPrompt
    from core.orchestrator.manager import TaskOrchestrator
    from core.orchestrator.models import CommandModel, ExecutionResult, ExecutionHistoryList
    from core.session import BrowserProfile, ChromeSession
    from core.session import ChromeSession as Browser
    from core.dom_processing.manager import DomService
    from core.ai_models.anthropic.chat import ChatAnthropic
    from core.ai_models.openai.chat import ChatOpenAI
    from core.actions.manager import Tools

# Lazy imports mapping
_LAZY_IMPORTS = {
    'TaskOrchestrator': ('core.orchestrator.manager', 'TaskOrchestrator'),
    'Agent': ('core.orchestrator.manager', 'TaskOrchestrator'),  # Alias for backwards compatibility
    'SystemPrompt': ('core.orchestrator.prompts', 'SystemPrompt'),
    'CommandModel': ('core.orchestrator.models', 'CommandModel'),
    'ExecutionResult': ('core.orchestrator.models', 'ExecutionResult'),
    'ExecutionHistoryList': ('core.orchestrator.models', 'ExecutionHistoryList'),
    'ChromeSession': ('core.session', 'ChromeSession'),
    'Browser': ('core.session', 'ChromeSession'),  # Alias
    'BrowserProfile': ('core.session', 'BrowserProfile'),
    'Tools': ('core.actions.manager', 'Tools'),
    'DomService': ('core.dom_processing.manager', 'DomService'),
    'ChatOpenAI': ('core.ai_models.openai.chat', 'ChatOpenAI'),
    'ChatAnthropic': ('core.ai_models.anthropic.chat', 'ChatAnthropic'),
}

def __getattr__(name: str):
    """Lazy import mechanism."""
    if name in _LAZY_IMPORTS:
        module_path, attr_name = _LAZY_IMPORTS[name]
        try:
            from importlib import import_module
            module = import_module(module_path)
            attr = getattr(module, attr_name)
            globals()[name] = attr
            return attr
        except ImportError as e:
            raise ImportError(f'Failed to import {name} from {module_path}: {e}') from e
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")

__all__ = [
    'TaskOrchestrator',
    'Agent',  # Alias for backwards compatibility
    'ChromeSession',
    'Browser',
    'BrowserProfile',
    'Tools',
    'DomService',
    'SystemPrompt',
    'ExecutionResult',
    'CommandModel',
    'ExecutionHistoryList',
    'ChatOpenAI',
    'ChatAnthropic',
]

