# FictionWriter

An autonomous long-form fiction generation engine built on a **LangGraph finite-state
machine**. A planning cascade decomposes a premise into arcs → chapters → scenes → beats,
each beat is drafted, audited programmatically, gated by a panel of adversarial critics,
and revised in a closed loop until it clears quality thresholds — then committed to a
six-store hybrid memory stack and streamed live to a Quart dashboard.

## System Architecture

```mermaid
flowchart TB
    subgraph Client["🖥️ Browser Dashboard"]
        UI["Dashboard / Codex / Alignment / Settings"]
        SSE["SSE event stream (/stream)"]
    end

    subgraph Web["⚡ Quart ASGI Server (app.py)"]
        direction TB
        DASH["dashboard_bp<br/>/ · /generate · /status · /stream"]
        CTRL["control_bp<br/>/control/{pause,resume,stop,patch,reset}"]
        CODEX["codex_bp<br/>/codex/{characters,threads,branches,raptor,events}"]
        ALIGN["alignment_bp<br/>/alignment/{claims,confirm,contradict,ingest}"]
        SET["settings_bp<br/>/settings · /settings/test"]
    end

    GM["core/generation_manager.py<br/>single-flight FSM task owner<br/>strong task ref · debug astream"]
    BUS["core/stream_bus.py<br/>pub/sub + live reattach snapshot"]

    subgraph FSM["🧠 LangGraph FSM (fsm/graph.py)"]
        GRAPH["compiled StateGraph<br/>OrchestratorState"]
    end

    subgraph LLM["🤖 LLM Layer (llm/)"]
        CALL["call_llm.py<br/>structured output · tool streaming"]
        GBNF["gbnf_compiler.py · tokenizer.py"]
        EP[("5 role endpoints:<br/>planner · drafter · critic<br/>pad_translator · craft_consultant")]
    end

    subgraph MEM["💾 Hybrid Memory Stack (memory/)"]
        SQL[("SQLite<br/>relational hub")]
        GRAPHITI[("Graphiti + FalkorDB<br/>temporal knowledge graph")]
        CHROMA[("ChromaDB<br/>HNSW vector index")]
        RAPTOR[("RAPTOR<br/>hierarchical summary tree")]
        STYLE[("Style Store<br/>voice baselines")]
        EVENTLOG[("Event Log<br/>append-only .jsonl")]
    end

    UI --> Web
    SSE -. subscribes .-> BUS
    DASH -->|/generate| GM
    CTRL -->|reset| RT["core/runtime.py<br/>init_resources()"]
    GM --> GRAPH
    GM -. publishes pipeline_status .-> BUS
    GRAPH --> CALL
    CALL --> EP
    CALL -.- GBNF
    GRAPH <--> MEM
    RT --> MEM
    CODEX --> MEM
    ALIGN --> MEM
    SET -->|writes| CFG["config.yaml"]
    CFG -. read at node entry .-> GRAPH
```

## FSM Generation Pipeline

The heart of the system. Nodes are wired in `fsm/graph.py`; conditional routing lives in
`fsm/routers/`. Solid arrows are unconditional edges; diamonds are router functions.

```mermaid
flowchart TD
    START([START]) --> PG[node_plan_global<br/>genre · premise · global arc]

    subgraph PLAN["📋 Planning Cascade"]
        PG --> PA[node_plan_arc]
        PA --> PC[node_plan_chapter]
        PC --> PB[node_plan_beat<br/>PAD grounded translation<br/>reads config thresholds]
    end

    PB --> AC[node_assemble_context<br/>RAPTOR + graph + vector recall]
    AC --> DP[node_draft_prose]
    DP --> PAU[node_programmatic_audit<br/>passive voice · slop · STEL Dc]

    PAU --> R1{edge_programmatic_router}
    R1 -->|"Dc &lt; threshold × 0.7<br/>(fast path)"| CT
    R1 -->|needs review| ADV

    subgraph QGATE["🎭 Quality Gate"]
        ADV[node_adversarial_critics<br/>continuity · dialogue · pacing]
    end

    ADV --> R2{edge_mode_selector}
    R2 -->|"Dc OK"| CT[node_commit_transaction]
    R2 -->|"retry ≤ 3"| RP[node_revise_prose]
    R2 -->|"retry &gt; 3"| CC[node_craft_consultant<br/>deadlock breaker]
    R2 -->|"retry &gt; 5"| FE[node_freeze_and_escalate]

    subgraph LOOP["🔄 Revision Loop"]
        RP --> PAU
        CC --> RP
    end

    subgraph FALLBACK["🚨 Fallback Subgraph"]
        FE --> R3{freeze_router}
        R3 -->|Tier 1| RP
        R3 -->|Tier 2| PB
        R3 -->|"replan &gt; 2"| PC
        R3 -->|last resort| HI[node_human_intervention]
    end

    HI --> PAU

    CT --> R4{edge_commit_router}
    R4 -->|"beats remain"| PB
    R4 -->|"scene done"| PC
    R4 -->|"chapter done"| PA
    R4 -->|"arc done"| PG
    R4 -->|"word_count_target met"| DONE([END → Export Pipeline])

    CT -. chapter boundary .-> CM[node_compress_memory] -.-> PC

    classDef plan fill:#1e3a5f,stroke:#4a90d9,color:#fff
    classDef gate fill:#5f1e3a,stroke:#d94a90,color:#fff
    classDef fb fill:#5f4a1e,stroke:#d9a04a,color:#fff
    class PG,PA,PC,PB plan
    class ADV,PAU gate
    class FE,CC,HI fb
```

### Routing thresholds (`config.yaml`)

| Router | Decision | Threshold key |
| --- | --- | --- |
| `edge_programmatic_router` | fast-path bypass of critics | `stel_cosine_distance × programmatic_fast_path_multiplier` (0.12 × 0.7) |
| `edge_mode_selector` | revise vs. consult vs. freeze | `craft_consultant_threshold` (3), `retry_count_max` (5) |
| `freeze_router` | re-plan tier escalation | `replan_count_max` (2) |
| `edge_commit_router` | manuscript complete | `word_count_target` (2000) |

## Hybrid Memory Stack

All six stores are file-based and reinitialized atomically by `core/runtime.py`
(`init_resources()` at boot, and again on `POST /control/reset`).

```mermaid
flowchart LR
    subgraph WRITE["Write Path (node_commit_transaction)"]
        BEAT["committed beat"]
    end

    BEAT --> SQL[("SQLite<br/>scenes · beats · threads<br/>ACID source of truth")]
    BEAT --> GR[("Graphiti / FalkorDB<br/>character & event<br/>temporal graph")]
    BEAT --> CH[("ChromaDB<br/>beat embeddings")]
    BEAT --> EL[("Event Log<br/>.jsonl audit + replay")]

    SQL --> RAP[("RAPTOR<br/>scene-cluster<br/>summary tree")]
    SQL --> ST[("Style Store<br/>voice baseline<br/>L2 drift guard")]

    subgraph READ["Read Path (node_assemble_context)"]
        CTX["assembled context window"]
    end

    RAP --> CTX
    GR --> CTX
    CH --> CTX
    ST --> CTX

    BR["branch_manager.py<br/>O(1) snapshot + .jsonl replay"] -. crash recovery .-> SQL
    EL -. replay .-> BR
    PROV["provisional_store.py<br/>coreference claims"] -. alignment review .-> GR
```

## Quick Start

```bash
# 1. start the FalkorDB knowledge-graph server
docker compose up -d

# 2. configure LLM endpoints (copy and edit per-role keys)
cp .env.example .env

# 3. run the async server
uv run app.py        # → http://localhost:5000
```

Open the dashboard, set the premise/genre in **Settings** (writes back to `config.yaml`,
picked up at the next beat boundary), and click **Generate**. The pipeline runs
server-side regardless of page focus — reload at any time to reattach to the live stream.

## Project Layout

| Path | Responsibility |
| --- | --- |
| `app.py` | Quart application factory, blueprint registration, startup lifecycle |
| `config.yaml` | All runtime thresholds and per-role LLM endpoint config (validated, `extra='forbid'`) |
| `core/` | Config loader, runtime resource lifecycle, generation manager, SSE stream bus, antislop |
| `fsm/` | LangGraph state graph, nodes, conditional routers, `OrchestratorState` |
| `llm/` | `call_llm` (structured output + tool streaming), GBNF compiler, tokenizer |
| `memory/` | The six persistent stores + branch/provisional managers |
| `ingestion/` | Non-blocking sliding-window ingestion + coreference resolution |
| `routes/` | Blueprints: dashboard, control, codex, alignment, settings |
| `prompts/` | Jinja2 XML node prompt templates + loader |
| `evals/` | Manuscript generator, error injection, LLM judge, runner |
