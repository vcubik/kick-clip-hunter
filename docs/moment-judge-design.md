# Moment judge: local-first second-stage scoring (design)

Status: proposed (not implemented). This documents the intended design for the
"ML" stage discussed on the roadmap, so implementation can be reviewed against
it. Numbers marked *initial* are starting points, expected to be tuned.

## Problem

The chat detector is deliberately high-recall / low-precision: it flags any
baseline-relative burst that *might* be a funny moment, and a clip is cut for
each one. A human then rates clips 1-5 on the dashboard. The detector cannot
tell a genuinely funny moment from e.g. a hype burst or an inside-joke ripple,
because it only sees message metadata - it never sees the clip.

## Goal

Add a second-stage **judge** that runs *only on the clips already cut* (tens
per day, not the 24/7 stream), looks at what actually happened in the clip,
and produces a structured quality judgment. It starts **advisory** - shown in
the dashboard next to the human rating - and is only promoted to filtering /
ranking once its agreement with human ratings is measured and acceptable.

## Non-goals

- **No hosted-API dependency.** The judge must be fully functional with local
  models only (explicit requirement). A hosted backend may exist behind the
  same interface as an optional accuracy baseline, default off.
- **No training on YouTube-scraped highlight clips** - no aligned chat exists
  for them, they're post-edited, and the licensing is murky.
- **No continuous stream watching.** The chat detector stays the trigger;
  the judge only ever sees already-cut clips.
- **No auto-publishing to social platforms.** The social-upload feedback loop
  (views/completion-rate as an outcome signal) is a separate future design;
  any publishing will be human-gated regardless.
- **Not replacing the chat detector or its tuning.**

## Where it sits

```
chat burst -> moment row -> clip cut from rolling buffer   (existing)
                                |
                                v
                        judge queue (in-process, concurrency 1)
                                |
        1. transcribe clip audio (faster-whisper, local)
        2. extract N frames (ffmpeg)          [frames mode only]
        3. build prompt: transcript + chat snippet
           + detector reasons/score/stream context
        4. backend.judge(...) -> structured JSON
        5. persist: transcript on moment, judgment row
                                |
                                v
                dashboard: judge verdict next to 1-5 rating buttons
```

The judge is asynchronous and advisory: a judge failure must never block or
delay detection, clip cutting, or the dashboard. Heavy work (whisper, model
inference) runs off the event loop (thread/subprocess), single-concurrency so
it never competes hard with the recorders - the host is a 4-core machine.

## Judge contract

One call per clip. Input assembled by us; output is constrained JSON
(Ollama structured outputs / JSON schema) so parsing is trivial:

```json
{
  "funny_score": 1-5,        // same scale as the dashboard rating
  "viral_potential": 1-5,    // would this land with someone who doesn't know the streamer?
  "self_contained": bool,    // understandable without prior stream context?
  "category": "funny | fail | hype | music | argument | boring | other",
  "reasoning": "1-3 sentences, English"
}
```

`funny_score` deliberately shares the dashboard's 1-5 scale so correlation
against human ratings needs no mapping. Prompt inputs:

- **Transcript** (Czech) - what the streamer actually said. For stream comedy
  this carries most of the signal; it is the judge's primary evidence.
- **Chat snippet** - the burst the detector saw (already stored per moment).
- **Detector metadata** - reasons, score, message rates, stream elapsed time.
- **Frames** (frames mode only) - N uniformly sampled, downscaled frames.

## Backends

Pluggable `JudgeBackend` with one method (`judge(inputs) -> Judgment`).
Selected by config (`JUDGE_BACKEND=ollama|anthropic|off`, default `ollama`).

### `ollama` (default, local)

Talks to a local Ollama server (`OLLAMA_URL`, default `http://localhost:11434`),
model from `JUDGE_MODEL`. Structured outputs enforce the contract.

What fits where (RAM figures are for Q4-ish quants):

| Hardware tier                      | Model                        | Mode      | Expectation |
|------------------------------------|------------------------------|-----------|-------------|
| **Current box** (i7-7700, 16 GB RAM, no GPU) | `qwen3:4b` / `gemma3:4b`     | text-only | minutes/clip on CPU; workable at tens of clips/day, weakest judgment quality |
| same, stretching                    | `gemma3:12b` (8.1 GB)        | text-only | slower, meaningfully better language understanding; RAM-tight next to recorders + browser |
| 12 GB GPU (e.g. used RTX 3060)      | `qwen3-vl:8b` / `gemma3:12b` | frames    | seconds/clip; realistic sweet spot for this project |
| 16-24 GB GPU                        | `gemma3:27b` QAT / `qwen3-vl:30b-a3b` | frames | best local quality; 27B QAT fits ~14 GB |

Honest expectation-setting: the judgment task is "is this Czech-language
stream moment funny and would it travel" - subtle, culture-specific, and
exactly where small models are weakest. A 4B model judging Czech humor from a
transcript will be noisy. That is acceptable *because the design measures
agreement before trusting it* (see Evaluation) - but nobody should expect
frontier-level judgment from the current hardware. If the measured agreement
is poor, the options are: bigger model (GPU purchase), hosted baseline for
comparison, or leaning harder on the learned-classifier path below.

### `anthropic` (optional, default off)

Same contract via the Claude API (vision input = the same frames). Exists so
that, if desired, a frontier model can be run over the *same* clips to answer
"is the local model the bottleneck, or is the task just hard?". Requires an
API key; nothing in the pipeline may require this backend to be configured.

### Transcription

`faster-whisper`, local, `language=cs`. Start with `small` int8 on CPU
(50 s clip transcribes well under a minute), upgrade to `medium` /
`large-v3-turbo` if Czech quality on real clips demands it and hardware
allows. Transcription runs even if the judge backend is `off` - transcripts
are independently useful (search, future classifier features).

## Text-only vs frames mode

`JUDGE_MODE=text_only|frames` (default `text_only` on current hardware).

Rationale: speech + chat reaction carry most of the comedy signal; frames add
visual-gag context at a steep local-compute cost (each image is hundreds of
tokens through a CPU-bound VLM). Frames mode: ~6 frames, uniform sampling
(`ffmpeg -vf fps=...`), downscaled to ≤512 px, written to a temp dir and
deleted after the call. Whether frames actually improve rating-agreement is
an empirical question the evaluation below can answer - don't pay for them
by default until they demonstrably help.

## Storage

Applied as idempotent `ALTER TABLE` / `CREATE TABLE IF NOT EXISTS` in
`get_connection()`, matching the existing convention:

- `moments.transcript TEXT NULL` - the whisper transcript (belongs to the
  clip, not to any particular judgment).
- New table `moment_judgments` - separate table rather than columns on
  `moments`, so a clip can be re-judged (new model, new prompt) without
  losing history, and so two backends can be compared on the same clips:

```sql
CREATE TABLE IF NOT EXISTS moment_judgments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    moment_id INTEGER NOT NULL,
    backend TEXT NOT NULL,          -- "ollama" | "anthropic"
    model TEXT NOT NULL,            -- e.g. "gemma3:4b"
    mode TEXT NOT NULL,             -- "text_only" | "frames"
    funny_score INTEGER NOT NULL,
    viral_potential INTEGER NOT NULL,
    self_contained INTEGER NOT NULL,
    category TEXT NOT NULL,
    reasoning TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
```

## Dashboard

Each moment card gains a judge line (latest judgment): score, category, and
the short reasoning, visually distinct from the human rating so the two are
never conflated. Unjudged moments show nothing (or "judging…" if queued).
Later, once enough pairs exist: a small agreement view (judge score vs own
rating) to make the promotion decision visible instead of vibes-based.

## Failure semantics

- Judge queue is in-process and best-effort: on restart, unjudged clips are
  picked up by a backfill sweep rather than a persistent queue.
- One retry on backend failure, then mark the moment unjudged and move on.
- `scripts/judge_moment.py <moment_id> | --all-unjudged` for manual runs,
  backfilling old clips, and re-judging after a model/prompt change.
- Ollama being down = log once per sweep, skip; never crash the app.

## Evaluation and promotion

Phase A (advisory) accumulates `(human rating, judge score)` pairs simply by
the user continuing to rate clips as they already do.

Once **n ≥ 50** rated-and-judged moments exist (*initial*), compute:

- Spearman correlation between judge `funny_score` and human rating;
- precision/recall of `judge ≥ 4` against `rating ≥ 4`.

Promotion gates (*initial*): correlation **≥ 0.5** and precision@≥4 **≥ 0.7**
before the judge is allowed to do anything besides display - e.g. sorting
moments by judge score, collapsing `judge ≤ 2` moments, or skipping clip
retention for them. Until then it stays a labeled opinion in the UI.

The same harness answers model-selection questions empirically: run two
backends/models over the same clips (multiple `moment_judgments` rows per
moment) and keep whichever agrees with the human more.

## Relationship to the learned-classifier path

The judge is not the end state. As rated data accumulates (hundreds of
examples), a small trained classifier - detector features + transcript
embeddings, no VLM at inference - becomes feasible, fully local and far
cheaper than any LLM judge. The judge bridges the gap (useful signal from
day one, no training data needed) and its stored judgments/transcripts become
features and weak labels for that later model. Few-shot personalization
(injecting a handful of the user's own rated examples into the judge prompt)
is a cheap intermediate step worth trying once ratings exist.

## Open questions

- **GPU**: a ~12 GB GPU changes the default from "4B text-only" to "8B
  vision + fast whisper" and is the single highest-leverage hardware change
  if the local judge underperforms.
- Per-channel language hint (some watched channels may be Slovak rather than
  Czech) - whisper and the prompt both accept it; not needed for v1.
- Whether `music` category judgments should feed back into detector tuning
  (e.g. corroborating the dance-emote exclusion) - out of scope for v1.
