"""Grok 视频协议：单次提交、有界轮询及同网关鉴权下载。

不管理本地任务、素材编排或界面；不依赖其他视频提供商。
"""

import hashlib
import math
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import quote, urlsplit

import requests
from loguru import logger

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect
from app.utils import utils

DEFAULT_MODEL_ID = "grok-imagine-video"
DEFAULT_RESOLUTION = "480p"
POLL_INTERVAL = 5
MAX_POLL_FAILURES = 5
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


class GrokVideoError(RuntimeError):
    """所有失败保留已知远端 ID；调用方停止继续创建任务。"""

    def __init__(self, message: str, task_id: str = ""):
        super().__init__(message)
        self.task_id = task_id


def _settings(settings=None):
    source = config.app if settings is None else settings
    values = {
        "base": str(source.get("grok_video_base_url", "") or "").strip().rstrip("/"),
        "key": str(source.get("grok_video_api_key", "") or "").strip(),
        "model": str(source.get("grok_video_model", DEFAULT_MODEL_ID) or "").strip(),
        "resolution": str(
            source.get("grok_video_resolution", DEFAULT_RESOLUTION) or ""
        ).strip(),
    }
    try:
        parsed = urlsplit(values["base"])
        valid_url = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and parsed.path.endswith("/v1")
        )
        parsed.port  # 提前校验非法端口，避免凭据请求阶段才报错。
        timeout = float(source.get("grok_video_run_timeout", 1800))
    except (TypeError, ValueError):
        raise GrokVideoError("Invalid Grok video URL or timeout") from None
    if not valid_url or not values["key"] or not values["model"]:
        raise GrokVideoError("Grok video requires a /v1 base URL, API key and model")
    if values["resolution"] not in {"480p", "720p", "1080p"}:
        raise GrokVideoError("Grok video resolution must be 480p, 720p or 1080p")
    if not math.isfinite(timeout) or not 1 <= timeout <= 7200:
        raise GrokVideoError(
            "Grok video timeout must be finite and within 1-7200 seconds"
        )
    values["timeout"] = timeout
    values["proxies"] = dict(config.proxy)
    values["verify"] = source.get("tls_verify", True)
    return values


def is_enabled(settings=None) -> bool:
    try:
        _settings(settings)
        return True
    except GrokVideoError:
        return False


def _request_options(settings):
    return {
        "headers": {"Authorization": f"Bearer {settings['key']}"},
        "proxies": settings["proxies"],
        "verify": settings["verify"],
        # 内容接口由网关直接返回字节，不把凭据交给重定向目标。
        "allow_redirects": False,
    }


def _json(response, task_id=""):
    if not 200 <= response.status_code < 300:
        raise GrokVideoError(f"Grok video HTTP {response.status_code}", task_id)
    try:
        body = response.json()
    except ValueError:
        raise GrokVideoError("Grok video returned invalid JSON", task_id) from None
    if not isinstance(body, dict):
        raise GrokVideoError("Grok video returned an invalid response", task_id)
    return body


def _task_path(task_id):
    if not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 256:
        raise GrokVideoError("Grok video submission is unconfirmed: missing request_id")
    return "/videos/" + quote(task_id, safe="")


def generate_videos(search_term, minimum_duration, video_aspect=VideoAspect.portrait):
    """每个关键词只创建一次远端任务；查询失败不触发重新生成。"""
    settings = _settings()
    if not isinstance(search_term, str) or not search_term.strip():
        raise GrokVideoError("Grok video prompt must not be empty")
    if (
        isinstance(minimum_duration, bool)
        or not isinstance(minimum_duration, int)
        or minimum_duration < 1
    ):
        raise GrokVideoError("Grok video clip duration must be a positive integer")
    duration = min(minimum_duration, 15)
    if duration != minimum_duration:
        logger.info(
            f"Grok video request duration capped at 15s: requested={minimum_duration}s"
        )
    try:
        aspect = VideoAspect(video_aspect)
    except ValueError:
        raise GrokVideoError("Invalid Grok video aspect ratio") from None
    payload = {
        "model": settings["model"],
        "prompt": search_term.strip(),
        "duration": duration,
        "aspect_ratio": aspect.value,
        "resolution": settings["resolution"],
    }
    try:
        with requests.post(
            settings["base"] + "/videos/generations",
            json=payload,
            timeout=(10, 60),
            **_request_options(settings),
        ) as response:
            if response.status_code >= 500:
                raise GrokVideoError(
                    "Grok video submission is unconfirmed: server error; do not resubmit automatically"
                )
            try:
                body = _json(response)
            except GrokVideoError:
                if 200 <= response.status_code < 300:
                    raise GrokVideoError(
                        "Grok video submission is unconfirmed: invalid response; "
                        "do not resubmit automatically"
                    ) from None
                raise
    except requests.RequestException:
        raise GrokVideoError(
            "Grok video submission is unconfirmed: network error; do not resubmit automatically"
        ) from None
    task_id = body.get("request_id")
    task_path = _task_path(task_id)
    deadline = time.monotonic() + settings["timeout"]
    failures = 0
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            with requests.get(
                settings["base"] + task_path,
                timeout=(min(10, remaining / 2), min(30, remaining / 2)),
                **_request_options(settings),
            ) as response:
                if response.status_code in RETRYABLE_STATUSES:
                    raise requests.ConnectionError("transient poll failure")
                body = _json(response, task_id)
            failures = 0
        except requests.RequestException:
            failures += 1
            if failures >= MAX_POLL_FAILURES:
                raise GrokVideoError(
                    "Grok video polling failed; remote result is unconfirmed", task_id
                ) from None
            body = {"status": "pending"}
        status = body.get("status")
        if status == "done":
            # 实际时长与尺寸由素材层探测下载文件，不能信任任务中的请求时长。
            item = MaterialInfo()
            item.provider = "grok_video"
            item.url = settings["base"] + task_path + "/content"
            item.duration = duration
            item.source_info = {"asset_id": task_id, "search_term": search_term.strip()}
            return [item]
        if status == "failed":
            # 不透传可能回显凭据、代理地址或签名 URL 的上游错误文本。
            raise GrokVideoError(
                "Grok video task failed; inspect the remote task", task_id
            )
        if status != "pending":
            raise GrokVideoError("Grok video returned an unknown task status", task_id)
        time.sleep(max(0, min(POLL_INTERVAL, deadline - time.monotonic())))
    raise GrokVideoError(
        "Grok video polling timed out; remote result is unconfirmed", task_id
    )


def download_video(request_id, save_dir=""):
    """只重试同一任务的内容下载；原子写入避免半成品命中缓存。"""
    try:
        settings = _settings()
    except GrokVideoError as exc:
        raise GrokVideoError(str(exc), request_id) from None
    url = settings["base"] + _task_path(request_id) + "/content"
    try:
        directory = Path(save_dir or utils.storage_dir("cache_videos"))
        directory.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(url.encode()).hexdigest()
        target = directory / f"grok-{digest}.mp4"
        if target.is_file() and target.stat().st_size > 0:
            return str(target)
    except OSError:
        raise GrokVideoError(
            "Grok video cache directory is not accessible", request_id
        ) from None
    deadline = time.monotonic() + settings["timeout"]
    for attempt in range(3):
        temporary = None
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            with requests.get(
                url,
                stream=True,
                timeout=(min(10, remaining / 2), min(120, remaining / 2)),
                **_request_options(settings),
            ) as response:
                if response.status_code in RETRYABLE_STATUSES:
                    raise requests.ConnectionError("transient download failure")
                if response.status_code != 200:
                    raise GrokVideoError(
                        f"Grok video download HTTP {response.status_code}", request_id
                    )
                with tempfile.NamedTemporaryFile(
                    dir=directory, suffix=".part", delete=False
                ) as output:
                    temporary = Path(output.name)
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if time.monotonic() >= deadline:
                            raise requests.Timeout("download deadline")
                        if chunk:
                            output.write(chunk)
                if not temporary.stat().st_size:
                    raise requests.ConnectionError("empty video")
                os.replace(temporary, target)
                return str(target)
        except requests.RequestException:
            if attempt < 2:
                time.sleep(max(0, min(attempt + 1, deadline - time.monotonic())))
        except OSError:
            raise GrokVideoError(
                "Grok video could not be saved locally", request_id
            ) from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    raise GrokVideoError(
                        "Grok video temporary file cleanup failed", request_id
                    ) from None
    raise GrokVideoError(
        "Grok video download failed; recover the existing remote task", request_id
    )
