# CTF Agent — Enhanced Fork

Autonomous CTF solver forked from [verialabs/ctf-agent](https://github.com/verialabs/ctf-agent) (1st place BSidesSF 2026, 52/52 challenges).

## What's New in This Fork

### 1. `reflect_and_reset()` — Fixes the core bottleneck

**The problem**: When a solver gives up and gets "bumped", the original code appends sibling insights to a 40k-80k token pile of failed attempts. The model is asked to think differently while cognitively drowned in everything that didn't work.

**The fix**: After each failed attempt, a cheap model (gpt-4o-mini, ~$0.001) distills the history into a compact `SolveReflection` — confirmed facts, failed approaches, dead ends, sandbox artifacts, best hypothesis. The message history is then **cleared**. The next attempt starts fresh with only the 400-token structured reflection.

```
Before: 60,000 tokens of failure → model produces variation of failure
After:  1,100 tokens of structure → model starts genuinely fresh
Savings: ~55x input token reduction per bump. Research: Reflexion (Shinn et al. 2023) → 2-2.8x improvement.
```

All three solver types updated: `Solver` (Pydantic AI), `ClaudeSolver`, `CodexSolver`.

### 2. `OllamaSolver` — Free local models, zero API cost

Run CTF challenges with local Qwen2.5-Coder, DeepSeek-R1, or Llama3 via Ollama:
- Full tool support (bash, file ops, flag submission, webhooks, sibling findings)
- Thought-action fallback for models that don't emit structured tool_calls
- Retry logic for model loading delays
- Automatic fallback to OpenRouter free tier if Ollama unreachable

### 3. OpenRouter free-tier support

Added `openrouter/` provider for free cloud models (qwq-32b, deepseek-r1, llama-4-maverick) as fallback when local models are unavailable.

## Quick Start

```bash
uv sync
docker build -f sandbox/Dockerfile.sandbox -t ctf-sandbox .
cp .env.example .env
# Edit .env with your credentials
```

### Run with Claude Code + Codex (subscription, no per-token cost)
```bash
uv run ctf-solve \
  --ctfd-url https://ctf.example.com \
  --ctfd-token ctfd_your_token \
  --coordinator claude
```

### Run free — Ollama only
```bash
# 1. Install Ollama: https://ollama.com
ollama pull qwen2.5-coder:14b

# 2. Run with local model only
uv run ctf-solve \
  --ctfd-url https://ctf.example.com \
  --ctfd-token ctfd_your_token \
  --models ollama/qwen2.5-coder:14b \
  --coordinator claude
```

### Run free — OpenRouter free tier
```bash
# Set OPENROUTER_API_KEY in .env, then:
uv run ctf-solve \
  --ctfd-url https://ctf.example.com \
  --ctfd-token ctfd_your_token \
  --models openrouter/qwen/qwq-32b:free,openrouter/deepseek/deepseek-r1:free
```

### Mix paid + free in the same swarm
```bash
uv run ctf-solve \
  --ctfd-url https://ctf.example.com \
  --ctfd-token ctfd_your_token \
  --models claude-sdk/claude-opus-4-6/medium,codex/gpt-5.4,ollama/qwen2.5-coder:14b
```

## Architecture

```
CTFd Poller (5s)
    │
    ▼
Coordinator (Claude SDK or Codex)
    │  spawn_swarm, check_status
    │
    ▼ per-challenge
ChallengeSwarm ──── MessageBus ──── cross-solver findings
    │
    ├── ClaudeSolver    (claude-sdk/*)   subscription, reflect_and_reset via session close
    ├── CodexSolver     (codex/*)        subscription, reflect_and_reset via thread kill
    ├── Solver          (bedrock/azure)  API, reflect_and_reset via message clear
    ├── OllamaSolver    (ollama/*)       FREE local, reflect_and_reset native
    └── OpenRouterSolver (openrouter/*) FREE cloud, reflect_and_reset native
         Each in isolated Docker (Kali) sandbox ↑
```

### Reflect & Reset Flow
```
Attempt 1: 30 tool calls → GAVE_UP
    ↓
reflect_and_reset():
  gpt-4o-mini reads trace → SolveReflection (confirmed facts, failures, next step)
  message history CLEARED (sandbox files survive)
    ↓
Attempt 2: starts with 400-token structured context, not 60k token failure pile
```

## Solver Models

| Spec | Provider | Cost | Notes |
|------|----------|------|-------|
| `claude-sdk/claude-opus-4-6/medium` | Claude Pro | Subscription | Best quality |
| `claude-sdk/claude-opus-4-6/max` | Claude Pro | Subscription | Extended thinking |
| `codex/gpt-5.4` | ChatGPT Plus | Subscription | Best overall |
| `codex/gpt-5.4-mini` | ChatGPT Plus | Subscription | Fast |
| `codex/gpt-5.3-codex` | ChatGPT Plus | Subscription | Reasoning (xhigh) |
| `ollama/qwen2.5-coder:32b` | Local | **Free** | Best local CTF model |
| `ollama/qwen2.5-coder:14b` | Local | **Free** | Good balance |
| `ollama/deepseek-r1:14b` | Local | **Free** | Strong reasoning |
| `openrouter/qwen/qwq-32b:free` | OpenRouter | **Free** | Rate-limited |
| `openrouter/deepseek/deepseek-r1:free` | OpenRouter | **Free** | Rate-limited |
| `bedrock/us.anthropic.claude-opus-4-6-v1` | AWS | API | Quota fallback |

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | — | For Claude API / bedrock fallback |
| `OPENAI_API_KEY` | — | For Codex API / gpt-4o-mini reflection |
| `REFLECTION_MODEL` | `openai:gpt-4o-mini` | Model used for reflect_and_reset() distillation |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Ollama endpoint |
| `OLLAMA_MODEL` | `ollama/qwen2.5-coder:14b` | Default local model |
| `OPENROUTER_API_KEY` | — | For OpenRouter free/paid models |

## Credits

- [verialabs/ctf-agent](https://github.com/verialabs/ctf-agent) — original architecture, 1st place BSidesSF 2026
- [Reflexion (Shinn et al. 2023)](https://arxiv.org/abs/2303.11366) — context reset research backing
- [Orionband/ctf-agent](https://github.com/Orionband/ctf-agent) — OpenRouter solver pattern
- [HackSynth](https://arxiv.org/html/2412.01778v1) — Summarizer architecture reference
