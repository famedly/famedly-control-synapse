from synapse.http.server import DirectServeJsonResource

from famedly_control_synapse.rest.room import RoomIdRouter


class RootResource(DirectServeJsonResource):
    def __init__(
        self,
    ) -> None:
        super().__init__()
        self._room_id_router: RoomIdRouter | None = None

    def getChild(self, path: bytes, request):
        """Override to handle dynamic routing."""
        # for static path
        if path in self.children:
            return self.children[path]

        # for dynamic room_id path
        if self._room_id_router:
            return self._room_id_router.getChild(path, request)
        return super().getChild(path, request)
