"""Менеджер для rerun истории агента."""

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from core.ai_models.models import BaseChatModel
from core.dom_processing.models import DOMInteractedElement
from core.orchestrator.models import (
    ExecutionHistory,
    ExecutionHistoryList,
    ExecutionResult,
    BrowserStateHistory,
    RerunSummaryAction,
    StepMetadata,
)
from core.session.models import BrowserStateSummary

if TYPE_CHECKING:
    from core.actions.registry.models import CommandModel
    from core.orchestrator.manager import TaskOrchestrator


class RerunManager:
    """Менеджер для rerun истории агента."""

    def __init__(self, agent: 'TaskOrchestrator'):
        self.orchestrator = agent
        self.logger = agent.logger

    async def rerun_history(
        self,
        history: ExecutionHistoryList,
        max_retries: int = 3,
        skip_failures: bool = True,
        delay_between_actions: float = 2.0,
        summary_llm: BaseChatModel | None = None,
        ai_step_llm: BaseChatModel | None = None,
    ) -> list[ExecutionResult]:
        """Rerun a saved history of actions with error handling and retry logic."""
        # Skip cloud sync session events for rerunning
        self.orchestrator.state.session_initialized = True

        # Initialize browser session
        await self.orchestrator.browser_session.start()

        results = []

        for i, history_item in enumerate(history.history):
            goal = history_item.model_output.current_state.next_goal if history_item.model_output else ''
            step_num = history_item.metadata.step_number if history_item.metadata else i
            step_name = 'Initial actions' if step_num == 0 else f'Step {step_num}'

            # Determine step delay
            if history_item.metadata and history_item.metadata.step_interval is not None:
                step_delay = history_item.metadata.step_interval
                if step_delay < 1.0:
                    delay_str = f'{step_delay * 1000:.0f}ms'
                else:
                    delay_str = f'{step_delay:.1f}s'
                delay_source = f'using saved step_interval={delay_str}'
            else:
                step_delay = delay_between_actions
                if step_delay < 1.0:
                    delay_str = f'{step_delay * 1000:.0f}ms'
                else:
                    delay_str = f'{step_delay:.1f}s'
                delay_source = f'using default delay={delay_str}'

            self.logger.info(f'Replaying {step_name} ({i + 1}/{len(history.history)}) [{delay_source}]: {goal}')

            if (
                not history_item.model_output
                or not history_item.model_output.action
                or history_item.model_output.action == [None]
            ):
                self.logger.warning(f'{step_name}: No action to replay, skipping')
                results.append(ExecutionResult(error='No action to replay'))
                continue

            retry_count = 0
            while retry_count < max_retries:
                try:
                    result = await self._execute_history_step(history_item, step_delay, ai_step_llm)
                    results.extend(result)
                    break

                except Exception as e:
                    retry_count += 1
                    if retry_count == max_retries:
                        error_msg = f'{step_name} failed after {max_retries} attempts: {str(e)}'
                        self.logger.error(error_msg)
                        if not skip_failures:
                            results.append(ExecutionResult(error=error_msg))
                            raise RuntimeError(error_msg)
                    else:
                        self.logger.warning(f'{step_name} failed (attempt {retry_count}/{max_retries}), retrying...')
                        await asyncio.sleep(delay_between_actions)

        # Generate AI summary of rerun completion
        self.logger.info('🤖 Generating AI summary of rerun completion...')
        summary_result = await self._generate_rerun_summary(self.orchestrator.task, results, summary_llm)
        results.append(summary_result)

        await self.orchestrator.close()
        return results

    async def _execute_history_step(
        self, history_item: ExecutionHistory, delay: float, ai_step_llm: BaseChatModel | None = None
    ) -> list[ExecutionResult]:
        """Execute a single step from history with element validation."""
        assert self.orchestrator.browser_session is not None, 'ChromeSession is not set up'

        await asyncio.sleep(delay)
        state = await self.orchestrator.browser_session.get_browser_state_summary(include_screenshot=False)
        if not state or not history_item.model_output:
            raise ValueError('Invalid state or model output')

        results = []
        pending_actions = []

        for i, action in enumerate(history_item.model_output.action):
            # Check if this is an extract action - use AI step instead
            action_data = action.model_dump(exclude_unset=True)
            action_name = next(iter(action_data.keys()), None)

            if action_name == 'extract':
                # Execute any pending actions first to maintain correct order
                if pending_actions:
                    batch_results = await self.orchestrator.multi_act(pending_actions)
                    results.extend(batch_results)
                    pending_actions = []

                # Now execute AI step for extract action
                extract_params = action_data['extract']
                query = extract_params.get('query', '')
                extract_links = extract_params.get('extract_links', False)

                self.logger.info(f'🤖 Using AI step for extract action: {query[:50]}...')
                ai_result = await self._execute_ai_step(
                    query=query,
                    include_screenshot=False,
                    extract_links=extract_links,
                    ai_step_llm=ai_step_llm,
                )
                results.append(ai_result)
            else:
                # For non-extract actions, update indices and collect for batch execution
                updated_action = await self._update_action_indices(
                    history_item.state.interacted_element[i],
                    action,
                    state,
                )
                if updated_action is None:
                    raise ValueError(f'Could not find matching element {i} in current page')
                pending_actions.append(updated_action)

        # Execute any remaining pending actions
        if pending_actions:
            batch_results = await self.orchestrator.multi_act(pending_actions)
            results.extend(batch_results)

        return results

    async def _update_action_indices(
        self,
        historical_element: DOMInteractedElement | None,
        action: 'CommandModel',
        browser_state_summary: BrowserStateSummary,
    ) -> 'CommandModel | None':
        """Update action indices based on current page state."""
        if not historical_element or not browser_state_summary.dom_state.selector_map:
            return action

        highlight_index, current_element = next(
            (
                (highlight_index, element)
                for highlight_index, element in browser_state_summary.dom_state.selector_map.items()
                if element.element_hash == historical_element.element_hash
            ),
            (None, None),
        )

        if not current_element or highlight_index is None:
            return None

        old_index = action.get_index()
        if old_index != highlight_index:
            action.set_index(highlight_index)
            self.logger.info(f'Element moved in DOM, updated index from {old_index} to {highlight_index}')

        return action

    async def _execute_ai_step(
        self,
        query: str,
        include_screenshot: bool = False,
        extract_links: bool = False,
        ai_step_llm: BaseChatModel | None = None,
    ) -> ExecutionResult:
        """Execute an AI step during rerun to re-evaluate extract actions."""
        from core.orchestrator.prompts import get_ai_step_system_prompt, get_ai_step_user_prompt, get_rerun_summary_message
        from core.ai_models.messages import SystemMessage, UserMessage
        from core.helpers import sanitize_surrogates

        # Use provided LLM or agent's LLM
        llm = ai_step_llm or self.orchestrator.llm
        self.logger.debug(f'Using LLM for AI step: {llm.model}')

        # Extract clean markdown
        try:
            from core.dom_processing.markdown_extractor import extract_clean_markdown

            content, content_stats = await extract_clean_markdown(
                browser_session=self.orchestrator.browser_session, extract_links=extract_links
            )
        except Exception as e:
            return ExecutionResult(error=f'Could not extract clean markdown: {type(e).__name__}: {e}')

        # Get screenshot if requested
        screenshot_b64 = None
        if include_screenshot:
            try:
                screenshot = await self.orchestrator.browser_session.take_screenshot(full_page=False)
                if screenshot:
                    import base64

                    screenshot_b64 = base64.b64encode(screenshot).decode('utf-8')
            except Exception as e:
                self.logger.warning(f'Failed to capture screenshot for ai_step: {e}')

        # Build prompt with content stats
        original_html_length = content_stats['original_html_chars']
        initial_markdown_length = content_stats['initial_markdown_chars']
        final_filtered_length = content_stats['final_filtered_chars']
        chars_filtered = content_stats['filtered_chars_removed']

        stats_summary = f"""Content processed: {original_html_length:,} HTML chars → {initial_markdown_length:,} initial markdown → {final_filtered_length:,} filtered markdown"""
        if chars_filtered > 0:
            stats_summary += f' (filtered {chars_filtered:,} chars of noise)'

        # Sanitize content
        content = sanitize_surrogates(content)
        query = sanitize_surrogates(query)

        # Get prompts from prompts.py
        system_prompt = get_ai_step_system_prompt()
        prompt_text = get_ai_step_user_prompt(query, stats_summary, content)

        # Build user message with optional screenshot
        if screenshot_b64:
            user_message = get_rerun_summary_message(prompt_text, screenshot_b64)
        else:
            user_message = UserMessage(content=prompt_text)

        try:
            response = await asyncio.wait_for(
                llm.ainvoke([SystemMessage(content=system_prompt), user_message]), timeout=120.0
            )

            current_url = await self.orchestrator.browser_session.get_current_page_url()
            extracted_content = (
                f'<url>\n{current_url}\n</url>\n<query>\n{query}\n</query>\n<result>\n{response.completion}\n</result>'
            )

            # Simple memory handling
            MAX_MEMORY_LENGTH = 1000
            if len(extracted_content) < MAX_MEMORY_LENGTH:
                memory = extracted_content
                include_extracted_content_only_once = False
            else:
                file_name = await self.orchestrator.file_system.save_extracted_content(extracted_content)
                memory = f'Query: {query}\nContent in {file_name} and once in <read_state>.'
                include_extracted_content_only_once = True

            self.logger.info(f'🤖 AI Step: {memory}')
            return ExecutionResult(
                extracted_content=extracted_content,
                include_extracted_content_only_once=include_extracted_content_only_once,
                long_term_memory=memory,
            )
        except Exception as e:
            self.logger.warning(f'Failed to execute AI step: {e.__class__.__name__}: {e}')
            self.logger.debug('Full error traceback:', exc_info=True)
            return ExecutionResult(error=f'AI step failed: {e}')

    async def _generate_rerun_summary(
        self, original_task: str, results: list[ExecutionResult], summary_llm: BaseChatModel | None = None
    ) -> ExecutionResult:
        """Generate AI summary of rerun completion using screenshot and last step info."""
        # Get current screenshot
        screenshot_b64 = None
        try:
            screenshot = await self.orchestrator.browser_session.take_screenshot(full_page=False)
            if screenshot:
                import base64

                screenshot_b64 = base64.b64encode(screenshot).decode('utf-8')
        except Exception as e:
            self.logger.warning(f'Failed to capture screenshot for rerun summary: {e}')

        # Build summary prompt and message
        error_count = sum(1 for r in results if r.error)
        success_count = len(results) - error_count

        from core.orchestrator.prompts import get_rerun_summary_message, get_rerun_summary_prompt

        prompt = get_rerun_summary_prompt(
            original_task=original_task,
            total_steps=len(results),
            success_count=success_count,
            error_count=error_count,
        )

        # Use provided LLM, agent's LLM, or fall back to OpenAI with structured output
        try:
            # Determine which LLM to use
            if summary_llm is None:
                summary_llm = self.orchestrator.llm
                self.logger.debug('Using agent LLM for rerun summary')
            else:
                self.logger.debug(f'Using provided LLM for rerun summary: {summary_llm.model}')

            # Build message with prompt and optional screenshot
            from core.ai_models.messages import BaseMessage

            message = get_rerun_summary_message(prompt, screenshot_b64)
            messages: list[BaseMessage] = [message]  # type: ignore[list-item]

            # Try calling with structured output first
            self.logger.debug(f'Calling LLM for rerun summary with {len(messages)} message(s)')
            try:
                kwargs: dict = {'output_format': RerunSummaryAction}
                response = await summary_llm.ainvoke(messages, **kwargs)
                summary: RerunSummaryAction = response.completion  # type: ignore[assignment]
                self.logger.debug(f'LLM response type: {type(summary)}')
                self.logger.debug(f'LLM response: {summary}')
            except Exception as structured_error:
                # Fall back to text response without parsing
                self.logger.debug(f'Structured output failed: {structured_error}, falling back to text response')

                response = await summary_llm.ainvoke(messages, None)
                response_text = response.completion
                self.logger.debug(f'LLM text response: {response_text}')

                # Use the text response directly as the summary
                summary = RerunSummaryAction(
                    summary=response_text if isinstance(response_text, str) else str(response_text),
                    success=error_count == 0,
                    completion_status='complete' if error_count == 0 else ('partial' if success_count > 0 else 'failed'),
                )

            self.logger.info(f'📊 Rerun Summary: {summary.summary}')
            self.logger.info(f'📊 Status: {summary.completion_status} (success={summary.success})')

            return ExecutionResult(
                is_done=True,
                success=summary.success,
                extracted_content=summary.summary,
                long_term_memory=f'Rerun completed with status: {summary.completion_status}. {summary.summary[:100]}',
            )

        except Exception as e:
            self.logger.warning(f'Failed to generate AI summary: {e.__class__.__name__}: {e}')
            self.logger.debug('Full error traceback:', exc_info=True)
            # Fallback to simple summary
            return ExecutionResult(
                is_done=True,
                success=error_count == 0,
                extracted_content=f'Rerun completed: {success_count}/{len(results)} steps succeeded',
                long_term_memory=f'Rerun completed: {success_count} steps succeeded, {error_count} errors',
            )

    async def _execute_initial_actions(self) -> None:
        """Execute initial actions if provided."""
        import time
        from core.orchestrator.models import ExecutionHistory, StepMetadata

        # Execute initial actions if provided
        if self.orchestrator.initial_actions and not self.orchestrator.state.follow_up_task:
            self.logger.debug(f'⚡ Executing {len(self.orchestrator.initial_actions)} initial actions...')
            result = await self.orchestrator.multi_act(self.orchestrator.initial_actions)
            # update result 1 to mention that its was automatically loaded
            if result and self.orchestrator.initial_url and result[0].long_term_memory:
                result[0].long_term_memory = f'Found initial url and automatically loaded it. {result[0].long_term_memory}'
            self.orchestrator.state.last_result = result

            # Save initial actions to history as step 0 for rerun capability
            if self.orchestrator.settings.flash_mode:
                model_output = self.orchestrator.StepDecision(
                    evaluation_previous_goal=None,
                    memory='Initial navigation',
                    next_goal=None,
                    action=self.orchestrator.initial_actions,
                )
            else:
                model_output = self.orchestrator.StepDecision(
                    evaluation_previous_goal='Start',
                    memory=None,
                    next_goal='Initial navigation',
                    action=self.orchestrator.initial_actions,
                )

            metadata = StepMetadata(step_number=0, step_start_time=time.time(), step_end_time=time.time(), step_interval=None)

            # Create minimal browser state history for initial actions
            state_history = BrowserStateHistory(
                url=self.orchestrator.initial_url or '',
                title='Initial Actions',
                tabs=[],
                interacted_element=[None] * len(self.orchestrator.initial_actions),
                screenshot_path=None,
            )

            history_item = ExecutionHistory(
                model_output=model_output,
                result=result,
                state=state_history,
                metadata=metadata,
            )

            self.orchestrator.history.add_item(history_item)
            self.logger.debug('📝 Saved initial actions to history as step 0')
            self.logger.debug('Initial actions completed')

    async def load_and_rerun(
        self,
        history_file: str | Path | None = None,
        variables: dict[str, str] | None = None,
        **kwargs,
    ) -> list[ExecutionResult]:
        """Load history from file and rerun it, optionally substituting variables."""
        if not history_file:
            history_file = 'ExecutionHistory.json'
        history = ExecutionHistoryList.load_from_file(history_file, self.orchestrator.StepDecision)

        # Substitute variables if provided
        if variables:
            history = self.orchestrator._substitute_variables_in_history(history, variables)

        return await self.rerun_history(history, **kwargs)

