"""Менеджер для оркестрации шагов и управления историей агента."""

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.orchestrator.message_manager.models import save_conversation
from core.orchestrator.models import (
    AgentHistory,
    AgentHistoryList,
    AgentOutput,
    ActionResult,
    BrowserStateHistory,
    DetectedVariable,
    StepMetadata,
)
from core.session.models import BrowserStateSummary

if TYPE_CHECKING:
    from core.orchestrator.manager import Agent
    from core.orchestrator.models import AgentStepInfo


class StepManager:
    """Менеджер для оркестрации шагов и управления историей агента."""

    def __init__(self, agent: 'Agent'):
        self.agent = agent
    
    def _get_page_state_attr(self, page_state: 'BrowserStateSummary | dict', attr: str, default: Any = None) -> Any:
        """Безопасное получение атрибута из page_state (поддерживает и объект, и словарь)"""
        try:
            if isinstance(page_state, dict):
                return page_state.get(attr, default)
            else:
                return getattr(page_state, attr, default) if page_state else default
        except AttributeError as e:
            # Логируем ошибку для отладки
            import traceback
            self.agent.logger.error(f'Ошибка доступа к атрибуту {attr} в page_state (тип: {type(page_state)}): {e}')
            self.agent.logger.error(f'Traceback: {traceback.format_exc()}')
            return default

    # ========== ОРКЕСТРАЦИЯ ШАГОВ ==========

    async def build_step_context(self, step_info: 'AgentStepInfo | None' = None) -> 'BrowserStateSummary':
        """Собирает контекст для шага: состояние браузера, модели действий, действия страницы"""
        assert self.agent.browser_session is not None, 'BrowserSession is not set up'

        # Получение и логирование состояния страницы
        page_state = await self.fetch_and_log_page_state()
        
        # Анализ и логирование элементов страницы
        await self.analyze_page_elements(page_state)
        
        # Подготовка действий и сообщений для LLM
        await self.prepare_actions_and_messages(page_state, step_info)
        
        return page_state

    async def fetch_and_log_page_state(self) -> 'BrowserStateSummary':
        """Получает состояние страницы и логирует базовую информацию"""
        self.agent.logger.debug(f'🌐 Шаг {self.agent.state.n_steps}: Собираю состояние браузера...')
        page_state = await self.agent.browser_session.get_browser_state_summary(
            include_screenshot=True,  # всегда делаем скриншот для всех шагов
            include_recent_events=self.agent.include_recent_events,
        )
        
        # Логирование базовой информации о странице
        self.log_page_basic_info(page_state)
        
        # Проверка новых загрузок после получения состояния браузера
        await self.agent._check_and_update_downloads(f'Шаг {self.agent.state.n_steps}: после получения состояния браузера')
        
        return page_state

    def log_page_basic_info(self, page_state: 'BrowserStateSummary') -> None:
        """Логирует базовую информацию о странице"""
        url = self._get_page_state_attr(page_state, 'url', '')
        title = self._get_page_state_attr(page_state, 'title', '')
        
        self.agent.logger.info(f'🌐 URL: {url}')
        self.agent.logger.info(f'📄 Заголовок: {title}')
        
        # Логирование информации о скриншоте
        screenshot = self._get_page_state_attr(page_state, 'screenshot')
        if screenshot:
            self.agent.logger.debug(f'📸 Скриншот получен, размер: {len(screenshot)} байт')
        else:
            self.agent.logger.debug('📸 Скриншот не получен')

    async def analyze_page_elements(self, page_state: 'BrowserStateSummary') -> None:
        """Анализирует элементы страницы и логирует информацию о них"""
        dom_state = self._get_page_state_attr(page_state, 'dom_state')
        selector_map = self._get_page_state_attr(dom_state, 'selector_map', {}) if dom_state else {}
        if not selector_map:
            return
        self.agent.logger.info(f'👁️ Агент видит {len(selector_map)} интерактивных элементов на странице')
        
        # Логирование превью элементов
        self.log_elements_preview(selector_map)
        
        # Специальная обработка для почтовых клиентов
        await self.handle_email_client_context(page_state)

    def log_elements_preview(self, selector_map: dict) -> None:
        """Логирует превью первых элементов страницы"""
        elements_preview = []
        for idx, (element_index, element) in enumerate(list(selector_map.items())[:10]):
            element_text = self.extract_element_text(element)
            element_role = self.extract_element_role(element)
            
            # Обрезаем текст до 50 символов
            text_preview = element_text[:50] + '...' if len(element_text) > 50 else element_text
            elements_preview.append(f'  [{element_index}] {element_role}: {text_preview}')
        
        if elements_preview:
            self.agent.logger.info(f'👁️ Первые элементы которые видит агент:\n' + '\n'.join(elements_preview))

    def extract_element_text(self, element) -> str:
        """Извлекает текст из элемента различными способами"""
        if hasattr(element, 'ax_node') and element.ax_node and element.ax_node.name:
            return element.ax_node.name
        elif hasattr(element, 'get_all_children_text'):
            return element.get_all_children_text()
        elif hasattr(element, 'get_meaningful_text_for_llm'):
            return element.get_meaningful_text_for_llm()
        elif hasattr(element, 'node_value'):
            return element.node_value or ''
        return ''

    def extract_element_role(self, element) -> str:
        """Извлекает роль элемента"""
        if hasattr(element, 'ax_node') and element.ax_node and element.ax_node.role:
            return element.ax_node.role
        elif hasattr(element, 'tag_name'):
            return element.tag_name or ''
        return ''

    async def handle_email_client_context(self, page_state: 'BrowserStateSummary') -> None:
        """Обрабатывает специальный контекст для почтовых клиентов"""
        if not self.agent.email_subagent.is_email_client(page_state):
            return
        
        # Извлекаем метаданные письма для логирования
        email_metadata = self.agent.email_subagent.extract_email_metadata(page_state)
        if email_metadata['is_opened']:
            self.log_email_metadata(email_metadata)
        
        # Проверяем наличие диалогов через субагента
        if self.agent.email_subagent.detect_dialog(page_state):
            self.agent.logger.warning('⚠️ Обнаружен открытый диалог на странице почтового клиента - он будет автоматически закрыт при следующем действии')

    def log_email_metadata(self, email_metadata: dict) -> None:
        """Логирует метаданные письма"""
        self.agent.logger.info('📧 Открыто письмо в почтовом клиенте')
        if email_metadata.get('subject'):
            self.agent.logger.info(f'   Тема: {email_metadata["subject"]}')
        if email_metadata.get('sender'):
            self.agent.logger.info(f'   Отправитель: {email_metadata["sender"]}')
        if email_metadata.get('body_preview'):
            body_preview = email_metadata['body_preview'][:200] + '...' if len(email_metadata['body_preview']) > 200 else email_metadata['body_preview']
            self.agent.logger.info(f'   Содержание (первые 200 символов): {body_preview}')

    async def prepare_actions_and_messages(self, page_state: 'BrowserStateSummary', step_info: 'AgentStepInfo | None') -> None:
        """Подготавливает действия и сообщения для LLM"""
        # Обновление моделей действий для текущей страницы
        page_url = self._get_page_state_attr(page_state, 'url', '')
        await self.update_page_action_models(page_url)

        # Получение отфильтрованных действий для текущей страницы
        page_filtered_actions = self.agent.tools.registry.get_prompt_description(page_url)

        # Логирование контекста шага
        self.agent._log_step_context(page_state)
        
        # Проверка на остановку после логирования
        await self.agent._check_stop_or_pause()

        # Проверка условий принудительного завершения
        await self.check_forced_completion(step_info)

        # Создание сообщений состояния с собранным контекстом
        await self.create_state_messages(page_state, step_info, page_filtered_actions)

    async def update_page_action_models(self, page_url: str) -> None:
        """Обновляет модели действий для текущей страницы"""
        self.agent.logger.debug(f'📝 Шаг {self.agent.state.n_steps}: Обновление моделей действий для страницы...')
        await self.agent._update_action_models_for_page(page_url)

    async def check_forced_completion(self, step_info: 'AgentStepInfo | None') -> None:
        """Проверяет условия принудительного завершения"""
        await self.agent._force_done_after_last_step(step_info)
        await self.agent._force_done_after_failure()

    async def create_state_messages(self, page_state: 'BrowserStateSummary', step_info: 'AgentStepInfo | None', page_filtered_actions: str | None) -> None:
        """Создает сообщения состояния для LLM"""
        self.agent.logger.debug(f'💬 Шаг {self.agent.state.n_steps}: Формирование сообщений состояния...')
        unavailable_skills_info = None

        # Получение последнего ответа LLM и результатов действий
        agent_decision = self.agent.state.last_model_output
        previous_action_results = self.agent.state.last_result

        self.agent._message_manager.create_state_messages(
            browser_state_summary=page_state,
            model_output=agent_decision,
            result=previous_action_results,
            step_info=step_info,
            use_vision=self.agent.settings.use_vision,
            page_filtered_actions=page_filtered_actions,
            sensitive_data=self.agent.sensitive_data,
            available_file_paths=self.agent.available_file_paths,
            unavailable_skills_info=unavailable_skills_info,
            email_subagent=self.agent.email_subagent,
        )

    async def apply_agent_actions(self) -> None:
        """Применяет действия из вывода модели"""
        # Проверка наличия вывода модели
        if not self.has_model_output():
            raise ValueError('No model output to execute actions from')

        # Получение действий из вывода модели
        actions = self.extract_actions_from_output()
        
        # Выполнение действий
        action_results = await self.agent.multi_act(actions)
        
        # Сохранение результата выполнения действий
        self.agent.state.last_result = action_results

    def has_model_output(self) -> bool:
        """Проверяет наличие вывода модели"""
        return self.agent.state.last_model_output is not None

    def extract_actions_from_output(self) -> list:
        """Извлекает действия из вывода модели"""
        return self.agent.state.last_model_output.action

    async def finalize_step_processing(self) -> None:
        """Завершает обработку шага: отслеживание загрузок и логирование результатов"""
        assert self.agent.browser_session is not None, 'BrowserSession is not set up'

        # Получение результатов выполнения действий
        action_results = self.agent.state.last_result

        # Проверка ошибок действий (делаем это раньше проверки загрузок)
        if action_results and len(action_results) == 1 and action_results[-1].error:
            self.agent.state.consecutive_failures += 1
            self.agent.logger.debug(f'🔄 Шаг {self.agent.state.n_steps}: Последовательные неудачи: {self.agent.state.consecutive_failures}')
            # Проверка загрузок даже при ошибке
            await self.agent._check_and_update_downloads('после выполнения действий')
            return

        # Сброс счетчика неудач при успехе
        if self.agent.state.consecutive_failures > 0:
            self.agent.state.consecutive_failures = 0
            self.agent.logger.debug(f'🔄 Шаг {self.agent.state.n_steps}: Последовательные неудачи сброшены до: {self.agent.state.consecutive_failures}')

        # Проверка новых загрузок после выполнения действий
        await self.agent._check_and_update_downloads('после выполнения действий')

        # Логирование результатов завершения
        if action_results and len(action_results) > 0 and action_results[-1].is_done:
            final_result = action_results[-1]
            success = final_result.success
            if success:
                # Green color for success
                self.agent.logger.info(f'\n📄 \033[32m Финальный результат:\033[0m \n{final_result.extracted_content}\n\n')
            else:
                # Red color for failure
                self.agent.logger.info(f'\n📄 \033[31m Финальный результат:\033[0m \n{final_result.extracted_content}\n\n')
            if final_result.attachments:
                total_attachments = len(final_result.attachments)
                for i, file_path in enumerate(final_result.attachments):
                    self.agent.logger.info(f'👉 Attachment {i + 1 if total_attachments > 1 else ""}: {file_path}')

    # ========== УПРАВЛЕНИЕ ИСТОРИЕЙ ==========

    async def make_history_item(
        self,
        agent_decision: AgentOutput | None,
        page_state: BrowserStateSummary,
        action_results: list[ActionResult],
        metadata: StepMetadata | None = None,
        state_message: str | None = None,
    ) -> None:
        """Создает и сохраняет элемент истории"""

        if agent_decision:
            dom_state = self._get_page_state_attr(page_state, 'dom_state')
            selector_map = self._get_page_state_attr(dom_state, 'selector_map', {}) if dom_state else {}
            interacted_elements = AgentHistory.get_interacted_element(agent_decision, selector_map)
        else:
            interacted_elements = [None]

        # Сохранение скриншота и получение пути
        screenshot_path = None
        screenshot = self._get_page_state_attr(page_state, 'screenshot')
        if screenshot:
            self.agent.logger.debug(
                f'📸 Storing screenshot for step {self.agent.state.n_steps}, screenshot length: {len(screenshot)}'
            )
            screenshot_path = await self.agent.screenshot_service.store_screenshot(screenshot, self.agent.state.n_steps)
            self.agent.logger.debug(f'📸 Screenshot stored at: {screenshot_path}')
        else:
            self.agent.logger.debug(f'📸 No screenshot in page_state for step {self.agent.state.n_steps}')

        state_history = BrowserStateHistory(
            url=self._get_page_state_attr(page_state, 'url', ''),
            title=self._get_page_state_attr(page_state, 'title', ''),
            tabs=self._get_page_state_attr(page_state, 'tabs', []),
            interacted_element=interacted_elements,
            screenshot_path=screenshot_path,
        )

        history_item = AgentHistory(
            model_output=agent_decision,
            result=action_results,
            state=state_history,
            metadata=metadata,
            state_message=state_message,
        )

        self.agent.history.add_item(history_item)

    async def handle_post_llm_processing(
        self,
        page_state: BrowserStateSummary,
        context_messages: list,
    ) -> None:
        """Обработка колбэков и сохранение разговора после взаимодействия с LLM"""
        import inspect
        
        agent_decision = self.agent.state.last_model_output
        if self.agent.register_new_step_callback and agent_decision:
            if inspect.iscoroutinefunction(self.agent.register_new_step_callback):
                await self.agent.register_new_step_callback(
                    page_state,
                    agent_decision,
                    self.agent.state.n_steps,
                )
            else:
                self.agent.register_new_step_callback(
                    page_state,
                    agent_decision,
                    self.agent.state.n_steps,
                )

        if self.agent.settings.save_conversation_path and agent_decision:
            # Обработка save_conversation_path как директории (согласованно с другими путями записи)
            conversation_dir = Path(self.agent.settings.save_conversation_path)
            conversation_filename = f'conversation_{self.agent.id}_{self.agent.state.n_steps}.txt'
            target = conversation_dir / conversation_filename
            await save_conversation(
                context_messages,
                agent_decision,
                target,
                self.agent.settings.save_conversation_path_encoding,
            )

    def detect_variables(self) -> dict[str, DetectedVariable]:
        """Detect reusable variables in agent history"""
        from core.orchestrator.models import detect_variables_in_history

        return detect_variables_in_history(self.agent.history)

    def save_history(self, file_path: str | Path | None = None) -> None:
        """Save the history to a file with sensitive data filtering"""
        if not file_path:
            file_path = 'AgentHistory.json'
        self.agent.history.save_to_file(file_path, sensitive_data=self.agent.sensitive_data)

    async def finalize(
        self,
        page_state: BrowserStateSummary | None,
        step_start_time: float,
    ) -> None:
        """Завершает шаг с историей, логированием и событиями"""
        step_end_time = time.time()
        action_results = self.agent.state.last_result
        if not action_results:
            return

        if page_state:
            step_interval = None
            if len(self.agent.history.history) > 0:
                last_history_item = self.agent.history.history[-1]

                if last_history_item.metadata:
                    previous_end_time = last_history_item.metadata.step_end_time
                    previous_start_time = last_history_item.metadata.step_start_time
                    step_interval = max(0, previous_end_time - previous_start_time)
            metadata = StepMetadata(
                step_number=self.agent.state.n_steps,
                step_start_time=step_start_time,
                step_end_time=step_end_time,
                step_interval=step_interval,
            )

            # Получение последнего ответа LLM
            agent_decision = self.agent.state.last_model_output

            # Использование _make_history_item как в main ветке
            await self.make_history_item(
                agent_decision,
                page_state,
                action_results,
                metadata,
                state_message=self.agent._message_manager.last_state_message_text,
            )

        # Логирование сводки завершения шага
        summary_message = self.agent._run_manager.log_step_completion_summary(step_start_time, action_results)
        if summary_message:
            await self.agent._run_manager.demo_mode_log(summary_message, 'info', {'step': self.agent.state.n_steps})

        # Сохранение состояния файловой системы после завершения шага
        self.agent.save_file_system_state()

        # Эмиссия событий создания и выполнения шага
        agent_decision = self.agent.state.last_model_output
        if page_state and agent_decision:
            # Извлечение ключевых данных шага для события
            actions_data = []
            if agent_decision.action:
                for action in agent_decision.action:
                    action_dict = action.model_dump() if hasattr(action, 'model_dump') else {}
                    actions_data.append(action_dict)


        # Увеличение счетчика шагов после полного завершения шага
        self.agent.state.n_steps += 1

    def _make_history_item_with_error(self, error: str):
        """Create a history item with an error."""
        return AgentHistory(
            model_output=None,
            result=[ActionResult(error=error, include_in_memory=True)],
            state=BrowserStateHistory(
                url='',
                title='',
                tabs=[],
                interacted_element=[],
                screenshot_path=None,
            ),
            metadata=None,
        )

