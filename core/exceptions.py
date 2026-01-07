"""Исключения для всех компонентов системы."""

# Базовые исключения
class LLMException(Exception):
	def __init__(self, status_code, message):
		self.status_code = status_code
		self.message = message
		super().__init__(f'Ошибка {status_code}: {message}')


# Исключения для моделей AI
class ModelError(Exception):
	"""Базовое исключение для ошибок модели."""
	pass


class ModelProviderError(ModelError):
	"""Исключение, возникающее при ошибке от провайдера модели."""

	def __init__(
		self,
		message: str,
		status_code: int = 502,
		model: str | None = None,
	):
		super().__init__(message)
		self.status_code = status_code
		self.model = model
		self.message = message


class ModelRateLimitError(ModelProviderError):
	"""Исключение, возникающее при ошибке rate limit от провайдера модели."""

	def __init__(
		self,
		message: str,
		status_code: int = 429,
		model: str | None = None,
	):
		super().__init__(message, status_code, model)
