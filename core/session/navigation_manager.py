"""Navigation and frame management - tab switching, URL navigation, frame hierarchy."""

import asyncio
from typing import TYPE_CHECKING, Any, cast

from cdp_use.cdp.target import TargetID

from core.session.events import (
    AgentFocusChangedEvent,
    CloseTabEvent,
    FileDownloadedEvent,
    UrlNavigationRequest,
    NavigationCompleteEvent,
    NavigationStartedEvent,
    SwitchTabEvent,
    TabClosedEvent,
    TabCreatedEvent,
)
from core.dom_processing.models import EnhancedDOMTreeNode
from core.helpers import is_new_tab_page

if TYPE_CHECKING:
    from core.session.session import ChromeSession, DevToolsSession


class NavigationManager:
    """Manages browser navigation, tab switching, and frame operations."""

    def __init__(self, browser_session: 'ChromeSession'):
        self.browser_session = browser_session
        self.logger = browser_session.logger

    async def on_UrlNavigationRequest(self, event: UrlNavigationRequest) -> None:
        """Handle navigation requests - core browser functionality."""
        self.logger.debug(f'[on_UrlNavigationRequest] Received UrlNavigationRequest: url={event.url}, new_tab={event.new_tab}')
        if not self.browser_session.agent_focus_target_id:
            self.logger.warning('Cannot navigate - browser not connected')
            return

        selected_target_id = None
        active_target_id = self.browser_session.agent_focus_target_id

        active_target = self.browser_session.session_manager.get_target(active_target_id)
        if event.new_tab and is_new_tab_page(active_target.url):
            self.logger.debug(f'[on_UrlNavigationRequest] Already on blank tab ({active_target.url}), reusing')
            event.new_tab = False

        try:
            self.logger.debug(f'[on_UrlNavigationRequest] Processing new_tab={event.new_tab}')

            if event.new_tab:
                all_page_targets = self.browser_session.session_manager.get_all_page_targets()
                self.logger.debug(f'[on_UrlNavigationRequest] Found {len(all_page_targets)} existing tabs')

                for tab_index, page_target in enumerate(all_page_targets):
                    self.logger.debug(f'[on_UrlNavigationRequest] Tab {tab_index}: url={page_target.url}, targetId={page_target.target_id}')
                    if page_target.url == 'about:blank' and page_target.target_id != active_target_id:
                        selected_target_id = page_target.target_id
                        self.logger.debug(f'Reusing existing about:blank tab #{selected_target_id[-4:]}')
                        break

                if not selected_target_id:
                    self.logger.debug('[on_UrlNavigationRequest] No reusable about:blank tab found, creating new tab...')
                    try:
                        selected_target_id = await self.browser_session._cdp_operations._cdp_create_new_page('about:blank')
                        self.logger.debug(f'Created new tab #{selected_target_id[-4:]}')
                        await self.browser_session.event_bus.dispatch(TabCreatedEvent(target_id=selected_target_id, url='about:blank'))
                    except Exception as e:
                        self.logger.error(f'[on_UrlNavigationRequest] Failed to create new tab: {type(e).__name__}: {e}')
                        selected_target_id = active_target_id
                        self.logger.warning(f'[on_UrlNavigationRequest] Falling back to current tab #{selected_target_id[-4:]}')
            else:
                selected_target_id = selected_target_id or active_target_id

            if self.browser_session.agent_focus_target_id is None or self.browser_session.agent_focus_target_id != selected_target_id:
                current_focus_suffix = self.browser_session.agent_focus_target_id[-4:] if self.browser_session.agent_focus_target_id else "none"
                self.logger.debug(
                    f'[on_UrlNavigationRequest] Switching to target tab {selected_target_id[-4:]} (current: {current_focus_suffix})'
                )
                await self.browser_session.event_bus.dispatch(SwitchTabEvent(target_id=selected_target_id))
            else:
                self.logger.debug(f'[on_UrlNavigationRequest] Already on target tab {selected_target_id[-4:]}, skipping SwitchTabEvent')

            assert self.browser_session.agent_focus_target_id is not None and self.browser_session.agent_focus_target_id == selected_target_id, (
                'Agent focus not updated to new target_id after SwitchTabEvent should have switched to it'
            )

            await self.browser_session.event_bus.dispatch(NavigationStartedEvent(target_id=selected_target_id, url=event.url))

            await self._navigate_and_wait(event.url, selected_target_id)

            await self._close_extension_options_pages()

            self.logger.debug(f'Dispatching NavigationCompleteEvent for {event.url} (tab #{selected_target_id[-4:]})')
            await self.browser_session.event_bus.dispatch(
                NavigationCompleteEvent(
                    target_id=selected_target_id,
                    url=event.url,
                    status=None,
                )
            )
            await self.browser_session.event_bus.dispatch(AgentFocusChangedEvent(target_id=selected_target_id, url=event.url))

        except Exception as e:
            self.logger.error(f'Navigation failed: {type(e).__name__}: {e}')
            if 'selected_target_id' in locals() and selected_target_id:
                await self.browser_session.event_bus.dispatch(
                    NavigationCompleteEvent(
                        target_id=selected_target_id,
                        url=event.url,
                        error_message=f'{type(e).__name__}: {e}',
                    )
                )
                await self.browser_session.event_bus.dispatch(AgentFocusChangedEvent(target_id=selected_target_id, url=event.url))
            raise

    async def _navigate_and_wait(self, url: str, target_id: str, timeout: float | None = None) -> None:
        """Navigate to URL and wait for page readiness using CDP lifecycle events."""
        cdp_session = await self.browser_session.get_or_create_cdp_session(target_id, focus=False)

        if timeout is None:
            target = self.browser_session.session_manager.get_target(target_id)
            current_url = target.url
            same_domain = (
                url.split('/')[2] == current_url.split('/')[2]
                if url.startswith('http') and current_url.startswith('http')
                else False
            )
            timeout = 2.0 if same_domain else 4.0

        navigation_start_time = asyncio.get_event_loop().time()

        navigation_result = await cdp_session.cdp_client.send.Page.navigate(
            params={'url': url, 'transitionType': 'address_bar'},
            session_id=cdp_session.session_id,
        )

        if navigation_result.get('errorText'):
            raise RuntimeError(f'Navigation failed: {navigation_result["errorText"]}')

        loader_id = navigation_result.get('loaderId')
        lifecycle_start_time = asyncio.get_event_loop().time()

        observed_events = []

        if not hasattr(cdp_session, '_lifecycle_events'):
            raise RuntimeError(
                f'❌ Lifecycle monitoring not enabled for {cdp_session.target_id[:8]}! '
                f'This is a bug - SessionManager should have initialized it. '
                f'Session: {cdp_session}'
            )

        poll_interval_seconds = 0.05
        while (asyncio.get_event_loop().time() - lifecycle_start_time) < timeout:
            try:
                for lifecycle_event in list(cdp_session._lifecycle_events):
                    lifecycle_event_name = lifecycle_event.get('name')
                    lifecycle_loader_id = lifecycle_event.get('loaderId')

                    event_description = f'{lifecycle_event_name}(loader={lifecycle_loader_id[:8] if lifecycle_loader_id else "none"})'
                    if event_description not in observed_events:
                        observed_events.append(event_description)

                    if lifecycle_loader_id and loader_id and lifecycle_loader_id != loader_id:
                        continue

                    if lifecycle_event_name == 'networkIdle':
                        elapsed_ms = (asyncio.get_event_loop().time() - navigation_start_time) * 1000
                        self.logger.debug(f'✅ Page ready for {url} (networkIdle, {elapsed_ms:.0f}ms)')
                        return

                    elif lifecycle_event_name == 'load':
                        elapsed_ms = (asyncio.get_event_loop().time() - navigation_start_time) * 1000
                        self.logger.debug(f'✅ Page ready for {url} (load, {elapsed_ms:.0f}ms)')
                        return

            except Exception as e:
                self.logger.debug(f'Error polling lifecycle events: {e}')

            await asyncio.sleep(poll_interval_seconds)

        elapsed_ms = (asyncio.get_event_loop().time() - navigation_start_time) * 1000
        if not observed_events:
            self.logger.error(
                f'❌ No lifecycle events received for {url} after {elapsed_ms:.0f}ms! '
                f'Monitoring may have failed. Target: {cdp_session.target_id[:8]}'
            )
        else:
            self.logger.debug(f'⚠️ Page readiness timeout ({timeout}s, {elapsed_ms:.0f}ms) for {url}')

    async def on_SwitchTabEvent(self, event: SwitchTabEvent) -> TargetID:
        """Handle tab switching - core browser functionality."""
        if not self.browser_session.agent_focus_target_id:
            raise RuntimeError('Cannot switch tabs - browser not connected')

        all_page_targets = self.browser_session.session_manager.get_all_page_targets()
        if event.target_id is None:
            if all_page_targets:
                event.target_id = all_page_targets[-1].target_id
            else:
                assert self.browser_session._cdp_client_root is not None, 'CDP client root not initialized - browser may not be connected yet'
                new_target_id = await self.browser_session._cdp_operations._cdp_create_new_page('about:blank')
                self.browser_session.event_bus.dispatch(TabCreatedEvent(url='about:blank', target_id=new_target_id))
                self.browser_session.event_bus.dispatch(AgentFocusChangedEvent(target_id=new_target_id, url='about:blank'))
                return new_target_id

        assert event.target_id is not None, 'target_id must be set at this point'
        cdp_session = await self.browser_session.get_or_create_cdp_session(target_id=event.target_id, focus=True)

        await cdp_session.cdp_client.send.Target.activateTarget(params={'targetId': event.target_id})

        target = self.browser_session.session_manager.get_target(event.target_id)

        await self.browser_session.event_bus.dispatch(
            AgentFocusChangedEvent(
                target_id=target.target_id,
                url=target.url,
            )
        )
        return target.target_id

    async def on_CloseTabEvent(self, event: CloseTabEvent) -> None:
        """Handle tab closure - update focus if needed."""
        try:
            await self.browser_session.event_bus.dispatch(TabClosedEvent(target_id=event.target_id))

            try:
                cdp_session = await self.browser_session.get_or_create_cdp_session(target_id=None, focus=False)
                await cdp_session.cdp_client.send.Target.closeTarget(params={'targetId': event.target_id})
            except Exception as e:
                self.logger.debug(f'Target may already be closed: {e}')
        except Exception as e:
            self.logger.warning(f'Error during tab close cleanup: {e}')

    async def on_TabCreatedEvent(self, event: TabCreatedEvent) -> None:
        """Handle tab creation - apply viewport settings to new tab."""
        if self.browser_session.browser_profile.viewport and not self.browser_session.browser_profile.no_viewport:
                    try:
                        viewport_width = self.browser_session.browser_profile.viewport.width
                        viewport_height = self.browser_session.browser_profile.viewport.height
                        device_scale_factor = self.browser_session.browser_profile.device_scale_factor or 1.0

                        self.logger.info(
                            f'Setting viewport to {viewport_width}x{viewport_height} with device scale factor {device_scale_factor} whereas original device scale factor was {self.browser_session.browser_profile.device_scale_factor}'
                        )
                        await self.browser_session._cdp_operations._cdp_set_viewport(viewport_width, viewport_height, device_scale_factor, target_id=event.target_id)

                        self.logger.debug(f'Applied viewport {viewport_width}x{viewport_height} to tab {event.target_id[-8:]}')
                    except Exception as e:
                        self.logger.warning(f'Failed to set viewport for new tab {event.target_id[-8:]}: {e}')

    async def on_TabClosedEvent(self, event: TabClosedEvent) -> None:
        """Handle tab closure - update focus if needed."""
        if not self.browser_session.agent_focus_target_id:
            return

        current_target_id = self.browser_session.agent_focus_target_id

        if current_target_id == event.target_id:
            await self.browser_session.event_bus.dispatch(SwitchTabEvent(target_id=None))

    async def on_AgentFocusChangedEvent(self, event: AgentFocusChangedEvent) -> None:
        """Handle agent focus change - update focus and clear cache."""
        self.logger.debug(f'🔄 AgentFocusChangedEvent received: target_id=...{event.target_id[-4:]} url={event.url}')

        if self.browser_session._dom_watchdog:
            self.browser_session._dom_watchdog.clear_cache()

        self.browser_session._cached_browser_state_summary = None
        self.browser_session._cached_selector_map.clear()
        self.logger.debug('🔄 Cached browser state cleared')

        if event.target_id:
            await self.browser_session.get_or_create_cdp_session(target_id=event.target_id, focus=True)

            if self.browser_session.browser_profile.viewport and not self.browser_session.browser_profile.no_viewport:
                try:
                    viewport_width = self.browser_session.browser_profile.viewport.width
                    viewport_height = self.browser_session.browser_profile.viewport.height
                    device_scale_factor = self.browser_session.browser_profile.device_scale_factor or 1.0

                    await self.browser_session._cdp_operations._cdp_set_viewport(viewport_width, viewport_height, device_scale_factor, target_id=event.target_id)

                    self.logger.debug(f'Applied viewport {viewport_width}x{viewport_height} to tab {event.target_id[-8:]}')
                except Exception as e:
                    self.logger.warning(f'Failed to set viewport for tab {event.target_id[-8:]}: {e}')
        else:
            raise RuntimeError('AgentFocusChangedEvent received with no target_id for newly focused tab')

    async def on_FileDownloadedEvent(self, event: FileDownloadedEvent) -> None:
        """Track downloaded files during this session."""
        self.logger.debug(f'FileDownloadedEvent received: {event.file_name} at {event.path}')
        if event.path and event.path not in self.browser_session._downloaded_files:
            self.browser_session._downloaded_files.append(event.path)
            self.logger.info(f'📁 Tracked download: {event.file_name} ({len(self.browser_session._downloaded_files)} total downloads in session)')
        else:
            if not event.path:
                self.logger.warning(f'FileDownloadedEvent has no path: {event}')
            else:
                self.logger.debug(f'File already tracked: {event.path}')

    async def _close_extension_options_pages(self) -> None:
        """Close any extension options/welcome pages that have opened."""
        try:
            page_targets = self.browser_session.session_manager.get_all_page_targets()

            for target in page_targets:
                target_url = target.url
                target_id = target.target_id

                if 'chrome-extension://' in target_url and (
                    'options.html' in target_url or 'welcome.html' in target_url or 'onboarding.html' in target_url
                ):
                    self.logger.info(f'[ChromeSession] 🚫 Closing extension options page: {target_url}')
                    try:
                        await self.browser_session._cdp_operations._cdp_close_page(target_id)
                    except Exception as e:
                        self.logger.debug(f'[ChromeSession] Could not close extension page {target_id}: {e}')

        except Exception as e:
            self.logger.debug(f'[ChromeSession] Error closing extension options pages: {e}')

    async def get_all_frames(self) -> tuple[dict[str, dict], dict[str, str]]:
        """Get a complete frame hierarchy from all browser targets."""
        all_frames = {}
        target_sessions = {}

        include_cross_origin = self.browser_session.browser_profile.cross_origin_iframes

        targets = await self.browser_session._cdp_operations._cdp_get_all_pages(
            include_http=True,
            include_about=True,
            include_pages=True,
            include_iframes=include_cross_origin,
            include_workers=False,
            include_chrome=False,
            include_chrome_extensions=False,
            include_chrome_error=include_cross_origin,
        )
        all_targets = targets

        for target in all_targets:
            target_id = target['targetId']

            if not include_cross_origin and target.get('type') == 'iframe':
                continue

            if not include_cross_origin:
                if self.browser_session.agent_focus_target_id and target_id != self.browser_session.agent_focus_target_id:
                    continue
                try:
                    cdp_session = await self.browser_session.get_or_create_cdp_session(self.browser_session.agent_focus_target_id, focus=False)
                except ValueError:
                    continue
            else:
                cdp_session = await self.browser_session.get_or_create_cdp_session(target_id, focus=False)

            if cdp_session:
                target_sessions[target_id] = cdp_session.session_id

                try:
                    frame_tree_result = await cdp_session.cdp_client.send.Page.getFrameTree(session_id=cdp_session.session_id)

                    def process_frame_tree(node, parent_frame_id=None):
                        """Recursively process frame tree and add to all_frames."""
                        frame = node.get('frame', {})
                        current_frame_id = frame.get('id')

                        if current_frame_id:
                            actual_parent_id = frame.get('parentId') or parent_frame_id

                            from core.session.cdp_operations import CDPOperationsManager
                            
                            frame_info = {
                                **frame,
                                'frameTargetId': target_id,
                                'parentFrameId': actual_parent_id,
                                'childFrameIds': [],
                                'isCrossOrigin': False,
                                'isValidTarget': CDPOperationsManager._is_valid_target(
                                    target,
                                    include_http=True,
                                    include_about=True,
                                    include_pages=True,
                                    include_iframes=True,
                                    include_workers=False,
                                    include_chrome=False,
                                    include_chrome_extensions=False,
                                    include_chrome_error=False,
                                ),
                            }

                            cross_origin_type = frame.get('crossOriginIsolatedContextType')
                            if cross_origin_type and cross_origin_type != 'NotIsolated':
                                frame_info['isCrossOrigin'] = True

                            if target.get('type') == 'iframe':
                                frame_info['isCrossOrigin'] = True

                            if not include_cross_origin and frame_info.get('isCrossOrigin'):
                                return

                            child_frames = node.get('childFrames', [])
                            for child in child_frames:
                                child_frame = child.get('frame', {})
                                child_frame_id = child_frame.get('id')
                                if child_frame_id:
                                    frame_info['childFrameIds'].append(child_frame_id)

                            if current_frame_id in all_frames:
                                existing = all_frames[current_frame_id]
                                if target.get('type') == 'iframe':
                                    existing['frameTargetId'] = target_id
                                    existing['isCrossOrigin'] = True
                            else:
                                all_frames[current_frame_id] = frame_info

                            if include_cross_origin or not frame_info.get('isCrossOrigin'):
                                for child in child_frames:
                                    process_frame_tree(child, current_frame_id)

                    process_frame_tree(frame_tree_result.get('frameTree', {}))

                except Exception as e:
                    self.logger.debug(f'Failed to get frame tree for target {target_id}: {e}')

        if include_cross_origin:
            await self._populate_frame_metadata(all_frames, target_sessions)

        return all_frames, target_sessions

    async def _populate_frame_metadata(self, all_frames: dict[str, dict], target_sessions: dict[str, str]) -> None:
        """Populate additional frame metadata like backend node IDs and parent target IDs."""
        for frame_id_iter, frame_info in all_frames.items():
            parent_frame_id = frame_info.get('parentFrameId')

            if parent_frame_id and parent_frame_id in all_frames:
                parent_frame_info = all_frames[parent_frame_id]
                parent_target_id = parent_frame_info.get('frameTargetId')

                frame_info['parentTargetId'] = parent_target_id

                if parent_target_id in target_sessions:
                    assert parent_target_id is not None
                    parent_session_id = target_sessions[parent_target_id]
                    try:
                        await self.browser_session.cdp_client.send.DOM.enable(session_id=parent_session_id)

                        frame_owner = await self.browser_session.cdp_client.send.DOM.getFrameOwner(
                            params={'frameId': frame_id_iter}, session_id=parent_session_id
                        )

                        if frame_owner:
                            frame_info['backendNodeId'] = frame_owner.get('backendNodeId')
                            frame_info['nodeId'] = frame_owner.get('nodeId')

                    except Exception:
                        pass

    async def find_frame_target(self, frame_id: str, all_frames: dict[str, dict] | None = None) -> dict | None:
        """Find the frame info for a specific frame ID."""
        if all_frames is None:
            all_frames, _ = await self.get_all_frames()

        return all_frames.get(frame_id)

    async def cdp_client_for_target(self, target_id: TargetID) -> 'DevToolsSession':
        return await self.browser_session.get_or_create_cdp_session(target_id, focus=False)

    async def cdp_client_for_frame(self, frame_id: str) -> 'DevToolsSession':
        """Get a CDP client attached to the target containing the specified frame."""
        if not self.browser_session.browser_profile.cross_origin_iframes:
            return await self.browser_session.get_or_create_cdp_session()

        all_frames, target_sessions = await self.get_all_frames()

        frame_info = await self.find_frame_target(frame_id, all_frames)

        if frame_info:
            target_id = frame_info.get('frameTargetId')

            if target_id in target_sessions:
                assert target_id is not None
                session_id = target_sessions[target_id]
                return await self.browser_session.get_or_create_cdp_session(target_id, focus=False)

        raise ValueError(f"Frame with ID '{frame_id}' not found in any target")

    async def cdp_client_for_node(self, node: EnhancedDOMTreeNode) -> 'DevToolsSession':
        """Get CDP client for a specific DOM node based on its frame."""
        if node.session_id and self.browser_session.session_manager:
            try:
                cdp_session = self.browser_session.session_manager.get_session(node.session_id)
                if cdp_session:
                    target = self.browser_session.session_manager.get_target(cdp_session.target_id)
                    self.logger.debug(f'✅ Using session from node.session_id for node {node.backend_node_id}: {target.url}')
                    return cdp_session
            except Exception as e:
                self.logger.debug(f'Failed to get session by session_id {node.session_id}: {e}')

        if node.frame_id:
            try:
                cdp_session = await self.cdp_client_for_frame(node.frame_id)
                target = self.browser_session.session_manager.get_target(cdp_session.target_id)
                self.logger.debug(f'✅ Using session from node.frame_id for node {node.backend_node_id}: {target.url}')
                return cdp_session
            except Exception as e:
                self.logger.debug(f'Failed to get session for frame {node.frame_id}: {e}')

        if node.target_id:
            try:
                cdp_session = await self.browser_session.get_or_create_cdp_session(target_id=node.target_id, focus=False)
                target = self.browser_session.session_manager.get_target(cdp_session.target_id)
                self.logger.debug(f'✅ Using session from node.target_id for node {node.backend_node_id}: {target.url}')
                return cdp_session
            except Exception as e:
                self.logger.debug(f'Failed to get session for target {node.target_id}: {e}')

        if self.browser_session.agent_focus_target_id:
            target = self.browser_session.session_manager.get_target(self.browser_session.agent_focus_target_id)
            try:
                cdp_session = await self.browser_session.get_or_create_cdp_session(self.browser_session.agent_focus_target_id, focus=False)
                if target:
                    self.logger.warning(
                        f'⚠️ Node {node.backend_node_id} has no session/frame/target info. Using agent_focus session: {target.url}'
                    )
                return cdp_session
            except ValueError:
                pass

        self.logger.error(f'❌ No session info for node {node.backend_node_id} and no agent_focus available. Using main session.')
        return await self.browser_session.get_or_create_cdp_session()

