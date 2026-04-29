from __future__ import annotations

import json
from pathlib import Path

from container_up.frontend_config import load_frontend_configs


def test_frontend_paths_follow_fixed_bucket_layout(tmp_path, monkeypatch) -> None:
    common_root = tmp_path / "common"
    config_file = common_root / "frontends.json"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(
        json.dumps(
            {
                "frontends": [
                    {
                        "id": "feishu-main",
                        "provider": "feishu",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BUCKET_MOUNT_ROOT", str(tmp_path))
    monkeypatch.setenv("BUCKET_MOUNT_PVC", "nanobot-data-pvc")
    monkeypatch.setattr("container_up.settings.BUCKET_MOUNT_ROOT", Path(tmp_path))
    monkeypatch.setattr("container_up.settings.BUCKET_COMMON_ROOT", common_root)
    monkeypatch.setattr("container_up.settings.FRONTENDS_CONFIG_PATH", config_file)
    monkeypatch.setattr("container_up.frontend_config.BUCKET_COMMON_ROOT", common_root)
    monkeypatch.setattr("container_up.frontend_config.FRONTENDS_CONFIG_PATH", config_file)

    configs = load_frontend_configs()
    frontend = configs["feishu-main"]

    assert frontend.common_root == tmp_path / "common" / "feishu-main"
    assert frontend.config_path == tmp_path / "common" / "feishu-main" / "config.json"
    assert frontend.builtin_skills_dir == tmp_path / "common" / "feishu-main" / "skills"
    assert frontend.template_dir == tmp_path / "common" / "feishu-main" / "templates"
