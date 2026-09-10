"""本机 HTTP 契约测试：实际请求、流式下载和 FFmpeg 视频探测，不调用上游。"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from unittest.mock import patch

from moviepy import ColorClip, VideoFileClip

from app.config import config
from app.services import grok_video, material


def test_local_gateway_to_real_material(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    with ColorClip((64, 64), color=(20, 80, 160), duration=0.5) as clip:
        clip.write_videofile(
            str(source), fps=10, codec="libx264", audio=False, logger=None
        )
    video_bytes = source.read_bytes()
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, body, content_type="application/json"):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(("POST", self.path, self.headers.get("Authorization"), body))
            self.reply(b'{"request_id":"mock-video-1"}')

        def do_GET(self):
            calls.append(("GET", self.path, self.headers.get("Authorization")))
            if self.path.endswith("/content"):
                self.reply(video_bytes, "video/mp4")
            else:
                self.reply(
                    b'{"status":"done","video":{"url":"https://unused.invalid/clip","duration":15}}'
                )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    try:
        with (
            patch.dict(
                config.app,
                {
                    "grok_video_base_url": f"http://127.0.0.1:{server.server_port}/v1",
                    "grok_video_api_key": "local-test-key",
                    "grok_video_model": "test-model",
                    "grok_video_resolution": "720p",
                    "grok_video_run_timeout": 30,
                    "material_directory": str(tmp_path),
                },
            ),
            patch.dict(config.proxy, {}, clear=True),
            patch.object(material, "_persist_material_sources") as persist,
        ):
            assert grok_video.is_enabled()
            paths = material.download_videos(
                "http-test",
                ["sunrise", "should not submit"],
                source="grok_video",
                audio_duration=0.4,
                max_clip_duration=5,
            )
        assert len(paths) == 1
        with VideoFileClip(paths[0]) as result:
            assert result.duration == 0.5
            assert result.size == [64, 64]
        assert [entry[1] for entry in calls] == [
            "/v1/videos/generations",
            "/v1/videos/mock-video-1",
            "/v1/videos/mock-video-1/content",
        ]
        assert all(entry[2] == "Bearer local-test-key" for entry in calls)
        assert persist.call_args.args[1][0]["asset_id"] == "mock-video-1"
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()
