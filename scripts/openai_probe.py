import json
import re
import time
from pathlib import Path

import httpx


def load_env_value(name: str) -> str | None:
    text = Path("run_fastapi.ps1").read_text(encoding="utf-8-sig")
    match = re.search(rf"\$env:{re.escape(name)}=\"([^\"]+)\"", text)
    return match.group(1) if match else None


def main() -> None:
    api_key = load_env_value("OPENAI_API_KEY")
    model = load_env_value("OPENAI_MODEL") or "gpt-5-mini"
    if not api_key:
        print(json.dumps({"ok": False, "error": "OPENAI_API_KEY not found"}))
        return

    body = {
        "model": model,
        "max_output_tokens": 80,
        "reasoning": {"effort": "minimal"},
        "input": [{"role": "user", "content": "Return only JSON: {\"ok\": true}"}],
        "text": {"format": {"type": "json_object"}},
    }
    started = time.time()
    try:
        response = httpx.post(
            "https://api.openai.com/v1/responses",
            headers={"authorization": f"Bearer {api_key}", "content-type": "application/json"},
            json=body,
            timeout=httpx.Timeout(12, connect=4, read=12, write=4, pool=4),
        )
        print(
            json.dumps(
                {
                    "elapsed": round(time.time() - started, 2),
                    "status": response.status_code,
                    "requestId": response.headers.get("x-request-id"),
                    "bodyPrefix": response.text[:300],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "elapsed": round(time.time() - started, 2),
                    "exception": type(exc).__name__,
                    "message": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
