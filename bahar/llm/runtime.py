"""llama-cpp-python runtime: model download, load, warmup, and static-prefix KV reuse.

llama-cpp-python only reuses the *previous* prompt's prefix, and the parser and composer have
different static prefixes. So each prefix is evaluated once, its KV state saved, and restored
before every call that uses it (docs/plan.md §4, risk 1).
"""

import asyncio
import logging
import os
import time
from pathlib import Path

from huggingface_hub import hf_hub_download
from llama_cpp import Llama, LlamaGrammar, LlamaState

from bahar.config.settings import Settings
from bahar.models import LlmHealth

log = logging.getLogger(__name__)

# Fixed ~150-token passage for the prompt-eval benchmark; its content is irrelevant.
_WARMUP_PROMPT = (
    "<|im_start|>system\nYou are a concise assistant.<|im_end|>\n<|im_start|>user\n"
    + "Count upward from one in words, separated by commas, without stopping. " * 8
    + "<|im_end|>\n<|im_start|>assistant\n"
)
_WARMUP_GEN_TOKENS = 48


def available_threads() -> int:
    """CPU threads this process may use: affinity mask capped by the cgroup quota.

    os.cpu_count() reports host cores, which oversubscribes threads on a 2-vCPU Space.
    """
    n = len(os.sched_getaffinity(0))
    try:  # cgroup v2: "<quota> <period>" or "max <period>"
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            n = min(n, max(1, round(int(quota) / int(period))))
    except (OSError, ValueError):
        try:  # cgroup v1
            quota_us = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text())
            period_us = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text())
            if quota_us > 0:
                n = min(n, max(1, round(quota_us / period_us)))
        except (OSError, ValueError):
            pass
    return n


class LlmRuntime:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._llm: Llama | None = None
        self._lock = asyncio.Lock()  # one llama context: calls are serialised
        self._prefix_tokens: dict[str, list[int]] = {}
        self._prefix_state: dict[str, LlamaState] = {}
        self._health = LlmHealth(
            model_loaded=False,
            model_repo=settings.model_repo,
            model_file=settings.model_file,
            n_ctx=settings.n_ctx,
            n_threads=settings.n_threads or available_threads(),
        )

    @property
    def health(self) -> LlmHealth:
        return self._health

    def load(self) -> None:
        """Blocking: download (or find in the HF cache), load, warm up. Never raises."""
        try:
            path = hf_hub_download(self._settings.model_repo, self._settings.model_file)
            t0 = time.perf_counter()
            self._llm = Llama(
                model_path=path,
                n_ctx=self._settings.n_ctx,
                n_threads=self._health.n_threads,
                n_threads_batch=self._health.n_threads,  # default is the host CPU count
                verbose=False,
            )
            self._health.model_loaded = True
            self._health.load_s = round(time.perf_counter() - t0, 1)
            self._warmup()
        except Exception as exc:  # startup must not kill /health
            log.exception("model load failed")
            self._health.model_loaded = False
            self._health.error = f"{type(exc).__name__}: {exc}"

    def _warmup(self) -> None:
        llm = self._require()
        tokens = llm.tokenize(_WARMUP_PROMPT.encode(), add_bos=False, special=True)

        llm.reset()
        t = time.perf_counter()
        llm.eval(tokens)
        self._health.prompt_eval_tok_s = round(len(tokens) / (time.perf_counter() - t), 1)

        # Generation speed: stream tokens after the prompt is already evaluated.
        n = 0
        t = time.perf_counter()
        for _ in llm.generate(tokens, temp=0.0):
            n += 1
            if n >= _WARMUP_GEN_TOKENS:
                break
        self._health.generation_tok_s = round(n / (time.perf_counter() - t), 1)

    def _require(self) -> Llama:
        if self._llm is None:
            raise RuntimeError("model not loaded")
        return self._llm

    def register_prefix(self, name: str, text: str) -> int:
        """Evaluate a static prompt prefix once and keep its KV state. Returns token count."""
        llm = self._require()
        tokens = llm.tokenize(text.encode(), add_bos=False, special=True)
        llm.reset()
        llm.eval(tokens)
        self._prefix_tokens[name] = tokens
        self._prefix_state[name] = llm.save_state()
        return len(tokens)

    def _complete_sync(
        self, prefix: str, suffix: str, max_tokens: int, grammar: str | None
    ) -> str:
        llm = self._require()
        prefix_tokens = self._prefix_tokens[prefix]
        suffix_tokens = llm.tokenize(suffix.encode(), add_bos=False, special=True)
        # Restore the prefix KV state; generate() then skips re-evaluating the shared tokens.
        llm.load_state(self._prefix_state[prefix])
        out = llm.create_completion(
            prefix_tokens + suffix_tokens,
            max_tokens=max_tokens,
            temperature=0.0,
            stop=["<|im_end|>"],
            grammar=LlamaGrammar.from_string(grammar) if grammar else None,
        )
        return out["choices"][0]["text"]

    async def complete(
        self, prefix: str, suffix: str, max_tokens: int = 220, grammar: str | None = None
    ) -> str:
        """Generate after the registered static prefix `prefix`, appending `suffix`."""
        async with self._lock:
            return await asyncio.to_thread(
                self._complete_sync, prefix, suffix, max_tokens, grammar
            )
