# ADR Naming Conventions in XTM One Python Backend

Status: Approved
Scope: `backend/`
Reviewed in refinement: 2026-04-28

## Context

The XTM One backend is a large Python codebase structured around FastAPI routes, Pydantic schemas, SQLAlchemy models, services, integrations, channels, and agent execution logic.

The same business concepts appear across multiple layers of the backend and need stable names to remain easy to navigate, review, refactor, and extend.

Contributors increasingly use LLM-based coding assistants for generation, refactoring, and reasoning tasks. Identifier quality therefore matters both for human maintainability and for AI-assisted development.

Recent research also shows that naming has a significant impact on LLM behavior. Unlike traditional compilers, LLMs rely heavily on natural-language signals embedded in code, especially identifier names:

- Naming quality directly affects code analysis performance, as models rely on identifier semantics.
- Removing identifier names severely degrades intent understanding, even when code structure is unchanged.
- Descriptive naming improves code completion accuracy compared to minimal or obfuscated naming.

Without an explicit convention, the backend risks drifting further toward generic identifiers, inconsistent acronym usage, and uneven naming across models, schemas, routes, and services.

## Decision

Adopt a semantic-first naming convention for all identifiers in the Python backend, with the following principles.

### Respect standard Python naming shapes

- modules, functions, variables, and fields use `snake_case`
- classes use `PascalCase`
- constants use `UPPER_SNAKE_CASE`

### Descriptive, multi-word identifiers are the default

```python
user_profile
llm_provider_secret_key_encoded
database_connection_pool
```

### Context is encoded only when it adds disambiguation

```python
UserRecord
DatabaseUser
PersistedOrder
```

By context, we mean surrounding information such as the module, class, or domain layer.
Avoid repeating information in the identifier when that surrounding context already makes the meaning clear.

### Domain vocabulary is preferred over generic terms

```python
jwt_access_token
assignment_run
agent_response
```

Technology or vendor names are appropriate when the boundary is explicitly technology-specific:

```python
postgres_event_store
slack_channel_id
redis_client
github_webhook_secret
```

They should not leak into domain names when the abstraction should stay technology-neutral. Prefer `storage`, `repository`, or `channel_id` at domain boundaries unless the implementation detail is the point of the identifier.

### Avoid low-signal or misleading naming

```python
# Avoid
data = ...
x = ...
process(value)

# Prefer
agent_description = ...
retry_backoff_seconds = ...
compute_time_to_first_token(execution_context)
```

### Avoid encoding type/system noise

```python
# Avoid
str_username
list_users

# Prefer
user_name
users
```

When a conversion is the point of the code, name the resulting representation or purpose, not the raw primitive type. Prefer `serialized_bundle` or `bundle_text` over `string_bundle`.

### Optimize for tokenization clarity

```python
# Avoid
integ # 2 tokens with OpenAI tokenizer
float2str  # still 3 tokens and lacks semantic clarity

# Prefer
integration
number_to_text
```

Avoid cryptic abbreviations such as `cfg`, `svc`, or `ctx`.
Preserve canonical acronym vocabulary consistently (but apply PEP 8 casing based on the identifier kind ; for example: llm_provider, MCPServer, OAuthClient, SSOConfig, and JWT_SECRET).

### Ensure consistency across the codebase

```python
get_user()
create_user()
delete_user()
```

For instance, avoid mixing `CreateX` and `XCreate`.

At the API boundary, prefer one schema family:

- `Create<Resource>Request`
- `Update<Resource>Request`
- `<Resource>Response`

## Rationale

### LLM performance depends on naming

LLMs rely heavily on identifier names as a primary semantic signal. Poor naming reduces performance in code completion, reasoning, and analysis tasks.

### Naming encodes intent, not just structure

Code communicates through two channels:

- structural logic
- human-readable naming

Removing naming significantly degrades intent-level understanding, even when behavior is unchanged.

### Descriptive naming improves accuracy

Empirical studies show descriptive identifiers outperform minimal or obfuscated naming in semantic similarity and completion quality.

### Tokenization effects matter

LLMs process code as token sequences. Identifier structure affects:

- token splitting
- embeddings
- semantic proximity
- pattern recognition

Poorly structured names reduce model effectiveness. Clear multi-token names also improve code search and lower the risk that humans or agents miss existing patterns and re-implement the same concept under a different label.

### Alignment with training data matters

LLMs are trained on real-world code, which predominantly uses:

- descriptive names
- multi-token identifiers
- natural language vocabulary

Following these patterns improves model alignment.

### Maintainability still benefits

The chosen approach aligns with established best practices for human readability while also improving LLM-assisted development, code review, navigation, and refactoring.

## Consequences

### Improvement

- Improved LLM-assisted development
- Better code generation, completion, and reasoning
- Better code comprehension for humans and machines
- Increased consistency across models, schemas, routes, and services
- Clearer boundaries between domain concepts and technology-specific adapters

### Risks

- Risk of redundancy if context is over-encoded
- Requires discipline and consistent review
- Some identifiers will become longer
- Old and new naming styles may temporarily coexist
- Large-scale cleanup may require dedicated refactoring effort

## Alternatives Considered

### Minimal or generic naming

```python
data = ...
entity = ...
process(x)
```

Rejected because it provides weak semantic signal and degrades both readability and LLM reasoning.

### Context-heavy naming everywhere

```python
EntityInMyAwesomePostgresDatabase
UserObjectFromRestApiResponse
```

Rejected because it introduces redundancy and increases token noise without proportional benefit.

### Hungarian or type-based naming

```python
strUserName
listUsers
```

Rejected because it adds low-value information, reduces semantic clarity, and duplicates what type hints already communicate.

### Ultra-compact or DSL-style naming

```python
cfg
ctx
fn
svc
```

Rejected because it is highly ambiguous and performs poorly for both humans and LLM interpretation.

## References

### Team inputs

- Refinement transcript: `[COORD] Naming convention in xtm one backend Refinement.docx`

### Generic

- [Naming convention (programming)](https://en.wikipedia.org/wiki/Naming_convention_(programming)) [consulted on April 27th, 2026]
- [LLM code generation quality overview](https://www.sonarsource.com/learn/llm-code-generation/) [consulted on April 27th, 2026]

### Engineering / implementation

- [GitHub tokenizer & BPE explanation](https://github.blog/ai-and-ml/llms/so-many-tokens-so-little-time-introducing-a-faster-more-flexible-byte-pair-tokenizer/) [consulted on April 27th, 2026]
- [Inside Copilot architecture (context + tokenization)](https://codetirtho97.medium.com/inside-github-copilots-architecture-how-ai-code-generation-actually-works-in-production-a907f7a7bdc3) [consulted on April 27th, 2026]

### System-level explanations

- [How AI code generation tools work](https://www.morphllm.com/ai-code-generation-tool) [consulted on April 27th, 2026]

### Model-level

- [OpenAI Codex overview](https://en.wikipedia.org/wiki/OpenAI_Codex_%28language_model%29) [consulted on April 27th, 2026]
- [GitHub Copilot technical background](https://en.wikipedia.org/wiki/GitHub_Copilot) [consulted on April 27th, 2026]

### Practical optimization report

- [How Does Naming Affect LLMs on Code Analysis Tasks?](https://arxiv.org/abs/2307.12488) [consulted on April 27th, 2026]
- [When Names Disappear: Revealing What LLMs Actually Understand About Code](https://arxiv.org/abs/2510.03178) [consulted on April 27th, 2026]
- [Variable Naming Impact on AI Code Completion: An Empirical Study](https://www.researchgate.net/publication/393939595_Variable_Naming_Impact_on_AI_Code_Completion_An_Empirical_Study) [consulted on April 27th, 2026]
