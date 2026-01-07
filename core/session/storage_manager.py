"""Менеджер storage (cookies, localStorage, sessionStorage) для ChromeSession."""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cdp_use.cdp.network import Cookie

if TYPE_CHECKING:
	from core.session.session import ChromeSession


class StorageManager:
	"""Менеджер для работы с cookies и storage браузера."""

	def __init__(self, browser_session: 'ChromeSession'):
		self.browser_session = browser_session

	async def cookies(self) -> list['Cookie']:
		"""Get cookies, optionally filtered by URLs."""
		result = await self.browser_session.cdp_client.send.Storage.getCookies()
		return result['cookies']

	async def clear_cookies(self) -> None:
		"""Clear all cookies."""
		await self.browser_session.cdp_client.send.Network.clearBrowserCookies()

	async def export_storage_state(self, output_path: str | Path | None = None) -> dict[str, Any]:
		"""Export all browser cookies and storage to storage_state format.

		Extracts decrypted cookies via CDP, bypassing keychain encryption.

		Args:
			output_path: Optional path to save storage_state.json. If None, returns dict only.

		Returns:
			Storage state dict со списком cookies (формат совместим с Playwright/Chromium).

		"""
		# Get all cookies using Storage.getCookies (returns decrypted cookies from all domains)
		cookies = await self._cdp_get_cookies()

		# Конвертация формата cookies CDP в компактный storage_state-формат
		storage_state = {
			'cookies': [
				{
					'name': c['name'],
					'value': c['value'],
					'domain': c['domain'],
					'path': c['path'],
					'expires': c.get('expires', -1),
					'httpOnly': c.get('httpOnly', False),
					'secure': c.get('secure', False),
					'sameSite': c.get('sameSite', 'Lax'),
				}
				for c in cookies
			],
			'origins': [],  # Could add localStorage/sessionStorage extraction if needed
		}

		if output_path:
			output_file = Path(output_path).expanduser().resolve()
			output_file.parent.mkdir(parents=True, exist_ok=True)
			output_file.write_text(json.dumps(storage_state, indent=2))
			self.browser_session.logger.info(f'💾 Exported {len(cookies)} cookies to {output_file}')

		return storage_state

	async def _cdp_get_cookies(self) -> list[Cookie]:
		"""Get cookies using CDP Network.getCookies."""
		cdp_session = await self.browser_session.get_or_create_cdp_session(target_id=None)
		import asyncio
		result = await asyncio.wait_for(
			cdp_session.cdp_client.send.Storage.getCookies(session_id=cdp_session.session_id), timeout=8.0
		)
		return result.get('cookies', [])

	async def _cdp_set_cookies(self, cookies: list[Cookie]) -> None:
		"""Set cookies using CDP Storage.setCookies."""
		if not self.browser_session.agent_focus_target_id or not cookies:
			return

		cdp_session = await self.browser_session.get_or_create_cdp_session(target_id=None)
		# Storage.setCookies expects params dict with 'cookies' key
		await cdp_session.cdp_client.send.Storage.setCookies(
			params={'cookies': cookies},  # type: ignore[arg-type]
			session_id=cdp_session.session_id,
		)

	async def _cdp_clear_cookies(self) -> None:
		"""Clear all cookies using CDP Network.clearBrowserCookies."""
		cdp_session = await self.browser_session.get_or_create_cdp_session()
		await cdp_session.cdp_client.send.Storage.clearCookies(session_id=cdp_session.session_id)

	async def _cdp_get_origins(self) -> list[dict[str, Any]]:
		"""Get origins with localStorage and sessionStorage using CDP."""
		origins = []
		cdp_session = await self.browser_session.get_or_create_cdp_session(target_id=None)

		try:
			# Enable DOMStorage domain to track storage
			await cdp_session.cdp_client.send.DOMStorage.enable(session_id=cdp_session.session_id)

			try:
				# Get all frames to find unique origins
				frames_result = await cdp_session.cdp_client.send.Page.getFrameTree(session_id=cdp_session.session_id)

				# Extract unique origins from frames
				unique_origins = set()

				def _extract_origins(frame_tree):
					"""Recursively extract origins from frame tree."""
					frame = frame_tree.get('frame', {})
					origin = frame.get('securityOrigin')
					if origin and origin != 'null':
						unique_origins.add(origin)

					# Process child frames
					for child in frame_tree.get('childFrames', []):
						_extract_origins(child)

				async def _get_storage_items(origin: str, is_local_storage: bool) -> list[dict[str, str]] | None:
					"""Helper to get storage items for an origin."""
					storage_type = 'localStorage' if is_local_storage else 'sessionStorage'
					try:
						result = await cdp_session.cdp_client.send.DOMStorage.getDOMStorageItems(
							params={'storageId': {'securityOrigin': origin, 'isLocalStorage': is_local_storage}},
							session_id=cdp_session.session_id,
						)

						items = []
						for item in result.get('entries', []):
							if len(item) == 2:  # Each item is [key, value]
								items.append({'name': item[0], 'value': item[1]})

						return items if items else None
					except Exception as e:
						self.browser_session.logger.debug(f'Failed to get {storage_type} for {origin}: {e}')
						return None

				_extract_origins(frames_result.get('frameTree', {}))

				# For each unique origin, get localStorage and sessionStorage
				for origin in unique_origins:
					origin_data = {'origin': origin}

					# Get localStorage
					local_storage = await _get_storage_items(origin, is_local_storage=True)
					if local_storage:
						origin_data['localStorage'] = local_storage

					# Get sessionStorage
					session_storage = await _get_storage_items(origin, is_local_storage=False)
					if session_storage:
						origin_data['sessionStorage'] = session_storage

					# Only add origin if it has storage data
					if 'localStorage' in origin_data or 'sessionStorage' in origin_data:
						origins.append(origin_data)

			finally:
				# Always disable DOMStorage tracking when done
				await cdp_session.cdp_client.send.DOMStorage.disable(session_id=cdp_session.session_id)

		except Exception as e:
			self.browser_session.logger.warning(f'Failed to get origins: {e}')

		return origins

	async def _cdp_get_storage_state(self) -> dict:
		"""Get storage state (cookies, localStorage, sessionStorage) using CDP."""
		# Use the _cdp_get_cookies helper which handles session attachment
		cookies = await self._cdp_get_cookies()

		# Get origins with localStorage/sessionStorage
		origins = await self._cdp_get_origins()

		return {
			'cookies': cookies,
			'origins': origins,
		}

