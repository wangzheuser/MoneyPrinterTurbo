"""Grok 协议与素材编排回归；全部请求均为测试替身。"""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

import cli
from app.config import config
from app.models.schema import MaterialInfo, VideoAspect, VideoParams
from app.services import grok_video as grok, material, state, task


@pytest.fixture(autouse=True)
def settings():
    with (
        patch.dict(
            config.app,
            {
                "grok_video_base_url": "http://localhost:8000/proxy/v1",
                "grok_video_api_key": "private-test-key",
                "grok_video_model": "grok-imagine-video",
                "grok_video_resolution": "720p",
                "grok_video_run_timeout": 60,
            },
        ),
        patch.dict(config.proxy, {}, clear=True),
    ):
        yield


def response(body=None, status=200, chunks=(b"video",)):
    result = MagicMock(status_code=status)
    result.__enter__.return_value = result
    result.json.return_value = body
    result.iter_content.return_value = iter(chunks)
    return result


@pytest.mark.parametrize(
    "field,value",
    [
        ("base_url", ""),
        ("base_url", "http://x/v1?key=secret"),
        ("base_url", "http://user:pass@x/v1"),
        ("base_url", "http://x/v2"),
        ("base_url", "http://x:bad/v1"),
        ("base_url", "file:///v1"),
        ("api_key", ""),
        ("model", ""),
        ("resolution", "4K"),
        ("run_timeout", float("nan")),
        ("run_timeout", float("inf")),
        ("run_timeout", 0),
        ("run_timeout", 7201),
    ],
)
def test_invalid_config_is_disabled_without_request(field, value):
    with patch.object(grok.requests, "post") as post:
        config.app["grok_video_" + field] = value
        assert not grok.is_enabled()
        post.assert_not_called()


def test_settings_snapshot_and_default_config():
    snapshot = dict(config.app)
    config.app["grok_video_api_key"] = ""
    assert grok.is_enabled(snapshot)
    assert not grok.is_enabled({})


def test_create_poll_contract_and_duration_cap():
    with (
        patch.object(
            grok.requests, "post", return_value=response({"request_id": "job/1"})
        ) as post,
        patch.object(
            grok.requests,
            "get",
            side_effect=[response({"status": "pending"}), response({"status": "done"})],
        ) as get,
        patch.object(grok.time, "sleep"),
    ):
        items = grok.generate_videos(" sun ", 20, VideoAspect.portrait)
    assert post.call_count == 1
    assert post.call_args.args[0] == "http://localhost:8000/proxy/v1/videos/generations"
    assert post.call_args.kwargs["json"] == {
        "model": "grok-imagine-video",
        "prompt": "sun",
        "duration": 15,
        "aspect_ratio": "9:16",
        "resolution": "720p",
    }
    assert get.call_count == 2
    assert get.call_args.args[0].endswith("/videos/job%2F1")
    assert items[0].source_info["asset_id"] == "job/1"
    assert items[0].provider == "grok_video"
    assert "private-test-key" not in repr(items[0].source_info)
    assert not post.call_args.kwargs["allow_redirects"]


@pytest.mark.parametrize(
    "prompt,duration",
    [("", 5), (" ", 5), (None, 5), ("sun", 0), ("sun", True), ("sun", 1.5)],
)
def test_bad_inputs_do_not_submit(prompt, duration):
    with (
        patch.object(grok.requests, "post") as post,
        pytest.raises(grok.GrokVideoError),
    ):
        grok.generate_videos(prompt, duration)
    post.assert_not_called()


@pytest.mark.parametrize(
    "reply",
    [
        requests.Timeout("private-test-key"),
        response({}, 503),
        response({}),
        response({"request_id": 123}),
        response({}, 401),
        response([], 200),
    ],
)
def test_unconfirmed_submission_never_retries(reply):
    with (
        patch.object(grok.requests, "post", side_effect=[reply]) as post,
        patch.object(grok.requests, "get") as get,
        pytest.raises(grok.GrokVideoError) as exc,
    ):
        grok.generate_videos("sun", 5)
    assert post.call_count == 1
    get.assert_not_called()
    assert "private-test-key" not in str(exc.value)


@pytest.mark.parametrize(
    "body,status",
    [
        ({"status": "failed", "error": {"message": "private-test-key"}}, 200),
        ({"status": "completed"}, 200),
        ({}, 403),
        ([], 200),
    ],
)
def test_poll_errors_preserve_id(body, status):
    with (
        patch.object(
            grok.requests, "post", return_value=response({"request_id": "remote-id"})
        ),
        patch.object(grok.requests, "get", return_value=response(body, status)),
        pytest.raises(grok.GrokVideoError) as exc,
    ):
        grok.generate_videos("sun", 5)
    assert exc.value.task_id == "remote-id"
    assert "private-test-key" not in str(exc.value)


def test_poll_retries_are_bounded():
    with (
        patch.object(
            grok.requests, "post", return_value=response({"request_id": "id"})
        ) as post,
        patch.object(grok.requests, "get", return_value=response({}, 429)) as get,
        patch.object(grok.time, "sleep"),
        pytest.raises(grok.GrokVideoError) as exc,
    ):
        grok.generate_videos("sun", 5)
    assert exc.value.task_id == "id"
    assert get.call_count == grok.MAX_POLL_FAILURES
    assert post.call_count == 1


def test_poll_timeout_preserves_id():
    with (
        patch.object(
            grok.requests, "post", return_value=response({"request_id": "id"})
        ),
        patch.object(grok.time, "monotonic", side_effect=[0, 61]),
        pytest.raises(grok.GrokVideoError, match="timed out") as exc,
    ):
        grok.generate_videos("sun", 5)
    assert exc.value.task_id == "id"


def test_download_retries_same_content_and_caches_atomically(tmp_path):
    with patch.object(
        grok.requests,
        "get",
        side_effect=[requests.Timeout(), response(chunks=(b"abc", b"def"))],
    ) as get:
        with patch.object(grok.time, "sleep"):
            saved = grok.download_video("id", str(tmp_path))
        assert grok.download_video("id", str(tmp_path)) == saved
    assert get.call_count == 2
    assert get.call_args.args[0].endswith("/videos/id/content")
    assert get.call_args.kwargs["headers"] == {
        "Authorization": "Bearer private-test-key"
    }
    assert not get.call_args.kwargs["allow_redirects"]
    assert list(tmp_path.glob("*.part")) == []
    assert next(tmp_path.glob("*.mp4")).read_bytes() == b"abcdef"


@pytest.mark.parametrize("status", [302, 401, 404])
def test_download_never_follows_redirect_or_retries_auth_failure(tmp_path, status):
    with patch.object(
        grok.requests, "get", return_value=response(status=status)
    ) as get:
        with pytest.raises(grok.GrokVideoError) as exc:
            grok.download_video("id", str(tmp_path))
    assert exc.value.task_id == "id"
    assert get.call_count == 1
    assert list(tmp_path.iterdir()) == []


def test_download_partial_failure_leaves_no_file(tmp_path):
    def partial():
        yield b"partial"
        raise requests.ConnectionError("private-test-key")

    def reply(*args, **kwargs):
        return response(chunks=partial())

    with (
        patch.object(grok.requests, "get", side_effect=reply) as get,
        patch.object(grok.time, "sleep"),
    ):
        with pytest.raises(grok.GrokVideoError) as exc:
            grok.download_video("id", str(tmp_path))
    assert exc.value.task_id == "id"
    assert get.call_count == 3
    assert list(tmp_path.iterdir()) == []


def test_download_directory_failure_preserves_task_id(tmp_path):
    from pathlib import Path

    with patch.object(Path, "mkdir", side_effect=PermissionError("private-test-key")):
        with pytest.raises(grok.GrokVideoError) as exc:
            grok.download_video("remote", str(tmp_path))
    assert exc.value.task_id == "remote"
    assert "private-test-key" not in str(exc.value)


def test_download_empty_response_is_retried_without_publishing(tmp_path):
    with patch.object(
        grok.requests, "get", side_effect=lambda *a, **k: response(chunks=())
    ) as get:
        with patch.object(grok.time, "sleep"), pytest.raises(grok.GrokVideoError):
            grok.download_video("remote", str(tmp_path))
    assert get.call_count == 3
    assert list(tmp_path.iterdir()) == []


def test_invalid_json_after_creation_preserves_remote_id():
    bad = response()
    bad.json.side_effect = ValueError("not JSON")
    with patch.object(
        grok.requests, "post", return_value=response({"request_id": "remote"})
    ):
        with (
            patch.object(grok.requests, "get", return_value=bad),
            pytest.raises(grok.GrokVideoError) as exc,
        ):
            grok.generate_videos("sun", 5)
    assert exc.value.task_id == "remote"


def item():
    value = MaterialInfo()
    value.provider = "grok_video"
    value.source_info = {"asset_id": "remote", "search_term": "sun"}
    value.duration = 15
    return value


def materials(**kwargs):
    return material.download_videos(
        task_id="grok-test",
        search_terms=["one", "two", "three"],
        source="grok_video",
        **kwargs,
    )


@contextmanager
def clip(path):
    yield SimpleNamespace(duration=2.5, size=(720, 1280))


def test_material_coverage_uses_actual_duration_and_stops():
    with (
        patch.object(
            grok, "generate_videos", side_effect=lambda *a: [item()]
        ) as generate,
        patch.object(grok, "download_video", return_value="saved.mp4"),
        patch.object(material, "VideoFileClip", clip),
        patch.object(material, "_persist_material_sources") as persist,
    ):
        assert len(materials(audio_duration=5, max_clip_duration=5)) == 2
    assert generate.call_count == 2
    assert persist.call_args.args[1][0]["asset_id"] == "remote"


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), "bad"])
def test_invalid_coverage_never_submits(duration):
    with (
        patch.object(grok, "generate_videos") as generate,
        pytest.raises(grok.GrokVideoError),
    ):
        materials(audio_duration=duration)
    generate.assert_not_called()


def test_zero_coverage_never_submits():
    with (
        patch.object(grok, "generate_videos") as generate,
        patch.object(material, "_persist_material_sources"),
    ):
        assert materials(audio_duration=0) == []
    generate.assert_not_called()


def test_invalid_video_stops_next_submission():
    with (
        patch.object(grok, "generate_videos", return_value=[item()]) as generate,
        patch.object(grok, "download_video", return_value="broken.mp4"),
        patch.object(material, "VideoFileClip", side_effect=ValueError("bad mp4")),
        patch.object(material, "_persist_material_sources"),
        pytest.raises(grok.GrokVideoError) as exc,
    ):
        materials(audio_duration=10)
    assert generate.call_count == 1
    assert exc.value.task_id == "remote"


def test_task_preflight_and_failure_record():
    params = VideoParams(video_subject="sun", video_source="grok_video")
    memory = state.MemoryState()
    with patch.object(task.sm, "state", memory):
        with (
            patch.object(grok, "is_enabled", return_value=False),
            patch.object(task, "generate_script") as script,
        ):
            assert (
                task.start("test-preflight", params, stop_at="materials")[
                    "failed_stage"
                ]
                == "preflight"
            )
            script.assert_not_called()
        with patch.object(
            material,
            "download_videos",
            side_effect=grok.GrokVideoError("failed", "remote"),
        ):
            assert task.get_video_materials("test-error", params, ["sun"], 5) is None
        assert "remote" in str(memory.get_task("test-error"))


def test_cli_confirmation_and_batch_backward_compatibility():
    arguments = ["--video-subject", "sun", "--video-source", "grok_video"]
    with pytest.raises(SystemExit):
        cli.parse_args(arguments)
    parsed = cli.parse_args(arguments + ["--confirm-grok-video-charge"])
    assert cli.build_video_params(parsed).video_source == "grok_video"
    assert (
        cli.parse_args(arguments + ["--stop-at", "script"]).video_source == "grok_video"
    )
    params = VideoParams(video_subject="sun", video_source="grok_video")
    options = dict(
        stop_at="materials",
        custom_position_is_explicit=False,
        seedance_charge_confirmed=False,
        ofox_charge_confirmed=False,
        metaso_minimax_charge_confirmed=False,
    )
    with pytest.raises(ValueError, match="confirm-grok-video-charge"):
        cli._validate_batch_task_params(params, **options)
    cli._validate_batch_task_params(params, grok_video_charge_confirmed=True, **options)
    params.video_source = "pexels"
    cli._validate_batch_task_params(params, **options)
