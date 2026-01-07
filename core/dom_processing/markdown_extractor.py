"""
Общие утилиты для извлечения markdown из содержимого браузера.

Этот модуль предоставляет унифицированный интерфейс для извлечения чистого markdown из содержимого браузера,
используемый как сервисом инструментов, так и актором страницы.
"""

import re
from typing import TYPE_CHECKING, Any

from core.dom_processing.serializer.html_serializer import HTMLSerializer
from core.dom_processing.manager import DomService

if TYPE_CHECKING:
	from core.session.session import ChromeSession
	from core.session.monitors.watchdogs.dom_watchdog import DOMWatchdog


async def extract_clean_markdown(
	dom_service: DomService | None = None,
	browser_session: 'ChromeSession | None' = None,
	extract_links: bool = False,
	target_id: str | None = None,
) -> tuple[str, dict[str, Any]]:
	"""Извлечь чистый markdown из содержимого браузера, используя улучшенное DOM-дерево.

	Эта унифицированная функция может извлекать markdown, используя либо сессию браузера (для сервиса инструментов),
	либо DOM-сервис с идентификатором цели (для актора страницы).

	Args:
	    dom_service: Экземпляр DOM-сервиса (путь актора страницы)
	    browser_session: Сессия браузера для извлечения содержимого (путь сервиса инструментов)
	    extract_links: Сохранять ли ссылки в markdown
	    target_id: Идентификатор цели для страницы (требуется при использовании dom_service)

	Returns:
	    tuple: (чистое_содержимое_markdown, статистика_содержимого)

	Raises:
	    ValueError: Если не предоставлены ни browser_session, ни (dom_service + target_id)
	"""
	# Валидировать входные параметры
	if browser_session is not None:
		if dom_service is not None or target_id is not None:
			raise ValueError('Cannot specify both browser_session and dom_service/target_id')
		# Путь сессии браузера (сервис инструментов)
		enhanced_dom_tree = await _get_enhanced_dom_tree_from_browser_session(browser_session)
		current_url = await browser_session.get_current_page_url()
		method = 'enhanced_dom_tree'
	elif dom_service is not None and target_id is not None:
		# Путь DOM-сервиса (актор страницы)
		# Ленивая загрузка all_frames внутри get_dom_tree при необходимости (для кросс-оригинных iframe)
		enhanced_dom_tree, _ = await dom_service.get_dom_tree(target_id=target_id, all_frames=None)
		current_url = None  # Недоступно через DOM-сервис
		method = 'dom_service'
	else:
		raise ValueError('Must provide either browser_session or both dom_service and target_id')

	# Использовать HTML-сериализатор с улучшенным DOM-деревом
	html_serializer = HTMLSerializer(extract_links=extract_links)
	page_html = html_serializer.serialize(enhanced_dom_tree)

	original_html_length = len(page_html)

	# Использовать markdownify для чистого преобразования в markdown
	from markdownify import markdownify as md

	content = md(
		page_html,
		autolinks=False,  # Не преобразовывать URL в формат <>
		bullets='-',  # Использовать - для неупорядоченных списков
		code_language='',  # Не добавлять язык к блокам кода
		default_title=False,  # Не добавлять атрибуты title по умолчанию
		escape_asterisks=False,  # Не экранировать звёздочки (чище вывод)
		escape_misc=False,  # Не экранировать другие символы (чище вывод)
		escape_underscores=False,  # Не экранировать подчёркивания (чище вывод)
		heading_style='ATX',  # Использовать стиль заголовков #
		keep_inline_images_in=[],  # Не сохранять встроенные изображения в любых тегах (мы уже фильтруем base64 в HTML)
		strip=['script', 'style'],  # Удалить эти теги
	)

	initial_markdown_length = len(content)

	# Минимальная очистка - markdownify уже выполняет большую часть работы
	content = re.sub(r'%[0-9A-Fa-f]{2}', '', content)  # Удалить любое оставшееся URL-кодирование

	# Применить лёгкую предобработку для очистки избыточных пробелов
	content, chars_filtered = _preprocess_markdown_content(content)

	final_filtered_length = len(content)

	# Статистика содержимого
	stats = {
		'final_filtered_chars': final_filtered_length,
		'filtered_chars_removed': chars_filtered,
		'initial_markdown_chars': initial_markdown_length,
		'method': method,
		'original_html_chars': original_html_length,
	}

	# Добавить URL в статистику, если доступен
	if current_url:
		stats['url'] = current_url

	return content, stats


async def _get_enhanced_dom_tree_from_browser_session(browser_session: 'ChromeSession'):
	"""Получить улучшенное DOM-дерево из сессии браузера через DOMWatchdog."""
	# Получить улучшенное DOM-дерево из DOMWatchdog
	# Это захватывает текущее состояние страницы, включая динамическое содержимое, shadow roots и т.д.
	dom_watchdog: DOMWatchdog | None = browser_session._dom_watchdog
	assert dom_watchdog is not None, 'DOMWatchdog not available'

	# Использовать кэшированное улучшенное DOM-дерево, если доступно, иначе построить его
	if dom_watchdog.enhanced_dom_tree is not None:
		return dom_watchdog.enhanced_dom_tree

	# Построить улучшенное DOM-дерево, если не кэшировано
	await dom_watchdog._build_dom_tree_without_highlights()
	enhanced_dom_tree = dom_watchdog.enhanced_dom_tree
	assert enhanced_dom_tree is not None, 'Enhanced DOM tree not available'

	return enhanced_dom_tree


# Используется унифицированная функция extract_clean_markdown


def _preprocess_markdown_content(content: str, max_newlines: int = 3) -> tuple[str, int]:
	"""
	Лёгкая предобработка вывода markdown - минимальная очистка с удалением JSON-блобов.

	Args:
	    content: Содержимое markdown для лёгкой фильтрации
	    max_newlines: Максимальное количество последовательных переносов строк для разрешения

	Returns:
	    tuple: (отфильтрованное_содержимое, отфильтровано_символов)
	"""
	original_length = len(content)

	# Удалить JSON-блобы (часто встречаются в SPA, таких как LinkedIn, Facebook и т.д.)
	# Они часто встроены как `{"key":"value",...}` и могут быть огромными
	# Сопоставить JSON-объекты/массивы длиной не менее 100 символов
	# Это захватывает данные состояния/конфигурации SPA без удаления небольших встроенных JSON
	content = re.sub(r'`\{["\w].*?\}`', '', content, flags=re.DOTALL)  # Удалить JSON в блоках кода
	content = re.sub(r'\{"[^"]{5,}":\{[^}]{100,}\}', '', content)  # Удалить вложенные JSON-объекты
	content = re.sub(r'\{"\$type":[^}]{100,}\}', '', content)  # Удалить JSON с полями $type (частая паттерн)

	# Сжать последовательные переносы строк (4+ переноса становятся max_newlines)
	content = re.sub(r'\n{4,}', '\n' * max_newlines, content)

	# Удалить строки, которые состоят только из пробелов или очень короткие (вероятно, артефакты)
	lines = content.split('\n')
	filtered_lines = []
	for line in lines:
		stripped = line.strip()
		# Сохранить строки с существенным содержимым
		if len(stripped) > 2:
			# Пропустить строки, которые выглядят как JSON (начинаются с { или [ и очень длинные)
			if (stripped.startswith('[') or stripped.startswith('{')) and len(stripped) > 100:
				continue
			filtered_lines.append(line)

	content = '\n'.join(filtered_lines)
	content = content.strip()

	chars_filtered = original_length - len(content)
	return content, chars_filtered
