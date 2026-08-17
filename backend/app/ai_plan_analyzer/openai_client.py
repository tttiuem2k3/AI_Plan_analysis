from __future__ import annotations

import json
import logging
import os
import random
import time
from typing import Any, Dict, Optional, Tuple

import requests
from ..utils.ai_flow_log import append_ai_flow_log

logger = logging.getLogger(__name__)


class OpenAIClientError(RuntimeError):
    pass


def _get_env(name: str, default: Optional[str] = None) -> str:
    # 1) Prefer real process env
    v = os.getenv(name)
    if v is not None and str(v).strip() != "":
        return str(v).strip()

    # 2) Fallback to .env via pydantic Settings (so Windows/uvicorn works without OS env injection)
    if name in (
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "GEMINI_API_KEY",
        "GEMINI_MODEL",
        "OPENROUTER_API_KEY",
        "OPENROUTER_MODEL",
        "LLM_PROVIDER",
    ):
        try:
            from ..core.config import settings  # local import to avoid import-time side effects

            sv = getattr(settings, name, None)
            if sv is not None and str(sv).strip() != "":
                return str(sv).strip()
        except Exception:
            pass

    # 3) Default if provided
    if default is not None and str(default).strip() != "":
        return str(default).strip()

    raise OpenAIClientError(f"Missing env var: {name}")


def _debug_enabled() -> bool:
    return str(os.getenv("AI_DEBUG_LLM_IO", "")).strip() in ("1", "true", "TRUE", "yes", "YES")


def _read_int_env(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(str(v).strip())
    except Exception:
        return int(default)


def _get_provider() -> str:
    try:
        # Must honor .env/settings; default only if truly missing.
        v = _get_env("LLM_PROVIDER", "openai")
    except Exception:
        v = os.getenv("LLM_PROVIDER") or "openai"
    v = str(v or "openai").strip().lower()
    return v if v in ("openai", "gemini", "openrouter") else "openai"


def _call_openrouter_json(
    *,
    system_prompt: str,
    user_prompt: str,
    timeout_s: int,
    max_retries: int,
) -> Dict[str, Any]:
    """Call OpenRouter Chat Completions API and return parsed JSON."""

    api_key = _get_env("OPENROUTER_API_KEY")
    model = os.getenv("OPENROUTER_MODEL")
    if not model:
        try:
            model = _get_env("OPENROUTER_MODEL", "qwen/qwen3.6-plus")
        except Exception:
            model = "qwen/qwen3.6-plus"

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }

    base_sleep = 1.2
    last_err: Optional[Exception] = None

    for attempt in range(max_retries):
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=int(timeout_s))
            if resp.status_code in (429, 500, 502, 503, 504):
                raise OpenAIClientError(f"Transient error: {resp.status_code} {resp.text[:300]}")
            if resp.status_code >= 400:
                raise OpenAIClientError(f"OpenRouter error: {resp.status_code} {resp.text[:500]}")

            data = resp.json()

            out_text = ""
            try:
                choices = data.get("choices") or []
                msg = (choices[0] or {}).get("message") if choices else None
                out_text = str((msg or {}).get("content") or "").strip() if isinstance(msg, dict) else ""
            except Exception:
                out_text = ""

            if not out_text:
                raise OpenAIClientError("Empty model output")

            try:
                parsed = json.loads(out_text)
            except Exception as e:
                raise OpenAIClientError(f"Invalid JSON from model: {e}; text={out_text[:500]}")

            if _debug_enabled():
                try:
                    parsed["__debug_openrouter_raw_output_text"] = out_text
                    parsed["__debug_openrouter_model"] = model
                    parsed["__debug_openrouter_timeout_s"] = int(timeout_s)
                except Exception:
                    pass

            return parsed

        except Exception as e:
            last_err = e
            if attempt >= max_retries - 1:
                break
            sleep_s = base_sleep * (2**attempt) + random.random() * 0.3
            logger.warning(
                "OpenRouter call failed (attempt %s/%s): %s; sleeping %.2fs",
                attempt + 1,
                max_retries,
                e,
                sleep_s,
            )
            time.sleep(sleep_s)

    raise OpenAIClientError(str(last_err) if last_err else "OpenRouter call failed")


def _call_gemini_json(
    *,
    system_prompt: str,
    user_prompt: str,
    timeout_s: int,
    max_retries: int,
) -> Dict[str, Any]:
    """Call Gemini (Google Generative Language API) and return parsed JSON."""

    api_key = _get_env("GEMINI_API_KEY")
    model = os.getenv("GEMINI_MODEL")
    if not model:
        try:
            model = _get_env("GEMINI_MODEL", "gemini-2.5-flash")
        except Exception:
            model = "gemini-2.5-flash"

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    headers = {"Content-Type": "application/json"}

    body = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.2,
        },
    }

    base_sleep = 1.2
    last_err: Optional[Exception] = None

    for attempt in range(max_retries):
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=int(timeout_s))
            if resp.status_code in (429, 500, 502, 503, 504):
                raise OpenAIClientError(f"Transient error: {resp.status_code} {resp.text[:300]}")
            if resp.status_code >= 400:
                raise OpenAIClientError(f"Gemini error: {resp.status_code} {resp.text[:500]}")

            data = resp.json()
            out_text = ""
            try:
                candidates = data.get("candidates") or []
                content = (candidates[0] or {}).get("content") if candidates else None
                parts = (content or {}).get("parts") if isinstance(content, dict) else None
                if isinstance(parts, list) and parts:
                    out_text = str(parts[0].get("text") or "").strip()
            except Exception:
                out_text = ""

            if not out_text:
                raise OpenAIClientError("Empty model output")

            try:
                parsed = json.loads(out_text)
            except Exception as e:
                raise OpenAIClientError(f"Invalid JSON from model: {e}; text={out_text[:500]}")

            if _debug_enabled():
                try:
                    parsed["__debug_gemini_raw_output_text"] = out_text
                    parsed["__debug_gemini_model"] = model
                    parsed["__debug_gemini_timeout_s"] = int(timeout_s)
                except Exception:
                    pass

            return parsed

        except Exception as e:
            last_err = e
            if attempt >= max_retries - 1:
                break
            sleep_s = base_sleep * (2**attempt) + random.random() * 0.3
            logger.warning(
                "Gemini call failed (attempt %s/%s): %s; sleeping %.2fs",
                attempt + 1,
                max_retries,
                e,
                sleep_s,
            )
            time.sleep(sleep_s)

    raise OpenAIClientError(str(last_err) if last_err else "Gemini call failed")


def call_gpt(
    payload: Dict[str, Any],
    system_prompt: str,
    user_prompt: str,
    timeout_s: Optional[int] = None,
    max_retries: Optional[int] = None,
) -> Dict[str, Any]:
    """Call OpenAI Responses API via plain HTTP.

    Env vars:
      - OPENAI_API_KEY
    - OPENAI_MODEL (default: gpt-4.1)
    - OPENROUTER_API_KEY
    - OPENROUTER_MODEL (default: qwen/qwen3.6-plus)
      - OPENAI_TIMEOUT_S (default: 500)
      - OPENAI_MAX_RETRIES (default: 2)
      - AI_DEBUG_LLM_IO=1 to include raw OpenAI output_text in debug logs/return

    Returns parsed JSON object.
    Raises OpenAIClientError on failures.
    """

    provider = _get_provider()

    append_ai_flow_log(
        "input_llms.txt",
        {
            "provider": provider,
            "payload": payload,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "timeout_s": timeout_s,
            "max_retries": max_retries,
        },
        title="llm_input",
    )

    # Provider-specific defaults
    if provider == "gemini":
        if timeout_s is None:
            timeout_s = _read_int_env("GEMINI_TIMEOUT_S", 500)
        if max_retries is None:
            max_retries = _read_int_env("GEMINI_MAX_RETRIES", 2)
        max_retries = max(1, min(6, int(max_retries)))
        try:
            out = _call_gemini_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                timeout_s=int(timeout_s),
                max_retries=int(max_retries),
            )
            append_ai_flow_log(
                "output_llms.txt",
                {
                    "provider": provider,
                    "model": os.getenv("GEMINI_MODEL") or "gemini-2.5-flash",
                    "result": out,
                },
                title="llm_output",
            )
            return out
        except Exception as e:
            append_ai_flow_log(
                "output_llms.txt",
                {
                    "provider": provider,
                    "error": str(e),
                },
                title="llm_output_error",
            )
            raise

    if provider == "openrouter":
        if timeout_s is None:
            timeout_s = _read_int_env("OPENROUTER_TIMEOUT_S", 500)
        if max_retries is None:
            max_retries = _read_int_env("OPENROUTER_MAX_RETRIES", 2)
        max_retries = max(1, min(6, int(max_retries)))
        try:
            out = _call_openrouter_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                timeout_s=int(timeout_s),
                max_retries=int(max_retries),
            )
            append_ai_flow_log(
                "output_llms.txt",
                {
                    "provider": provider,
                    "model": os.getenv("OPENROUTER_MODEL") or "qwen/qwen3.6-plus",
                    "result": out,
                },
                title="llm_output",
            )
            return out
        except Exception as e:
            append_ai_flow_log(
                "output_llms.txt",
                {
                    "provider": provider,
                    "error": str(e),
                },
                title="llm_output_error",
            )
            raise

    api_key = _get_env("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL") or _get_env("OPENAI_MODEL", "gpt-4.1")

    if timeout_s is None:
        # Default: allow long waits for complex optimization + JSON outputs.
        timeout_s = _read_int_env("OPENAI_TIMEOUT_S", 500)

    if max_retries is None:
        max_retries = _read_int_env("OPENAI_MAX_RETRIES", 2)
    max_retries = max(1, min(6, int(max_retries)))

    url = "https://api.openai.com/v1/responses"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    body = {
        "model": model,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        # Encourage JSON-only output
        "text": {"format": {"type": "json_object"}},
        "metadata": {"module": "ai_plan_analyzer"},
    }

    base_sleep = 1.2

    last_err: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=int(timeout_s))
            if resp.status_code in (429, 500, 502, 503, 504):
                raise OpenAIClientError(f"Transient error: {resp.status_code} {resp.text[:300]}")
            if resp.status_code >= 400:
                raise OpenAIClientError(f"OpenAI error: {resp.status_code} {resp.text[:500]}")

            data = resp.json()

            # Responses API can return content in multiple shapes; prefer output_text when present
            out_text = data.get("output_text")
            if not out_text:
                # Try extracting from output[0].content
                try:
                    out = data.get("output") or []
                    parts = out[0].get("content") if out else []
                    out_text = "".join([p.get("text", "") for p in parts if p.get("type") == "output_text"]).strip()
                except Exception:
                    out_text = ""

            if not out_text:
                raise OpenAIClientError("Empty model output")

            try:
                parsed = json.loads(out_text)
            except Exception as e:
                raise OpenAIClientError(f"Invalid JSON from model: {e}; text={out_text[:500]}")

            if _debug_enabled():
                # Attach for upstream debug (returned in final response when enabled)
                try:
                    parsed["__debug_openai_raw_output_text"] = out_text
                    parsed["__debug_openai_model"] = model
                    parsed["__debug_openai_timeout_s"] = int(timeout_s)
                except Exception:
                    pass

            append_ai_flow_log(
                "output_llms.txt",
                {
                    "provider": provider,
                    "model": model,
                    "result": parsed,
                    "raw_output_text": out_text,
                },
                title="llm_output",
            )

            return parsed

        except Exception as e:
            last_err = e
            if attempt >= max_retries - 1:
                break
            sleep_s = base_sleep * (2**attempt) + random.random() * 0.3
            logger.warning(
                "OpenAI call failed (attempt %s/%s): %s; sleeping %.2fs",
                attempt + 1,
                max_retries,
                e,
                sleep_s,
            )
            time.sleep(sleep_s)

    err_msg = str(last_err) if last_err else "OpenAI call failed"
    append_ai_flow_log(
        "output_llms.txt",
        {
            "provider": provider,
            "model": model,
            "error": err_msg,
        },
        title="llm_output_error",
    )
    raise OpenAIClientError(err_msg)


class OpenAIClient:
    def __init__(self, model: str | None = None, timeout_s: int | None = None, max_retries: int | None = None):
        self.model = model
        self.timeout_s = timeout_s
        self.max_retries = max_retries

    def chat_text(self, system_prompt: str, user_prompt: str) -> str:
        # call_gpt already enforces JSON-only via text.format=json_object.
        provider = _get_provider()
        env_model_name = (
            "GEMINI_MODEL"
            if provider == "gemini"
            else "OPENROUTER_MODEL"
            if provider == "openrouter"
            else "OPENAI_MODEL"
        )
        old_model = os.getenv(env_model_name)
        try:
            if self.model:
                os.environ[env_model_name] = str(self.model)
            parsed = call_gpt(
                payload={},
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                timeout_s=self.timeout_s,
                max_retries=self.max_retries,
            )
        finally:
            # restore env to avoid side-effects
            if old_model is None:
                os.environ.pop(env_model_name, None)
            else:
                os.environ[env_model_name] = old_model

        # If model already returned parsed JSON, but we need raw JSON text for optional parsing upstream.
        # Re-serialize deterministically.
        return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"), default=str)
