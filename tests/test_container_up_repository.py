from __future__ import annotations

from container_up.binding_repository import BindingRepository
from container_up.bucket_allocator import BucketAllocator


def test_binding_repository_upsert_and_get(tmp_path) -> None:
    repo = BindingRepository(str(tmp_path / "bindings.db"))
    repo.init_db()

    binding = repo.upsert("feishu-main", "user-1", 2)

    assert binding["frontend_id"] == "feishu-main"
    assert binding["user_id"] == "user-1"
    assert binding["bucket_id"] == 2
    assert repo.get("feishu-main", "user-1")["bucket_id"] == 2


def test_bucket_allocator_picks_least_loaded_bucket(tmp_path) -> None:
    repo = BindingRepository(str(tmp_path / "bindings.db"))
    repo.init_db()
    repo.upsert("feishu-main", "user-1", 1)
    repo.upsert("feishu-main", "user-2", 1)
    repo.upsert("web-main", "user-3", 2)

    allocator = BucketAllocator(repo, 4)

    assert allocator.allocate() == 0
