# LLM Provider

The `llm_client` interface, the current Groq-backed implementation, and
exactly what changes to point it at Azure OpenAI later.

## The interface

`agent-service/app/core/llm_client.py`:

```python
client = get_llm_client()
response = await client.generate(
    system_prompt="...",
    user_prompt="...",
    tools=[{"type": "function", "function": {...}}],  # optional, OpenAI-style
)
# response: LLMResponse(content, tool_calls, model, finish_reason, usage, provider)
```

One interface for every agent. Tool/function calling is part of the
interface **now** (OpenAI-style schemas passed through; requested calls
surface on `LLMResponse.tool_calls`) so the Recommendation Agent (Prompt 4+)
can adopt it without another interface break — this prompt's agents do not
use tools yet.

**AZURE OPENAI NOTE:** the active implementation is a stand-in. A second
implementation can be swapped in with **no change to any agent code** that
calls `llm_client.generate(...)` — only the wiring inside
`get_llm_client()` changes.

## Current implementation: Groq chat completions

| Aspect | Value |
|---|---|
| Endpoint | `https://api.groq.com/openai/v1/chat/completions` (OpenAI-compatible) |
| Model | `LLM_MODEL` env var, default `llama-3.3-70b-versatile` |
| API key | `GROQ_API_KEY` env var — **fail fast at startup if missing** (same pattern as Prompt 1's config loader; in staging/prod it comes from Azure Key Vault via managed identity) |
| Temperature | 0.2 (low variance; diagnosis is not a creative task) |
| Timeout | `PGAI_LLM__TIMEOUT_SECONDS`, default 30s |

### Fake mode (offline dev / CI / demo rehearsal)

`PGAI_LLM__FAKEMODE=true` swaps in `FakeScriptedLLMClient`:

- `PGAI_LLM__FAKE_RESPONSES` (JSON array) scripts exact replies, FIFO —
  useful for rehearsing the rejection/retry path;
- without it, the fake derives a plausible, **fully-cited** answer from the
  evidence actually embedded in the user prompt — it never invents
  citations that were not provided.

Every fake response is logged as FAKE. The compose default enables fake
mode so `docker compose up` works with no key; set it to `false` (and add a
key) to exercise real Groq inference. Citation enforcement is
provider-independent and is unit-tested against a mocked client, so the
safety guarantees hold in either mode.

## Swapping to Azure OpenAI later — the exact diff

1. Add `AzureOpenAIClient(LLMClient)` in `app/core/llm_client.py`
   implementing `generate(...)` against Azure's
   `/openai/deployments/{deployment}/chat/completions?api-version=...`
   endpoint (same message/tool wire format; auth via
   `api-key` header from Key Vault / managed identity instead of
   `Authorization: Bearer`).
2. In `get_llm_client()`, select it — e.g. `PGAI_LLM__PROVIDER=azure-openai`
   — replacing the Groq branch. Nothing else changes:
   - no agent code changes (they only see `generate(...)` / `LLMResponse`);
   - no prompt changes (the system prompt is provider-neutral);
   - no schema changes (`cited_evidence` shape stays identical);
   - the deterministic citation enforcement stays exactly where it is.
3. Remove/retire `GROQ_API_KEY` and `LLM_MODEL` (replaced by the Azure
   deployment name + Key Vault secret name); update `.env.example`,
   `docs/environment.md`, and the compose `environment:` block.
