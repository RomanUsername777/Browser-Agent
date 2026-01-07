"""Session lifecycle management - browser start, stop, connect, watchdog initialization."""

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Self
from urllib.parse import urlparse, urlunparse

import httpx
from cdp_use import CDPClient
from cdp_use.cdp.fetch import AuthRequiredEvent, RequestPausedEvent
from cdp_use.cdp.target import AttachedToTargetEvent, SessionID

from core.session.events import (
    AgentFocusChangedEvent,
    BrowserConnectedEvent,
    BrowserErrorEvent,
    BrowserLaunchEvent,
    BrowserLaunchResult,
    BrowserStartEvent,
    BrowserStopEvent,
    BrowserStoppedEvent,
    SaveStorageStateEvent,
    TabCreatedEvent,
)
from core.helpers import create_task_with_error_handling, is_new_tab_page
from core.observability import observe_debug

if TYPE_CHECKING:
    from core.session.session import ChromeSession


class SessionLifecycleManager:
    """Manages browser session lifecycle: initialization, connection, shutdown."""

    def __init__(self, browser_session: 'ChromeSession'):
        self.browser_session = browser_session
        self.logger = browser_session.logger

    @observe_debug(ignore_input=True, ignore_output=True, name='browser_session_start')
    async def start(self) -> None:
        """Start the browser session."""
        start_event = self.browser_session.event_bus.dispatch(BrowserStartEvent())
        await start_event
        await start_event.event_result(raise_if_any=True, raise_if_none=False)

    async def kill(self) -> None:
        """Kill the browser session and reset all state."""
        self.logger.debug('🛑 kill() вызван - останавливаю браузер с force=True и сбрасываю состояние')

        save_event = self.browser_session.event_bus.dispatch(SaveStorageStateEvent())
        await save_event

        await self.browser_session.event_bus.dispatch(BrowserStopEvent(force=True))
        await self.browser_session.event_bus.stop(clear=True, timeout=5)
        await self.reset()
        self.browser_session.event_bus = __import__('bubus').EventBus()

    async def stop(self) -> None:
        """Stop the browser session without killing the browser process."""
        self.logger.debug('⏸️  stop() вызван - останавливаю браузер корректно (force=False) и сбрасываю состояние')

        save_event = self.browser_session.event_bus.dispatch(SaveStorageStateEvent())
        await save_event

        await self.browser_session.event_bus.dispatch(BrowserStopEvent(force=False))
        await self.browser_session.event_bus.stop(clear=True, timeout=5)
        await self.reset()
        self.browser_session.event_bus = __import__('bubus').EventBus()

    async def reset(self) -> None:
        """Clear all cached CDP sessions with proper cleanup."""
        connection_status = 'connected' if self.browser_session._cdp_client_root else 'not connected'
        manager_status = 'exists' if self.browser_session.session_manager else 'None'
        focus_id_suffix = self.browser_session.agent_focus_target_id[-4:] if self.browser_session.agent_focus_target_id else "None"
        self.logger.debug(
            f'🔄 Resetting browser session (CDP: {connection_status}, SessionManager: {manager_status}, '
            f'focus: {focus_id_suffix})'
        )

        if self.browser_session.session_manager:
            await self.browser_session.session_manager.clear()
            self.browser_session.session_manager = None

        if self.browser_session._cdp_client_root:
            try:
                await self.browser_session._cdp_client_root.stop()
                self.logger.debug('Closed CDP client WebSocket during reset')
            except Exception as e:
                self.logger.debug(f'Error closing CDP client during reset: {e}')

        self.browser_session._cdp_client_root = None
        self.browser_session._cached_browser_state_summary = None
        self.browser_session._cached_selector_map.clear()
        self.browser_session._downloaded_files.clear()

        self.browser_session.agent_focus_target_id = None
        if self.browser_session.is_local:
            self.browser_session.browser_profile.cdp_url = None

        self.browser_session._crash_watchdog = None
        self.browser_session._downloads_watchdog = None
        self.browser_session._security_watchdog = None
        self.browser_session._storage_state_watchdog = None
        self.browser_session._local_browser_watchdog = None
        self.browser_session._default_action_watchdog = None
        self.browser_session._dom_watchdog = None
        self.browser_session._recording_watchdog = None
        self.browser_session._popups_watchdog = None
        if self.browser_session._demo_mode:
            self.browser_session._demo_mode.reset()
            self.browser_session._demo_mode = None

        self.logger.info('✅ Сброс сессии браузера завершен')

    @observe_debug(ignore_input=True, ignore_output=True, name='browser_start_event_handler')
    async def on_BrowserStartEvent(self, event: BrowserStartEvent) -> dict[str, str]:
        """Handle browser start request."""
        await self.attach_all_watchdogs()

        try:
            if not self.browser_session.cdp_url:
                if self.browser_session.browser_profile.use_cloud or self.browser_session.browser_profile.cloud_browser_params is not None:
                    raise ValueError('Облачный режим браузера недоступен в данной сборке агента')
                elif self.browser_session.is_local:
                    launch_event = self.browser_session.event_bus.dispatch(BrowserLaunchEvent())
                    await launch_event

                    from typing import cast
                    launch_result: BrowserLaunchResult = cast(
                        BrowserLaunchResult, await launch_event.event_result(raise_if_none=True, raise_if_any=True)
                    )
                    self.browser_session.browser_profile.cdp_url = launch_result.cdp_url
                else:
                    raise ValueError('Got ChromeSession(is_local=False) but no cdp_url was provided to connect to!')

            assert self.browser_session.cdp_url and '://' in self.browser_session.cdp_url

            async with self.browser_session._connection_lock:
                if self.browser_session._cdp_client_root is None:
                    await self.connect(cdp_url=self.browser_session.cdp_url)
                    assert self.browser_session.cdp_client is not None

                    self.browser_session.event_bus.dispatch(BrowserConnectedEvent(cdp_url=self.browser_session.cdp_url))

                    if self.browser_session.browser_profile.demo_mode:
                        try:
                            demo = self.browser_session.demo_mode
                            if demo:
                                await demo.ensure_ready()
                        except Exception as exc:
                            self.logger.warning(f'[DemoMode] Failed to inject demo overlay: {exc}')
                else:
                    self.logger.debug('Уже подключен к CDP, пропускаю переподключение')
                    if self.browser_session.browser_profile.demo_mode:
                        try:
                            demo = self.browser_session.demo_mode
                            if demo:
                                await demo.ensure_ready()
                        except Exception as exc:
                            self.logger.warning(f'[DemoMode] Failed to inject demo overlay: {exc}')

            return {'cdp_url': self.browser_session.cdp_url}

        except Exception as e:
            self.browser_session.event_bus.dispatch(
                BrowserErrorEvent(
                    error_type='BrowserStartEventError',
                    message=f'Failed to start browser: {type(e).__name__} {e}',
                    details={'cdp_url': self.browser_session.cdp_url, 'is_local': self.browser_session.is_local},
                )
            )
            raise

    async def on_BrowserStopEvent(self, event: BrowserStopEvent) -> None:
        """Handle browser stop request."""
        try:
            if self.browser_session.browser_profile.keep_alive and not event.force:
                self.browser_session.event_bus.dispatch(BrowserStoppedEvent(reason='Kept alive due to keep_alive=True'))
                return

            if self.browser_session.browser_profile.use_cloud:
                try:
                    await self.browser_session._cloud_browser_client.stop_browser()
                    self.logger.info('🌤️ Cloud browser session cleaned up')
                except Exception as e:
                    self.logger.debug(f'Failed to cleanup cloud browser session: {e}')

            self.logger.info(
                f'📢 on_BrowserStopEvent - Calling reset() (force={event.force}, keep_alive={self.browser_session.browser_profile.keep_alive})'
            )
            await self.reset()

            if self.browser_session.is_local:
                self.browser_session.browser_profile.cdp_url = None

            stop_event = self.browser_session.event_bus.dispatch(BrowserStoppedEvent(reason='Stopped by request'))
            await stop_event

        except Exception as e:
            self.browser_session.event_bus.dispatch(
                BrowserErrorEvent(
                    error_type='BrowserStopEventError',
                    message=f'Failed to stop browser: {type(e).__name__} {e}',
                    details={'cdp_url': self.browser_session.cdp_url, 'is_local': self.browser_session.is_local},
                )
            )

    async def attach_all_watchdogs(self) -> None:
        """Initialize and attach all watchdogs with explicit handler registration."""
        if hasattr(self.browser_session, '_watchdogs_attached') and self.browser_session._watchdogs_attached:
            self.logger.debug('Watchdogs already attached, skipping duplicate attachment')
            return

        from core.session.monitors.watchdogs.default_action_watchdog import DefaultActionWatchdog
        from core.session.monitors.watchdogs.dom_watchdog import DOMWatchdog
        from core.session.monitors.watchdogs.downloads_watchdog import DownloadsWatchdog
        from core.session.monitors.watchdogs.local_browser_watchdog import LocalBrowserWatchdog
        from core.session.monitors.watchdogs.ui_watchdog import PopupsWatchdog, SecurityWatchdog
        from core.session.monitors.watchdogs.recording_watchdog import RecordingWatchdog
        from core.session.monitors.watchdogs.system_watchdog import StorageStateWatchdog
        from core.session.events import ScreenshotEvent

        DownloadsWatchdog.model_rebuild()
        self.browser_session._downloads_watchdog = DownloadsWatchdog(event_bus=self.browser_session.event_bus, browser_session=self.browser_session)
        self.browser_session._downloads_watchdog.attach_to_session()
        if self.browser_session.browser_profile.auto_download_pdfs:
            self.logger.debug('📄 PDF auto-download enabled for this session')

        should_enable_storage_state = (
            self.browser_session.browser_profile.storage_state is not None or self.browser_session.browser_profile.user_data_dir is not None
        )

        if should_enable_storage_state:
            StorageStateWatchdog.model_rebuild()
            self.browser_session._storage_state_watchdog = StorageStateWatchdog(
                event_bus=self.browser_session.event_bus,
                browser_session=self.browser_session,
                auto_save_interval=60.0,
                save_on_change=False,
            )
            self.browser_session._storage_state_watchdog.attach_to_session()
            self.logger.debug(
                f'🍪 StorageStateWatchdog enabled (storage_state: {bool(self.browser_session.browser_profile.storage_state)}, user_data_dir: {bool(self.browser_session.browser_profile.user_data_dir)})'
            )
        else:
            self.logger.debug('🍪 StorageStateWatchdog disabled (no storage_state or user_data_dir configured)')

        LocalBrowserWatchdog.model_rebuild()
        self.browser_session._local_browser_watchdog = LocalBrowserWatchdog(event_bus=self.browser_session.event_bus, browser_session=self.browser_session)
        self.browser_session._local_browser_watchdog.attach_to_session()

        SecurityWatchdog.model_rebuild()
        self.browser_session._security_watchdog = SecurityWatchdog(event_bus=self.browser_session.event_bus, browser_session=self.browser_session)
        self.browser_session._security_watchdog.attach_to_session()

        PopupsWatchdog.model_rebuild()
        self.browser_session._popups_watchdog = PopupsWatchdog(event_bus=self.browser_session.event_bus, browser_session=self.browser_session)
        self.browser_session._popups_watchdog.attach_to_session()

        self.browser_session._default_action_watchdog = DefaultActionWatchdog(browser_session=self.browser_session)
        self.browser_session._default_action_watchdog.attach(self.browser_session.event_bus)

        DOMWatchdog.model_rebuild()
        self.browser_session._dom_watchdog = DOMWatchdog(event_bus=self.browser_session.event_bus, browser_session=self.browser_session)
        self.browser_session._dom_watchdog.attach_to_session()

        RecordingWatchdog.model_rebuild()
        self.browser_session._recording_watchdog = RecordingWatchdog(event_bus=self.browser_session.event_bus, browser_session=self.browser_session)
        self.browser_session._recording_watchdog.attach_to_session()

        self.browser_session.event_bus.on(ScreenshotEvent, self.browser_session._visual_operations._on_ScreenshotEvent)

        self.browser_session._watchdogs_attached = True

    async def connect(self, cdp_url: str | None = None) -> Self:
        """Connect to a remote chromium-based browser via CDP using cdp-use."""
        self.browser_session.browser_profile.cdp_url = cdp_url or self.browser_session.cdp_url
        if not self.browser_session.cdp_url:
            raise RuntimeError('Cannot setup CDP connection without CDP URL')

        if self.browser_session._cdp_client_root is not None:
            self.logger.warning(
                '⚠️ connect() called but CDP client already exists! Cleaning up old connection before creating new one.'
            )
            try:
                await self.browser_session._cdp_client_root.stop()
            except Exception as e:
                self.logger.debug(f'Error stopping old CDP client: {e}')
            self.browser_session._cdp_client_root = None

        if not self.browser_session.cdp_url.startswith('ws'):
            parsed_url = urlparse(self.browser_session.cdp_url)
            path = parsed_url.path.rstrip('/')

            if not path.endswith('/json/version'):
                path = path + '/json/version'

            url = urlunparse(
                (parsed_url.scheme, parsed_url.netloc, path, parsed_url.params, parsed_url.query, parsed_url.fragment)
            )

            async with httpx.AsyncClient() as client:
                headers = self.browser_session.browser_profile.headers or {}
                version_info = await client.get(url, headers=headers)
                self.logger.debug(f'Raw version info: {str(version_info)}')
                self.browser_session.browser_profile.cdp_url = version_info.json()['webSocketDebuggerUrl']

        assert self.browser_session.cdp_url is not None, 'CDP URL is None.'

        browser_location = 'local browser' if self.browser_session.is_local else 'remote browser'
        self.logger.debug(f'🌎 Connecting to existing chromium-based browser via CDP: {self.browser_session.cdp_url} -> ({browser_location})')

        try:
            headers = getattr(self.browser_session.browser_profile, 'headers', None)
            self.browser_session._cdp_client_root = CDPClient(
                self.browser_session.cdp_url,
                additional_headers=headers,
                max_ws_frame_size=200 * 1024 * 1024,
            )
            assert self.browser_session._cdp_client_root is not None
            await self.browser_session._cdp_client_root.start()

            from core.session.session_manager import SessionManager

            self.browser_session.session_manager = SessionManager(self.browser_session)
            await self.browser_session.session_manager.start_monitoring()
            self.logger.debug('Event-driven session manager started')

            await self._inject_window_open_override()

            await self.browser_session._cdp_client_root.send.Target.setAutoAttach(
                params={'autoAttach': True, 'waitForDebuggerOnStart': False, 'flatten': True}
            )
            self.logger.debug('CDP client connected with auto-attach enabled')

            page_targets_from_manager = self.browser_session.session_manager.get_all_page_targets()

            for target in page_targets_from_manager:
                target_url = target.url
                if is_new_tab_page(target_url) and target_url != 'about:blank':
                    target_id = target.target_id
                    self.logger.debug(f'🔄 Redirecting {target_url} to about:blank for target {target_id}')
                    try:
                        session = await self.browser_session.get_or_create_cdp_session(target_id, focus=False)
                        await session.cdp_client.send.Page.navigate(params={'url': 'about:blank'}, session_id=session.session_id)
                        target.url = 'about:blank'
                    except Exception as e:
                        self.logger.warning(f'Failed to redirect {target_url}: {e}')

            if not page_targets_from_manager:
                new_target = await self.browser_session._cdp_client_root.send.Target.createTarget(params={'url': 'about:blank'})
                target_id = new_target['targetId']
                self.logger.debug(f'📄 Created new blank page: {target_id}')
            else:
                target_id = page_targets_from_manager[0].target_id
                self.logger.debug(f'📄 Using existing page: {target_id}')

            try:
                await self.browser_session.get_or_create_cdp_session(target_id, focus=True)
                self.logger.debug(f'📄 Agent focus set to {target_id[:8]}...')
            except ValueError as e:
                raise RuntimeError(f'Failed to get session for initial target {target_id}: {e}') from e

            await self._setup_proxy_auth()

            if self.browser_session.agent_focus_target_id:
                target = self.browser_session.session_manager.get_target(self.browser_session.agent_focus_target_id)
                if target.title == 'Unknown title':
                    self.logger.warning('Target created but title is unknown (may be normal for about:blank)')

            for idx, target in enumerate(page_targets_from_manager):
                target_url = target.url
                self.logger.debug(f'Dispatching TabCreatedEvent for initial tab {idx}: {target_url}')
                self.browser_session.event_bus.dispatch(TabCreatedEvent(url=target_url, target_id=target.target_id))

            if page_targets_from_manager:
                initial_url = page_targets_from_manager[0].url
                self.browser_session.event_bus.dispatch(AgentFocusChangedEvent(target_id=page_targets_from_manager[0].target_id, url=initial_url))
                self.logger.debug(f'Initial agent focus set to tab 0: {initial_url}')

        except Exception as e:
            self.logger.error(f'❌ FATAL: Failed to setup CDP connection: {e}')
            self.logger.error('❌ Browser cannot continue without CDP connection')

            if self.browser_session.session_manager:
                try:
                    await self.browser_session.session_manager.clear()
                    self.logger.debug('Cleared SessionManager state after initialization failure')
                except Exception as cleanup_error:
                    self.logger.debug(f'Error clearing SessionManager: {cleanup_error}')

            if self.browser_session._cdp_client_root:
                try:
                    await self.browser_session._cdp_client_root.stop()
                    self.logger.debug('Closed CDP client WebSocket after initialization failure')
                except Exception as cleanup_error:
                    self.logger.debug(f'Error closing CDP client: {cleanup_error}')

            self.browser_session.session_manager = None
            self.browser_session._cdp_client_root = None
            self.browser_session.agent_focus_target_id = None
            raise RuntimeError(f'Failed to establish CDP connection to browser: {e}') from e

        return self.browser_session

    async def _setup_proxy_auth(self) -> None:
        """Enable CDP Fetch auth handling for authenticated proxy."""
        assert self.browser_session._cdp_client_root

        try:
            proxy_cfg = self.browser_session.browser_profile.proxy
            username = proxy_cfg.username if proxy_cfg else None
            password = proxy_cfg.password if proxy_cfg else None
            if not username or not password:
                self.logger.debug('Proxy credentials not provided; skipping proxy auth setup')
                return

            try:
                await self.browser_session._cdp_client_root.send.Fetch.enable(params={'handleAuthRequests': True})
                self.logger.debug('Fetch.enable(handleAuthRequests=True) enabled on root client')
            except Exception as e:
                self.logger.debug(f'Fetch.enable on root failed: {type(e).__name__}: {e}')

            try:
                if self.browser_session.agent_focus_target_id:
                    cdp_session = await self.browser_session.get_or_create_cdp_session(self.browser_session.agent_focus_target_id, focus=False)
                    await cdp_session.cdp_client.send.Fetch.enable(
                        params={'handleAuthRequests': True},
                        session_id=cdp_session.session_id,
                    )
                    self.logger.debug('Fetch.enable(handleAuthRequests=True) enabled on focused session')
            except Exception as e:
                self.logger.debug(f'Fetch.enable on focused session failed: {type(e).__name__}: {e}')

            def _on_auth_required(event: AuthRequiredEvent, session_id: SessionID | None = None):
                request_id = event.get('requestId') or event.get('request_id')
                if not request_id:
                    return

                challenge = event.get('authChallenge') or event.get('auth_challenge') or {}
                source = (challenge.get('source') or '').lower()
                if source == 'proxy' and request_id:

                    async def _respond():
                        assert self.browser_session._cdp_client_root
                        try:
                            await self.browser_session._cdp_client_root.send.Fetch.continueWithAuth(
                                params={
                                    'requestId': request_id,
                                    'authChallengeResponse': {
                                        'response': 'ProvideCredentials',
                                        'username': username,
                                        'password': password,
                                    },
                                },
                                session_id=session_id,
                            )
                        except Exception as e:
                            self.logger.debug(f'Proxy auth respond failed: {type(e).__name__}: {e}')

                    create_task_with_error_handling(
                        _respond(), name='auth_respond', logger_instance=self.logger, suppress_exceptions=True
                    )
                else:
                    async def _default():
                        assert self.browser_session._cdp_client_root
                        try:
                            await self.browser_session._cdp_client_root.send.Fetch.continueWithAuth(
                                params={'requestId': request_id, 'authChallengeResponse': {'response': 'Default'}},
                                session_id=session_id,
                            )
                        except Exception as e:
                            self.logger.debug(f'Default auth respond failed: {type(e).__name__}: {e}')

                    if request_id:
                        create_task_with_error_handling(
                            _default(), name='auth_default', logger_instance=self.logger, suppress_exceptions=True
                        )

            def _on_request_paused(event: RequestPausedEvent, session_id: SessionID | None = None):
                request_id = event.get('requestId') or event.get('request_id')
                if not request_id:
                    return

                async def _continue():
                    assert self.browser_session._cdp_client_root
                    try:
                        await self.browser_session._cdp_client_root.send.Fetch.continueRequest(
                            params={'requestId': request_id},
                            session_id=session_id,
                        )
                    except Exception:
                        pass

                create_task_with_error_handling(
                    _continue(), name='request_continue', logger_instance=self.logger, suppress_exceptions=True
                )

            try:
                self.browser_session._cdp_client_root.register.Fetch.authRequired(_on_auth_required)
                self.browser_session._cdp_client_root.register.Fetch.requestPaused(_on_request_paused)
                if self.browser_session.agent_focus_target_id:
                    cdp_session = await self.browser_session.get_or_create_cdp_session(self.browser_session.agent_focus_target_id, focus=False)
                    cdp_session.cdp_client.register.Fetch.authRequired(_on_auth_required)
                    cdp_session.cdp_client.register.Fetch.requestPaused(_on_request_paused)
                self.logger.debug('Registered Fetch.authRequired handlers')
            except Exception as e:
                self.logger.debug(f'Failed to register authRequired handlers: {type(e).__name__}: {e}')

            def _on_attached(event: AttachedToTargetEvent, session_id: SessionID | None = None):
                sid = event.get('sessionId') or event.get('session_id') or session_id
                if not sid:
                    return

                async def _enable():
                    assert self.browser_session._cdp_client_root
                    try:
                        await self.browser_session._cdp_client_root.send.Fetch.enable(
                            params={'handleAuthRequests': True},
                            session_id=sid,
                        )
                        self.logger.debug(f'Fetch.enable(handleAuthRequests=True) enabled on attached session {sid}')
                    except Exception as e:
                        self.logger.debug(f'Fetch.enable on attached session failed: {type(e).__name__}: {e}')

                create_task_with_error_handling(
                    _enable(), name='fetch_enable_attached', logger_instance=self.logger, suppress_exceptions=True
                )

            try:
                self.browser_session._cdp_client_root.register.Target.attachedToTarget(_on_attached)
                self.logger.debug('Registered Target.attachedToTarget handler for Fetch.enable')
            except Exception as e:
                self.logger.debug(f'Failed to register attachedToTarget handler: {type(e).__name__}: {e}')

            try:
                if self.browser_session.agent_focus_target_id:
                    cdp_session = await self.browser_session.get_or_create_cdp_session(self.browser_session.agent_focus_target_id, focus=False)
                    await cdp_session.cdp_client.send.Fetch.enable(
                        params={'handleAuthRequests': True, 'patterns': [{'urlPattern': '*'}]},
                        session_id=cdp_session.session_id,
                    )
            except Exception as e:
                self.logger.debug(f'Fetch.enable on focused session failed: {type(e).__name__}: {e}')
        except Exception as e:
            self.logger.debug(f'Skipping proxy auth setup: {type(e).__name__}: {e}')

    async def _inject_window_open_override(self) -> None:
        """Inject script to override window.open() to prevent new tabs."""
        script = '''
			// Override window.open to navigate in the same tab instead of opening new tabs
			(function() {
				const originalOpen = window.open;
				window.open = function(url, target, features) {
					// If URL is provided, navigate to it in the current tab
					if (url && url !== '' && url !== 'about:blank') {
						window.location.href = url;
						return window;
					}
					// For about:blank or no URL, allow original behavior
					return originalOpen.call(this, url, target, features);
				};
			})();
		'''
        try:
            await self.browser_session._cdp_operations._cdp_add_init_script(script)
            self.logger.debug('🔗 window.open() override injected to prevent new tabs')
        except Exception as e:
            self.logger.debug(f'Failed to inject window.open override: {e}')

