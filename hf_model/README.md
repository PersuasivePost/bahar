---
license: apache-2.0
base_model: Qwen/Qwen2.5-1.5B-Instruct
base_model_relation: quantized
language:
  - en
  - hi
  - mr
pipeline_tag: text-generation
tags:
  - gguf
  - llama-cpp
  - qwen2.5
  - grounded-generation
  - geoai
  - india
  - air-quality
---

# Bahar SLM: Qwen2.5-1.5B-Instruct (GGUF Q4_K_M), as used by Bahar

This repository documents the language model inside **Bahar**, an outdoor-activity planner
built for the GeoAI Hackathon @ CSRE, IIT Bombay. Bahar answers free-form questions such as
*"asthma hai, kal subah Powai ke paas walk kahan karu?"* with the best nearby place and time,
what to carry, what to watch for and when to avoid going. Every claim in the answer is backed
by data fetched live for that query.

**Current version: the unmodified base model.** The weights are the official
[`Qwen/Qwen2.5-1.5B-Instruct-GGUF`](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF)
file `qwen2.5-1.5b-instruct-q4_k_m.gguf`. What Bahar adds is the prompts, the decoding
constraints and the grounding contract described below. A QLoRA fine-tune for the parser
and composer tasks will replace these weights in a later version of this repository; Bahar
reads the model repo and file from environment variables, so the swap needs no code change.

## How Bahar uses the model

The model does exactly two jobs, and **never writes a number, time or place name itself**.

1. **Parser.** It turns a messy English / Hinglish / Hindi / Marathi question into a
   `ParsedQuery` JSON object (activity, place, time, health profile, preferences, commute,
   language). Decoding is constrained by a GBNF grammar generated from the JSON schema, so
   the output is always valid JSON with valid enum values. Deterministic code then checks the
   result against the words the user actually wrote (days, conditions, distances, place
   names, language).
2. **Composer.** It writes the answer from a compact facts block in which every value is an
   evidence reference: `{{e7}}` for a value, `{{v1}}` for a place (`{{v0}}` = the user's own
   location). Code later replaces each reference with the formatted value and its source.
   A per-request GBNF grammar allows only the references present in the facts block, and
   no character that is a digit in any script (ASCII, Devanagari, Arabic-Indic, superscript,
   subscript, full-width), so the model *cannot* type a number.

A validator then checks every composed answer: unknown references, digits, number words next
to a unit or time word ("do ghante", "five km"), place names written outside a reference,
the recommended place and time present, the right script for the language, and each
labelled line citing only the facts of its own section. A line that cites another section's
facts is replaced by the deterministic template line; any other failure gets one retry if
at least 15 s of the latency budget remain, and otherwise the whole answer comes from a
deterministic template (English or Hinglish). The response always reports
`grounding: {validator_passed, fallback_used, retries, mode, rejections}`.

### Prompts

All prompts are static ChatML prefixes, evaluated once at startup. Their KV state is saved
and restored before each call, so a request only pays for its own suffix.

| prefix | tokens | content |
|---|---|---|
| `parser` | 1294 | task rules + 10 few-shots (en, Hinglish, hi, mr, typos, out of scope, missing location, commute, time idioms) |
| `composer_en` | 787 | grounding rules, 6-line answer format, 2 shots (no safe time; ok) |
| `composer_hinglish` | 895 | same, Hinglish shots |
| `composer_hi` / `composer_mr` | 761 / 785 | same rules, shots that write only the recommendation and the reasons (see limitations) |

Each language has its own composer prefix because the model imitates the language and shape
of the last few-shot it saw. With mixed-language shots, a Hinglish question got a Hindi
"no safe time" answer even when a safe time existed.

The prompt sources are `bahar/llm/prompts.py` and the grammars are `bahar/llm/parser.py` /
`bahar/llm/composer.py` in the Bahar code repository. The runtime settings are in
[`config.json`](config.json).

## Intended use

- Phrasing grounded answers for Bahar's outdoor-activity recommendations (11 activities:
  running, walking, cycling, golf, cricket, football, tennis, hiking, picnic, outdoor event,
  photography) in India, in English, Hinglish, Hindi and Marathi.
- Parsing such questions into structured queries.
- CPU inference with llama.cpp / llama-cpp-python. No GPU is needed.

**Not intended** for medical advice, general chat, or any use where its text is shown
without Bahar's grounding contract: the base model on its own will happily state numbers it
does not know.

## Performance (measured October 2026, Intel i7-11800H, 8 threads, llama-cpp-python 0.3.36)

| metric | value |
|---|---|
| prompt evaluation | ~91 tok/s |
| generation, unconstrained | ~30 tok/s |
| generation with the composer grammar | ~12-15 tok/s |
| parser smoke set (32 messy queries, 4 languages) | 32/32 valid JSON and fully correct after normalisation; p50 4.0 s |
| `/ask` end to end, warm data cache | English ~13 s, Hindi ~17 s, Hinglish ~22 s |

## Limitations

- **Faithfulness is the weak point, not grounding.** The grammar and the validator make
  invented numbers and places impossible to show, but the base model sometimes puts the
  right kind of reference in the wrong sentence. In the Step 7 smoke run it was off by one
  in later sections (it followed the few-shot's numbering), invented an "Avoid" line when
  the facts had none, and wrote semantically muddled reasons ("fine dust 86 µg/m³ ... heat
  rises 82 µg/m³"). The section check catches references taken from the wrong section; it
  cannot catch a muddled sentence whose references are in the right section. This is the
  main target of the planned fine-tune.
- **Hindi and Marathi** cost about twice the tokens of Hinglish (a 5-line Hindi answer is
  ~290 tokens, over the 200-token cap). For those languages the model writes only the
  recommendation and the reasons, and the remaining sections come from the Hinglish (for
  Hindi) or English (for Marathi) template. The answer states this as an assumption.
  Hindi grammar in the reasons line is sometimes awkward.
- **Parser transliteration.** Devanagari place names are transliterated to Latin letters
  for geocoding, and the model sometimes gets them wrong (इंडिया गेट → "Indi Gate"), which
  leads to a wrong or missing place. Phrases such as "after work" are not yet mapped to an
  evening window.
- Air-quality values in the answers are **modelled estimates** (CAMS forecast + ground
  stations + nearby pollution sources), not measurements. Bahar says so in every answer that
  cites them.
- At temperature 0 the output is deterministic for a given facts block, and so are its
  mistakes.

## License

Apache-2.0, inherited from [Qwen2.5-1.5B-Instruct](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct)
(Qwen2.5-3B uses a different licence, which is why Bahar stays at 1.5B). The prompts and
configuration in this repository are also Apache-2.0.

## Attribution

Answers composed with this model are built on data from © OpenStreetMap contributors,
Open-Meteo.com, CAMS (Copernicus), WAQI / OpenAQ ground stations and OSRM; see the Bahar
README for the full list of sources.
