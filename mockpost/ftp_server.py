"""FTP/FTPS server (pyftpdlib) with username-as-app-name identity.

pyftpdlib runs its own reactor loop in a background thread, not asyncio
(same shape aiosmtpd's Controller uses for SMTP): control/data callbacks
fire off the main event loop, so store.insert_message is bridged via
asyncio.run_coroutine_threadsafe against the loop captured at start_ftp()
time.

Explicit FTPS (AUTH TLS) and implicit FTPS each get their own port: the
TLS handshake timing differs (implicit clients never send AUTH), so
auto-detecting one from the other is not attempted.
"""

from __future__ import annotations

import asyncio
import os
import threading

from pyftpdlib.authorizers import AuthenticationFailed, DummyAuthorizer
from pyftpdlib.handlers import FTPHandler, TLS_FTPHandler
from pyftpdlib.servers import FTPServer

from mockpost.apps import ftp_root, resolve_app_by_login
from mockpost.config import settings
from mockpost.store import insert_message

_main_loop: asyncio.AbstractEventLoop | None = None


def _resolve_sync(username: str) -> dict | None:
    assert _main_loop is not None, "start_ftp() not called"
    return asyncio.run_coroutine_threadsafe(
        resolve_app_by_login("ftp", username), _main_loop
    ).result(timeout=5)


def _record_sync(app_id: str | None, command: str, path: str, size: int | None, direction: str,
                 protocol: str = "ftp") -> None:
    assert _main_loop is not None, "start_ftp() not called"
    asyncio.run_coroutine_threadsafe(
        insert_message(
            "ftp", direction, f"{command} {path}",
            app_id=app_id,
            meta={"protocol": protocol, "command": command, "path": path, "size": size},
        ),
        _main_loop,
    )


class AppAuthorizer(DummyAuthorizer):
    """Resolves a 'virtual user' (an app) per login, instead of a static
    pre-registered table: the login username IS the app's name. Any
    password is accepted, unless MOCKPOST_STRICT_AUTH is on and the
    username matches no registered app.
    """

    def __init__(self) -> None:
        self._cache: dict[str, dict] = {}

    def _entry(self, username: str) -> dict:
        if username not in self._cache:
            app = _resolve_sync(username)
            if app is None and settings.strict_auth:
                raise AuthenticationFailed("Login incorrect.")
            self._cache[username] = {
                "app_id": app["id"] if app else None,
                "home": str(ftp_root(app["name"] if app else None)),
            }
        return self._cache[username]

    def validate_authentication(self, username, password, handler) -> None:
        self._entry(username)

    def get_home_dir(self, username) -> str:
        return self._entry(username)["home"]

    def has_user(self, username) -> bool:
        return True

    def has_perm(self, username, perm, path=None) -> bool:
        return True

    def get_perms(self, username) -> str:
        return self.read_perms + self.write_perms

    def impersonate_user(self, username, password) -> None:
        pass

    def terminate_impersonation(self, username) -> None:
        pass

    def get_msg_login(self, username) -> str:
        return "Login successful."

    def get_msg_quit(self, username) -> str:
        return "Goodbye."


class CaptureHandlerMixin:
    """Records STOR/RETR/DELE/MKD/RMD/LIST via store.insert_message."""

    def _app_id(self) -> str | None:
        return self.authorizer._entry(self.username)["app_id"]

    def _protocol(self) -> str:
        return "ftps" if isinstance(self, TLS_FTPHandler) else "ftp"

    def on_file_sent(self, file) -> None:
        _record_sync(self._app_id(), "RETR", self.fs.fs2ftp(file), os.path.getsize(file), "outbound",
                    self._protocol())

    def on_file_received(self, file) -> None:
        _record_sync(self._app_id(), "STOR", self.fs.fs2ftp(file), os.path.getsize(file), "inbound",
                    self._protocol())

    def ftp_DELE(self, path):
        result = super().ftp_DELE(path)
        if result is not None:
            _record_sync(self._app_id(), "DELE", self.fs.fs2ftp(path), None, "inbound", self._protocol())
        return result

    def ftp_MKD(self, path):
        result = super().ftp_MKD(path)
        if result is not None:
            _record_sync(self._app_id(), "MKD", self.fs.fs2ftp(path), None, "inbound", self._protocol())
        return result

    def ftp_RMD(self, path):
        existed = self.fs.isdir(path)
        result = super().ftp_RMD(path)
        if existed and not self.fs.lexists(path):
            _record_sync(self._app_id(), "RMD", self.fs.fs2ftp(path), None, "inbound", self._protocol())
        return result

    def ftp_LIST(self, path):
        result = super().ftp_LIST(path)
        if result is not None:
            _record_sync(self._app_id(), "LIST", self.fs.fs2ftp(path), None, "inbound", self._protocol())
        return result


class AppFTPHandler(CaptureHandlerMixin, FTPHandler):
    pass


class AppTLSFTPHandler(CaptureHandlerMixin, TLS_FTPHandler):
    pass


class ImplicitTLSFTPHandler(AppTLSFTPHandler):
    """Implicit FTPS: the TLS handshake starts immediately on connect,
    unlike explicit FTPS where the client issues AUTH TLS first.
    """

    def __init__(self, conn, server, ioloop=None) -> None:
        super().__init__(conn, server, ioloop)
        if self.connected:
            self.secure_connection(self.ssl_context)


_servers: list[FTPServer] = []
_threads: list[threading.Thread] = []


async def start_ftp() -> None:
    global _main_loop
    if _servers:
        return
    _main_loop = asyncio.get_running_loop()

    authorizer = AppAuthorizer()
    passive_ports = range(settings.ftp_passive_ports[0], settings.ftp_passive_ports[1] + 1)

    AppFTPHandler.authorizer = authorizer
    AppFTPHandler.passive_ports = passive_ports
    AppFTPHandler.banner = "MockPost FTP ready."

    for handler_cls, port in ((AppTLSFTPHandler, settings.ftps_port),
                              (ImplicitTLSFTPHandler, settings.ftps_implicit_port)):
        handler_cls.authorizer = authorizer
        handler_cls.passive_ports = passive_ports
        handler_cls.certfile = settings.ftp_cert_path
        handler_cls.keyfile = settings.ftp_key_path

    # All three FTPServer instances share pyftpdlib's default (global) IOLoop,
    # so a single background thread driving serve_forever() on any one of
    # them polls every registered socket; running one loop per thread would
    # have them fight over that shared ioloop and its reactor internals.
    for handler_cls, port in ((AppFTPHandler, settings.ftp_port),
                              (AppTLSFTPHandler, settings.ftps_port),
                              (ImplicitTLSFTPHandler, settings.ftps_implicit_port)):
        server = FTPServer(("127.0.0.1", port), handler_cls)
        _servers.append(server)

    def _run() -> None:
        _servers[0].serve_forever(handle_exit=False)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    _threads.append(thread)


async def stop_ftp() -> None:
    # One shared IOLoop backs all three servers: closing any one of them
    # closes every registered socket (see AppServer._run in start_ftp()).
    if _servers:
        _servers[0].close_all()
    for thread in _threads:
        thread.join(timeout=5)
    _servers.clear()
    _threads.clear()
