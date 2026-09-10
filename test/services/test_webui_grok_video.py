from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app.config import config


def widget(elements, key):
    return next(
        item
        for item in elements
        if str(getattr(item, "key", "")) == key
        or str(getattr(item, "key", "")).startswith(key + "_")
    )


def test_grok_source_requires_configuration_and_confirmation():
    settings = dict(
        config.app,
        llm_provider="openai",
        video_source="pexels",
        grok_video_base_url="http://localhost:8000/v1",
        grok_video_api_key="",
        grok_video_model="grok-imagine-video",
        grok_video_resolution="720p",
        grok_video_run_timeout=1800,
    )
    with (
        patch.object(config, "app", settings),
        patch.object(config, "try_save_config", return_value=True),
        patch("app.services.webui_task.submit_generation") as submit,
    ):
        app = AppTest.from_file(
            str(Path(__file__).parents[2] / "webui/Main.py"), default_timeout=60
        )
        app.session_state["ui_language"] = "en"
        app.run()
        widget(app.text_area, "video_subject").set_value("sunrise").run()
        widget(app.text_area, "video_script").set_value(
            "The sun rises over a quiet lake."
        ).run()
        widget(app.text_area, "video_terms").set_value("sunrise, lake").run()
        app.session_state["video_source_select_en"] = "grok_video"
        app.run()
        widget(app.button, "generate_video_button").click().run()
        submit.assert_not_called()
        assert any("Configure a Grok" in str(error.value) for error in app.error)
        settings["grok_video_api_key"] = "grok-private-key"
        widget(app.button, "generate_video_button").click().run()
        submit.assert_not_called()
        assert any("confirm" in str(error.value).lower() for error in app.error)
        widget(app.checkbox, "grok_video_confirm_charge").check().run()
        widget(app.button, "generate_video_button").click().run()
        assert submit.call_count == 1
        params = submit.call_args.kwargs["params"]
        assert params.video_source == "grok_video"
        assert "grok-private-key" not in params.model_dump_json()
        assert not app.exception


def test_grok_settings_inputs_preserve_custom_model_and_save_key():
    settings = dict(
        config.app,
        video_source="pexels",
        grok_video_base_url="",
        grok_video_api_key="",
        grok_video_model="custom-video-alias",
        grok_video_resolution="480p",
    )
    with (
        patch.object(config, "app", settings),
        patch.object(config, "try_save_config", return_value=True),
    ):
        app = AppTest.from_file(
            str(Path(__file__).parents[2] / "webui/Main.py"), default_timeout=60
        )
        app.session_state["ui_language"] = "en"
        app.session_state["settings_dialog_open"] = True
        app.session_state["settings_dialog_target_tab"] = "material"
        app.run()
        assert (
            widget(app.text_input, "grok_video_model_input").value
            == "custom-video-alias"
        )
        widget(app.text_input, "grok_video_base_url_input").set_value(
            "http://localhost:8000/v1"
        ).run()
        widget(app.text_input, "grok_video_api_key_input").set_value(
            "new-private-key"
        ).run()
        assert settings["grok_video_base_url"] == "http://localhost:8000/v1"
        assert settings["grok_video_api_key"] == "new-private-key"
        assert settings["grok_video_model"] == "custom-video-alias"
        assert not app.exception
