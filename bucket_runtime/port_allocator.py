from __future__ import annotations


class PortAllocator:
    def __init__(self, start: int, end: int) -> None:
        if end < start:
            raise ValueError("end must be >= start")
        self._free_ports = set(range(start, end + 1))
        self._used_ports: dict[str, int] = {}

    def allocate(self, user_key: str) -> int:
        existing = self._used_ports.get(user_key)
        if existing is not None:
            return existing
        if not self._free_ports:
            raise RuntimeError("no free port available")
        port = min(self._free_ports)
        self._free_ports.remove(port)
        self._used_ports[user_key] = port
        return port

    def release(self, user_key: str) -> None:
        port = self._used_ports.pop(user_key, None)
        if port is not None:
            self._free_ports.add(port)
