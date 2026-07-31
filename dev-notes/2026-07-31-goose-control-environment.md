# Goose control-environment policy

This note records the controls relevant to reproducible, sandboxed Goose CLI runs.
It is based on the local Goose reference working tree at
`ee61c7c499dbf08786a75948d949639cbab14150`, with this project's
history-provenance patch applied. Source paths below are relative to that Goose tree.

The main recommendation is to launch Goose from a small, constructed environment,
not the caller's complete environment. A few Goose defaults are actively unsuitable
for this project, while model capacity and timeouts must be selected for each provider
run. Everything else should remain absent unless a test names it as part of the test
contract.

## Force for every project-managed Goose process

| Control | Project policy | Reason and source |
| --- | --- | --- |
| `GOOSE_PATH_ROOT` | Set to a fresh or deliberately selected **absolute** run directory. | This is the supported root override; there is no `GOOSE_HOME` lookup. It relocates config, data, state, user plugins, and agents. Relative values are ignored. See `crates/goose/src/config/paths.rs`, `Paths::path_root` and `Paths::get_dir`. |
| `GOOSE_TOOL_PAIR_SUMMARIZATION` | Force `false`, overriding an inherited value. | `tool_pair_summarization_enabled()` uses `unwrap_or(true)`, so the feature is on by default. It asynchronously archives persisted tool request/response rows and inserts generated user-role summary rows. See `crates/goose/src/context_mgmt/mod.rs`, `tool_pair_summarization_enabled`, `maybe_summarize_tool_pairs`, and `summarize_tool_call`; and `crates/goose/src/agents/agent.rs`, `reply_stream`. |
| `GOOSE_TOOLSHIM` | Force `false`. | The project requires the selected model's native tool calls. Enabling the shim introduces a second interpreter path and potentially a second model. The unset default is false; forcing it prevents inheritance. See `crates/goose/src/model_config.rs`, `get_goose_toolshim` and `base_model_config_from_user_config`. |
| `GOOSE_DISABLE_KEYRING` | Set `true`. | Test credentials and state must stay in the isolated root rather than a host keyring. Presence of this environment key disables the keyring, irrespective of its textual value. See `crates/goose/src/config/base.rs`, `Config::default`. |
| `GOOSE_TELEMETRY_ENABLED` | Set `false`. Also set `GOOSE_TELEMETRY_OFF=true` when compatibility with older launch configurations matters. | Telemetry is opt-in in this tree, but an explicit negative value prevents an isolated config or inherited setting from enabling it. `GOOSE_TELEMETRY_OFF` is a separate hard opt-out recognized for `1` or `true`. See `crates/goose/src/posthog.rs`, `get_telemetry_choice`. |
| `GOOSE_DISABLE_SESSION_NAMING` | Set `true`. | Automated runs should not make an auxiliary model request merely to name a session. See `crates/goose/src/config/base.rs`, `GOOSE_DISABLE_SESSION_NAMING`, and `crates/goose/src/agents/agent.rs`, the session-naming branch in `reply`. |
| `CONTEXT_FILE_NAMES` | Set to the JSON array `[]` for controlled tests. | The default loads `.goosehints` and `AGENTS.md` from the host working tree and may later load subdirectory hints based on tool arguments. That is both nondeterministic prompt input and a host-to-model data path outside the sandbox MCP. Enable only explicitly curated files in a test that intends to exercise hints. See `crates/goose/src/hints/load_hints.rs`, `get_context_filenames` and `SubdirectoryHintTracker`. |
| `GOOSE_MODE` | Set `auto` for noninteractive tests, but only after verifying the exposed tool inventory. | Approval modes can block a headless driver. `auto` is safe here only because the launch contract exposes the sandbox extension and no host tool. The tool-inventory oracle is therefore part of this setting's safety case. See `crates/goose/src/config/base.rs`, `GOOSE_MODE`, and `crates/goose-cli/src/cli.rs`, `ExtensionOptions`. |
| `GOOSE_MAX_TOOL_RESPONSE_SIZE` | Set above the maximum response that the sandbox MCP itself permits. | Goose spills larger text to a host temporary file and returns that host pathname to the model. Such a file is outside the sandbox filesystem and bypasses the intended response contract. Bound output in the MCP first, then choose a Goose threshold that prevents this fallback. See `crates/goose/src/agents/large_response_handler.rs`, `process_tool_response`. |

The launcher should also remove these inherited variables rather than assign them a
generic value:

- `GOOSE_ADDITIONAL_CONFIG_FILES`, which adds configuration files before the isolated
  user config;
- `GOOSE_SYSTEM_PROMPT_FILE_PATH`, which replaces the system prompt from a host file;
- `GOOSE_MOIM_MESSAGE_TEXT` and `GOOSE_MOIM_MESSAGE_FILE`, which inject content on
  every turn;
- `GOOSE_STATUS_HOOK`, which is executed through `sh -c` on Unix;
- `GOOSE_SEARCH_PATHS` and `GOOSE_WORKING_DIR`, because this project uses absolute
  extension executables and an intentional process working directory; and
- proxy variables unless the selected provider explicitly requires them. For a direct
  local or private provider, an inherited proxy is an unintended prompt and credential
  egress path; use a deliberate `NO_PROXY` value when necessary.

Evidence for configuration merging and prompt/status overrides is in
`crates/goose/src/config/base.rs` (`Config::default`),
`crates/goose-cli/src/session/builder.rs` (`configure_session_prompts`),
`crates/goose/src/agents/moim.rs`, and
`crates/goose-cli/src/session/output.rs` (`run_status_hook`). Note that
`GOOSE_PATH_ROOT` does **not** suppress `/etc/goose/config.yaml`; Goose always places
the system config first in its merge list. Explicit environment invariants above take
precedence over it.

## Set explicitly for each test or provider run

| Control | Recommendation | Reason and source |
| --- | --- | --- |
| `GOOSE_PROVIDER`, `GOOSE_MODEL` | Select both for every run, preferably with matching CLI flags, and record them in the artifact manifest. | Avoid dependence on mutable isolated-config defaults. Provider/model selection is materialized in `crates/goose/src/model_config.rs` and the provider registry. |
| `GOOSE_CONTEXT_LIMIT`, `GOOSE_INPUT_LIMIT` | Set both to the same verified usable context size for an unknown or custom Ollama model. Do not set either from marketing metadata alone. | The former drives Goose bookkeeping and compaction; the latter becomes Ollama `options.num_ctx`. Keeping them equal prevents Goose from retaining more context than the loaded runner accepts. See the capacity analysis below. |
| `GOOSE_MAX_TOKENS` | Set an output budget appropriate to the test. | It limits each model response and maps to Ollama `num_predict`. An uncatalogued model otherwise falls back to 4,096 output tokens in `ModelConfig::max_output_tokens`. See `crates/goose-provider-types/src/model.rs` and `crates/goose-providers/src/ollama.rs`, `apply_ollama_options`. |
| `GOOSE_TEMPERATURE` | Set `0` for strict-oracle tests; choose and record another value for behavioral tests. | Unset means provider/model behavior decides it. See `crates/goose/src/model_config.rs`, `get_goose_temperature`. |
| `GOOSE_AUTO_COMPACT_THRESHOLD` | Make this a declared test dimension: use `0` for tests that exclude full compaction, or a chosen value such as `0.8` for normal long sessions. | Full compaction is separate from tool-pair summarization. Source disables it for thresholds `<= 0` **or** `>= 1`, and otherwise compacts only when usage ratio exceeds the threshold. See `crates/goose/src/context_mgmt/mod.rs`, `check_if_compaction_needed`. |
| `OLLAMA_HOST` | Set and validate the exact origin per run. | The default is localhost, and URL/port normalization is provider-specific. See `crates/goose/src/providers/ollama_def.rs`, `from_env`. |
| `OLLAMA_TIMEOUT`, `OLLAMA_STREAM_TIMEOUT` | Set finite values suited to model-loading and generation speed, and also retain an outer driver timeout. | The request timeout defaults to 600 seconds. Per-chunk precedence is `OLLAMA_STREAM_TIMEOUT`, then `GOOSE_STREAM_TIMEOUT`, then `OLLAMA_TIMEOUT`, then 120 seconds. These do not bound the entire multi-turn process. See `crates/goose/src/providers/ollama_def.rs`, `resolve_ollama_chunk_timeout`, and `crates/goose-providers/src/ollama.rs`, `with_line_timeout`. |
| turn and repetition limits | Prefer the CLI's `--max-turns` and `--max-tool-repetitions` on every automated invocation. | `GOOSE_MAX_TURNS` otherwise defaults to 1,000, while identical-call repetition has a CLI control. See `crates/goose/src/agents/agent.rs`, `DEFAULT_MAX_TURNS`, and `crates/goose-cli/src/cli.rs`, `SessionOptions`. |

Set `GOOSE_THINKING_EFFORT` only for a model/test that supports and needs a fixed
reasoning mode; otherwise leave it absent. If auxiliary calls are deliberately enabled,
also pin `GOOSE_FAST_MODEL`. With tool-pair summarization and session naming disabled,
there should be no incidental fast-model call in the CLI workflow under test.

## Leave at the Goose default

- Leave `GOOSE_TOOL_CALL_CUTOFF` unset. It is not a disable switch and is irrelevant
  when `GOOSE_TOOL_PAIR_SUMMARIZATION=false`; setting it to zero would make summaries
  eligible sooner if the feature were accidentally re-enabled.
- Leave `GOOSE_PROVIDER_SKIP_BACKOFF=false`. The Ollama provider has a fixed transient-
  only retry policy of ten retries with jittered backoff. Skipping only the delays does
  not remove retries and can hammer a loading server. Use it only in a deliberately
  fail-fast fault-injection test. See `crates/goose-providers/src/ollama.rs`,
  `retry_config`, and `crates/goose-provider-types/src/retry.rs`.
- Leave `OLLAMA_STREAM_USAGE=true` unless testing an older incompatible server. It is
  the current default and preserves provider usage accounting. See
  `crates/goose/src/providers/ollama_def.rs`, `options_from_config`.
- Leave `GOOSE_STREAM_TIMEOUT` unset when the more specific Ollama timeout is set.
- Leave planner, subagent, gateway, recipe, OAuth, TLS-server, tool-shell, UI/theme,
  debug-display, and observability variables absent. Those subsystems are outside this
  harness contract. If one is introduced later, add it as an explicit test dimension
  rather than inheriting a host setting.
- Do not set `GOOSE_CONTEXT_STRATEGY`. It appears in documentation in this checkout but
  has no Rust reader under `crates/`; it does not control the current implementation.

The prompt-injection classifier variables should also remain at their default-disabled
state for now. A probabilistic classifier is not a sandbox boundary and may introduce
an external endpoint. Hostile commands must remain safe because the MCP implementation
and container policy enforce limits, not because a classifier approves their text.

## Why the observed rewrite was not context-window compaction

There are two independent mechanisms:

1. Full auto-compaction compares estimated or reported tokens with the provider context
   limit and the `GOOSE_AUTO_COMPACT_THRESHOLD` ratio.
2. Tool-pair summarization counts visible tool calls. Its default cutoff is
   `clamp(3 * (context_limit * threshold) / 20,000, 10, 500)`. At the 128,000-token
   fallback and `0.8` threshold, the cutoff is 15. Once eligible calls exceed that
   cutoff plus the fixed batch size of 10, Goose summarizes the oldest 10 pairs.

The sustained test crossed that call-count condition with 26 and then 28 visible tool
calls. The corresponding provider inputs were only about 21,000 and 39,000 tokens.
Thus the ten inserted summaries were not evidence that the model context was full;
they were the default background tool-pair mechanism firing well below the context
limit. The formula and its 128,000-token test case are in
`crates/goose/src/context_mgmt/mod.rs`, `compute_tool_call_cutoff` and
`tool_ids_to_summarize`.

This behavior is especially unsuitable for a session-filesystem oracle: it mutates
persisted visibility and inserts rows while the current provider request is already in
flight. Stable source-row paths remain correct, but disabling the mechanism removes
unrequested lossy history rewriting and ten auxiliary model calls per batch.

## The three context sizes that must agree

For an uncatalogued Ollama model, three independent values can diverge:

1. The model's native metadata may advertise, for example, roughly 256,000 tokens.
2. Goose uses `DEFAULT_CONTEXT_LIMIT = 128_000` when `ModelConfig.context_limit` is
   absent. This value drives context accounting and the tool-call cutoff.
3. The Ollama server may actually load a runner at, for example, 64,000 tokens.

The subtle point is that Goose's Ollama request builder calls
`options.input_limit.or(model_config.context_limit)`, using the optional field rather
than `ModelConfig::context_limit()`. When the model is uncatalogued and neither override
is set, the optional field is absent: Goose accounts as though 128,000 were available
but sends no `num_ctx`, leaving the server to select its runner default. See
`crates/goose-provider-types/src/model.rs`, `DEFAULT_CONTEXT_LIMIT` and
`ModelConfig::context_limit`; `crates/goose-providers/src/ollama.rs`,
`resolve_ollama_num_ctx`; and `crates/goose/src/agents/agent.rs`,
`prepare_reply_context`.

Therefore:

- preflight the model's native maximum and the server's actually loaded/requestable
  runner size;
- choose a value no larger than either capacity and practical memory limits;
- set **both** `GOOSE_CONTEXT_LIMIT` and `GOOSE_INPUT_LIMIT` to it;
- set and record `GOOSE_MAX_TOKENS` separately; and
- verify from request/server diagnostics that `num_ctx` and the loaded runner agree
  before interpreting a compaction event.

`GOOSE_INPUT_LIMIT` alone is unsafe because Goose may still account against 128,000.
`GOOSE_CONTEXT_LIMIT` alone currently also reaches Ollama as the fallback `num_ctx`, but
setting both makes the intended contract explicit and avoids relying on that coupling.

## Extension, profile, and hook controls not covered by environment variables

Every new CLI session must use `--no-profile` and exactly one absolute
`--with-extension` command. `collect_extension_configs` returns no configured profile
extensions for a new `--no-profile` session, but a resumed session takes its persisted
extension state first. Consequently, create sessions only under the isolated root and
verify the effective tool inventory on both creation and resume. See
`crates/goose-cli/src/session/builder.rs`, `collect_extension_configs`.

`--no-profile` does **not** disable plugin hooks. `Agent::new` constructs a
`HookManager` that scans enabled plugins in the current project and isolated user
plugin roots. Newly discovered plugins default to enabled, and command hooks execute
as host processes. There is no reviewed environment switch that disables this scan.
Until Goose has such a switch, run from a controlled directory with no
`.agents/plugins`, keep the isolated user plugin directory empty, and fail preflight if
either contains a plugin. See `crates/goose/src/agents/agent.rs`, `Agent::new`;
`crates/goose/src/hooks/mod.rs`, `HookManager::load`; and
`crates/goose/src/plugins/discovery.rs`, `discover_enabled_plugins` and
`filter_by_config`.

Finally, construct the environment passed to Goose and its inherited stdio extension
from an allowlist. This prevents unrelated API credentials, proxy controls, prompt
overrides, and executable-search paths from crossing into the MCP process. The
sandbox's own narrowly scoped configuration and `AGENT_SESSION_ID` are the only
additional values the extension should need.
