# DSPy — Mechanism-Level Distillation for Lancelot

Source: `researchfms/teardowns/DSPY.md` (3 rounds, commit `da1f0871` round 1, `ccf375e` round 3).
Version: `3.2.1` (`dspy/__metadata__.py`). MIT license, ~51k LoC.

---

## What DSPy actually is

DSPy's primitive is the **Signature + Module + Teleprompter (compiler)**. It is not a prompting library — it is a LLM program compiler. The "programming not prompting" thesis: you declare *what* your program does (typed I/O fields), compose modules, give training data + metric, and the compiler generates + optimizes the prompts. The prompt is an *artifact of compilation*, not a file you maintain.

Without compilation DSPy is verbose for no gain. With compilation it can buy 5-15% accuracy gains over hand-written prompts (unverified reproduction; sourced from DSPy papers). Whether that tradeoff is worth the cost and complexity for a lean framework is the focus question below.

---

## Layer 1 — Signature (`dspy/signatures/signature.py`, 836 LoC)

A Pydantic `BaseModel` subclass with a custom metaclass (`SignatureMeta`). Declares:
- `InputField()` / `OutputField()` as class attributes with Python type hints.
- `instructions` (mutable docstring) — this is the string the compiler rewrites.
- Per-field: `desc` (shown in system message), `prefix` (literal starter text in the rendered prompt), `format`.

Two equivalent forms:
```python
class GenerateAnswer(dspy.Signature):
    """Answer questions with short factual responses."""
    context: list[str] = dspy.InputField(desc="passages that may contain the answer")
    question: str = dspy.InputField()
    answer: str = dspy.OutputField(desc="often between 1 and 5 words")

sig = dspy.Signature("context, question -> answer", "Answer questions with short factual responses.")
```

`input_fields` / `output_fields` are computed properties partitioning `model_fields` by the `__dspy_field_type__` JSON-schema marker. `signature.with_instructions(new_str)` produces a new Signature with a rewritten docstring — this is the mutation the compiler uses. Parser for string form: `_parse_signature` at `signature.py:615`.

**Lancelot byte-path lens:** `lancelot.tool` already emits `ToolSpec` with auto-derived JSON schema and `output_schema`. The Signature is richer (field-level `desc`, per-field `prefix`, mutable instructions) but the same core idea. The typed I/O contract for LLM calls is the transferable primitive — not DSPy's specific class hierarchy.

---

## Layer 2 — Adapter (`dspy/adapters/base.py`, `chat_adapter.py`, `json_adapter.py`)

The adapter is the `Signature ⇄ prompt` bijection. It turns `(Signature, demos, inputs)` into a chat-messages list and parses the completion back into a typed dict. Every subclass shares the same pipeline:

```python
# base.py:314
def __call__(self, lm, lm_kwargs, signature, demos, inputs):
    processed_sig = self._call_preprocess(...)   # native-tool field removal
    messages = self.format(processed_sig, demos, inputs)
    response = self._call_lm(lm, request)
    return self._call_postprocess(processed_sig, signature, outputs, lm, lm_kwargs)
```

`format_system_message` (shared by all adapters, `base.py:450`):
```
{field_description}    # "Your input fields are: 1. `x` (str): ...\nYour output fields are: ..."
{field_structure}      # ADAPTER-SPECIFIC — the output template
{task_description}     # signature.instructions (what MIPRO rewrites)
```

Demos are injected as user/assistant message pairs between the system message and the live user message.

### ChatAdapter — the default

`format_field_structure` emits:
```
[[ ## field_name ## ]]
{value}
[[ ## completed ## ]]
```

Parser: `field_header_pattern = re.compile(r"\[\[ ## (\w+) ## \]\]")` — splits response on markers, `parse_value(chunk, type_annotation)` per field. No `response_format` is set — the model is free-form; the markers are the only structure.

### JSONAdapter — the fallback (and strict path)

Used automatically when ChatAdapter parse fails (non-`ContextWindowExceeded` error). Also the explicit choice for structured-output capable models.

User messages still use `[[ ## field ## ]]` markers. Assistant side switches to strict JSON with `response_format` attached. The schema builder (`json_adapter.py:231-309`, function `_get_structured_outputs_response_format`):

```python
# Builds a Pydantic model from output fields, then rewrites its JSON schema to
# satisfy OpenAI Structured Outputs' hard requirements:
def enforce_required(part):
    if part.get("type") == "object":
        part["required"] = list(part.get("properties", {}).keys())  # ALL properties required
        part["additionalProperties"] = False                         # no extra keys
        for sub in part.get("properties", {}).values():
            enforce_required(sub)   # recurse into nested objects
    if part.get("type") == "array":
        enforce_required(part["items"])   # recurse into array items
    for d in part.get("$defs", {}).values():
        enforce_required(d)
```

Falls back to `response_format={"type": "json_object"}` (loose JSON mode) when:
- Any output field is annotated as `dict` (open-ended mapping — no fixed schema possible).
- Structured output raises.

Parse: `json_repair.loads(completion)` (tolerant of trailing commas / unquoted keys), then regex fallback `r"\{(?:[^{}]|(?R))*\}"` (recursive brace match via the `regex` module) to extract an embedded JSON object if the full completion isn't a dict, then `parse_value(v, annotation)` per field, strict keys check → `AdapterParseError`.

**The automatic ChatAdapter → JSONAdapter fallback chain** is a concrete production pattern: "try the free-form parse first; if the model ignored the markers, retry with forced JSON." Two network calls in the worst case.

### BAMLAdapter

Subclasses JSONAdapter. Renders Pydantic models as BAML-style simplified schema using `#` comments and braced blocks instead of full JSON Schema. More token-efficient for small models. `Literal["US", "CA"]` → `"US" or "CA"`. Optional → `T or null`. Cannot handle recursive Pydantic models (raises at construction).

### XMLAdapter

Parser regex: `re.compile(r"<(?P<name>\w+)>((?P<content>.*?))</\1>", re.DOTALL)`. For models biased toward XML.

### TwoStepAdapter

Stage 1: main (reasoning) LM with **no structured-output constraint** — just thinks freely.
Stage 2: a cheaper `extraction_model` (e.g. `dspy.LM("gpt-4o-mini")`) with ChatAdapter receives the free-form output and extracts the structured fields.

This is the explicit pattern for using o3/R1-class reasoning models (which struggle with structured outputs) while still getting typed outputs downstream. Two LM calls per Predict call.

**Lancelot byte-path lens:** `TransportPool` handles transport; the Adapter is the *message-shaping layer* above it. Lancelot currently owns transport but not message-shape translation. The Adapter abstraction — and especially the `ChatAdapter → JSONAdapter` fallback and the TwoStepAdapter reasoning-model extraction pattern — are the transferable primitives. They live between `TransportPool.request()` and the user's handler. The struct-output schema enforcer (`enforce_required`) is directly reusable.

---

## Layer 3 — Predict (`dspy/predict/predict.py`, 396 LoC)

The atomic LM call. `forward()` is 5 lines:
```python
lm, config, signature, demos, kwargs = self._forward_preprocess(**kwargs)
adapter = settings.adapter or ChatAdapter()
completions = adapter(lm, lm_kwargs=config, signature=signature, demos=demos, inputs=kwargs)
return self._forward_postprocess(completions, signature, **kwargs)
```

Load-bearing preprocessing detail: **auto-temperature override** (`predict.py:164-169`):
```python
if (temperature is None or temperature <= 0.15) and num_generations > 1:
    config["temperature"] = 0.7
```
When sampling `n > 1` at a deterministic temperature, the framework silently bumps to `0.7` to avoid mode collapse. This is a non-obvious constant that matters for BestOfN and all bootstrapping.

Security detail: `api_base`/`base_url`/`model_list` are stripped when loading state from disk unless `allow_unsafe_lm_state=True` (`predict.py:25-40`) — deserialization-injection mitigation.

During compilation, every Predict call appends `(self, kwargs, prediction)` to `dspy.settings.trace` — this is how BootstrapFewShot reads intermediate predictions.

---

## Layer 4 — Modules

### ChainOfThought (`predict/chain_of_thought.py`, 41 LoC)

Entirely defined as:
```python
extended_signature = signature.prepend(name="reasoning", field=OutputField(desc="${reasoning}"), type_=str)
self.predict = dspy.Predict(extended_signature, **config)
```

No "let me think step by step" prompt magic. The `[[ ## reasoning ## ]]` marker appears before `[[ ## answer ## ]]` in the response template; the model fills reasoning first naturally. The reasoning type is overridable (`list[str]`, Pydantic model, etc.).

### ReAct (`predict/react.py`, 236 LoC)

Two sub-signatures built at init:
- `react_signature`: original inputs + `trajectory: str (InputField)` + `next_thought: str` + `next_tool_name: Literal[tuple(tools.keys())]` + `next_tool_args: dict[str, Any]`.
- `fallback_signature` (for the final `extract` step): original inputs + all output fields + trajectory.

Verbatim system instruction (react.py:24-50):
```
You are an Agent. In each episode, you will be given the fields {inputs} as input.
And you can see your past trajectory so far.
Your goal is to use one or more of the supplied tools to collect any necessary information for producing {outputs}.

To do this, you will interleave next_thought, next_tool_name, and next_tool_args in each turn, and also when finishing the task.

After each tool call, you receive a resulting observation, which gets appended to your trajectory.

When writing next_thought, you may reason about the current situation and plan for future steps.
When selecting the next_tool_name and its next_tool_args, the tool must be one of:

(1) finish: Marks the task as complete. ...
(2) <tool_name>: <tool_desc>
    <tool_args description>
...

When providing `next_tool_args`, the value inside the field must be in JSON format
```

Loop (`react.py:95-118`):
- `max_iters = 20` default (constructor, line 17).
- `finish` tool is auto-injected, always listed as `(1)` in the enumeration.
- `next_tool_name` is typed as `Literal[tuple(tools.keys())]` — Pydantic validation rejects any unknown tool name at parse time.
- On exception in tool execution: `trajectory[f"observation_{idx}"] = f"Execution error in {name}: {err}"` — error is part of trajectory, loop continues.
- Trajectory truncation on context window exceeded: drop oldest 4 keys (`thought_i`, `tool_name_i`, `tool_args_i`, `observation_i`) as a unit, retry. Up to 3 attempts (`react.py:145-156`).
- After the loop: a separate `extract` sub-call (ChainOfThought over `fallback_signature`) translates the full trajectory into the user's actual output fields.

**Lancelot byte-path lens:** The ReAct loop itself (20-iter budget, finish-tool injection, trajectory-dict accumulation, error-to-observation, truncation) is pure Python loop logic — this maps to Lancelot's agent loop primitive. The `Literal`-constrained tool name (Pydantic validation → reject invalid tool at parse time) is directly reusable in the Lancelot model-adapter layer.

### Refine (`predict/refine.py`, 219 LoC)

N rollouts with `rollout_id = start..start+N-1` and `temperature=1.0`. Grades each by `reward_fn`. If threshold met, return immediately. Otherwise: invokes `OfferFeedback` signature (inputs: `program_code`, `modules_defn`, `program_inputs`, `program_trajectory`, `program_outputs`, `reward_code`, `target_threshold`, `reward_value`, `module_names`; outputs: `discussion: str`, `advice: dict[str, str]`) to generate per-module blame and concrete behavioral advice. The `advice` dict (keyed by module name, values = "specific mistakes + what to do differently") is injected into the next call's prompt.

This is structured self-improvement: not "retry on error message" but "generate per-component behavioral advice, inject as context."

---

## Compilation (Teleprompters)

The compiler mutates exactly two things per `Predict` in the student: `predictor.demos` (list of few-shot examples) and `predictor.signature.instructions` (the docstring). Everything else is fixed.

### BootstrapFewShot (`teleprompt/bootstrap.py`, 272 LoC)

**Constants** (`bootstrap.py:36`):
- `max_bootstrapped_demos = 4`
- `max_labeled_demos = 16`
- `max_rounds = 1`

**Algorithm:**
1. Run teacher on each training example with `dspy.settings.trace = []`.
2. Grade with `metric(example, prediction, trace)`.
3. If passes: for every `(predictor, inputs, outputs)` in trace, add `dspy.Example(augmented=True, **inputs, **outputs)` to that predictor's demo bucket.
4. Stop when `max_bootstrapped_demos = 4` accepted examples collected.
5. `predictor.demos = bootstrapped[:4] + random.sample(labeled, k=max_labeled_demos - 4)`.

**The trace trick (load-bearing):** when the top-level prediction is correct, every intermediate Predict call that produced it is assumed correct too. Multi-step programs auto-bootstrap demos for every component by grading only the end-to-end result. No intermediate labeling.

**Cache bypass for multi-round sampling** (`bootstrap.py:191`): `lm.copy(rollout_id=round_idx, temperature=1.0)`. `rollout_id` is a DSPy-specific key parameter that bypasses the request cache for sampling diversity. Rounds 2+ are always `temperature=1.0` (forced high-entropy). Importantly: `rollout_id` has no effect at `temperature=0` (a warning is emitted — `lm.py:140`).

**`metric_threshold`:** numeric float; if metric returns a value ≥ this, accept. If `None`, any truthy value accepts.

**`teacher_settings`:** passed as `dspy.context(**teacher_settings)` — lets you bootstrap with a stronger model (higher `max_tokens`, different `lm`) while compiling for a cheaper one.

### MIPROv2 (`teleprompt/mipro_optimizer_v2.py`, 866 LoC)

**Constants** (`mipro_optimizer_v2.py:42-50`):
```python
BOOTSTRAPPED_FEWSHOT_EXAMPLES_IN_CONTEXT = 3   # demos shown to proposer LM
LABELED_FEWSHOT_EXAMPLES_IN_CONTEXT = 0         # labeled examples NOT shown to proposer
MIN_MINIBATCH_SIZE = 50

AUTO_RUN_SETTINGS = {
    "light":  {"n": 6,  "val_size": 100},
    "medium": {"n": 12, "val_size": 300},
    "heavy":  {"n": 18, "val_size": 1000},
}
```

Constructor defaults (`mipro_optimizer_v2.py:67-79`):
- `max_bootstrapped_demos = 4`, `max_labeled_demos = 4` (NOT 16 — less than BootstrapFewShot)
- `seed = 9`
- `init_temperature = 1.0`

Compile defaults (`mipro_optimizer_v2.py:109-129`):
- `minibatch = True`, `minibatch_size = 35`, `minibatch_full_eval_steps = 5`
- `program_aware_proposer = True`, `data_aware_proposer = True`
- `view_data_batch_size = 10`, `tip_aware_proposer = True`, `fewshot_aware_proposer = True`

**Valset auto-split**: `min(1000, max(1, int(len(trainset) * 0.80)))` from the trainset tail (`:326`).

**num_trials formula** (`:280-285`):
```python
num_vars = len(program.predictors())
if not zeroshot_opt: num_vars *= 2   # both instruction and demo dimensions
num_trials = int(max(2 * num_vars * math.log2(num_candidates), 1.5 * num_candidates))
```
For `light` (n=6) with 1 predictor optimizing both: `num_vars=2`, `num_trials = max(2*2*log2(6), 1.5*6) = max(10.3, 9) = 10`. For 2 predictors: `num_vars=4`, `num_trials = max(20.67, 9) = 20`.

**Budget allocation** (`:312`): when optimizing demos+instructions, `num_fewshot_candidates = n`, `num_instruct_candidates = n // 2`. Demos get more budget because empirically more valuable.

**Three-stage pipeline:**
1. **Bootstrap**: `create_n_fewshot_demo_sets` = BootstrapFewShot with `n` different random seeds → `n` distinct demo sets per predictor.
2. **Propose instructions**: `GroundedProposer` generates `n//2` candidate instructions per predictor using a meta-LLM.
3. **Optuna TPE search**: `TPESampler(seed=9, multivariate=True)`. Search variables: per-predictor categorical `(instruction_idx, demo_idx)`. Objective = minibatch eval on 35 examples. Every 5 trials, full-eval the current best; inject result as synthetic trial to update the posterior.

The default program (no optimization) is evaluated first and injected as trial 0 (baseline for TPE). `_select_and_insert_instructions_and_demos` (`mipro_optimizer_v2.py:572`):
```python
chosen_instruct_idx = trial.suggest_categorical(f"{i}_predictor_instruction", range(num_instruct_candidates))
chosen_demo_idx = trial.suggest_categorical(f"{i}_predictor_demos", range(num_fewshot_candidates))
predictor.signature = predictor.signature.with_instructions(instruction_candidates[i][chosen_instruct_idx])
predictor.demos = demo_candidates[i][chosen_demo_idx]
```

### GroundedProposer (`dspy/propose/grounded_proposer.py`, 417 LoC)

**Tips bank** (`grounded_proposer.py:17-24`):
```python
TIPS = {
    "none": "",
    "creative": "Don't be afraid to be creative when creating the new instruction!",
    "simple": "Keep the instruction clear and concise.",
    "description": "Make sure your instruction is very informative and descriptive.",
    "high_stakes": "The instruction should include a high stakes scenario in which the LM must solve the task!",
    "persona": 'Include a persona that is relevant to the task in the instruction (ie. "You are a ...")',
}
```

`MAX_INSTRUCT_IN_HISTORY = 5` — show only the last 5 tried instructions with scores.

Three Signatures (the actual prompts the meta-LLM sees):
1. `DescribeProgram`: given `program_code` + `program_example` → `program_description`.
2. `DescribeModule`: given above + `module` (source of predictor i) → `module_description`.
3. `GenerateSingleModuleInstruction` (generated dynamically): given dataset_description + program context + `task_demos` (3 bootstrapped demos) + `previous_instructions` (last 5 with scores) + `basic_instruction` (current docstring) + `tip` (randomly sampled) → `proposed_instruction`.

`fewshot_aware_proposer=True` rotates the demo set shown to the proposer in cyclic order across proposal calls (`grounded_proposer.py:173-176`).

### COPRO (`teleprompt/copro_optimizer.py`, 355 LoC)

Constants: `breadth=10, depth=3, init_temperature=1.4`.

Optimizes two fields per predictor: `proposed_instruction` (docstring) AND `proposed_prefix_for_output_field` (the literal starter text before each output field in the prompt scaffold). MIPROv2 optimizes only instruction.

Verbatim signature for iterative refinement:
```python
class GenerateInstructionGivenAttempts(dspy.Signature):
    """You are an instruction optimizer for large language models. I will give some task 
    instructions I've tried, along with their corresponding validation scores. The instructions 
    are arranged in increasing order based on their scores, where higher scores indicate better 
    quality. Your task is to propose a new instruction that will lead a good language model 
    to perform the task even better. Don't be afraid to be creative."""
    attempted_instructions = dspy.InputField()
    proposed_instruction = dspy.OutputField(...)
    proposed_prefix_for_output_field = dspy.OutputField(...)
```

This "here are N attempts with scores, sorted by score; propose a better one" pattern is the cheapest optimizer to implement and a useful primitive in its own right.

### GRPO (`teleprompt/grpo.py`, 635 LoC)

RL fine-tuning of the underlying model weights. **Off-thesis for Lancelot** — no framework that does inference-time serving needs this. Skip unless building a training loop.

Constants for reference: `num_threads=6`, `num_train_steps=100`, `failure_score=0`, `format_failure_score=-1`.

---

## Evaluation harness (`evaluate/evaluate.py`, `utils/parallelizer.py`)

Every optimizer uses `Evaluate` to score candidate programs. The core:
```python
executor = ParallelExecutor(num_threads=num_threads, max_errors=...)
def process_item(example):
    prediction = program(**example.inputs())
    score = metric(example, prediction)
    return prediction, score
results = executor.execute(process_item, devset)
# failed item → failure_score = 0.0 (not an exception)
```

`ParallelExecutor` uses `ThreadPoolExecutor` with **per-thread `dspy.settings` copy** so concurrent evals don't clobber each other's LM configuration. `failure_score = 0.0` (`evaluate.py:81`) — a crashed prediction scores zero, not an exception that kills the run. `max_errors` budget aborts the run if too many items raise.

`Evaluate` is what makes minibatch scoring inside the optimizer work: `eval_candidate_program(batch_size, valset, candidate_program, evaluate, rng)` is a thin wrapper around this, sampling `batch_size` examples from valset with `rng`.

---

## Two-layer cache (`dspy/clients/cache.py`)

1. **In-memory**: `cachetools.LRUCache(maxsize=1_000_000)`.
2. **On-disk**: `diskcache.FanoutCache(directory=..., shards=16, timeout=10, size_limit=10*1024*1024)` — 10 MB default, 16 shards for parallel write performance.

Cache key: SHA256 over a transformed request dict. `_transform_value` rules:
- Pydantic model class → `model_json_schema()` (the *schema* hashes, not instance values).
- Pydantic model instance → `model_dump(mode="json")`.
- Callable → `inspect.getsource(value)` — changing a tool function's body busts the cache automatically.
- `api_key`, `api_base`, `base_url` are **excluded** from the cache key (so switching from dev to staging endpoint reuses cache).

`rollout_id` is the explicit cache bypass: any non-None value is included in the key, so different `rollout_id` values are different cache entries. Used by all bootstrap/refine loops to get diverse samples from the same prompt. WARNING: `rollout_id` has no effect at `temperature=0` (deterministic call — same input always produces same output).

`restrict_pickle=True` mode: deserializes from disk only types in `safe_types` — prevents RCE via planted cache files.

**Lancelot byte-path lens:** Lancelot has no request-level LLM cache. The two-layer LRU+diskcache pattern (SHA256 keyed, callable-source-hash for tool functions, explicit `rollout_id` bypass) is directly applicable to `TransportPool`. The "callable source → cache key" trick is specifically useful for `@app.batch` key functions and `@app.tool` handlers. The `restrict_pickle` pattern is a security primitive worth copying in any cache that persists to disk.

---

## Reasoning-model handling (`dspy/clients/lm.py:95`)

Verbatim regex:
```python
model_pattern = re.match(
    r"^(?:o[1345](?:-(?:mini|nano|pro))?(?:-\d{4}-\d{2}-\d{2})?|gpt-5(?!-chat)(?:-.*)?)$",
    model_family,
)
```

When matched:
- `temperature` must be `1.0` or `None` (raises otherwise).
- `max_tokens` must be `>= 16000` or `None`.
- Kwarg is renamed: `max_tokens` → `max_completion_tokens` (OpenAI API requirement for reasoning models).

**Lancelot byte-path lens:** `lancelot.upstream_metrics` does provider detection. The reasoning-model constraint check (temperature + max_tokens + kwarg rename) should live in the Lancelot model-adapter layer, not the user's code. Currently unbuilt.

---

## LM client defaults (`lm.py:29-42`)

- `model_type: Literal["chat", "text", "responses"] = "chat"` — the `"responses"` type routes to OpenAI Responses API.
- `temperature = None`, `max_tokens = None`, `cache = True`, `num_retries = 3`.
- `use_developer_role = False` — for Responses API, converts system → developer role.

LiteLLM-side caching is explicitly disabled when DSPy's own cache is on (double-caching prevention). Usage tracking: `settings.usage_tracker.add_usage(model, usage_dict)` when not a cache hit.

---

## Sandboxing (CodeAct / ProgramOfThought) — `dspy/primitives/python_interpreter.py`

**Correction from Round 1:** not `exec()` — the runtime is **Deno + Pyodide**. `PythonInterpreter` launches `deno run --allow-read=<allowlist> [--allow-write=...] [--allow-net=...] [--allow-env=...] runner.js`. Pyodide runs Python in WASM inside Deno. Triple isolation: WASM (no native syscalls), Deno capability flags, subprocess boundary. Thread-bound (`_check_thread_ownership`) — one interpreter per thread.

The `SUBMIT({"field": value})` preloaded function signals successful completion (runner.js). Code that never calls SUBMIT produces no output.

`CodeAct` exposes tools by extracting their source with `inspect.getsource(tool.func)` and exec-ing it inside Pyodide — the tool runs inside the sandbox.

**Lancelot byte-path lens:** if Lancelot builds a CodeAct-equivalent, the Deno+Pyodide approach (over Docker) is lighter for "run a math/data snippet" workloads. Not an immediate concern; this is deep Tier-D for a lean framework.

---

## Streaming (`dspy/streaming/messages.py`)

Two event types:
```python
@dataclass
class StreamResponse:
    predict_name: str       # which Predict module emitted this
    signature_field_name: str  # which output field
    chunk: str
    is_last_chunk: bool

@dataclass
class StatusMessage:
    message: str
```

`StreamingListener` parses `[[ ## field ## ]]` markers on the fly and routes per-field tokens to per-field listeners. Not raw streaming — field-routed streaming. `anyio.streams.memory.MemoryObjectSendStream` bridges sync DSPy modules to async consumers.

`StatusMessageProvider` has 6 overridable hooks: `tool_start_status_message`, `tool_end_status_message`, `module_start_status_message`, `module_end_status_message`, `lm_start_status_message`, `lm_end_status_message`.

**Lancelot byte-path lens:** Lancelot already has `@app.stream` with SSE keepalive, TTFT metric, and backpressure. The field-routed streaming (route tokens to the listener for the field currently being parsed) is a DSPy-specific protocol for its structured-output scaffold — not needed unless Lancelot adopts the `[[ ## field ## ]]` scaffold itself.

---

## Constants table (complete)

| Constant | Value | Source |
|---|---|---|
| `ReAct.max_iters` | **20** | `react.py:17` |
| `ReAct` trajectory truncation attempts | **3** | `react.py:145-156` |
| `BootstrapFewShot.max_bootstrapped_demos` | **4** | `bootstrap.py:42` |
| `BootstrapFewShot.max_labeled_demos` | **16** | `bootstrap.py:43` |
| `BootstrapFewShot.max_rounds` | **1** | `bootstrap.py:44` |
| `MIPROv2.max_bootstrapped_demos` | **4** | `mipro_optimizer_v2.py:67` |
| `MIPROv2.max_labeled_demos` | **4** (NOT 16) | `:68` |
| `MIPROv2.seed` | **9** | `:73` |
| `MIPROv2.init_temperature` | **1.0** | `:74` |
| `MIPROv2.minibatch_size` | **35** | `:121` |
| `MIPROv2.minibatch_full_eval_steps` | **5** | `:122` |
| `MIPROv2.view_data_batch_size` | **10** | `:125` |
| `MIPROv2 AUTO_RUN_SETTINGS["light"]` | n=6, val_size=100 | `:47` |
| `MIPROv2 AUTO_RUN_SETTINGS["medium"]` | n=12, val_size=300 | `:48` |
| `MIPROv2 AUTO_RUN_SETTINGS["heavy"]` | n=18, val_size=1000 | `:49` |
| `MIPROv2 BOOTSTRAPPED_FEWSHOT_IN_CONTEXT` | **3** | `:42` |
| `MIPROv2 LABELED_FEWSHOT_IN_CONTEXT` | **0** | `:43` |
| `MIPROv2 MIN_MINIBATCH_SIZE` | **50** | `:44` |
| `MIPROv2 num_trials formula` | `max(2*num_vars*log2(n), 1.5*n)` | `:280-285` |
| `MIPROv2 valset auto-split` | `min(1000, max(1, int(len(trainset)*0.80)))` | `:326` |
| `COPRO.breadth` | **10** | `copro_optimizer.py:65` |
| `COPRO.depth` | **3** | `:66` |
| `COPRO.init_temperature` | **1.4** | `:67` |
| `GroundedProposer.MAX_INSTRUCT_IN_HISTORY` | **5** | `grounded_proposer.py:15` |
| `Predict` auto-temp at `n>1` | if `temp <= 0.15`, bump to **0.7** | `predict.py:164-169` |
| `LM.num_retries` | **3** | `lm.py:42` |
| `LM.cache` | **True** | `lm.py:40` |
| Reasoning model regex | `^(?:o[1345](-mini\|-nano\|-pro)?(-\d{4}-\d{2}-\d{2})?\|gpt-5(?!-chat)(-.*)?)$` | `lm.py:96` |
| Reasoning model temp constraint | must be 1.0 or None | `lm.py:101` |
| Reasoning model max_tokens constraint | must be >= 16000 or None | `lm.py:101` |
| Reasoning model kwarg rename | `max_tokens` → `max_completion_tokens` | `lm.py:106` |
| Cache LRU maxsize | **1,000,000** entries | `cache.py:54` |
| Cache diskcache size | **10 MB** | `cache.py:53` |
| Cache diskcache shards | **16** | `cache.py:84` |
| `Evaluate.failure_score` | **0.0** | `evaluate/evaluate.py:81` |
| `ChatAdapter` parser regex | `r"\[\[ ## (\w+) ## \]\]"` | `chat_adapter.py:211` |
| `JSONAdapter` json repair | `json_repair.loads` + `r"\{(?:[^{}]\|(?R))*\}"` | `json_adapter.py:149,168` |
| `JSONAdapter` schema invariants | `required=[all keys]`, `additionalProperties=False` (recursive) | `json_adapter.py:286-287` |
| `JSONAdapter` open-dict downgrade | `response_format={"type":"json_object"}` | `:247-250` |
| `XMLAdapter` parser regex | `r"<(?P<name>\w+)>((?P<content>.*?))</\1>"` (DOTALL) | `xml_adapter.py:15` |
| `ProgramOfThought.max_iters` | **3** | `program_of_thought.py:30` |
| `CodeAct.max_iters` | **5** | `code_act.py:19` |
| `Refine` rollout temperature | **1.0** | `refine.py:51` |
| `GRPO.num_threads` | **6** | `grpo.py:34` |
| `GRPO.num_train_steps` | **100** | `:35` |
| `GRPO.failure_score` | **0** | `:42` |
| `GRPO.format_failure_score` | **-1** | `:43` |
| `PythonInterpreter` runtime | Deno + Pyodide subprocess | `python_interpreter.py:87,154` |

---

## Honest verdict on "programming not prompting"

**Is the optimizer worth it for a lean framework?**

**Short answer: the optimizer itself is off-thesis for Lancelot; the building blocks it runs on are high-value.**

The longer case:

- The Teleprompter (MIPROv2/BootstrapFewShot) is a **developer-time workflow** — you run it once before deployment, pay in LLM calls (light mode: maybe $1-5), get a module with baked-in instructions and demos. The result is a static artifact you ship. This is not a per-request or per-tenant cost.
- For the **"lean framework"** positioning, this is correctly scoped: Lancelot does not need to build an optimizer engine. But it should be able to *serve* applications that were compiled with DSPy — which just means accepting arbitrary prompts via `TransportPool`, which it already does.
- The "programming not prompting" thesis **does deprecate on a ~1-2yr model clock**: stronger models need less instruction engineering and fewer demos. MIPROv2 is already less valuable against o3 than against GPT-3.5. GRPO (RL fine-tuning of weights) is arguably the post-optimizer direction. The Signature-as-typed-contract and Adapter-as-message-shaper layers are the durable parts.
- **What IS durable (and worth extracting):** (1) the typed Signature as the I/O contract for any LLM call — it makes the structured-output path deterministic and auditable; (2) the Adapter pattern (message shaping → format-agnostic swap) — especially `ChatAdapter → JSONAdapter` automatic fallback; (3) `json_repair.loads` + recursive brace regex for tolerant JSON parse; (4) the `enforce_required` schema rewriter for OpenAI Structured Outputs; (5) the TwoStepAdapter "reasoning model + extraction model" split; (6) the BootstrapFewShot trace trick (end-to-end grading labels intermediate steps) — directly usable for Lancelot eval harness design; (7) the `rollout_id` cache bypass pattern.
- **The optimizer signatures themselves** (COPRO's `GenerateInstructionGivenAttempts`, GroundedProposer's `DescribeProgram`/`GenerateSingleModuleInstruction`) are worth copying verbatim if Lancelot ever builds an eval/iteration harness. They are the most-tested meta-prompts for instruction optimization in the open-source ecosystem.

---

## Anti-patterns to avoid

1. **Adopting the `[[ ## field ## ]]` scaffold as Lancelot's transport protocol.** This is DSPy's internal wire format, tightly coupled to its parser and adapters. Lancelot's SSE/JSON streaming is cleaner. The scaffold is an output format, not a transport protocol.
2. **Building the Teleprompter (MIPROv2) as a runtime component.** It's a developer-time compiler. Putting it in the hot path (per-request instruction optimization) would be ~$0.05-5/compile × #requests. Only makes sense offline.
3. **The GRPO RL fine-tuning path.** Off-thesis entirely — this is about modifying model weights, not the byte path.
4. **`exec()`-based code sandboxing.** DSPy corrected this to Deno+Pyodide; any Lancelot CodeAct equivalent should do the same or use container isolation.
5. **LiteLLM as a hard dependency.** DSPy vendors it as `lm.py` (596 LoC thin wrapper). Lancelot already has `TransportPool` doing the same job at the HTTP layer. Adding LiteLLM would violate the `pydantic-only` runtime dep rule.
