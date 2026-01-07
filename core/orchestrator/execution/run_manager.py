"""Менеджер для основного цикла выполнения агента с логированием."""

from __future__ import annotations  # Отложенное разрешение аннотаций типов

import asyncio
import inspect
import logging
import time
from typing import TYPE_CHECKING, Any

from core.ai_models.models import BaseChatModel
from core.config import CONFIG
from core.helpers import check_latest_agent_version
from core.orchestrator.models import ExecutionHistoryList, AgentStructuredOutput, StepContext, StepDecision, ExecutionResult
from core.session.models import BrowserStateSummary  # Импортируем для runtime использования

if TYPE_CHECKING:
    from core.orchestrator.manager import TaskOrchestrator


class RunManager:
    """Менеджер для основного цикла выполнения агента с логированием."""

    def __init__(self, agent: 'TaskOrchestrator'):
        self.orchestrator = agent
        self.logger = agent.logger

    # ========== ЛОГИРОВАНИЕ ==========
    
    async def log_agent_run(self) -> None:
        """Log the agent run."""
        # Blue color for task
        self.logger.info(f'\033[34m🎯 Task: {self.orchestrator.task}\033[0m')

        self.logger.debug(f'🤖 Версия библиотеки агента {self.orchestrator.version} ({self.orchestrator.source})')

        # Check for latest version and log upgrade message if needed
        if CONFIG.AGENT_VERSION_CHECK:
            latest_version = await check_latest_agent_version()
            if latest_version and latest_version != self.orchestrator.version:
                self.logger.info(
                    f'📦 Доступна новая версия агента: {latest_version} (текущая: {self.orchestrator.version}).'
                )

    def log_first_step_startup(self) -> None:
        """Log startup message only on the first step."""
        if len(self.orchestrator.history.history) == 0:
            self.logger.info(
                f'Запуск агента версии {self.orchestrator.version} с провайдером={self.orchestrator.llm.provider} и моделью={self.orchestrator.llm.model}'
            )

    def log_step_context(self, browser_state_summary: BrowserStateSummary) -> None:
        """Log step context information."""
        # Проверяем, что browser_state_summary - это объект, а не словарь
        if isinstance(browser_state_summary, dict):
            url = browser_state_summary.get('url', '') if browser_state_summary else ''
            dom_state = browser_state_summary.get('dom_state', {}) if browser_state_summary else {}
            selector_map = dom_state.get('selector_map', {}) if isinstance(dom_state, dict) else getattr(dom_state, 'selector_map', {})
        else:
            url = browser_state_summary.url if browser_state_summary else ''
            dom_state = browser_state_summary.dom_state if browser_state_summary and hasattr(browser_state_summary, 'dom_state') else None
            selector_map = dom_state.selector_map if dom_state and hasattr(dom_state, 'selector_map') else {}
        
        url_short = url[:50] + '...' if len(url) > 50 else url
        interactive_count = len(selector_map) if selector_map else 0
        self.logger.info('\n')
        self.logger.info(f'📍 Step {self.orchestrator.state.n_steps}:')
        self.logger.debug(f'Evaluating page with {interactive_count} interactive elements on: {url_short}')

    def log_next_action_summary(self, parsed: StepDecision) -> None:
        """Log a comprehensive summary of the next action(s)."""
        if not (self.logger.isEnabledFor(logging.DEBUG) and parsed.action):
            return

        action_count = len(parsed.action)

        # Collect action details
        action_details = []
        for i, action in enumerate(parsed.action):
            action_data = action.model_dump(exclude_unset=True)
            action_name = next(iter(action_data.keys())) if action_data else 'unknown'
            action_params = action_data.get(action_name, {}) if action_data else {}

            # Format key parameters concisely
            param_summary = []
            if isinstance(action_params, dict):
                for key, value in action_params.items():
                    if key == 'index':
                        param_summary.append(f'#{value}')
                    elif key == 'text' and isinstance(value, str):
                        text_preview = value[:30] + '...' if len(value) > 30 else value
                        param_summary.append(f'text="{text_preview}"')
                    elif key == 'url':
                        param_summary.append(f'url="{value}"')
                    elif key == 'success':
                        param_summary.append(f'success={value}')
                    elif isinstance(value, (str, int, bool)):
                        val_str = str(value)[:30] + '...' if len(str(value)) > 30 else str(value)
                        param_summary.append(f'{key}={val_str}')

            param_str = f'({", ".join(param_summary)})' if param_summary else ''
            action_details.append(f'{action_name}{param_str}')

    def log_step_completion_summary(self, step_start_time: float, result: list[ExecutionResult]) -> str | None:
        """Log step completion summary with action count, timing, and success/failure stats."""
        if not result:
            return None

        step_duration = time.time() - step_start_time
        action_count = len(result)

        # Count success and failures
        success_count = sum(1 for r in result if not r.error)
        failure_count = action_count - success_count

        # Format success/failure indicators
        success_indicator = f'✅ {success_count}' if success_count > 0 else ''
        failure_indicator = f'❌ {failure_count}' if failure_count > 0 else ''
        status_parts = [part for part in [success_indicator, failure_indicator] if part]
        status_str = ' | '.join(status_parts) if status_parts else '✅ 0'

        message = (
            f'Step {self.orchestrator.state.n_steps}: Ran {action_count} action{"" if action_count == 1 else "s"} '
            f'in {step_duration:.2f}s: {status_str}'
        )
        self.logger.debug(message)
        return message

    def log_final_outcome_messages(self) -> None:
        """Log helpful messages to user based on agent run outcome."""
        # Check if agent failed
        is_successful = self.orchestrator.history.is_successful()

        if is_successful is False or is_successful is None:
            # Get final result to check for specific failure reasons
            final_result = self.orchestrator.history.final_result()
            final_result_str = str(final_result).lower() if final_result else ''

            # Check for captcha/cloudflare related failures
            captcha_keywords = ['captcha', 'cloudflare', 'recaptcha', 'challenge', 'bot detection', 'access denied']
            has_captcha_issue = any(keyword in final_result_str for keyword in captcha_keywords)

            if has_captcha_issue:
                # Suggest use_cloud=True for captcha/cloudflare issues
                task_preview = self.orchestrator.task[:10] if len(self.orchestrator.task) > 10 else self.orchestrator.task
                self.logger.info('')
                self.logger.info('Failed because of CAPTCHA? For better browser stealth, try:')
                self.logger.info(f'   agent = Agent(task="{task_preview}...", browser=Browser())')

            # General failure message
            self.logger.info('')
            self.logger.info('Did the Agent not work as expected? Let us fix this!')
            self.logger.info('   Если ошибка повторяется, сохраните лог и пример задачи для дальнейшей отладки.')

    async def log_completion(self) -> None:
        """Log the completion of the task."""
        if self.orchestrator.history.is_successful():
            self.logger.info('✅ Task completed successfully')
            await self.demo_mode_log('Task completed successfully', 'success', {'tag': 'task'})

    def _prepare_demo_message(self, message: str, limit: int = 600) -> str:
        """Prepare demo message (previously truncated long entries)."""
        return message.strip()

    async def demo_mode_log(self, message: str, level: str = 'info', metadata: dict[str, Any] | None = None) -> None:
        """Send log message to demo mode panel."""
        if not self.orchestrator._demo_mode_enabled or not message or self.orchestrator.browser_session is None:
            return
        try:
            await self.orchestrator.browser_session.send_demo_mode_log(
                message=self._prepare_demo_message(message),
                level=level,
                metadata=metadata or {},
            )
        except Exception as exc:
            self.logger.debug(f'[DemoMode] Failed to send overlay log: {exc}')

    async def broadcast_model_state(self, parsed: StepDecision) -> None:
        """Broadcast model state to demo mode."""
        if not self.orchestrator._demo_mode_enabled:
            return

        state = parsed.current_state
        step_meta = {'step': self.orchestrator.state.n_steps}

        if state.thinking:
            await self.demo_mode_log(state.thinking, 'thought', step_meta)

        if state.evaluation_previous_goal:
            eval_text = state.evaluation_previous_goal
            level = 'success' if 'success' in eval_text.lower() else 'warning' if 'failure' in eval_text.lower() else 'info'
            await self.demo_mode_log(eval_text, level, step_meta)

        if state.memory:
            await self.demo_mode_log(f'Memory: {state.memory}', 'info', step_meta)

        if state.next_goal:
            await self.demo_mode_log(f'Next goal: {state.next_goal}', 'info', step_meta)

    # ========== ВЫПОЛНЕНИЕ ==========

    async def run(
        self,
        max_steps: int = 100,
        on_step_start=None,
        on_step_end=None,
    ) -> ExecutionHistoryList[AgentStructuredOutput]:
        """Execute the task with maximum number of steps."""
        loop = asyncio.get_event_loop()
        agent_run_error: str | None = None
        should_delay_close = False

        # Set up the signal handler with callbacks specific to this agent
        from core.helpers import SignalHandler

        signal_handler = SignalHandler(
            loop=loop,
            pause_callback=self.orchestrator.pause,
            resume_callback=self.orchestrator.resume,
            custom_exit_callback=None,
            exit_on_second_int=True,
        )
        signal_handler.register()

        try:
            await self.log_agent_run()

            self.logger.debug(
                f'🔧 Agent setup: Agent Session ID {self.orchestrator.session_id[-4:]}, Task ID {self.orchestrator.task_id[-4:]}, Browser Session ID {self.orchestrator.browser_session.id[-4:] if self.orchestrator.browser_session else "None"} {"(connecting via CDP)" if (self.orchestrator.browser_session and self.orchestrator.browser_session.cdp_url) else "(launching local browser)"}'
            )

            # Initialize timing for session and task
            self.orchestrator._session_start_time = time.time()
            self.orchestrator._task_start_time = self.orchestrator._session_start_time

            # Only dispatch session events if this is the first run
            if not self.orchestrator.state.session_initialized:
                self.orchestrator.state.session_initialized = True

            # Log startup message on first step
            self.log_first_step_startup()
            # Start browser session and attach watchdogs
            await self.orchestrator.browser_session.start()
            if self.orchestrator._demo_mode_enabled:
                await self.demo_mode_log(f'Started task: {self.orchestrator.task}', 'info', {'tag': 'task'})
                await self.demo_mode_log(
                    'Demo mode active - follow the side panel for live thoughts and actions.',
                    'info',
                    {'tag': 'status'},
                )

            # Register skills as actions if SkillService is configured
            await self.orchestrator._register_skills_as_actions()

            # Пересчитываем initial_actions если задача была изменена после инициализации
            if self.orchestrator.directly_open_url and not self.orchestrator.state.follow_up_task and not self.orchestrator.initial_actions:
                initial_url = self.orchestrator._extract_start_url(self.orchestrator.task)
                if initial_url:
                    self.logger.info(f'🔗 Найден URL в задаче: {initial_url}, добавляю как начальное действие...')
                    self.orchestrator.initial_url = initial_url
                    self.orchestrator.initial_actions = self.orchestrator._convert_initial_actions([{'navigate': {'url': initial_url, 'new_tab': False}}])

            # Normally there was no try catch here but the callback can raise an InterruptedError
            try:
                await self.orchestrator._rerun_manager._execute_initial_actions()
            except InterruptedError:
                pass
            except Exception as e:
                raise e

            self.logger.debug(
                f'🔄 Starting main execution loop with max {max_steps} steps (currently at step {self.orchestrator.state.n_steps})...'
            )
            while self.orchestrator.state.n_steps <= max_steps:
                current_step = self.orchestrator.state.n_steps - 1  # Convert to 0-indexed for step_info

                # Use the consolidated pause state management
                if self.orchestrator.state.paused:
                    self.logger.debug(f'⏸️ Step {self.orchestrator.state.n_steps}: Agent paused, waiting to resume...')
                    await self.orchestrator._external_pause_event.wait()
                    signal_handler.reset()

                # Check if we should stop due to too many failures
                if (self.orchestrator.state.consecutive_failures) >= self.orchestrator.settings.max_failures + int(
                    self.orchestrator.settings.final_response_after_failure
                ):
                    self.logger.error(f'❌ Stopping due to {self.orchestrator.settings.max_failures} consecutive failures')
                    agent_run_error = f'Stopped due to {self.orchestrator.settings.max_failures} consecutive failures'
                    break

                # Check control flags before each step
                if self.orchestrator.state.stopped:
                    self.logger.info('🛑 Agent stopped')
                    agent_run_error = 'Agent stopped programmatically'
                    break

                step_info = StepContext(step_number=current_step, max_steps=max_steps)
                is_done = await self._execute_step(current_step, max_steps, step_info, on_step_start, on_step_end)

                if is_done:
                    # Agent has marked the task as done
                    if self.orchestrator._demo_mode_enabled and self.orchestrator.history.history:
                        final_result_text = self.orchestrator.history.final_result() or 'Task completed'
                        await self.demo_mode_log(f'Final Result: {final_result_text}', 'success', {'tag': 'task'})

                    should_delay_close = True
                    break
            else:
                agent_run_error = 'Failed to complete task in maximum steps'

                self.orchestrator.history.add_item(
                    self.orchestrator._history_manager._make_history_item_with_error(agent_run_error)
                )

                self.logger.info(f'❌ {agent_run_error}')

            self.orchestrator.history.usage = await self.orchestrator.token_cost_service.get_usage_summary()

            # set the model output schema and call it on the fly
            if self.orchestrator.history._output_model_schema is None and self.orchestrator.output_model_schema is not None:
                self.orchestrator.history._output_model_schema = self.orchestrator.output_model_schema

            return self.orchestrator.history

        except KeyboardInterrupt:
            # Already handled by our signal handler, but catch any direct KeyboardInterrupt as well
            self.logger.debug('Got KeyboardInterrupt during execution, returning current history')
            agent_run_error = 'KeyboardInterrupt'

            self.orchestrator.history.usage = await self.orchestrator.token_cost_service.get_usage_summary()

            return self.orchestrator.history

        except Exception as e:
            self.logger.error(f'Agent run failed with exception: {e}', exc_info=True)
            agent_run_error = str(e)
            raise e

        finally:
            if should_delay_close and self.orchestrator._demo_mode_enabled and agent_run_error is None:
                await asyncio.sleep(30)
            if agent_run_error:
                await self.demo_mode_log(f'Agent stopped: {agent_run_error}', 'error', {'tag': 'run'})
            # Log token usage summary
            await self.orchestrator.token_cost_service.log_usage_summary()

            # Unregister signal handlers before cleanup
            signal_handler.unregister()

            # Generate GIF if needed before stopping event bus
            if self.orchestrator.settings.generate_gif:
                output_path: str = 'agent_history.gif'
                if isinstance(self.orchestrator.settings.generate_gif, str):
                    output_path = self.orchestrator.settings.generate_gif

                # Lazy import gif module to avoid heavy startup cost
                try:
                    from core.orchestrator.gif import create_history_gif
                    create_history_gif(task=self.orchestrator.task, history=self.orchestrator.history, output_path=output_path)
                except ImportError:
                    self.logger.warning('GIF generation module not available')

            # Log final messages to user based on outcome
            self.log_final_outcome_messages()

            # Stop the event bus gracefully
            await self.orchestrator.eventbus.stop(timeout=3.0)

            await self.orchestrator.close()

    async def _execute_step(
        self,
        step: int,
        max_steps: int,
        step_info: StepContext,
        on_step_start=None,
        on_step_end=None,
    ) -> bool:
        """Execute a single step with timeout."""
        if on_step_start is not None:
            await on_step_start(self.orchestrator)

        await self.demo_mode_log(
            f'Starting step {step + 1}/{max_steps}',
            'info',
            {'step': step + 1, 'total_steps': max_steps},
        )

        self.logger.debug(f'🚶 Starting step {step + 1}/{max_steps}...')

        try:
            await asyncio.wait_for(
                self.orchestrator.step(step_info),
                timeout=self.orchestrator.settings.step_timeout,
            )
            self.logger.debug(f'✅ Completed step {step + 1}/{max_steps}')
        except TimeoutError:
            # Handle step timeout gracefully
            error_msg = f'Step {step + 1} timed out after {self.orchestrator.settings.step_timeout} seconds'
            self.logger.error(f'⏰ {error_msg}')
            await self.demo_mode_log(error_msg, 'error', {'step': step + 1})
            self.orchestrator.state.consecutive_failures += 1
            self.orchestrator.state.last_result = [ExecutionResult(error=error_msg)]

        if on_step_end is not None:
            await on_step_end(self.orchestrator)

        if self.orchestrator.history.is_done():
            await self.log_completion()

            # Run judge before done callback if enabled

            if self.orchestrator.register_done_callback:
                if inspect.iscoroutinefunction(self.orchestrator.register_done_callback):
                    await self.orchestrator.register_done_callback(self.orchestrator.history)
                else:
                    self.orchestrator.register_done_callback(self.orchestrator.history)

            return True

        return False

