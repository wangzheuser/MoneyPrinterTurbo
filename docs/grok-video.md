# Grok 视频素材源

`grok_video` 对接 grok2api 的异步视频接口，不使用 OpenAI Videos 或 OFox 协议。
首版仅提供文生视频素材，复用现有剪辑、配音、字幕和导出；图片仍使用 `openai_image`。

## 配置

在项目 `config.toml` 已有 `[app]` 中设置（不要重复创建该段）：

```toml
video_source = "grok_video"
grok_video_base_url = "http://127.0.0.1:8000/v1"
grok_video_api_key = "填写网关调用密钥"
grok_video_model = "grok-imagine-video"
grok_video_resolution = "720p"
grok_video_run_timeout = 1800
```

地址必须以 `/v1` 结尾，可包含反向代理前缀。容器中的 localhost 指向容器本身，
请改为可达的网关主机名。密钥独立于 LLM 的 `grok_api_key`，不接受上游账号凭据。
模型支持自定义别名；分辨率接受 480p、720p、1080p，实际可用性由网关和模型决定。
超时为 1～7200 秒；片段时长复用 `video_clip_duration`，请求最大 15 秒。

WebUI：在素材设置填写配置，选择 AI 视频组中的 Grok 视频，确认额度消耗后生成。
CLI（在项目根目录运行，`--batch-file` 同样需要确认参数）：

```shell
uv run python cli.py --video-subject "湖边日出" --video-source grok_video --confirm-grok-video-charge
```

API 仍使用原有视频请求结构，只需指定 `video_source: "grok_video"`；凭据保存在服务配置中。
文案模型、配音等其他服务仍需各自配置。仅生成 script/terms/audio/subtitle 时不要求视频调用确认。

## 任务与失败语义

- 每个关键词串行创建一次任务，下载后按实际视频时长累计，素材覆盖目标后停止提交。
- 查询只识别 pending/done/failed，不把其他提供商的 completed 等状态当作成功。
- 创建超时或 5xx 不自动重发；查询或下载失败不重新生成，不自动切换其他素材源。
- 下载使用同网关 `/v1/videos/{request_id}/content`，不依赖公开 URL，也不向重定向目标发送密钥。
- 失败状态中的 `grok_video_task_id` 可用于查询或找回远端产物。创建响应丢失时 ID 可能未知，
  此时先在网关核对任务，再决定是否重新生成。重新运行整个本地任务可能创建新视频。
- 不实现进程重启自动恢复；已经下载的片段可作为本地素材使用。

## 兼容与回退

新配置缺失时旧素材源照常运行，不更改默认来源、请求模型或数据库。
切回原 `video_source` 即停止使用 Grok。生产代码不依赖其他视频提供商的私有函数，
也不改变通用下载器。测试包含模拟协议、WebUI/CLI 入口以及本机 HTTP + FFmpeg 素材验证；
本机模拟测试不代表真实账号、额度或上游生成服务已验证。
