"""
Orquestrador do Plano — Plan Orchestrator

Componente de estado global do LLMCompiler acoplado à Task Fetching Unit e ao Replanner.
Persiste em Redis o estado completo de execução de cada sessão:
  - tarefas concluídas
  - tarefas pendentes / em espera
  - ordem das tarefas e dependências
  - histórico de mensagens
  - estado de desambiguação com o usuário

A sessão funciona como chave de busca no Redis, permitindo pausar a execução para
desambiguação e retomá-la do ponto exato onde parou, sem re-planejar.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

import redis
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    FunctionMessage,
    HumanMessage,
    SystemMessage,
)

from src.utils.logger_utils import log


class SessionStatus(str, Enum):
    RUNNING = "running"
    PAUSED_FOR_DISAMBIGUATION = "paused_for_disambiguation"
    COMPLETED = "completed"


def serialize_messages(messages: List[BaseMessage]) -> List[dict]:
    """Serializa uma lista de mensagens LangChain para dict JSON-safe."""
    result = []
    for msg in messages:
        entry: Dict[str, Any] = {
            "type": msg.__class__.__name__,
            "content": msg.content,
            "additional_kwargs": getattr(msg, "additional_kwargs", {}),
        }
        if isinstance(msg, FunctionMessage):
            entry["name"] = msg.name
        result.append(entry)
    return result


def deserialize_messages(data: List[dict]) -> List[BaseMessage]:
    """Desserializa uma lista de dicts para mensagens LangChain."""
    mapping = {
        "HumanMessage": HumanMessage,
        "AIMessage": AIMessage,
        "SystemMessage": SystemMessage,
    }
    messages: List[BaseMessage] = []
    for entry in data:
        msg_type = entry["type"]
        content = entry["content"]
        additional_kwargs = entry.get("additional_kwargs", {})

        if msg_type == "FunctionMessage":
            messages.append(
                FunctionMessage(
                    name=entry.get("name", ""),
                    content=content,
                    additional_kwargs=additional_kwargs,
                )
            )
        elif msg_type in mapping:
            msg = mapping[msg_type](content=content)
            if additional_kwargs:
                msg.additional_kwargs.update(additional_kwargs)
            messages.append(msg)
        else:
            # Fallback: trata como AIMessage para não perder contexto
            messages.append(AIMessage(content=content))
    return messages


class PlanOrchestrator:
    """
    Orquestrador do Plano — estado global da execução do LLMCompiler.

    Responsabilidades:
      - Registrar todas as tarefas pendentes antes da execução (Task Fetching Unit)
      - Registrar tarefas concluídas após cada ciclo de execução
      - Pausar a execução quando um agente sinaliza necessidade de desambiguação
      - Persistir o estado completo no Redis para retomada sem re-planejamento
      - Prover contexto para o Replanner ao retomar a sessão

    Uso básico:
        redis_client = redis.Redis(host='localhost', port=6380, decode_responses=True)
        orch = PlanOrchestrator(redis_client)
        session_id = orch.initialize_session("pergunta do usuário")
        # ... execução ...
        orch.record_pending_tasks(tasks_dict)
        orch.record_completed_tasks(tool_messages)
        # quando precisa de desambiguação:
        orch.pause_for_disambiguation("Qual é o valor exato?", messages)
        # quando usuário responde:
        saved_state = orch.resume("R$ 500,00")
    """

    SESSION_TTL_SECONDS = 60 * 60 * 24  # 24 horas

    def __init__(
        self,
        redis_client: redis.Redis,
        session_id: Optional[str] = None,
    ):
        self.redis = redis_client
        self.session_id = session_id or str(uuid.uuid4())

    # ------------------------------------------------------------------
    # Acesso ao Redis
    # ------------------------------------------------------------------

    def _key(self) -> str:
        return f"plan_orchestrator:session:{self.session_id}"

    def _save_state(self, state: dict) -> None:
        state["updated_at"] = datetime.utcnow().isoformat()
        self.redis.setex(
            self._key(),
            self.SESSION_TTL_SECONDS,
            json.dumps(state, ensure_ascii=False),
        )

    def load_state(self) -> Optional[dict]:
        """Carrega o estado da sessão do Redis. Retorna None se não encontrado."""
        raw = self.redis.get(self._key())
        if raw is None:
            return None
        return json.loads(raw)

    # ------------------------------------------------------------------
    # Ciclo de vida da sessão
    # ------------------------------------------------------------------

    def initialize_session(self, original_query: str) -> str:
        """
        Inicializa uma nova sessão e persiste o estado inicial no Redis.
        Retorna o session_id para ser usado como chave de busca.
        """
        state: dict = {
            "session_id": self.session_id,
            "status": SessionStatus.RUNNING,
            "original_query": original_query,
            "completed_tasks": {},
            "pending_tasks": {},
            "task_order": [],
            "messages": [],
            "disambiguation_question": None,
            "final_response": None,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }
        self._save_state(state)
        log(f"[PlanOrchestrator] Sessão {self.session_id} inicializada")
        return self.session_id

    def complete_session(self, final_response: str) -> None:
        """Marca a sessão como concluída com a resposta final."""
        state = self.load_state()
        if state is None:
            return
        state["status"] = SessionStatus.COMPLETED
        state["final_response"] = final_response
        state["pending_tasks"] = {}
        self._save_state(state)
        log(f"[PlanOrchestrator] Sessão {self.session_id} concluída")

    # ------------------------------------------------------------------
    # Registro de tarefas (acoplado à Task Fetching Unit)
    # ------------------------------------------------------------------

    def record_pending_tasks(self, tasks: Dict[int, Any]) -> None:
        """
        Registra as tarefas que estão prestes a ser executadas.
        Chamado pela Task Fetching Unit antes do dispatch paralelo.
        """
        state = self.load_state()
        if state is None:
            return
        for idx, task in tasks.items():
            task_record = {
                "idx": task.idx,
                "name": task.name,
                "args": [str(a) for a in task.args],
                "dependencies": list(task.dependencies),
                "is_join": task.is_join,
            }
            state["pending_tasks"][str(idx)] = task_record
            if str(idx) not in state["task_order"]:
                state["task_order"].append(str(idx))
        self._save_state(state)
        log(f"[PlanOrchestrator] {len(tasks)} tarefas pendentes registradas")

    def record_completed_tasks(self, tool_messages: List[FunctionMessage]) -> None:
        """
        Registra as tarefas concluídas após a execução pelo Executor.
        Chamado após cada ciclo da Task Fetching Unit.
        """
        state = self.load_state()
        if state is None:
            return
        for msg in tool_messages:
            idx = str(msg.additional_kwargs.get("idx", ""))
            state["completed_tasks"][idx] = {
                "name": msg.name,
                "args": str(msg.additional_kwargs.get("args", "")),
                "observation": msg.content,
                "completed_at": datetime.utcnow().isoformat(),
            }
            state["pending_tasks"].pop(idx, None)
        self._save_state(state)
        log(f"[PlanOrchestrator] {len(tool_messages)} tarefas concluídas registradas")

    # ------------------------------------------------------------------
    # Desambiguação (acoplado ao Replanner)
    # ------------------------------------------------------------------

    def pause_for_disambiguation(
        self,
        question: str,
        messages: List[BaseMessage],
    ) -> None:
        """
        Pausa a execução e salva o estado completo para permitir retomada
        após o usuário responder à pergunta de desambiguação.

        Args:
            question: Pergunta gerada pelo agente para o usuário.
            messages: Estado completo das mensagens até o ponto de pausa.
        """
        state = self.load_state()
        if state is None:
            return
        state["status"] = SessionStatus.PAUSED_FOR_DISAMBIGUATION
        state["disambiguation_question"] = question
        state["messages"] = serialize_messages(messages)
        self._save_state(state)
        log(
            f"[PlanOrchestrator] Sessão {self.session_id} pausada para desambiguação. "
            f"Pergunta: {question!r}"
        )

    def resume(self, user_response: str) -> Optional[dict]:
        """
        Retoma a sessão após o usuário responder à desambiguação.

        Args:
            user_response: Resposta do usuário à pergunta de desambiguação.

        Returns:
            Estado salvo da sessão (com mensagens serializadas), ou None se
            a sessão não existir ou não estiver pausada.
        """
        state = self.load_state()
        if state is None or state["status"] != SessionStatus.PAUSED_FOR_DISAMBIGUATION:
            return None
        state["status"] = SessionStatus.RUNNING
        state["disambiguation_question"] = None
        self._save_state(state)
        log(f"[PlanOrchestrator] Sessão {self.session_id} retomada com resposta do usuário")
        return state

    # ------------------------------------------------------------------
    # Consulta de estado
    # ------------------------------------------------------------------

    def is_paused(self) -> bool:
        """Retorna True se a sessão está pausada aguardando resposta do usuário."""
        state = self.load_state()
        return (
            state is not None
            and state["status"] == SessionStatus.PAUSED_FOR_DISAMBIGUATION
        )

    def get_summary(self) -> dict:
        """
        Retorna um resumo legível do estado atual da sessão.
        Útil para debugging e para entender o que já foi executado.
        """
        state = self.load_state()
        if state is None:
            return {"error": f"Sessão {self.session_id!r} não encontrada no Redis"}
        return {
            "session_id": state["session_id"],
            "status": state["status"],
            "original_query": state["original_query"],
            "task_order": state["task_order"],
            "completed_tasks": state["completed_tasks"],
            "pending_tasks": state["pending_tasks"],
            "disambiguation_question": state.get("disambiguation_question"),
            "final_response": state.get("final_response"),
            "created_at": state["created_at"],
            "updated_at": state["updated_at"],
        }
