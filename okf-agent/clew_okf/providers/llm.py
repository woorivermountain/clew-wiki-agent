"""JSON model interface and Chat Completions implementation."""

from __future__ import annotations

import json
import os
from http.client import HTTPException
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

JsonObject = dict[str, object]
_MAX_RESPONSE = 256 * 1024


class JsonModel(Protocol):
    """Return a JSON object or raise RuntimeError if a call cannot be completed."""

    def complete(
        self, system: str, payload: JsonObject, *, model: str | None = None
    ) -> JsonObject: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ChatCompletionsModel:
    """Call the configured endpoint, with an optional model override per request."""

    def __init__(self, config: JsonObject) -> None:
        endpoint = config.get("endpoint")
        model = config.get("model")
        if not isinstance(endpoint, str) or not isinstance(model, str) or not model.strip():
            raise ValueError("synthesis에는 endpoint와 model을 지정해야 합니다.")
        url = urlsplit(endpoint)
        local = url.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            not url.hostname
            or url.query
            or url.fragment
            or url.username is not None
            or (url.scheme != "https" and not (local and url.scheme == "http"))
        ):
            raise ValueError("synthesis endpoint는 HTTPS 또는 로컬 HTTP URL이어야 합니다.")
        key_env = config.get("api_key_env")
        key = None
        if key_env is not None:
            if not isinstance(key_env, str) or not key_env.isidentifier() or not (key := os.environ.get(key_env)):
                raise ValueError("synthesis의 api_key_env에 해당하는 환경 변수를 설정하세요.")
        if key is not None and (not key.isascii() or any(not 33 <= ord(ch) <= 126 for ch in key)):
            raise ValueError("API 키는 공백 없이 발급된 키 문자열만 입력하세요.")
        json_mode = config.get("json_mode", False)
        if type(json_mode) is not bool:
            raise ValueError("synthesis.json_mode는 true 또는 false여야 합니다.")
        timeout = config.get("timeout_seconds", 90)
        if type(timeout) is not int or timeout <= 0:
            raise ValueError("synthesis timeout_seconds는 양의 정수여야 합니다.")

        self._endpoint = endpoint
        self._model = model
        self._key = key
        self._timeout = timeout
        self._json_mode = json_mode
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def complete(
        self, system: str, payload: JsonObject, *, model: str | None = None
    ) -> JsonObject:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        body = {"model": model or self._model, "messages": messages}
        if self._json_mode:
            body["response_format"] = {"type": "json_object"}
        request = Request(
            self._endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read(_MAX_RESPONSE + 1)
                if len(raw) > _MAX_RESPONSE:
                    raise ValueError
                # A bounded read can return EOF without raising, even with Content-Length.
                remaining = getattr(response, "length", None)
                if isinstance(remaining, int) and remaining > 0:
                    raise HTTPException("Incomplete response")
            content = json.loads(raw)["choices"][0]["message"]["content"]
            if isinstance(content, str) and not content.strip():
                raise RuntimeError("모델이 빈 본문을 반환했습니다.")
            result = json.loads(content)
            if not isinstance(result, dict):
                raise ValueError
        except HTTPError as exc:
            raise RuntimeError(f"모델 요청이 실패했습니다 (HTTP {exc.code}).") from None
        except HTTPException:
            raise RuntimeError("모델 HTTP 응답을 끝까지 읽지 못했습니다.") from None
        except (URLError, OSError):
            raise RuntimeError("모델 엔드포인트에 연결하지 못했습니다.") from None
        except (ValueError, KeyError, TypeError, IndexError, RecursionError):
            raise RuntimeError("모델이 올바른 JSON 객체를 반환하지 않았습니다.") from None
        return result
