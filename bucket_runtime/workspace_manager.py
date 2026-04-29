from __future__ import annotations

import shutil
from pathlib import Path


class WorkspaceManager:
    def ensure_workspace(
        self,
        workspace_path: Path,
        *,
        template_root: Path,
    ) -> Path:
        workspace = workspace_path.expanduser().resolve(strict=False)
        init_flag = workspace / ".workspace_initialized"
        workspace.mkdir(parents=True, exist_ok=True)
        source_templates = template_root
        if source_templates.exists() and not init_flag.exists():
            self._copy_templates(source_templates, workspace)
            init_flag.write_text("true\n", encoding="utf-8")
        return workspace

    def _copy_templates(self, src: Path, dst: Path) -> None:
        for item in src.iterdir():
            target = dst / item.name
            if target.exists():
                continue
            if item.is_dir():
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)
