"""Is OpenAI slow, or is it us? (2026-10-22)

Sends a short classification request straight to the OpenAI API (no LangChain, no
coordinator) and prints, per call: the total time seen here, OpenAI's own processing time
(the ``openai-processing-ms`` response header), the difference (network and queueing), the
tokens (reasoning tokens included) and the rate-limit headroom. Run it from the repository
root while the stack runs: ``uv run python bench/probe_openai.py``. The key comes from
``.env`` and is never printed.
"""
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(Path.cwd() / ".env")
c = OpenAI(max_retries=0, timeout=90)
MSGS = [
    {
        "role": "system",
        "content": "Classify the operator message as goal, status, explain, stop or other. "
        "Answer with one word.",
    },
    {"role": "user", "content": "send summit to base"},
]


def probe(model: str, **kw) -> None:
    t = time.perf_counter()
    try:
        r = c.chat.completions.with_raw_response.create(model=model, messages=MSGS, **kw)
    except Exception as e:
        print(f"P {model:<12} ERROR {type(e).__name__}: {str(e)[:150]}")
        return
    total = time.perf_counter() - t
    h, body = r.headers, r.parse()
    u = body.usage
    rt = getattr(getattr(u, "completion_tokens_details", None), "reasoning_tokens", None)
    proc = int(h.get("openai-processing-ms", 0)) / 1000
    print(
        f"P {model:<12} {kw.get('reasoning_effort', '-'):<8} total {total:5.1f}s  "
        f"openai-processing {proc:5.1f}s  network+queue {total - proc:4.1f}s  "
        f"out {u.completion_tokens} (reasoning {rt})  "
        f"ratelimit remaining req {h.get('x-ratelimit-remaining-requests')} "
        f"tok {h.get('x-ratelimit-remaining-tokens')}"
    )


for _ in range(3):
    probe("gpt-5-nano", reasoning_effort="low")
for _ in range(2):
    probe("gpt-5-nano", reasoning_effort="minimal")
for _ in range(2):
    probe("gpt-4.1-nano")
