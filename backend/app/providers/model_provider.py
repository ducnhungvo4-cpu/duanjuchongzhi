from __future__ import annotations

import json
import mimetypes
import os
import urllib.error
import urllib.request
import uuid
from base64 import b64decode
from dataclasses import dataclass
from typing import Any


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderAgent:
    mode: str
    provider: str
    model_key: str
    model_name: str
    prompt_file: str
    output_format: str
    ready: bool


class BaseModelProvider:
    mode = "base"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def prepare_agent(self, agent_key: str, agent: dict[str, Any]) -> ProviderAgent:
        raise NotImplementedError

    def status(self) -> dict[str, Any]:
        raise NotImplementedError

    def chat_completion(
        self,
        *,
        model_name: str,
        system_prompt: str,
        user_content: str,
        temperature: float | None = None,
        response_format: dict[str, str] | None = None,
        video_url: str | None = None,
        video_file_id: str | None = None,
        video_fps: float | None = None,
    ) -> str:
        raise NotImplementedError

    def upload_file(self, file_path: str, *, purpose: str = "user_data", video_fps: float | None = 1) -> dict[str, Any]:
        raise NotImplementedError

    def retrieve_file(self, file_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def generate_image(
        self,
        *,
        model_name: str,
        prompt: str,
        size: str = "2K",
        response_format: str = "url",
        sequential_image_generation: str = "disabled",
        watermark: bool = True,
        stream: bool = False,
    ) -> dict[str, Any]:
        raise NotImplementedError


class MockModelProvider(BaseModelProvider):
    mode = "mock"

    def prepare_agent(self, agent_key: str, agent: dict[str, Any]) -> ProviderAgent:
        model_key = agent.get("model", "")
        model_name = self.config.get("volcengine", {}).get(model_key, model_key)
        return ProviderAgent(
            mode="mock",
            provider="mock",
            model_key=model_key,
            model_name=model_name or f"mock-{agent_key}",
            prompt_file=agent.get("prompt_file", ""),
            output_format=agent.get("output_format", "json"),
            ready=True,
        )

    def status(self) -> dict[str, Any]:
        return {
            "mode": "mock",
            "ready": True,
            "missing": [],
            "message": "mock 模式已启用，不会调用外部模型。",
            "models": self.config.get("volcengine", {})
        }

    def chat_completion(
        self,
        *,
        model_name: str,
        system_prompt: str,
        user_content: str,
        temperature: float | None = None,
        response_format: dict[str, str] | None = None,
        video_url: str | None = None,
        video_file_id: str | None = None,
        video_fps: float | None = None,
    ) -> str:
        return ""

    def upload_file(self, file_path: str, *, purpose: str = "user_data", video_fps: float | None = 1) -> dict[str, Any]:
        return {
            "id": f"mock-file-{uuid.uuid4().hex[:8]}",
            "status": "processed",
            "purpose": purpose,
            "filename": os.path.basename(file_path),
        }

    def retrieve_file(self, file_id: str) -> dict[str, Any]:
        return {"id": file_id, "status": "processed"}

    def generate_image(
        self,
        *,
        model_name: str,
        prompt: str,
        size: str = "2K",
        response_format: str = "url",
        sequential_image_generation: str = "disabled",
        watermark: bool = True,
        stream: bool = False,
    ) -> dict[str, Any]:
        return {
            "mock": True,
            "model": model_name,
            "prompt": prompt,
            "size": size,
            "mime_type": "image/svg+xml",
            "image_bytes": b"",
            "raw": {},
        }


class VolcengineModelProvider(BaseModelProvider):
    mode = "volcengine"

    def prepare_agent(self, agent_key: str, agent: dict[str, Any]) -> ProviderAgent:
        missing = self._missing_requirements(agent)
        if missing:
            raise ProviderError(
                f"火山引擎模型配置不完整，节点 {agent_key} 缺少：{', '.join(missing)}"
            )
        model_key = agent.get("model", "")
        model_name = self.config.get("volcengine", {}).get(model_key, "")
        return ProviderAgent(
            mode="volcengine",
            provider="volcengine",
            model_key=model_key,
            model_name=model_name,
            prompt_file=agent.get("prompt_file", ""),
            output_format=agent.get("output_format", "json"),
            ready=True,
        )

    def status(self) -> dict[str, Any]:
        missing = []
        if not self._api_key():
            missing.append("ARK_API_KEY")
        if not self._base_url():
            missing.append("config.volcengine.baseUrl")
        for agent_key, agent in self.config.get("agents", {}).items():
            if not agent.get("enabled", True):
                continue
            model_key = agent.get("model", "")
            if model_key and not self.config.get("volcengine", {}).get(model_key):
                missing.append(f"config.volcengine.{model_key} ({agent_key})")
        return {
            "mode": "volcengine",
            "ready": not missing,
            "missing": missing,
            "message": "火山引擎配置完整。" if not missing else "火山引擎配置不完整。",
            "models": self.config.get("volcengine", {})
        }

    def _missing_requirements(self, agent: dict[str, Any]) -> list[str]:
        missing = []
        if not self._api_key():
            missing.append("ARK_API_KEY")
        if not self._base_url():
            missing.append("config.volcengine.baseUrl")
        model_key = agent.get("model", "")
        if model_key and not self.config.get("volcengine", {}).get(model_key):
            missing.append(f"config.volcengine.{model_key}")
        return missing

    def chat_completion(
        self,
        *,
        model_name: str,
        system_prompt: str,
        user_content: str,
        temperature: float | None = None,
        response_format: dict[str, str] | None = None,
        video_url: str | None = None,
        video_file_id: str | None = None,
        video_fps: float | None = None,
    ) -> str:
        api_key = self._api_key()
        base_url = self._base_url()
        if not api_key:
            raise ProviderError("缺少 ARK_API_KEY 环境变量。")
        if not base_url:
            raise ProviderError("缺少 config.volcengine.baseUrl。")
        if not model_name:
            raise ProviderError("缺少模型 ID。")

        instructions = system_prompt
        input_content: list[dict[str, Any]] = []
        if video_file_id:
            input_content.append(
                {
                    "type": "input_video",
                    "file_id": video_file_id,
                }
            )
        elif video_url:
            video_content: dict[str, Any] = {
                "type": "input_video",
                "video_url": video_url,
            }
            if video_fps is not None:
                video_content["fps"] = video_fps
            input_content.append(video_content)
        input_content.append({"type": "input_text", "text": user_content})
        payload: dict[str, Any] = {
            "model": model_name,
            "input": [
                {
                    "role": "user",
                    "content": input_content,
                }
            ],
        }
        max_output_tokens = self._max_output_tokens()
        if max_output_tokens:
            payload["max_output_tokens"] = max_output_tokens
        thinking = self._thinking_config()
        if thinking:
            payload["thinking"] = thinking
        if instructions:
            payload["instructions"] = instructions
        if temperature is not None:
            payload["temperature"] = temperature
        if response_format is not None:
            payload["text"] = {"format": response_format}

        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/responses",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._response_timeout()) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ProviderError(f"方舟接口请求失败：HTTP {exc.code} {body}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"方舟接口连接失败：{exc.reason}") from exc
        except TimeoutError as exc:
            raise ProviderError(f"方舟接口响应超时：超过 {self._response_timeout()} 秒未返回") from exc

        data = json.loads(raw)
        extracted = self._extract_response_text(data)
        if extracted:
            return extracted
        raise ProviderError(f"方舟接口响应格式异常：{raw[:500]}")

    def _api_key(self) -> str:
        return (
            os.getenv("ARK_API_KEY")
            or os.getenv("VOLCENGINE_API_KEY")
            or ""
        )

    def _base_url(self) -> str:
        return self.config.get("volcengine", {}).get("baseUrl") or "https://ark.cn-beijing.volces.com/api/v3"

    def _response_timeout(self) -> int:
        base = int(self.config.get("volcengine", {}).get("responseTimeoutSeconds") or 60)
        extra = int(self.config.get("volcengine", {}).get("agentExtraTimeoutSeconds") or 0)
        return base + extra

    def chat_completion_stream(
        self,
        *,
        model_name: str,
        system_prompt: str,
        user_content: str,
        temperature: float | None = None,
        response_format: dict[str, str] | None = None,
        video_url: str | None = None,
        video_file_id: str | None = None,
        video_fps: float | None = None,
    ):
        """Yields (event_type: str, data: dict | str) tuples from the SSE stream."""
        api_key = self._api_key()
        base_url = self._base_url()
        if not api_key:
            raise ProviderError("缺少 ARK_API_KEY 环境变量。")
        if not base_url:
            raise ProviderError("缺少 config.volcengine.baseUrl。")
        if not model_name:
            raise ProviderError("缺少模型 ID。")

        instructions = system_prompt
        input_content: list[dict[str, Any]] = []
        if video_file_id:
            input_content.append({"type": "input_video", "file_id": video_file_id})
        elif video_url:
            video_content: dict[str, Any] = {"type": "input_video", "video_url": video_url}
            if video_fps is not None:
                video_content["fps"] = video_fps
            input_content.append(video_content)
        input_content.append({"type": "input_text", "text": user_content})

        payload: dict[str, Any] = {
            "model": model_name,
            "input": [{"role": "user", "content": input_content}],
            "stream": True,
        }
        max_output_tokens = self._max_output_tokens()
        if max_output_tokens:
            payload["max_output_tokens"] = max_output_tokens
        thinking = self._thinking_config()
        if thinking:
            payload["thinking"] = thinking
        if instructions:
            payload["instructions"] = instructions
        if temperature is not None:
            payload["temperature"] = temperature
        if response_format is not None:
            payload["text"] = {"format": response_format}

        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/responses",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._response_timeout()) as resp:
                for line_bytes in resp:
                    line = line_bytes.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        yield ("done", "")
                        return
                    try:
                        chunk: dict[str, Any] = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    event_type: str = chunk.get("type", "")
                    yield (event_type, chunk)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ProviderError(f"方舟接口 SSE 流请求失败：HTTP {exc.code} {body}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"方舟接口 SSE 连接失败：{exc.reason}") from exc
        except TimeoutError as exc:
            raise ProviderError(f"方舟接口 SSE 响应超时：超过 {self._response_timeout()} 秒未返回") from exc

    def _max_output_tokens(self) -> int:
        return int(self.config.get("volcengine", {}).get("maxOutputTokens") or 8192)

    def _thinking_config(self) -> dict[str, Any]:
        thinking = self.config.get("volcengine", {}).get("thinking")
        return thinking if isinstance(thinking, dict) else {"type": "disabled"}

    def _extract_response_text(self, data: dict[str, Any]) -> str:
        output_text = data.get("output_text")
        if isinstance(output_text, str) and output_text:
            return output_text

        chunks: list[str] = []
        output = data.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    text = part.get("text") or part.get("content")
                    if isinstance(text, str):
                        chunks.append(text)
        if chunks:
            return "\n".join(chunks)

        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                return message["content"]
        return ""

    def upload_file(self, file_path: str, *, purpose: str = "user_data", video_fps: float | None = 1) -> dict[str, Any]:
        api_key = self._api_key()
        base_url = self._base_url()
        if not api_key:
            raise ProviderError("缺少 ARK_API_KEY 环境变量。")
        if not os.path.exists(file_path):
            raise ProviderError(f"文件不存在：{file_path}")

        boundary = f"----arkFormBoundary{uuid.uuid4().hex}"
        file_name = os.path.basename(file_path)
        mime_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        fields: list[tuple[str, str]] = [("purpose", purpose)]
        if video_fps is not None and mime_type.startswith("video/"):
            fields.append(("preprocess_configs[video][fps]", str(video_fps)))

        body_parts: list[bytes] = []
        for name, value in fields:
            body_parts.append(f"--{boundary}\r\n".encode("utf-8"))
            body_parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
            body_parts.append(str(value).encode("utf-8"))
            body_parts.append(b"\r\n")

        body_parts.append(f"--{boundary}\r\n".encode("utf-8"))
        body_parts.append(
            (
                f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'
                f"Content-Type: {mime_type}\r\n\r\n"
            ).encode("utf-8")
        )
        with open(file_path, "rb") as file:
            body_parts.append(file.read())
        body_parts.append(b"\r\n")
        body_parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(body_parts)

        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/files",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            raise ProviderError(f"方舟文件上传失败：HTTP {exc.code} {body_text}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"方舟文件上传连接失败：{exc.reason}") from exc
        except TimeoutError as exc:
            raise ProviderError("方舟文件上传超时：超过 300 秒未返回") from exc
        return json.loads(raw)

    def retrieve_file(self, file_id: str) -> dict[str, Any]:
        api_key = self._api_key()
        base_url = self._base_url()
        if not api_key:
            raise ProviderError("缺少 ARK_API_KEY 环境变量。")
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/files/{file_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            raise ProviderError(f"方舟文件查询失败：HTTP {exc.code} {body_text}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"方舟文件查询连接失败：{exc.reason}") from exc

    def generate_image(
        self,
        *,
        model_name: str,
        prompt: str,
        size: str = "2K",
        response_format: str = "url",
        sequential_image_generation: str = "disabled",
        watermark: bool = True,
        stream: bool = False,
    ) -> dict[str, Any]:
        api_key = self._api_key()
        base_url = self._base_url()
        if not api_key:
            raise ProviderError("缺少 ARK_API_KEY 环境变量。")
        if not base_url:
            raise ProviderError("缺少 config.volcengine.baseUrl。")
        if not model_name:
            raise ProviderError("缺少图片模型 ID。")
        if not prompt.strip():
            raise ProviderError("缺少图片生成提示词。")

        payload = {
            "model": model_name,
            "prompt": prompt,
            "sequential_image_generation": sequential_image_generation,
            "response_format": response_format,
            "size": size,
            "stream": stream,
            "watermark": watermark,
        }
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/images/generations",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._response_timeout()) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ProviderError(f"方舟图片生成请求失败：HTTP {exc.code} {body}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"方舟图片生成连接失败：{exc.reason}") from exc
        except TimeoutError as exc:
            raise ProviderError(f"方舟图片生成超时：超过 {self._response_timeout()} 秒未返回") from exc

        data = json.loads(raw)
        image_url, image_bytes, mime_type = self._extract_generated_image(data)
        if image_url and not image_bytes:
            image_bytes, mime_type = self._download_image(image_url)
        if not image_bytes:
            raise ProviderError(f"方舟图片生成响应未包含可保存图片：{raw[:500]}")
        return {
            "model": model_name,
            "prompt": prompt,
            "size": size,
            "image_url": image_url,
            "image_bytes": image_bytes,
            "mime_type": mime_type,
            "raw": data,
        }

    def _extract_generated_image(self, data: dict[str, Any]) -> tuple[str, bytes, str]:
        items = data.get("data")
        if not isinstance(items, list) or not items:
            return "", b"", ""
        first = items[0] if isinstance(items[0], dict) else {}
        image_url = first.get("url") if isinstance(first.get("url"), str) else ""
        b64_json = first.get("b64_json") if isinstance(first.get("b64_json"), str) else ""
        if b64_json:
            return image_url, b64decode(b64_json), "image/png"
        return image_url, b"", ""

    def _download_image(self, image_url: str) -> tuple[bytes, str]:
        request = urllib.request.Request(image_url, headers={"User-Agent": "video-remake-workshop/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                mime_type = response.headers.get_content_type() or "image/png"
                return response.read(), mime_type
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ProviderError(f"下载生成图片失败：HTTP {exc.code} {body}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"下载生成图片连接失败：{exc.reason}") from exc


def get_model_provider(config: dict[str, Any]) -> BaseModelProvider:
    mode = config.get("mode", "mock")
    if mode == "mock":
        return MockModelProvider(config)
    if mode == "volcengine":
        return VolcengineModelProvider(config)
    raise ProviderError(f"Unsupported model mode: {mode}")


def get_model_status(config: dict[str, Any]) -> dict[str, Any]:
    return get_model_provider(config).status()
