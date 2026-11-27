from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from asset_sync.config import Assets, McpSpec, SkillSpec
from asset_sync.mcp import catalog_server


class Source(Protocol):
    def discover(self) -> Assets: ...

    def skill_files(self, spec: SkillSpec) -> tuple[str, object]: ...

    def download_skill_files(self, spec: SkillSpec, version: str,
                             names: list[str], destination: Path) -> None: ...

    def mcp_server(self, spec: McpSpec) -> dict: ...


class ZhichengSource:
    def __init__(self, *, endpoint: str | None = None, token: str | None = None) -> None:
        from zhicheng import ZhichengAssetClient, ZhichengPublicClient
        from zhicheng.constants import DEFAULT_ZHICHENG_DOMAIN

        self.endpoint = endpoint or os.environ.get("ZHICHENG_DOMAIN") or DEFAULT_ZHICHENG_DOMAIN
        self.token = token or os.environ.get("ZHICHENG_TOKEN")
        self.assets = ZhichengAssetClient(endpoint=self.endpoint, token=self.token)
        self.public = ZhichengPublicClient(endpoint=self.endpoint, token=self.token)

    def close(self) -> None:
        self.assets.session.close()
        self.public.session.close()

    def discover(self) -> Assets:
        skills = []
        for item in self._pages(self.public.list_skills):
            repo_id = item["path"]
            if "/" not in repo_id:
                repo_id = f"{item['namespace']}/{repo_id}"
            skills.append(SkillSpec(repo_id=repo_id, asset_id=str(item["repository_id"])))
        mcp = [McpSpec(asset_id=item["id"], name=item["name"])
               for item in self._pages(self.assets.list_apps, kind="mcp")]
        return Assets(skills=skills, mcp=mcp)

    @staticmethod
    def _pages(fetch, *args, **kwargs):
        page_token = None
        seen = set()
        while True:
            page = fetch(*args, page_token=page_token, **kwargs)
            if isinstance(page, list):
                yield from page
                return
            yield from page["items"]
            page_token = page.get("next_page_token")
            if not page_token:
                return
            if page_token in seen:
                raise ValueError("repeated SDK page token")
            seen.add(page_token)

    def skill_files(self, spec: SkillSpec) -> tuple[str, object]:
        version = self._latest_version(spec.asset_id)
        return version, self.assets.list_version_files(spec.asset_id, version)

    def _latest_version(self, asset_id: str) -> str:
        versions = [item for item in self._pages(self.assets.list_asset_versions,
                                                asset_id, page_size=100)
                    if item["publish_status"] == "published"]
        if not versions:
            raise ValueError(f"skill {asset_id} has no published versions")
        latest = max(versions, key=lambda item: datetime.fromisoformat(item["created_at"].replace("Z", "+00:00")))
        return latest["version_name"]

    def download_skill_files(self, spec: SkillSpec, version: str,
                             names: list[str], destination: Path) -> None:
        import tempfile

        from zhicheng.snapshot_download import snapshot_download

        patterns = [name.replace("[", "[[]").replace("*", "[*]").replace("?", "[?]")
                    for name in names]
        with tempfile.TemporaryDirectory(prefix="asset-sync-cache-") as cache:
            snapshot_download(
                repo_id=spec.repo_id, repo_type="skill", revision=version,
                local_dir=str(destination), cache_dir=cache, allow_patterns=patterns,
                endpoint=self.endpoint, token=self.token, source="csg",
                force_download=True, quiet=True,
            )

    def mcp_server(self, spec: McpSpec) -> dict:
        # SDK get_app() uses the admin route, which omits mcp_config.
        url = f"{self.endpoint.rstrip('/')}/asset/api/v1/catalog/apps/{quote(spec.asset_id, safe='')}"
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        with self.assets.session.get(url, headers=headers, timeout=30) as response:
            response.raise_for_status()
            body = response.json()
        detail = body.get("data", body)
        return catalog_server(detail["mcp_config"])
