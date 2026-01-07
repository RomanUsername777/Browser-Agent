"""Low-level CDP operations - direct Chrome DevTools Protocol commands."""

from typing import TYPE_CHECKING, Any

from cdp_use.cdp.network import Cookie
from cdp_use.cdp.target import TargetID

from core.dom_processing.models import TargetInfo

if TYPE_CHECKING:
    from core.session.session import ChromeSession


class CDPOperationsManager:
    """Manages low-level CDP operations for browser control."""

    def __init__(self, browser_session: 'ChromeSession'):
        self.browser_session = browser_session
        self.logger = browser_session.logger

    async def _cdp_get_all_pages(
        self,
        include_http: bool = True,
        include_about: bool = True,
        include_pages: bool = True,
        include_iframes: bool = False,
        include_workers: bool = False,
        include_chrome: bool = False,
        include_chrome_extensions: bool = False,
        include_chrome_error: bool = False,
    ) -> list[TargetInfo]:
        """Get all browser pages/tabs using SessionManager (source of truth)."""
        if not self.browser_session.session_manager:
            return []

        result = []
        for target_id, target in self.browser_session.session_manager.get_all_targets().items():
            target_info: TargetInfo = {
                'targetId': target.target_id,
                'type': target.target_type,
                'title': target.title,
                'url': target.url,
                'attached': True,
                'canAccessOpener': False,
            }

            if self._is_valid_target(
                target_info,
                include_http=include_http,
                include_about=include_about,
                include_pages=include_pages,
                include_iframes=include_iframes,
                include_workers=include_workers,
                include_chrome=include_chrome,
                include_chrome_extensions=include_chrome_extensions,
                include_chrome_error=include_chrome_error,
            ):
                result.append(target_info)

        return result

    async def _cdp_create_new_page(self, url: str = 'about:blank', background: bool = False, new_window: bool = False) -> str:
        """Create a new page/tab using CDP Target.createTarget. Returns target ID."""
        if self.browser_session._cdp_client_root:
            result = await self.browser_session._cdp_client_root.send.Target.createTarget(
                params={'url': url, 'newWindow': new_window, 'background': background}
            )
        else:
            result = await self.browser_session.cdp_client.send.Target.createTarget(
                params={'url': url, 'newWindow': new_window, 'background': background}
            )
        return result['targetId']

    async def _cdp_close_page(self, target_id: TargetID) -> None:
        """Close a page/tab using CDP Target.closeTarget."""
        await self.browser_session.cdp_client.send.Target.closeTarget(params={'targetId': target_id})

    async def _cdp_get_cookies(self) -> list[Cookie]:
        """Get cookies using CDP Network.getCookies. Delegates to BrowserOperationsManager."""
        return await self.browser_session._browser_operations._cdp_get_cookies()

    async def _cdp_set_cookies(self, cookies: list[Cookie]) -> None:
        """Set cookies using CDP Storage.setCookies. Delegates to BrowserOperationsManager."""
        await self.browser_session._browser_operations._cdp_set_cookies(cookies)

    async def _cdp_clear_cookies(self) -> None:
        """Clear all cookies using CDP Network.clearBrowserCookies. Delegates to BrowserOperationsManager."""
        await self.browser_session._browser_operations._cdp_clear_cookies()

    async def _cdp_set_extra_headers(self, headers: dict[str, str]) -> None:
        """Set extra HTTP headers using CDP Network.setExtraHTTPHeaders."""
        if not self.browser_session.agent_focus_target_id:
            return

        cdp_session = await self.browser_session.get_or_create_cdp_session()
        raise NotImplementedError('Not implemented yet')

    async def _cdp_grant_permissions(self, permissions: list[str], origin: str | None = None) -> None:
        """Grant permissions using CDP Browser.grantPermissions."""
        params = {'permissions': permissions}
        cdp_session = await self.browser_session.get_or_create_cdp_session()
        raise NotImplementedError('Not implemented yet')

    async def _cdp_set_geolocation(self, latitude: float, longitude: float, accuracy: float = 100) -> None:
        """Set geolocation using CDP Emulation.setGeolocationOverride."""
        await self.browser_session.cdp_client.send.Emulation.setGeolocationOverride(
            params={'latitude': latitude, 'longitude': longitude, 'accuracy': accuracy}
        )

    async def _cdp_clear_geolocation(self) -> None:
        """Clear geolocation override using CDP."""
        await self.browser_session.cdp_client.send.Emulation.clearGeolocationOverride()

    async def _cdp_add_init_script(self, script: str) -> str:
        """Add script to evaluate on new document using CDP Page.addScriptToEvaluateOnNewDocument."""
        assert self.browser_session._cdp_client_root is not None
        cdp_session = await self.browser_session.get_or_create_cdp_session()

        result = await cdp_session.cdp_client.send.Page.addScriptToEvaluateOnNewDocument(
            params={'source': script, 'runImmediately': True}, session_id=cdp_session.session_id
        )
        return result['identifier']

    async def _cdp_remove_init_script(self, identifier: str) -> None:
        """Remove script added with addScriptToEvaluateOnNewDocument."""
        cdp_session = await self.browser_session.get_or_create_cdp_session(target_id=None)
        await cdp_session.cdp_client.send.Page.removeScriptToEvaluateOnNewDocument(
            params={'identifier': identifier}, session_id=cdp_session.session_id
        )

    async def _cdp_set_viewport(
        self, width: int, height: int, device_scale_factor: float = 1.0, mobile: bool = False, target_id: str | None = None
    ) -> None:
        """Set viewport using CDP Emulation.setDeviceMetricsOverride."""
        if target_id:
            cdp_session = await self.browser_session.get_or_create_cdp_session(target_id, focus=False)
        elif self.browser_session.agent_focus_target_id:
            try:
                cdp_session = await self.browser_session.get_or_create_cdp_session(self.browser_session.agent_focus_target_id, focus=False)
            except ValueError:
                self.logger.warning('Cannot set viewport: focused target has no sessions')
                return
        else:
            self.logger.warning('Cannot set viewport: no target_id provided and agent_focus not initialized')
            return

        await cdp_session.cdp_client.send.Emulation.setDeviceMetricsOverride(
            params={'width': width, 'height': height, 'deviceScaleFactor': device_scale_factor, 'mobile': mobile},
            session_id=cdp_session.session_id,
        )

    async def _cdp_get_origins(self) -> list[dict[str, Any]]:
        """Get origins with localStorage and sessionStorage using CDP. Delegates to BrowserOperationsManager."""
        return await self.browser_session._browser_operations._cdp_get_origins()

    async def _cdp_get_storage_state(self) -> dict:
        """Get storage state (cookies, localStorage, sessionStorage) using CDP. Delegates to BrowserOperationsManager."""
        return await self.browser_session._browser_operations._cdp_get_storage_state()

    async def _cdp_navigate(self, url: str, target_id: TargetID | None = None) -> None:
        """Navigate to URL using CDP Page.navigate."""
        assert self.browser_session._cdp_client_root is not None, 'CDP client not initialized - browser may not be connected yet'
        assert self.browser_session.agent_focus_target_id is not None, 'Agent focus not initialized - browser may not be connected yet'

        target_id_to_use = target_id or self.browser_session.agent_focus_target_id
        cdp_session = await self.browser_session.get_or_create_cdp_session(target_id_to_use, focus=True)

        await cdp_session.cdp_client.send.Page.navigate(params={'url': url}, session_id=cdp_session.session_id)

    @staticmethod
    def _is_valid_target(
        target_info: TargetInfo,
        include_http: bool = True,
        include_chrome: bool = False,
        include_chrome_extensions: bool = False,
        include_chrome_error: bool = False,
        include_about: bool = True,
        include_iframes: bool = True,
        include_pages: bool = True,
        include_workers: bool = False,
    ) -> bool:
        """Check if a target should be processed."""
        target_type = target_info.get('type', '')
        url = target_info.get('url', '')

        url_allowed, type_allowed = False, False

        from core.helpers import is_new_tab_page

        if is_new_tab_page(url):
            url_allowed = True

        if url.startswith('chrome-error://') and include_chrome_error:
            url_allowed = True

        if url.startswith('chrome://') and include_chrome:
            url_allowed = True

        if url.startswith('chrome-extension://') and include_chrome_extensions:
            url_allowed = True

        if url == 'about:blank' and include_about:
            url_allowed = True

        if (url.startswith('http://') or url.startswith('https://')) and include_http:
            url_allowed = True

        if target_type in ('service_worker', 'shared_worker', 'worker') and include_workers:
            type_allowed = True

        if target_type in ('page', 'tab') and include_pages:
            type_allowed = True

        if target_type in ('iframe', 'webview') and include_iframes:
            type_allowed = True

        return url_allowed and type_allowed

