from fastapi import WebSocket


class StarletteSocket:
    def __init__(self, socket: WebSocket):
        self.socket = socket

    async def send(self, data: str):
        await self.socket.send_text(data)

    async def recv(self) -> str:
        packet = await self.socket.receive()
        if packet["type"] == "websocket.disconnect":
            raise ConnectionError("bucket disconnected")
        if packet.get("text") is None:
            raise ValueError("control protocol requires text frames")
        return packet["text"]
