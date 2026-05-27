from __future__ import annotations

from container_up.binding_repository import BindingRepository


def test_repository_reserve_online_and_release(tmp_path) -> None:
    repo = BindingRepository(tmp_path / "bindings.db")
    repo.init_db()

    user, bucket, created = repo.reserve_user_instance(
        frontend_id="feishu-main",
        user_id="user-1",
        workspace_path="/tmp/workspaces/user-1",
    )

    assert created is True
    assert user["frontend_id"] == "feishu-main"
    assert user["user_id"] == "user-1"
    assert user["workspace_path"] == "/tmp/workspaces/user-1"
    assert user["status"] == "creating"
    assert user["instance_id"] == "feishu-main__user-1"
    assert bucket["bucket_id"] == "bucket-0"

    repo.mark_user_instance_online("feishu-main", "user-1")
    online = repo.get_user_instance("feishu-main", "user-1")

    assert online is not None
    assert online["status"] == "online"
    assert online["bucket_id"] == "bucket-0"
    assert online["workspace_path"] == "/tmp/workspaces/user-1"

    repo.release_user_instance(
        "feishu-main",
        "user-1",
        bucket_id="bucket-0",
        instance_id="feishu-main__user-1",
    )
    released = repo.get_user_instance("feishu-main", "user-1")
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
            frontend_id="feishu-main",
            user_id=f"user-{index}",
            workspace_path=f"/tmp/workspaces/user-{index}",
        )

    _, bucket, created = repo.reserve_user_instance(
        frontend_id="feishu-main",
        user_id="user-overflow",
        workspace_path="/tmp/workspaces/user-overflow",
    )

    assert created is True
    assert bucket["bucket_id"] == "bucket-1"


def test_repository_reuses_bucket_after_instance_release(tmp_path) -> None:
    repo = BindingRepository(tmp_path / "bindings.db")
    repo.init_db()

    for index in range(20):
        user_id = f"user-{index}"
        repo.reserve_user_instance(
            frontend_id="feishu-main",
            user_id=user_id,
            workspace_path=f"/tmp/workspaces/{user_id}",
        )
        repo.mark_user_instance_online("feishu-main", user_id)

    bucket = repo.get_bucket("bucket-0")
    assert bucket is not None
    assert bucket["current_instances"] == 20
    assert bucket["status"] == "full"

    repo.release_user_instance(
        "feishu-main",
        "user-0",
        bucket_id="bucket-0",
        instance_id="feishu-main__user-0",
    )

    bucket = repo.get_bucket("bucket-0")
    assert bucket is not None
    assert bucket["current_instances"] == 19
    assert bucket["status"] == "running"

    _, bucket, created = repo.reserve_user_instance(
        frontend_id="feishu-main",
        user_id="user-20",
        workspace_path="/tmp/workspaces/user-20",
    )

    assert created is True
    assert bucket["bucket_id"] == "bucket-0"


def test_repository_reuses_creating_reservation_without_double_counting(tmp_path) -> None:
    repo = BindingRepository(tmp_path / "bindings.db")
    repo.init_db()

    first_user, first_bucket, first_created = repo.reserve_user_instance(
        frontend_id="feishu-main",
        user_id="user-1",
        workspace_path="/tmp/workspaces/user-1",
    )
    second_user, second_bucket, second_created = repo.reserve_user_instance(
        frontend_id="feishu-main",
        user_id="user-1",
        workspace_path="/tmp/workspaces/user-1",
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
        frontend_id="feishu-main",
        user_id="user-1",
        workspace_path="/tmp/workspaces/user-1",
    )
    repo.mark_user_instance_online("feishu-main", "user-1")
    repo.release_user_instance(
        "feishu-main",
        "user-1",
        bucket_id="bucket-0",
        instance_id="feishu-main__user-1",
    )
    with repo._conn() as conn:
        conn.execute(
            "UPDATE buckets SET updated_at = '2000-01-01T00:00:00Z' WHERE bucket_id = 'bucket-0'"
        )
        conn.commit()

    buckets = repo.list_idle_buckets_ready_for_scale_down()

    assert len(buckets) == 1
    assert buckets[0]["bucket_id"] == "bucket-0"


def test_repository_keeps_same_user_id_isolated_by_frontend(tmp_path) -> None:
    repo = BindingRepository(tmp_path / "bindings.db")
    repo.init_db()

    first, _, _ = repo.reserve_user_instance(
        frontend_id="web-wd",
        user_id="user-1",
        workspace_path="/tmp/workspaces/web-wd/user-1",
    )
    second, _, _ = repo.reserve_user_instance(
        frontend_id="web-stream",
        user_id="user-1",
        workspace_path="/tmp/workspaces/web-stream/user-1",
    )

    assert first["instance_id"] == "web-wd__user-1"
    assert second["instance_id"] == "web-stream__user-1"
    assert repo.get_user_instance("web-wd", "user-1")["workspace_path"] == "/tmp/workspaces/web-wd/user-1"
    assert repo.get_user_instance("web-stream", "user-1")["workspace_path"] == "/tmp/workspaces/web-stream/user-1"
    assert [item["frontend_id"] for item in repo.list_for_user("user-1")] == ["web-stream", "web-wd"]
