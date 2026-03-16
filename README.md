# LLMCompiler — Plan, Execute & Replan Architecture

Implementação do framework **LLMCompiler** com uma extensão original: o **Orquestrador do Plano**, componente de estado global persistido em Redis que permite pausar a execução para desambiguação com o usuário e retomá-la do ponto exato onde parou.

> Desenvolvido por **Leonardo José da Silva**, inspirado no artigo [LLMCompiler: An LLM Compiler for Parallel Function Calling](https://arxiv.org/pdf/2312.04511).

---

## Sumário

1. [Arquitetura Original — LLMCompiler](#1-arquitetura-original--llmcompiler)
2. [Extensão — Orquestrador do Plano](#2-extensão--orquestrador-do-plano)
3. [Estrutura de Arquivos](#3-estrutura-de-arquivos)
4. [Instalação](#4-instalação)
5. [Configuração do Redis](#5-configuração-do-redis)
6. [Como usar](#6-como-usar)
7. [Fluxo de Desambiguação](#7-fluxo-de-desambiguação)
8. [Referência da API](#8-referência-da-api)

---

## 1. Arquitetura Original — LLMCompiler

O LLMCompiler é o primeiro framework a otimizar a orquestração de *function calling* em LLMs, inspirado nos compiladores clássicos de linguagens de programação. Sua principal otimização consiste em identificar instruções que podem ser executadas em paralelo e gerenciar suas dependências de forma eficiente.

### Componentes

```
┌─────────────────────────────────────────────────────────────┐
│                        LLMCompiler                          │
│                                                             │
│   ┌───────────┐    ┌──────────────────┐    ┌───────────┐    │
│   │  Planner  │───▶│ Task Fetching    │───▶│ Executor  │    │
│   │  (LLM)    │    │ Unit (TF Unit)   │    │ (paralelo)│    │
│   └───────────┘    └──────────────────┘    └──────┬────┘    │
│                                                   │         │
│   ┌───────────────────────────────────────────────▼──────┐  │
│   │                     Joiner (LLM)                     │  │
│   │   FinalResponse | Replan | Clarification             │  │
│   └───────┬──────────────┬──────────────────┬────────────┘  │
│           │              │                  │               │
│         END          Replanner        Orquestrador          │
│                                       do Plano (novo)       │
└─────────────────────────────────────────────────────────────┘
```

### 1.1 Function Calling Planner

Gera uma sequência de tarefas a serem executadas, juntamente com suas dependências. Suporta replanejamento ao receber um `SystemMessage` com contexto do ciclo anterior.

**Dois modos de operação:**
- **Planejamento inicial** — recebe apenas o `HumanMessage` com a query do usuário.
- **Replanejamento** — recebe o histórico de execução + contexto de falha/desambiguação via `SystemMessage`.

### 1.2 Task Fetching Unit (TF Unit)

Inspirada nas unidades de *instruction fetching* das arquiteturas modernas de computadores. Busca tarefas para o Executor assim que estão prontas para execução (política gulosa/greedy), respeitando as dependências declaradas no grafo de tarefas.

Responsabilidades:
- Resolver placeholders de dependência (`$1`, `${2}`) com os valores reais das observações anteriores.
- Disparar tarefas independentes de forma concorrente com `ThreadPoolExecutor`.
- Aguardar a conclusão de dependências antes de submeter tarefas que dependem delas.

### 1.3 Executor

Executa de forma assíncrona/concorrente as tarefas obtidas da TF Unit. Como a TF Unit garante que todas as tarefas enviadas são independentes entre si, o Executor pode rodá-las concorrentemente sem condições de corrida.

### 1.4 Joiner

LLM que analisa as observações coletadas e decide a próxima ação:

| Ação | Descrição |
|---|---|
| `FinalResponse(response)` | Resposta final suficiente — encerra o grafo. |
| `Replan(feedback)` | Resultado insuficiente — replana com contexto de falha. |
| `Clarification(question)` | Informação crítica ausente — pausa para desambiguação com o usuário. |

### 1.5 Replanejamento (Replanning)

Permite adaptar o grafo de execução com base em resultados intermediários desconhecidos a priori (análogo ao *branching* em tempo de execução). O Replanner recebe o histórico completo de tarefas executadas e suas observações, garantindo que nenhuma tarefa já concluída seja repetida.

---

## 2. Extensão — Orquestrador do Plano

### Motivação

Em fluxos de execução reais, o agente pode precisar de informações do usuário que não estão disponíveis a priori (desambiguação). Sem o Orquestrador do Plano, o sistema teria que reiniciar o planejamento do zero ao receber a resposta do usuário, desperdiçando tokens e aumentando latência.

### Solução

O **Orquestrador do Plano** mantém um **estado global da execução** persistido no Redis. Quando uma desambiguação é necessária:

1. O agente emite `Clarification(question)`.
2. O nó `orchestrator_save` salva o estado completo no Redis e encerra o ciclo.
3. O sistema retorna a pergunta ao usuário.
4. Ao receber a resposta, `resume_session()` restaura o estado e retoma a execução.

### Estado mantido pelo Orquestrador

```json
{
  "session_id": "uuid-da-sessao",
  "status": "paused_for_disambiguation | running | completed",
  "original_query": "query original do usuário",
  "task_order": ["1", "2", "3"],
  "completed_tasks": {
    "1": { "name": "cofrinhos_extrato", "observation": "R$ 3000,00", "completed_at": "..." },
    "2": { "name": "cofrinhos_recuperar", "observation": "Recuperado R$ 500,00", "completed_at": "..." }
  },
  "pending_tasks": {
    "3": { "name": "execute_pix", "args": ["João", "$2"], "dependencies": [2] }
  },
  "disambiguation_question": "Para qual destinatário deseja enviar o PIX?",
  "messages": [...],
  "final_response": null
}
```

### Integração com os demais componentes

```
Task Fetching Unit ──▶ record_pending_tasks()   ─┐
                                                  ├── PlanOrchestrator (Redis)
Executor           ──▶ record_completed_tasks()  ─┘
                                                  │
Joiner (Clarify)   ──▶ pause_for_disambiguation() ─▶ estado salvo ──▶ END

resume_session()   ──▶ carrega estado ──▶ Replanner ──▶ continua execução
```

### Nó `orchestrator_save` no LangGraph

```
plan_and_schedule ──▶ join ──┬──▶ END                  (FinalResponse)
                             ├──▶ plan_and_schedule     (Replan)
                             └──▶ orchestrator_save     (Clarification)
                                        │
                                       END
```

---

## 3. Estrutura de Arquivos

```
plan-execute-replan-architecture/
├── LLMcompiler_revised.ipynb       # Notebook principal com toda a implementação
├── LLMcompiler.ipynb               # Notebook de referência (versão original)
├── README.md
├── requirements.txt
├── src/
│   ├── llm_compiler/
│   │   ├── output_parser.py        # Parser do plano gerado pelo LLM (LLMCompilerPlanParser)
│   │   ├── task_fetching_unit.py   # Task, TaskFetchingUnit — grafo de dependências e execução paralela
│   │   └── plan_orchestrator.py   # [NOVO] PlanOrchestrator — estado global persistido em Redis
│   ├── tools/
│   │   └── base.py                 # Tool, StructuredTool — wrappers de ferramentas
│   └── utils/
│       └── logger_utils.py         # Logger utilitário
└── env/                            # Ambiente virtual Python
```

### Descrição dos módulos

| Arquivo | Responsabilidade |
|---|---|
| `output_parser.py` | Parseia o texto gerado pelo LLM em um `dict[idx → Task]`. Detecta dependências via padrão `$1` / `${1}`. |
| `task_fetching_unit.py` | Define `Task` (dataclass) e `TaskFetchingUnit`. Implementa o scheduler paralelo com política greedy. |
| `plan_orchestrator.py` | `PlanOrchestrator` — persiste no Redis o estado completo da sessão. Gerencia o ciclo de vida: inicialização, rastreamento de tarefas, pausa e retomada. |
| `base.py` | `Tool` e `StructuredTool` — wrappers sobre `BaseTool` do LangChain para criação de ferramentas callable. |
| `logger_utils.py` | `Logger` para métricas de latência e acurácia. Função `log()` com toggle global. |

---

## 4. Instalação

```bash
# Clone o repositório
git clone https://github.com/Leonardojdss/plan-execute-replan-architecture.git
cd plan-execute-replan-architecture

# Crie e ative o ambiente virtual
python3 -m venv env
source env/bin/activate

# Instale as dependências
pip install -r requirements.txt
```

### Dependências

| Pacote | Versão | Uso |
|---|---|---|
| `langchain` | 1.2.10 | Framework base LLM |
| `langchain_openai` | 1.1.10 | Integração OpenAI |
| `langgraph` | 1.0.10 | Orquestração de grafo stateful |
| `redis` | 7.3.0 | Persistência do estado do Orquestrador do Plano |
| `numexpr` | 2.14.1 | Otimização numérica |
| `load_dotenv` | — | Carregamento de variáveis de ambiente |

### Variáveis de ambiente

Crie um arquivo `.env` na raiz do projeto:

```env
OPENAI_API_KEY=sk-...
```

---

## 5. Configuração do Redis

O Orquestrador do Plano requer uma instância Redis rodando em `localhost:6380`.

```bash
# Verificar se o Redis está rodando
redis-cli -p 6380 ping
# Esperado: PONG

# Iniciar Redis na porta 6380 (se necessário)
redis-server --port 6380 --daemonize yes
```

O estado de cada sessão é armazenado com a chave:

```
plan_orchestrator:session:<session_id>
```

TTL padrão: **24 horas** (configurável em `PlanOrchestrator.SESSION_TTL_SECONDS`).

---

## 6. Como usar

Abra o notebook [LLMcompiler_revised.ipynb](LLMcompiler_revised.ipynb) e execute as células em ordem.

### Execução simples (sem desambiguação)

```python
result, session_id = run_session("quero fazer um resgate de 500 reais e pix de 100 para Leonardo")
print(result[-1].content)
```

### Execução com rastreamento de estado

```python
# Inicia e obtém o session_id
result, session_id = run_session("quero resgatar dos cofrinhos e fazer um pix")

# Inspeciona o que foi executado
show_session_state(session_id)
```

### Retomada após desambiguação

```python
# Se o agente fez uma pergunta ao usuário, a execução fica pausada.
# O sistema imprime algo como:
# "Desambiguação necessária (session_id='abc123'):
#   Pergunta: Para qual destinatário deseja enviar o PIX?"

# Retoma com a resposta do usuário
result = resume_session(session_id, "Para João, R$ 200,00")
print(result[-1].content)
```

---

## 7. Fluxo de Desambiguação

```
Usuário: "quero resgatar e fazer um pix"
         │
         ▼
   [Planner] gera grafo de tarefas
         │
         ▼
   [TF Unit] registra tarefas pendentes no Redis
         │
         ▼
   [Executor] executa tasks em paralelo → observações
         │
         ▼
   [Joiner] analisa resultados
         │
         ├── FinalResponse ──▶ resposta ao usuário (END)
         │
         ├── Replan ──▶ volta ao Planner com contexto
         │
         └── Clarification("Para quem é o PIX?")
                   │
                   ▼
         [orchestrator_save]
         - Salva estado completo no Redis
         - status = "paused_for_disambiguation"
         - Inclui: tarefas concluídas, pendentes, mensagens
                   │
                   ▼
            END (ciclo atual)

Usuário responde: "Para Maria, R$ 150"
         │
         ▼
   resume_session(session_id, "Para Maria, R$ 150")
         │
         ▼
   Estado restaurado do Redis
   Histórico completo reconstruído
         │
         ▼
   [Replanner] recebe histórico + resposta do usuário
         │
         ├── Replanner decide replanear → novo ciclo (sem repetir tasks anteriores)
         │
         └── Replanner decide finalizar → FinalResponse direto
```

---

## 8. Referência da API

### `PlanOrchestrator`

```python
from src.llm_compiler.plan_orchestrator import PlanOrchestrator

orch = PlanOrchestrator(redis_client, session_id=None)
```

| Método | Parâmetros | Retorno | Descrição |
|---|---|---|---|
| `initialize_session(query)` | `query: str` | `session_id: str` | Inicia nova sessão no Redis. |
| `record_pending_tasks(tasks)` | `tasks: Dict[int, Task]` | `None` | Registra tarefas antes do dispatch. |
| `record_completed_tasks(msgs)` | `msgs: List[FunctionMessage]` | `None` | Registra tarefas concluídas. |
| `pause_for_disambiguation(q, msgs)` | `question: str`, `messages: List` | `None` | Pausa a sessão e salva estado. |
| `resume(user_answer)` | `user_answer: str` | `dict \| None` | Retoma sessão pausada. |
| `is_paused()` | — | `bool` | Verifica se a sessão está pausada. |
| `get_summary()` | — | `dict` | Resumo legível do estado atual. |
| `load_state()` | — | `dict \| None` | Estado bruto do Redis. |

### Funções de conveniência (notebook)

| Função | Descrição |
|---|---|
| `run_session(query, session_id=None)` | Executa o LLMCompiler com rastreamento. Retorna `(messages, session_id)`. |
| `resume_session(session_id, answer)` | Retoma sessão pausada com a resposta do usuário. |
| `show_session_state(session_id)` | Imprime o estado atual da sessão em JSON. |

### Modelos Pydantic do Joiner

| Modelo | Campos | Quando usar |
|---|---|---|
| `FinalResponse` | `response: str` | Resposta suficiente para encerrar. |
| `Replan` | `feedback: str` | Resultado insuficiente, precisa replanejar. |
| `Clarification` | `question: str` | Informação crítica ausente, precisa do usuário. |
