"""Native dispatcher wiring. Tool implementations and schemas remain owned by Hermes."""

from __future__ import annotations

import importlib.util
import inspect
import json
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any


class NativeSession:
    """The real Hermes stores needed by its original inline tool executors."""

    def __init__(self, runtime: NativeRuntime, session_id: str):
        from tools.memory_tool import (
            MemoryStore,
            get_builtin_memory_config,
            get_builtin_memory_store_flags,
        )
        from tools.todo_tool import TodoStore

        self.runtime = runtime
        self.session_id = session_id
        self.enabled_toolsets = runtime.toolsets
        self.disabled_toolsets = runtime.disabled_toolsets
        self.platform = "api_server"
        self._todo_store = TodoStore()
        self._memory_manager = None
        self._memory_store = None
        settings = get_builtin_memory_config(runtime.config)
        memory_enabled, user_enabled = get_builtin_memory_store_flags(runtime.config)
        if memory_enabled or user_enabled:
            self._memory_store = MemoryStore(
                memory_char_limit=settings.get("memory_char_limit", 2200),
                user_char_limit=settings.get("user_char_limit", 1375),
                memory_enabled=memory_enabled,
                user_profile_enabled=user_enabled,
            )
            self._memory_store.load_from_disk()
        self._session_db = None
        self._database_lock = threading.Lock()
        self._delegate_agent = None
        self.clarify_callback = None

    def _get_session_db_for_recall(self):
        from hermes_state_registry import acquire

        with self._database_lock:
            if self._session_db is None:
                self._session_db = acquire()
            return self._session_db

    def _dispatch_delegate_task(self, arguments: dict[str, Any]) -> Any:
        if self._delegate_agent is None:
            from hermes_cli.config import split_model_config_default
            from hermes_cli.runtime_provider import resolve_runtime_provider
            from run_agent import AIAgent

            model_config = self.runtime.config.get("model", {})
            model, provider = split_model_config_default(model_config.get("default", ""))
            route = resolve_runtime_provider(
                requested=provider or model_config.get("provider"),
                target_model=model,
            )
            accepted = inspect.signature(AIAgent).parameters
            options = {key: value for key, value in route.items() if key in accepted}
            self._delegate_agent = AIAgent(
                **options,
                model=model,
                session_id=self.session_id,
                quiet_mode=True,
                platform="api_server",
                enabled_toolsets=self.enabled_toolsets,
                disabled_toolsets=self.disabled_toolsets,
                skip_context_files=True,
                skip_background_review=True,
            )
        return self._delegate_agent._dispatch_delegate_task(arguments)

    def close(self) -> None:
        try:
            if self._delegate_agent is not None:
                self._delegate_agent.close()
        finally:
            if self._session_db is not None:
                from hermes_state_registry import release

                release(self._session_db)
                self._session_db = None


class NativeRuntime:
    """Reuse Hermes discovery, availability, middleware, guards, and original handlers."""

    def __init__(self, model_tools: Any, *, toolsets: list[str] | None = None):
        self.model_tools = model_tools
        self.toolsets = toolsets
        self.disabled_toolsets = None
        self.sessions: dict[str, NativeSession] = {}
        self._session_index_lock = threading.Lock()
        self._session_locks: dict[str, threading.Lock] = {}
        self.used_tasks: set[str] = set()
        self.config: dict[str, Any] = {}
        self.inline: dict[str, Any] = {}
        if importlib.util.find_spec("hermes_constants") is not None:
            from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS
            from hermes_cli.config import load_config_readonly

            self.config = load_config_readonly()
            self.disabled_toolsets = self.config.get("agent", {}).get("disabled_toolsets")
            self.inline = INLINE_TOOL_EXECUTORS
        self.definition_kwargs: dict[str, Any] = {"quiet_mode": True}
        parameters = inspect.signature(model_tools.get_tool_definitions).parameters
        if "skip_tool_search_assembly" in parameters:
            self.definition_kwargs["skip_tool_search_assembly"] = True
        if toolsets is not None:
            self.definition_kwargs["enabled_toolsets"] = toolsets
        if self.disabled_toolsets:
            self.definition_kwargs["disabled_toolsets"] = self.disabled_toolsets

    def definitions(self) -> list[dict[str, Any]]:
        return self.model_tools.get_tool_definitions(**self.definition_kwargs)

    def parallel_safe(self, session_id: str, requests: list[dict[str, Any]]) -> bool:
        """Use the native planner; unknown tools/runtimes remain sequential barriers."""
        if not self.inline:
            return False
        try:
            from agent.tool_dispatch_helpers import _plan_tool_batch_segments
            from tools.terminal_tool_lifecycle import get_active_env

            calls = [
                SimpleNamespace(
                    function=SimpleNamespace(
                        name=request["name"],
                        arguments=json.dumps(request["arguments"], ensure_ascii=False),
                    )
                )
                for request in requests
            ]
            # Reject native barriers before consulting a possibly occupied environment.
            planned = _plan_tool_batch_segments(calls)
            if len(planned) != 1 or planned[0][0] != "parallel":
                return False
            environment = get_active_env("hermes2mcp-" + session_id)
            cwd = Path(environment.cwd) if environment is not None and environment.cwd else None
            planned = _plan_tool_batch_segments(calls, execution_cwd=cwd)
            return len(planned) == 1 and planned[0][0] == "parallel"
        except Exception:
            return False  # Missing/version-changed native admission must never enable unsafe calls.

    def _inline_call(self, name: str, arguments: dict[str, Any], task_id: str) -> Any:
        from agent.agent_runtime_helpers import invoke_tool
        from tools.arg_coercion import coerce_tool_args

        with self._session_index_lock:
            lock = self._session_locks.setdefault(task_id, threading.Lock())
        with lock:
            agent = self.sessions.get(task_id)
            if agent is None:
                agent = self.sessions[task_id] = NativeSession(self, task_id)
        agent.valid_tool_names = {
            definition.get("function", definition)["name"] for definition in self.definitions()
        }
        return invoke_tool(
            agent,
            name,
            coerce_tool_args(name, arguments),
            task_id,
            uuid.uuid4().hex,
            messages=[],
        )

    def call(self, name: str, arguments: dict[str, Any], session_id: str) -> Any:
        names = [
            definition.get("function", definition)["name"] for definition in self.definitions()
        ]
        if name not in names:
            raise ValueError("Tool is not in the active Hermes catalog.")
        task_id = "hermes2mcp-" + session_id
        self.used_tasks.add(task_id)
        if not self.inline:
            return self.model_tools.handle_function_call(
                name,
                arguments,
                task_id=task_id,
                tool_call_id=uuid.uuid4().hex,
                session_id=task_id,
                enabled_tools=names,
            )
        from gateway.session_context import clear_session_vars, set_session_vars
        from tools.approval_context import reset_current_session_key, set_current_session_key

        tokens = set_session_vars(
            platform="api_server",
            source="hermes2mcp",
            session_key=task_id,
            session_id=task_id,
            cwd="",
            async_delivery=False,
        )
        approval_token = set_current_session_key(task_id)
        try:
            if name in self.inline:
                return self._inline_call(name, arguments, task_id)
            return self.model_tools.handle_function_call(
                name,
                arguments,
                task_id=task_id,
                tool_call_id=uuid.uuid4().hex,
                session_id=task_id,
                enabled_tools=names,
                enabled_toolsets=self.toolsets,
                disabled_toolsets=self.disabled_toolsets,
            )
        finally:
            reset_current_session_key(approval_token)
            clear_session_vars(tokens)

    def release(self, session_id: str) -> None:
        task_id = "hermes2mcp-" + session_id
        with self._session_index_lock:
            session = self.sessions.pop(task_id, None)
            self._session_locks.pop(task_id, None)
        try:
            if session is not None:
                session.close()
        finally:
            try:
                if self.inline:
                    from agent.client_lifecycle import ClientLifecycleMixin

                    owner = SimpleNamespace(_process_owner_task_ids={task_id})
                    ClientLifecycleMixin._close_task_resources(owner, task_id)
            finally:
                self.used_tasks.discard(task_id)

    def close(self) -> None:
        failure = None
        for task_id in list(self.used_tasks):
            try:
                self.release(task_id.removeprefix("hermes2mcp-"))
            except Exception as exc:
                if failure is None:
                    failure = exc
        if failure is not None:
            raise failure
