"""FTP/FTPS/SFTP: transfers, identity resolution, isolation, timeline capture."""

from __future__ import annotations

import asyncio
import io
import os
import socket
import ssl
from ftplib import FTP, FTP_TLS

import asyncssh
import pytest


def _register(client, name: str) -> None:
    r = client.post("/api/apps", json={"name": name})
    assert r.status_code == 200, r.text


def test_ftp_upload_download_delete_list_mkdir_rmdir(client):
    _register(client, "ftp-app")
    with FTP() as ftp:
        ftp.connect("127.0.0.1", client.ftp_port, timeout=5)
        ftp.login("ftp-app", "whatever")
        ftp.storbinary("STOR report.csv", io.BytesIO(b"a,b,c\n1,2,3\n"))
        assert "report.csv" in ftp.nlst()

        buf = io.BytesIO()
        ftp.retrbinary("RETR report.csv", buf.write)
        assert buf.getvalue() == b"a,b,c\n1,2,3\n"

        ftp.mkd("sub")
        assert "sub" in ftp.nlst()
        ftp.rmd("sub")
        assert "sub" not in ftp.nlst()

        ftp.delete("report.csv")
        assert "report.csv" not in ftp.nlst()

    msgs = client.get("/api/messages", params={"channel": "ftp"}).json()
    commands = {m["meta"] and __import__("json").loads(m["meta"])["command"] for m in msgs}
    assert {"STOR", "RETR", "MKD", "RMD", "DELE"} <= commands
    app = client.get("/api/apps/by-name/ftp-app").json()
    assert all(m["app_id"] == app["id"] for m in msgs)


def test_ftps_explicit_transfer(client):
    _register(client, "ftps-app")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with FTP_TLS(context=ctx) as ftp:
        ftp.connect("127.0.0.1", client.ftps_port, timeout=5)
        ftp.auth()
        ftp.login("ftps-app", "whatever")
        ftp.prot_p()
        ftp.storbinary("STOR secure.txt", io.BytesIO(b"secret"))
        buf = io.BytesIO()
        ftp.retrbinary("RETR secure.txt", buf.write)
        assert buf.getvalue() == b"secret"


def test_ftps_implicit_handshake(client):
    """Implicit FTPS: TLS starts immediately on connect, no AUTH command."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection(("127.0.0.1", client.ftps_implicit_port), timeout=5) as sock:
        with ctx.wrap_socket(sock) as tls_sock:
            banner = tls_sock.recv(1024)
            assert banner.startswith(b"220")


def test_sftp_upload_download_delete_list_mkdir_rmdir(client):
    _register(client, "sftp-app")

    async def _run():
        async with asyncssh.connect(
            "127.0.0.1", client.sftp_port, username="sftp-app", password="whatever",
            known_hosts=None,
        ) as conn:
            async with conn.start_sftp_client() as sftp:
                await sftp.open("report.csv", "wb")
                async with sftp.open("report.csv", "wb") as f:
                    await f.write(b"hello")
                names = await sftp.listdir(".")
                assert "report.csv" in names

                async with sftp.open("report.csv", "rb") as f:
                    data = await f.read()
                assert data == b"hello"

                await sftp.mkdir("sub")
                names = await sftp.listdir(".")
                assert "sub" in names
                await sftp.rmdir("sub")

                await sftp.remove("report.csv")
                names = await sftp.listdir(".")
                assert "report.csv" not in names

    asyncio.run(_run())

    msgs = client.get("/api/messages", params={"channel": "sftp"}).json()
    import json as _json
    commands = {_json.loads(m["meta"])["command"] for m in msgs if m["meta"]}
    assert {"STOR", "RETR", "MKD", "RMD", "DELE"} <= commands


def test_username_as_appname_isolation(client):
    _register(client, "app-a")
    _register(client, "app-b")
    with FTP() as ftp:
        ftp.connect("127.0.0.1", client.ftp_port, timeout=5)
        ftp.login("app-a", "x")
        ftp.storbinary("STOR only-a.txt", io.BytesIO(b"a"))

    with FTP() as ftp:
        ftp.connect("127.0.0.1", client.ftp_port, timeout=5)
        ftp.login("app-b", "x")
        assert "only-a.txt" not in ftp.nlst()


def test_unregistered_username_permissive_by_default(client):
    with FTP() as ftp:
        ftp.connect("127.0.0.1", client.ftp_port, timeout=5)
        ftp.login("no-such-app", "whatever")
        ftp.storbinary("STOR x.txt", io.BytesIO(b"x"))

    msgs = client.get("/api/messages", params={"channel": "ftp"}).json()
    assert any(m["app_id"] is None for m in msgs)


def test_unregistered_username_rejected_in_strict_mode(client):
    from mockpost import config as cfg

    cfg.settings.strict_auth = True
    try:
        with FTP() as ftp:
            ftp.connect("127.0.0.1", client.ftp_port, timeout=5)
            with pytest.raises(Exception):
                ftp.login("no-such-app", "whatever")
    finally:
        cfg.settings.strict_auth = False


def test_path_traversal_is_contained(client):
    """pyftpdlib confines '..'/absolute paths to the virtual root instead
    of erroring: CWD past the root just stays at the root, never escaping
    to another app's files or the host filesystem."""
    _register(client, "trav-app")
    with FTP() as ftp:
        ftp.connect("127.0.0.1", client.ftp_port, timeout=5)
        ftp.login("trav-app", "x")
        ftp.cwd("../../..")
        assert ftp.pwd() == "/"


def test_tls_cert_and_ssh_host_key_are_persisted():
    from mockpost.config import _generate_or_load_ssh_host_key, _generate_or_load_tls_cert

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        cert_path = os.path.join(d, "cert.pem")
        key_path = os.path.join(d, "key.pem")
        host_key_path = os.path.join(d, "host_key")

        _generate_or_load_tls_cert(cert_path, key_path)
        cert_bytes_1 = open(cert_path, "rb").read()
        _generate_or_load_tls_cert(cert_path, key_path)
        cert_bytes_2 = open(cert_path, "rb").read()
        assert cert_bytes_1 == cert_bytes_2

        _generate_or_load_ssh_host_key(host_key_path)
        key_bytes_1 = open(host_key_path, "rb").read()
        _generate_or_load_ssh_host_key(host_key_path)
        key_bytes_2 = open(host_key_path, "rb").read()
        assert key_bytes_1 == key_bytes_2


def test_ftp_events_visible_in_timeline_markdown(client):
    _register(client, "tl-app")
    with FTP() as ftp:
        ftp.connect("127.0.0.1", client.ftp_port, timeout=5)
        ftp.login("tl-app", "x")
        ftp.storbinary("STOR seen.txt", io.BytesIO(b"seen"))

    md = client.get("/timeline/markdown").text
    assert "ftp" in md
    assert "seen.txt" in md or "STOR" in md
