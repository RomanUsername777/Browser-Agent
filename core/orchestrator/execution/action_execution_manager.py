"""Менеджер для выполнения действий агента с security layer и обработкой специальных случаев."""

import asyncio
import time
from typing import TYPE_CHECKING

from core.actions.registry.models import ActionModel
from core.orchestrator.models import ActionResult

if TYPE_CHECKING:
    from core.orchestrator.manager import Agent


class ActionExecutionManager:
    """Менеджер для выполнения действий агента с security layer."""

    def __init__(self, agent: 'Agent'):
        self.agent = agent
        self.logger = agent.logger

    async def multi_act(self, actions: list[ActionModel]) -> list[ActionResult]:
        """Execute multiple actions with security layer and special case handling."""
        results: list[ActionResult] = []
        total_actions = len(actions)

        assert self.agent.browser_session is not None, 'BrowserSession is not set up'
        try:
            cached_state = self.agent.browser_session._cached_browser_state_summary
            if cached_state is not None:
                if isinstance(cached_state, dict):
                    dom_state = cached_state.get('dom_state', {})
                    selector_map = dom_state.get('selector_map', {}) if isinstance(dom_state, dict) else {}
                else:
                    dom_state = cached_state.dom_state if hasattr(cached_state, 'dom_state') else None
                    selector_map = dom_state.selector_map if dom_state and hasattr(dom_state, 'selector_map') else {}
                
                if selector_map:
                    cached_selector_map = dict(selector_map)
                    cached_element_hashes = {e.parent_branch_hash() for e in cached_selector_map.values()}
                else:
                    cached_selector_map = {}
                    cached_element_hashes = set()
            else:
                cached_selector_map = {}
                cached_element_hashes = set()
        except Exception as e:
            self.logger.error(f'Error getting cached selector map: {e}')
            cached_selector_map = {}
            cached_element_hashes = set()

        for i, action in enumerate(actions):
            if i > 0:
                # ONLY ALLOW TO CALL `done` IF IT IS A SINGLE ACTION
                if action.model_dump(exclude_unset=True).get('done') is not None:
                    msg = f'Done action is allowed only as a single action - stopped after action {i} / {total_actions}.'
                    self.logger.debug(msg)
                    break

            # wait between actions (only after first action)
            if i > 0:
                self.logger.debug(f'Waiting {self.agent.browser_profile.wait_between_actions} seconds between actions')
                await asyncio.sleep(self.agent.browser_profile.wait_between_actions)

            try:
                await self.agent._check_stop_or_pause()
                # Get action name from the action model
                action_data = action.model_dump(exclude_unset=True)
                action_name = next(iter(action_data.keys())) if action_data else 'unknown'

                # Security layer: проверка на капчу, форму входа и деструктивные действия
                action, action_name, action_data = await self._apply_security_layer(action, action_name, action_data)

                # Email subagent handling
                await self._handle_email_subagent_context(action_name, action_data)

                # Log action before execution
                await self._log_action(action, action_name, i + 1, total_actions)

                time_start = time.time()

                result = await self.agent.tools.act(
                    action=action,
                    browser_session=self.agent.browser_session,
                    file_system=self.agent.file_system,
                    page_extraction_llm=self.agent.settings.page_extraction_llm,
                    sensitive_data=self.agent.sensitive_data,
                    available_file_paths=self.agent.available_file_paths,
                )

                time_end = time.time()
                time_elapsed = time_end - time_start

                # Post-action handling (DOM updates, modal tracking)
                await self._handle_post_action(action_name, result)

                if result.error:
                    await self.agent._demo_mode_log(
                        f'Action "{action_name}" failed: {result.error}',
                        'error',
                        {'action': action_name, 'step': self.agent.state.n_steps},
                    )
                elif result.is_done:
                    completion_text = result.long_term_memory or result.extracted_content or 'Task marked as done.'
                    level = 'success' if result.success is not False else 'warning'
                    await self.agent._demo_mode_log(
                        completion_text,
                        level,
                        {'action': action_name, 'step': self.agent.state.n_steps},
                    )

                results.append(result)

                if results[-1].is_done or results[-1].error or i == total_actions - 1:
                    break

            except Exception as e:
                # Handle any exceptions during action execution
                self.logger.error(f'❌ Executing action {i + 1} failed -> {type(e).__name__}: {e}')
                await self.agent._demo_mode_log(
                    f'Action "{action_name}" raised {type(e).__name__}: {e}',
                    'error',
                    {'action': action_name, 'step': self.agent.state.n_steps},
                )
                raise e

        return results

    async def _apply_security_layer(
        self, action: ActionModel, action_name: str, action_data: dict
    ) -> tuple[ActionModel, str, dict]:
        """Apply security layer checks: captcha, login forms, destructive actions."""
        # Проверка на капчу и форму входа перед выполнением действий
        if action_name in ['click', 'navigate', 'input'] and self.agent.browser_session is not None:
            browser_state = self.agent.browser_session._cached_browser_state_summary
            if browser_state:
                url = browser_state['url'] if isinstance(browser_state, dict) else (browser_state.url if browser_state else '')
                title = browser_state['title'] if isinstance(browser_state, dict) else (browser_state.title if browser_state else '')
                url_lower = url.lower()
                title_lower = title.lower()
                
                # Проверяем URL и заголовок на наличие капчи
                is_captcha_page = (
                    'captcha' in url_lower or 'showcaptcha' in url_lower or
                    'робот' in title_lower or 'robot' in title_lower
                )
                
                # Проверяем наличие формы входа
                is_login_form, has_password_field, has_login_field, has_submit_button = self._detect_login_form(browser_state)
                
                # Проверяем текст элементов на наличие капчи и деструктивных действий
                is_captcha_element, is_destructive_action, destructive_action_type = self._check_action_security(
                    action_name, action_data, browser_state
                )
                
                # Если обнаружена форма входа, блокируем действия
                if is_login_form:
                    action, action_name, action_data = await self._handle_login_form(action, action_name, action_data, browser_state)
                
                # Если обнаружена капча, блокируем действия
                elif is_captcha_page or is_captcha_element:
                    action, action_name, action_data = self._handle_captcha(action, action_name, action_data)
                
                # Security layer: проверка на деструктивные действия
                elif is_destructive_action:
                    action, action_name, action_data = self._handle_destructive_action(action, action_name, action_data, destructive_action_type)
                
                # Если достигнут лимит неудачных попыток клика в модальном окне
                elif self.agent.state.modal_click_failures >= 3 and action_name == 'click':
                    if browser_state and self.agent.email_subagent.detect_dialog(browser_state):
                        action, action_name, action_data = self._handle_modal_failure(action, action_name, action_data)

        return action, action_name, action_data

    def _detect_login_form(self, browser_state) -> tuple[bool, bool, bool, bool]:
        """Detect if current page has a login form."""
        is_login_form = False
        has_password_field = False
        has_login_field = False
        has_submit_button = False
        
        if browser_state.dom_state and browser_state.dom_state.selector_map:
            selector_map = browser_state.dom_state.selector_map
            for element in selector_map.values():
                element_type = getattr(element, 'type', '') or ''
                element_role = getattr(element, 'role', '') or ''
                element_text = getattr(element, 'text', '') or ''
                element_placeholder = getattr(element, 'placeholder', '') or ''
                
                text_lower = element_text.lower()
                placeholder_lower = element_placeholder.lower()
                type_lower = element_type.lower()
                role_lower = element_role.lower()
                
                # Проверяем на поле пароля
                if (
                    type_lower == 'password' or
                    'password' in text_lower or
                    'password' in placeholder_lower or
                    'пароль' in text_lower or
                    'пароль' in placeholder_lower
                ):
                    has_password_field = True
                
                # Проверяем на поле логина/email (но не пароль!)
                if type_lower != 'password':
                    if (
                        (type_lower in ['email', 'text'] and role_lower == 'textbox') or
                        'login' in text_lower or 'логин' in text_lower or
                        'email' in text_lower or 'почта' in text_lower or
                        'username' in text_lower or 'имя пользователя' in text_lower or
                        'login' in placeholder_lower or 'логин' in placeholder_lower or
                        'email' in placeholder_lower or 'почта' in placeholder_lower or
                        'телефон' in text_lower or 'phone' in text_lower
                    ):
                        has_login_field = True
                
                # Проверяем на кнопку отправки формы
                if (
                    role_lower == 'button' and (
                        'войти' in text_lower or 'login' in text_lower or
                        'войти' in placeholder_lower or 'login' in placeholder_lower or
                        'отправить' in text_lower or 'submit' in text_lower or
                        'вход' in text_lower or 'sign in' in text_lower
                    )
                ):
                    has_submit_button = True
        
        # Форма входа определяется наличием поля пароля И (поля логина ИЛИ кнопки отправки)
        is_login_form = has_password_field and (has_login_field or has_submit_button)
        return is_login_form, has_password_field, has_login_field, has_submit_button

    def _check_action_security(
        self, action_name: str, action_data: dict, browser_state
    ) -> tuple[bool, bool, str | None]:
        """Check action for captcha elements and destructive actions."""
        is_captcha_element = False
        is_destructive_action = False
        destructive_action_type = None
        
        if action_name == 'click' and 'index' in action_data.get('click', {}):
            click_params = action_data.get('click', {})
            index = click_params.get('index')
            if index is not None and browser_state.dom_state:
                selector_map = browser_state.dom_state.selector_map
                clicked_element = selector_map.get(index) if index in selector_map else None
                if clicked_element:
                    # Получаем текст элемента разными способами
                    element_text = ''
                    if hasattr(clicked_element, 'ax_node') and clicked_element.ax_node and clicked_element.ax_node.name:
                        element_text = clicked_element.ax_node.name
                    elif hasattr(clicked_element, 'get_all_children_text'):
                        element_text = clicked_element.get_all_children_text() or ''
                    elif hasattr(clicked_element, 'get_meaningful_text_for_llm'):
                        element_text = clicked_element.get_meaningful_text_for_llm() or ''
                    elif hasattr(clicked_element, 'text'):
                        element_text = getattr(clicked_element, 'text', '') or ''
                    
                    text_lower = element_text.lower()
                    
                    # Проверка на капчу
                    is_captcha_element = (
                        'робот' in text_lower or 'robot' in text_lower or
                        'не робот' in text_lower or 'not a robot' in text_lower
                    )
                    
                    # Проверка на деструктивные действия
                    if not is_captcha_element and element_text:
                        payment_keywords = [
                            'оплат', 'pay now', 'checkout', 'place order', 'оформить заказ',
                            'подтвердить заказ', 'оплатить заказ', 'купить сейчас', 'buy now',
                            'подтвердить и оплатить', 'confirm and pay', 'proceed to payment'
                        ]
                        delete_keywords = [
                            'удалить письмо', 'delete email', 'удалить навсегда',
                            'delete permanently', 'удалить безвозвратно'
                        ]
                        
                        is_payment_action = any(kw in text_lower for kw in payment_keywords)
                        is_delete_action = any(kw in text_lower for kw in delete_keywords)
                        
                        if is_payment_action:
                            is_destructive_action = True
                            destructive_action_type = 'payment'
                        elif is_delete_action:
                            is_destructive_action = True
                            destructive_action_type = 'delete'
        
        return is_captcha_element, is_destructive_action, destructive_action_type

    async def _handle_login_form(
        self, action: ActionModel, action_name: str, action_data: dict, browser_state
    ) -> tuple[ActionModel, str, dict]:
        """Handle login form detection - replace action with wait_for_user_input."""
        # Проверяем историю агента, чтобы не запрашивать вход повторно
        already_waited_for_login = False
        if hasattr(self.agent, 'history') and self.agent.history and hasattr(self.agent.history, 'history') and self.agent.history.history:
            previous_url = None
            for history_item in reversed(self.agent.history.history[-5:]):
                if history_item.state:
                    previous_url = history_item.state['url'] if isinstance(history_item.state, dict) else (history_item.state.url if history_item.state else None)
                    if previous_url:
                        break
            
            for history_item in reversed(self.agent.history.history[-5:]):
                if history_item.model_output and history_item.model_output.action:
                    for act in history_item.model_output.action:
                        act_data = act.model_dump(exclude_unset=True)
                        if 'wait_for_user_input' in act_data or 'request_user_input' in act_data:
                            browser_url = browser_state['url'] if isinstance(browser_state, dict) else (browser_state.url if browser_state else '')
                            if previous_url and browser_url != previous_url:
                                already_waited_for_login = True
                                break
                    if already_waited_for_login:
                        break
        
        if not already_waited_for_login:
            self.logger.warning(
                f'⚠️ Обнаружена форма входа - блокирую действие {action_name} и запрашиваю wait_for_user_input'
            )
            from core.actions.models import WaitForUserInputAction
            from core.actions.registry.models import ActionModel
            from pydantic import create_model, Field
            
            WaitForUserInputActionModel = create_model(
                'WaitForUserInputActionModel',
                __base__=ActionModel,
                wait_for_user_input=(WaitForUserInputAction, Field(...))
            )
            
            login_action = WaitForUserInputActionModel(
                wait_for_user_input=WaitForUserInputAction(
                    message='Пожалуйста, заполните форму входа в браузере (логин, пароль и т.д.)'
                )
            )
            action = login_action
            action_name = 'wait_for_user_input'
            action_data = {'wait_for_user_input': {'message': 'Пожалуйста, заполните форму входа в браузере (логин, пароль и т.д.)'}}
        
        return action, action_name, action_data

    def _handle_captcha(self, action: ActionModel, action_name: str, action_data: dict) -> tuple[ActionModel, str, dict]:
        """Handle CAPTCHA detection - replace action with request_user_input."""
        self.logger.warning(
            f'⚠️ Блокирую действие {action_name} на странице с капчей - агент должен использовать request_user_input'
        )
        from core.actions.models import RequestUserInputAction
        from core.actions.registry.models import ActionModel
        from pydantic import create_model, Field
        
        RequestUserInputActionModel = create_model(
            'RequestUserInputActionModel',
            __base__=ActionModel,
            request_user_input=(RequestUserInputAction, Field(...))
        )
        
        captcha_action = RequestUserInputActionModel(
            request_user_input=RequestUserInputAction(
                prompt='Пожалуйста, решите капчу в браузере и введите "готово" (или "done") когда закончите'
            )
        )
        action = captcha_action
        action_name = 'request_user_input'
        action_data = {'request_user_input': {'prompt': 'Пожалуйста, решите капчу в браузере и введите "готово" (или "done") когда закончите'}}
        return action, action_name, action_data

    def _handle_destructive_action(
        self, action: ActionModel, action_name: str, action_data: dict, destructive_action_type: str
    ) -> tuple[ActionModel, str, dict]:
        """Handle destructive action detection - replace with request_user_input for confirmation."""
        action_description = 'оплату/подтверждение заказа' if destructive_action_type == 'payment' else 'удаление'
        self.logger.warning(
            f'🛡️ Security layer: блокирую деструктивное действие {action_name} ({action_description}) - запрашиваю подтверждение пользователя'
        )
        from core.actions.models import RequestUserInputAction
        from core.actions.registry.models import ActionModel
        from pydantic import create_model, Field
        
        RequestUserInputActionModel = create_model(
            'RequestUserInputActionModel',
            __base__=ActionModel,
            request_user_input=(RequestUserInputAction, Field(...))
        )
        
        if destructive_action_type == 'payment':
            prompt_text = 'Обнаружена кнопка оплаты/подтверждения заказа. Вы хотите оплатить/подтвердить заказ? Ответьте только "да"/"yes" для подтверждения или "нет"/"no" для отмены.'
        else:  # delete
            prompt_text = 'Обнаружена кнопка удаления. Вы хотите удалить этот элемент? Ответьте только "да"/"yes" для подтверждения или "нет"/"no" для отмены.'
        
        destructive_action = RequestUserInputActionModel(
            request_user_input=RequestUserInputAction(prompt=prompt_text)
        )
        action = destructive_action
        action_name = 'request_user_input'
        action_data = {'request_user_input': {'prompt': prompt_text}}
        return action, action_name, action_data

    def _handle_modal_failure(self, action: ActionModel, action_name: str, action_data: dict) -> tuple[ActionModel, str, dict]:
        """Handle modal click failure - replace with request_user_input."""
        self.logger.warning(
            f'🛑 Блокирую действие {action_name} - достигнут лимит неудачных попыток клика в модальном окне (3). '
            'Запрашиваю помощь пользователя.'
        )
        from core.actions.models import RequestUserInputAction
        from core.actions.registry.models import ActionModel
        from pydantic import create_model, Field
        
        RequestUserInputActionModel = create_model(
            'RequestUserInputActionModel',
            __base__=ActionModel,
            request_user_input=(RequestUserInputAction, Field(...))
        )
        
        modal_action = RequestUserInputActionModel(
            request_user_input=RequestUserInputAction(
                prompt='Не удалось найти кнопку отправки в модальном окне после 3 попыток. Пожалуйста, нажмите кнопку отправки формы в модальном окне вручную, затем введите "готово" (или "done") когда форма будет отправлена.'
            )
        )
        action = modal_action
        action_name = 'request_user_input'
        action_data = {'request_user_input': {'prompt': 'Не удалось найти кнопку отправки в модальном окне после 3 попыток. Пожалуйста, нажмите кнопку отправки формы в модальном окне вручную, затем введите "готово" (или "done") когда форма будет отправлена.'}}
        self.agent.state.modal_click_failures = 0
        return action, action_name, action_data

    async def _handle_email_subagent_context(self, action_name: str, action_data: dict) -> None:
        """Handle email subagent context logging."""
        if self.agent.browser_session is not None:
            browser_state = self.agent.browser_session._cached_browser_state_summary
            if browser_state:
                # Информируем агента о наличии диалога
                if self.agent.email_subagent.detect_dialog(browser_state):
                    self.logger.info('ℹ️ Обнаружен открытый диалог - агент должен решить: работать с ним или закрыть через Escape')
                
                # Логируем метаданные письма только для почтовых клиентов
                if self.agent.email_subagent.is_email_client(browser_state):
                    email_metadata = self.agent.email_subagent.extract_email_metadata(browser_state)
                    
                    if email_metadata['is_opened'] and action_name == 'click':
                        click_params = action_data.get('click', {})
                        index = click_params.get('index')
                        if index is not None and browser_state.dom_state:
                            selector_map = browser_state.dom_state.selector_map
                            clicked_element = selector_map.get(index)
                            if clicked_element:
                                self.logger.info(f'📧 Действие на странице почтового клиента:')
                                if email_metadata['subject']:
                                    self.logger.info(f'   Тема письма: {email_metadata["subject"]}')
                                if email_metadata['sender']:
                                    self.logger.info(f'   Отправитель: {email_metadata["sender"]}')
                                if email_metadata['body_preview']:
                                    body_preview = email_metadata['body_preview'][:300] + '...' if len(email_metadata['body_preview']) > 300 else email_metadata['body_preview']
                                    self.logger.info(f'   Содержание (первые 300 символов): {body_preview}')

    async def _log_action(self, action, action_name: str, action_num: int, total_actions: int) -> None:
        """Log the action before execution with colored formatting."""
        blue = '\033[34m'
        magenta = '\033[35m'
        reset = '\033[0m'

        if total_actions > 1:
            action_header = f'▶️  [{action_num}/{total_actions}] {blue}{action_name}{reset}:'
            plain_header = f'▶️  [{action_num}/{total_actions}] {action_name}:'
        else:
            action_header = f'▶️   {blue}{action_name}{reset}:'
            plain_header = f'▶️  {action_name}:'

        action_data = action.model_dump(exclude_unset=True)
        params = action_data.get(action_name, {})

        param_parts = []
        plain_param_parts = []

        if params and isinstance(params, dict):
            for param_name, value in params.items():
                if isinstance(value, str) and len(value) > 150:
                    display_value = value[:150] + '...'
                elif isinstance(value, list) and len(str(value)) > 200:
                    display_value = str(value)[:200] + '...'
                else:
                    display_value = value

                param_parts.append(f'{magenta}{param_name}{reset}: {display_value}')
                plain_param_parts.append(f'{param_name}: {display_value}')

        if param_parts:
            params_string = ', '.join(param_parts)
            self.logger.info(f'  {action_header} {params_string}')
        else:
            self.logger.info(f'  {action_header}')

        if self.agent._demo_mode_enabled:
            panel_message = plain_header
            if plain_param_parts:
                panel_message = f'{panel_message} {", ".join(plain_param_parts)}'
            await self.agent._demo_mode_log(panel_message.strip(), 'action', {'action': action_name, 'step': self.agent.state.n_steps})

    async def _handle_post_action(self, action_name: str, result: ActionResult) -> None:
        """Handle post-action processing: DOM updates, modal tracking."""
        # После действий, которые могут изменить DOM (особенно в SPA), ждем обновления страницы
        if action_name in ['click', 'navigate']:
            wait_time = 2.0
            self.logger.info(f'⏳ Ожидание {wait_time}s после {action_name} для обновления DOM (SPA)')
            await asyncio.sleep(wait_time)
            
            # Инвалидируем кэш DOM watchdog и selector_map
            if self.agent.browser_session and self.agent.browser_session._dom_watchdog:
                self.agent.browser_session._dom_watchdog.clear_cache()
                # Также очищаем кэшированную selector_map в BrowserSession
                self.agent.browser_session._cached_selector_map.clear()
                self.logger.info(f'🔄 Кэш DOM очищен после {action_name} - следующее получение browser_state будет свежим')
            
            # Проверяем, осталось ли модальное окно открытым после клика
            if action_name == 'click' and self.agent.browser_session:
                await asyncio.sleep(0.5)
                fresh_browser_state = self.agent.browser_session._cached_browser_state_summary
                if fresh_browser_state and self.agent.email_subagent.detect_dialog(fresh_browser_state):
                    self.agent.state.modal_click_failures += 1
                    self.logger.warning(
                        f'⚠️ Модальное окно все еще открыто после клика. Счетчик неудачных попыток: {self.agent.state.modal_click_failures}/3'
                    )
                    if self.agent.state.modal_click_failures >= 3:
                        self.logger.warning(
                            '🛑 Достигнут лимит неудачных попыток клика в модальном окне (3). '
                            'В следующем шаге будет запрошена помощь пользователя.'
                        )
                else:
                    if self.agent.state.modal_click_failures > 0:
                        self.logger.info(f'✅ Модальное окно закрыто. Счетчик неудачных попыток сброшен с {self.agent.state.modal_click_failures} до 0')
                        self.agent.state.modal_click_failures = 0
            
            # После request_user_input проверяем, закрыто ли модальное окно
            if action_name == 'request_user_input' and self.agent.browser_session:
                await asyncio.sleep(0.5)
                fresh_browser_state = self.agent.browser_session._cached_browser_state_summary
                if fresh_browser_state:
                    if not self.agent.email_subagent.detect_dialog(fresh_browser_state):
                        if self.agent.state.modal_click_failures > 0:
                            self.logger.info(f'✅ Модальное окно закрыто после request_user_input. Счетчик неудачных попыток сброшен с {self.agent.state.modal_click_failures} до 0')
                            self.agent.state.modal_click_failures = 0
                        
                        # Если модальное окно закрыто после request_user_input с подтверждением, задача выполнена
                        if result.extracted_content and ('подтвердил' in result.extracted_content.lower() or 'выполнено' in result.extracted_content.lower()):
                            self.logger.info('✅ Модальное окно закрыто после request_user_input с подтверждением пользователя. Задача выполнена пользователем - завершаем выполнение.')
                            result.is_done = True
                            result.success = True
                            result.long_term_memory = 'Пользователь успешно выполнил действие (например, нажал кнопку отправки отклика). Задача выполнена.'
                            result.extracted_content = 'Задача выполнена пользователем. Модальное окно закрыто, действие успешно завершено.'

