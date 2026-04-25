from __future__ import annotations

from agent_gateway.repositories.binding_repository import BindingRepository


class BucketAllocator:
    def __init__(self, repo: BindingRepository, bucket_count: int) -> None:
        if bucket_count <= 0:
            raise ValueError("bucket_count must be positive")
        self._repo = repo
        self._bucket_count = bucket_count

    def allocate(self) -> int:
        counts = self._repo.count_by_bucket()
        return min(
            range(self._bucket_count),
            key=lambda bucket_id: (counts.get(bucket_id, 0), bucket_id),
        )
