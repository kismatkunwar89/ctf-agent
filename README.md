# ctf-agent — Enhanced Fork

> Fork of [verialabs/ctf-agent](https://github.com/verialabs/ctf-agent) — the agent that won **1st place at BSidesSF 2026 (52/52 challenges)**.
> This fork adds three things: `reflect_and_reset()`, a free Ollama local solver, and OpenRouter free-tier support.

---

## What's New vs Upstream

### 1. `reflect_and_reset()` — fixes the core bump bottleneck

**The problem in the original**: when a solver gives up and gets "bumped", the code appends sibling insights on top of 40k–80k tokens of failed attempts. The model retries while drowning in its own failures.

**The fix**: a cheap LLM call (gpt-4o-mini, ~$0.001) distills the failed history into a compact `SolveReflection` — confirmed facts, failed approaches, dead ends, sandbox artifacts still usable. History is cleared. Next attempt starts with ~400 tokens of structure instead of 60k tokens of noise.

```
Before bump:  60,000 tokens of failure  →  model produces variation of failure
After reset:   1,100 tokens of structure →  model starts genuinely fresh

Token reduction: ~55x per bump
Research backing: Reflexion (Shinn et al. 2023) — 2–2.8x improvement on fresh-context retries
```

All three solver types updated: `Solver` (Pydantic AI / API), `ClaudeSolver` (Claude Code CLI), `CodexSolver` (Codex CLI).

### 2. `OllamaSolver` — free local models, zero API cost

Run challenges with local Qwen2.5-Coder, DeepSeek-R1, or Llama3 via Ollama. Full tool support, loop detection, sibling findings injection, and native `reflect_and_reset`. Falls back to OpenRouter free tier if Ollama is unreachable.

### 3. OpenRouter free-tier support

Added `openrouter/` provider for free cloud models (`qwq-32b`, `deepseek-r1`, `llama-4-maverick`) with key-pool rotation across multiple accounts.

---

## Quick Start

```bash
git clone https://github.com/kismatkunwar89/ctf-agent
cd ctf-agent
uv sync
docker build -f sandbox/Dockerfile.sandbox -t ctf-sandbox .
cp .env.example .env
# fill in .env
```

### Run with Claude Code + Codex (subscription — no per-token cost)
```bash
# Step 1: pull challenges
python pull_challenges.py \
  --url https://ctf.example.com \
  --token ctfd_your_token \
  --output ./challenges

# Step 2: solve
uv run ctf-solve \
  --ctfd-url https://ctf.example.com \
  --ctfd-token ctfd_your_token \
  --challenges-dir ./challenges \
  --coordinator claude
```

### Run free — Ollama only (no API keys at all)
```bash
# Install Ollama: https://ollama.com
ollama pull qwen2.5-coder:14b

python pull_challenges.py --url https://ctf.example.com --token ctfd_token --output ./challenges

uv run ctf-solve \
  --ctfd-url https://ctf.example.com \
  --ctfd-token ctfd_your_token \
  --challenges-dir ./challenges \
  --models ollama/qwen2.5-coder:14b \
  --coordinator claude
```

### Run free — OpenRouter free tier
```bash
# Get free key at https://openrouter.ai (set OPENROUTER_API_KEY in .env)
uv run ctf-solve \
  --ctfd-url https://ctf.example.com \
  --ctfd-token ctfd_your_token \
  --models openrouter/qwen/qwq-32b:free,openrouter/deepseek/deepseek-r1:free \
  --coordinator claude
```

### Mix paid + free in the same swarm
```bash
uv run ctf-solve \
  --ctfd-url https://ctf.example.com \
  --ctfd-token ctfd_your_token \
  --models claude-sdk/claude-opus-4-6/medium,codex/gpt-5.4,ollama/qwen2.5-coder:14b
```

---

## Architecture

```
CTFd Poller (5s)
    │
    ▼
Coordinator LLM (Claude Code CLI or Codex CLI)
    │  fetch_challenges → spawn_swarm → check_status
    │
    ▼  one swarm per challenge
ChallengeSwarm ──── MessageBus ──── cross-solver findings every 5 steps
    │
    ├── ClaudeSolver     claude-sdk/*       subscription  reflect_and_reset: close session
    ├── CodexSolver      codex/*            subscription  reflect_and_reset: kill thread
    ├── Solver           bedrock/azure/zen  API           reflect_and_reset: clear messages
    ├── OllamaSolver     ollama/*           FREE local    reflect_and_reset: native
    └── OpenRouterSolver openrouter/*       FREE cloud    reflect_and_reset: native
         │
         └── isolated Docker (Kali) sandbox per solver
             pwntools, radare2, gdb, angr, SageMath,
             volatility3, RsaCtfTool, z3, binwalk ...
```

### reflect_and_reset flow
```
Attempt 1  →  30 tool calls  →  GAVE_UP
                ↓
    reflect_and_reset(sibling_insights):
      gpt-4o-mini reads tracer log
      → SolveReflection { confirmed_facts, failed_approaches, dead_ends, next_direction }
      message history CLEARED  (sandbox files survive)
                ↓
Attempt 2  →  starts with 400-token structured reflection, not 60k-token failure pile
```

---

## Solver Models

| Spec | Provider | Cost | VRAM | Notes |
|---|---|---|---|---|
| `claude-sdk/claude-opus-4-6/medium` | Claude Pro | Subscription | — | Best quality |
| `claude-sdk/claude-opus-4-6/max` | Claude Pro | Subscription | — | Extended thinking |
| `codex/gpt-5.4` | ChatGPT Plus | Subscription | — | Best overall |
| `codex/gpt-5.4-mini` | ChatGPT Plus | Subscription | — | Fast |
| `codex/gpt-5.3-codex` | ChatGPT Plus | Subscription | — | Reasoning xhigh |
| `ollama/qwen2.5-coder:32b` | Local | **Free** | 20GB | Best local CTF |
| `ollama/qwen2.5-coder:14b` | Local | **Free** | 10GB | Good balance |
| `ollama/qwen2.5-coder:7b` | Local | **Free** | 5GB | Fast |
| `ollama/deepseek-r1:14b` | Local | **Free** | 9GB | Strong reasoning |
| `ollama/llama3.1:8b` | Local | **Free** | 5GB | Fallback |
| `openrouter/qwen/qwq-32b:free` | OpenRouter | **Free** | — | Rate-limited |
| `openrouter/deepseek/deepseek-r1:free` | OpenRouter | **Free** | — | Rate-limited |
| `bedrock/us.anthropic.claude-opus-4-6-v1` | AWS | API | — | Quota fallback |

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `CTFD_URL` | — | CTFd instance URL |
| `CTFD_TOKEN` | — | CTFd API token |
| `ANTHROPIC_API_KEY` | — | Claude API / Bedrock fallback |
| `OPENAI_API_KEY` | — | Codex API + reflection model |
| `REFLECTION_MODEL` | `openai:gpt-4o-mini` | Cheap model for `reflect_and_reset()` distillation |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Ollama endpoint |
| `OLLAMA_MODEL` | `ollama/qwen2.5-coder:14b` | Default local model |
| `OPENROUTER_API_KEY` | — | OpenRouter free/paid models |
| `AWS_REGION` | `us-east-1` | For Bedrock fallback |

---

## New Files in This Fork

| File | Description |
|---|---|
| `backend/reflexion.py` | `SolveReflection` dataclass + `reflect()` — distills message history via cheap LLM |
| `backend/agents/ollama_solver.py` | Full Ollama solver with tools, loop detection, reflect_and_reset |

## Modified Files

| File | Change |
|---|---|
| `backend/agents/solver.py` | Added `reflect_and_reset()`, reflection injection into first message |
| `backend/agents/claude_solver.py` | Added `reflect_and_reset()` via tracer log + session close |
| `backend/agents/codex_solver.py` | Added `reflect_and_reset()` via tracer log + thread kill |
| `backend/agents/swarm.py` | `ollama/` + `openrouter/` routing, `reflect_and_reset` dispatch |
| `backend/models.py` | `OLLAMA_MODELS`, `OPENROUTER_FREE_MODELS`, provider resolution |
| `.env.example` | New env vars for Ollama, OpenRouter, reflection model |

---

## Credits

- [verialabs/ctf-agent](https://github.com/verialabs/ctf-agent) — original architecture, BSidesSF 2026 winner
- [Reflexion — Shinn et al. 2023](https://arxiv.org/abs/2303.11366) — research backing for reflect_and_reset
- [Orionband/ctf-agent](https://github.com/Orionband/ctf-agent) — OpenRouter solver pattern reference
- [D1a0y1bb/HuntingBlade](https://github.com/D1a0y1bb/HuntingBlade) — KnowledgeStore architecture reference
