# Moment judge: second-stage clip scoring (design)

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

## Backend decision: Claude API (pay-per-use)

Judging Czech-language stream comedy - subtle, culture-specific, needs both
speech and visual context - is exactly where small local models are weakest.
A local-first design was considered and dropped: the host has no usable LLM
accelerator (AMD RX 480, 4 GB VRAM - no practical CUDA/ROCm path on Windows;
16 GB system RAM; 4-core CPU), so a local VLM would be both slow and low
quality. The judge therefore calls the **Claude API**.

Two clarifications, because they were the reason local-first was requested:

- This is the **Claude API** (a metered API key, billed per token), **not the
  Claude Pro subscription**. It is a separate, pay-as-you-go dependency.
- Only the **judge** depends on the network. Detection, recording, and clip
  cutting stay fully local - if the API is unreachable, clips are still
  captured and simply judged later (see Failure semantics).

The one component that stays local is **audio transcription** (Claude has no
audio input): `faster-whisper` runs on the CPU, which it handles fine - the
RX 480 is irrelevant to it.

## Non-goals

- **No training on YouTube-scraped highlight clips** - no aligned chat exists
  for them, they're post-edited, and the licensing is murky.
- **No continuous stream watching.** The chat detector stays the trigger;
  the judge only ever sees already-cut clips.
- **No auto-publishing to social platforms.** The social-upload feedback loop
  (views/completion-rate as an outcome signal) is a separate future design;
  any publishing will be human-gated regardless.
- **Not replacing the chat detector or its tuning.**

## Privacy tradeoff (conscious)

Sending clips to the API means clip **frames, the audio transcript, and the
chat snippet leave the machine** to a third party. The chat snippet contains
other viewers' usernames and messages. This is an accepted tradeoff of the
backend decision, but it is a real change from an all-local pipeline and is
noted here so it's a deliberate choice, not a silent one. Nothing sensitive
(keys, tokens) is ever part of a judge payload.

## Where it sits

```
chat burst -> moment row -> clip cut from rolling buffer   (existing, local)
                                |
                                v
                        judge queue (in-process, concurrency 1)
                                |
        1. transcribe clip audio (faster-whisper, local, CPU)
        2. extract N frames (ffmpeg)              [frames mode]
        3. build request: frames + transcript + chat snippet
           + detector reasons/score/stream context
        4. Claude API call -> structured JSON (tool-enforced)
        5. persist: transcript on moment, judgment row
                                |
                                v
                dashboard: judge verdict next to 1-5 rating buttons
```

The judge is asynchronous and advisory: a judge failure or a slow/unreachable
API must never block or delay detection, clip cutting, or the dashboard.
Transcription and frame extraction run off the event loop, single-concurrency
so they don't compete with the recorders on a 4-core box.

## Judge contract

One API call per clip. The JSON contract is enforced with a **tool definition**
(`tool_use` + `input_schema`), so the model must return exactly these fields
and parsing can't drift:

```json
{
  "funny_score": 1-5,        // same scale as the dashboard rating
  "viral_potential": 1-5,    // would this land with someone who doesn't know the streamer?
  "self_contained": true,    // understandable without prior stream context?
  "category": "funny | fail | hype | music | argument | boring | other",
  "reasoning": "1-3 sentences, English"
}
```

`funny_score` deliberately shares the dashboard's 1-5 scale so correlation
against human ratings needs no mapping. Request inputs:

- **Transcript** (Czech) - what the streamer actually said. For stream comedy
  this carries most of the signal; it is the judge's primary evidence.
- **Frames** - N uniformly sampled, downscaled frames (visual gags, fails,
  on-screen events the transcript misses).
- **Chat snippet** - the burst the detector saw (already stored per moment).
- **Detector metadata** - reasons, score, message rates, stream elapsed time.

The static rubric/instructions go in the system prompt and are **prompt-cached**,
so only the per-clip content is billed at the full rate.

## Model choice and cost

Config selects the model (`JUDGE_MODEL`). Default is **`claude-opus-4-8`** -
start at the quality ceiling so the evaluation (below) measures whether the
*approach* works, not whether a cheap model is holding it back. Once the judge
demonstrably agrees with human ratings, dropping to a cheaper model is a
data-backed decision, not a guess.

Rough cost per clip (frames mode, ~6 downscaled frames + transcript + chat +
cached rubric, ~11 K input / ~300 output tokens):

| Model                        | ~ per clip | ~ per month @ 50 clips/day |
|------------------------------|-----------:|---------------------------:|
| `claude-opus-4-8` (default)  |     ~$0.07 |                      ~$100 |
| `claude-sonnet-5`            |     ~$0.04 |                       ~$60 |
| `claude-haiku-4-5-20251001`  |     ~$0.01 |                       ~$20 |

These scale linearly with volume, and *moments are meant to be rare* - real
volume is likely well under 50/day, pulling cost down proportionally. To
economize, set `JUDGE_MODEL` to Sonnet 5 or Haiku 4.5; the evaluation harness
will show whether the cheaper model still tracks human ratings closely enough.
Text-only mode (below) also cuts input tokens roughly in half.

## Backend abstraction

A thin `JudgeBackend` interface (`judge(inputs) -> Judgment`) selected by
`JUDGE_BACKEND` (default `anthropic`; `off` disables judging but still stores
transcripts). The interface exists so a local backend could be added later if
the hardware changes, without touching the rest of the pipeline - but the only
implemented backend is Anthropic. `ANTHROPIC_API_KEY` lives in `.env`
(gitignored) alongside the Kick credentials, with a blank entry added to
`.env.example`.

### Transcription

`faster-whisper`, local, `language=cs`. Start with `small` int8 on CPU
(a 50 s clip transcribes in well under a minute), upgrade to `medium` /
`large-v3-turbo` if Czech quality on real clips demands it. Transcription runs
even if `JUDGE_BACKEND=off` - transcripts are independently useful (search,
future classifier features) and are the one part that must be local anyway.

## Text-only vs frames mode

`JUDGE_MODE=frames|text_only` (default `frames` - via the API, frames are just
tokens, not local compute, and add real signal for visual gags/fails). Frames
mode: ~6 frames, uniform sampling (`ffmpeg -vf fps=...`), downscaled to
≤512 px, written to a temp dir and deleted after the call. `text_only` sends
just the transcript + chat + metadata, roughly halving input cost; whether the
frames actually improve rating-agreement enough to justify their tokens is an
empirical question the evaluation can answer.

## Storage

Applied as idempotent `ALTER TABLE` / `CREATE TABLE IF NOT EXISTS` in
`get_connection()`, matching the existing convention:

- `moments.transcript TEXT NULL` - the whisper transcript (belongs to the
  clip, not to any particular judgment).
- New table `moment_judgments` - separate table rather than columns on
  `moments`, so a clip can be re-judged (new model, new prompt) without
  losing history, and so models can be compared on the same clips:

```sql
CREATE TABLE IF NOT EXISTS moment_judgments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    moment_id INTEGER NOT NULL,
    backend TEXT NOT NULL,          -- "anthropic"
    model TEXT NOT NULL,            -- e.g. "claude-opus-4-8"
    mode TEXT NOT NULL,             -- "frames" | "text_only"
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
- API error / timeout / rate-limit: one retry with backoff, then leave the
  moment unjudged and move on. An unreachable API degrades to "clips captured
  now, judged when it's back" - it never affects detection or clipping.
- `scripts/judge_moment.py <moment_id> | --all-unjudged` for manual runs,
  backfilling old clips, and re-judging after a model/prompt change.

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

The same harness answers model-selection empirically: judge a batch with two
models (multiple `moment_judgments` rows per moment) and keep the cheapest one
that still agrees with the human - the disciplined way to drop from Opus to
Sonnet/Haiku.

## Relationship to the learned-classifier path

The judge is not the end state. As rated data accumulates (hundreds of
examples), a small trained classifier - detector features + transcript
embeddings, no API call at inference - becomes feasible, and would return the
pipeline to fully local, near-zero-cost, and private-by-default scoring. The
API judge bridges the gap (useful signal from day one, no training data
needed) and its stored judgments/transcripts become features and weak labels
for that later model. Few-shot personalization (injecting a handful of the
user's own rated examples into the judge prompt) is a cheap intermediate step
worth trying once ratings exist.

## Open questions

- **Model tier**: start on Opus 4.8, let the evaluation justify dropping to
  Sonnet 5 / Haiku 4.5. Decision is data-driven, not upfront.
- **Frames vs text-only** default: revisit once agreement is measured both
  ways on real clips - text-only may be almost as good for far fewer tokens.
- Per-channel language hint (some watched channels may be Slovak rather than
  Czech) - whisper and the prompt both accept it; not needed for v1.
- Whether `music` category judgments should feed back into detector tuning
  (e.g. corroborating the dance-emote exclusion) - out of scope for v1.
