"""SFTP server (asyncssh) with username-as-app-name identity.

Runs natively on the main asyncio loop (unlike pyftpdlib's own reactor
thread for FTP): no thread bridge is needed to reach store.insert_message.
"""

from __future__ import annotations

import os

import asyncssh

from mockpost.apps import resolve_app_by_login, sftp_root
from mockpost.config import settings
from mockpost.store import insert_message


async def _record(app_id: str | None, command: str, path: bytes, size: int | None, direction: str) -> None:
    text_path = path.decode(errors="replace")
    await insert_message(
        "sftp", direction, f"{command} {text_path}",
        app_id=app_id,
        meta={"protocol": "sftp", "command": command, "path": text_path, "size": size},
    )


class AppSSHServer(asyncssh.SSHServer):
    """Resolves the app from the login username, permissive by default."""

    def connection_made(self, conn: asyncssh.SSHServerConnection) -> None:
        self._conn = conn

    def begin_auth(self, username: str) -> bool:
        return True

    def password_auth_supported(self) -> bool:
        return True

    async def validate_password(self, username: str, password: str) -> bool:
        app = await resolve_app_by_login("sftp", username)
        if app is None and settings.strict_auth:
            return False
        self._conn.set_extra_info(mockpost_app=app)
        return True


class AppSFTPServer(asyncssh.SFTPServer):
    """Chroots each session to the resolved app's SFTP root and records
    write/read/remove/mkdir/rmdir/readdir via store.insert_message.
    """

    def __init__(self, chan: asyncssh.SSHServerChannel) -> None:
        app = chan.get_connection().get_extra_info("mockpost_app")
        self._app_id = app["id"] if app else None
        home = sftp_root(app["name"] if app else None)
        super().__init__(chan, chroot=str(home))
        self._open_info: dict[int, tuple[bytes, str]] = {}

    async def open(self, path, pflags, attrs):
        file_obj = super().open(path, pflags, attrs)
        mode = "write" if pflags & asyncssh.FXF_WRITE else "read"
        self._open_info[id(file_obj)] = (path, mode)
        return file_obj

    async def close(self, file_obj) -> None:
        info = self._open_info.pop(id(file_obj), None)
        size = None
        if info is not None:
            try:
                size = os.fstat(file_obj.fileno()).st_size
            except OSError:
                size = None
        super().close(file_obj)
        if info is not None:
            path, mode = info
            command, direction = ("STOR", "inbound") if mode == "write" else ("RETR", "outbound")
            await _record(self._app_id, command, path, size, direction)

    async def remove(self, path) -> None:
        super().remove(path)
        await _record(self._app_id, "DELE", path, None, "inbound")

    async def mkdir(self, path, attrs) -> None:
        super().mkdir(path, attrs)
        await _record(self._app_id, "MKD", path, None, "inbound")

    async def rmdir(self, path) -> None:
        super().rmdir(path)
        await _record(self._app_id, "RMD", path, None, "inbound")

    async def scandir(self, path):
        async for entry in super().scandir(path):
            yield entry
        await _record(self._app_id, "LIST", path, None, "inbound")


_servers: list[asyncssh.SSHAcceptor] = []


async def start_sftp() -> None:
    if _servers:
        return
    server = await asyncssh.listen(
        "127.0.0.1", settings.sftp_port,
        server_factory=AppSSHServer,
        server_host_keys=[settings.ssh_host_key_path],
        sftp_factory=AppSFTPServer,
        allow_scp=False,
    )
    _servers.append(server)


async def stop_sftp() -> None:
    for server in _servers:
        server.close()
        await server.wait_closed()
    _servers.clear()
