from __future__ import annotations

import json

from container_up.frontend_config import load_frontend_configs


def test_frontend_common_root_derives_config_skills_and_templates(tmp_path, monkeypatch) -> None:
    config_file = tmp_path / "frontends.json"
    config_file.write_text(
        json.dumps(
            {
                "frontends": [
                    {
                        "id": "feishu-main",
                        "provider": "feishu",
                        "common_root": str(tmp_path / "common" / "feishu-main"),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FRONTENDS_CONFIG_PATH", str(config_file))

    configs = load_frontend_configs()
    frontend = configs["feishu-main"]

    assert frontend.config_path == tmp_path / "common" / "feishu-main" / "config.json"
    assert frontend.builtin_skills_dir == tmp_path / "common" / "feishu-main" / "skills"
    assert frontend.template_dir == tmp_path / "common" / "feishu-main" / "templates"
