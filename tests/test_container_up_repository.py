from __future__ import annotations

from container_up.binding_repository import BindingRepository


def test_repository_reserve_online_and_release(tmp_path) -> None:
    repo = BindingRepository(tmp_path / "bindings.db")
    repo.init_db()

    user, bucket, created = repo.reserve_user_instance(
        user_id="user-1",
        workspace_path="/tmp/workspaces/user-1",
        frontend_id="feishu-main",
    )

    assert created is True
    assert user["status"] == "creating"
    assert bucket["bucket_id"] == "bucket-0"

    repo.mark_user_instance_online("user-1")
    online = repo.get_user_instance("user-1")

    assert online is not None
    assert online["status"] == "online"
    assert online["bucket_id"] == "bucket-0"
    assert online["workspace_path"] == "/tmp/workspaces/user-1"

    repo.release_user_instance("user-1", bucket_id="bucket-0", instance_id="user-1")
    released = repo.get_user_instance("user-1")
    bucket = repo.get_bucket("bucket-0")

    assert released is not None
    assert released["status"] == "destroyed"
    assert released["bucket_id"] is None
    assert released["instance_id"] is None
    assert released["workspace_path"] == "/tmp/workspaces/user-1"
    assert bucket is not None
    assert bucket["current_instances"] == 0
    assert bucket["status"] == "idle"


def test_repository_creates_new_bucket_when_capacity_is_exhausted(tmp_path) -> None:
    repo = BindingRepository(tmp_path / "bindings.db")
    repo.init_db()

    for index in range(20):
        repo.reserve_user_instance(
            user_id=f"user-{index}",
            workspace_path=f"/tmp/workspaces/user-{index}",
            frontend_id="feishu-main",
        )

    _, bucket, created = repo.reserve_user_instance(
        user_id="user-overflow",
        workspace_path="/tmp/workspaces/user-overflow",
        frontend_id="feishu-main",
    )

    assert created is True
    assert bucket["bucket_id"] == "bucket-1"


def test_repository_reuses_creating_reservation_without_double_counting(tmp_path) -> None:
    repo = BindingRepository(tmp_path / "bindings.db")
    repo.init_db()

    first_user, first_bucket, first_created = repo.reserve_user_instance(
        user_id="user-1",
        workspace_path="/tmp/workspaces/user-1",
        frontend_id="feishu-main",
    )
    second_user, second_bucket, second_created = repo.reserve_user_instance(
        user_id="user-1",
        workspace_path="/tmp/workspaces/user-1",
        frontend_id="feishu-main",
    )

    bucket = repo.get_bucket("bucket-0")

    assert first_created is True
    assert second_created is True
    assert first_user["user_id"] == second_user["user_id"]
    assert first_bucket["bucket_id"] == second_bucket["bucket_id"] == "bucket-0"
    assert bucket is not None
    assert bucket["current_instances"] == 1


def test_repository_lists_idle_buckets_ready_for_scale_down(tmp_path) -> None:
    repo = BindingRepository(tmp_path / "bindings.db")
    repo.init_db()

    repo.reserve_user_instance(
        user_id="user-1",
        workspace_path="/tmp/workspaces/user-1",
        frontend_id="feishu-main",
    )
    repo.mark_user_instance_online("user-1")
    repo.release_user_instance("user-1", bucket_id="bucket-0", instance_id="user-1")
    with repo._conn() as conn:
        conn.execute(
            "UPDATE buckets SET updated_at = '2000-01-01T00:00:00Z' WHERE bucket_id = 'bucket-0'"
        )
        conn.commit()

    buckets = repo.list_idle_buckets_ready_for_scale_down()

    assert len(buckets) == 1
    assert buckets[0]["bucket_id"] == "bucket-0"
