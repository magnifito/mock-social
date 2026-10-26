"""MockPost configuration: env vars with defaults generated at startup.

Secrets are generated once per process and stay reachable via the
internal /api/config API and the get_channel_config MCP tool.
"""

from __future__ import annotations

import base64
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _random_secret(prefix: str = "") -> str:
    return prefix + secrets.token_hex(16)


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _parse_port_range(raw: str, default: tuple[int, int]) -> tuple[int, int]:
    if raw and "-" in raw:
        lo, hi = raw.split("-", 1)
        if lo.strip().isdigit() and hi.strip().isdigit():
            return int(lo), int(hi)
    return default


def _generate_or_load_tls_cert(cert_path: str, key_path: str) -> None:
    """Self-signed FTPS certificate, persisted so pinning clients keep trusting it."""
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return
    import datetime as _dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mockpost.local")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(issuer).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_dt.datetime.now(_dt.timezone.utc))
        .not_valid_after(_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    os.makedirs(os.path.dirname(os.path.abspath(cert_path)), exist_ok=True)
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                   serialization.NoEncryption()))
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))


def _generate_or_load_ssh_host_key(key_path: str) -> None:
    """SSH host key for SFTP, persisted so a client's known_hosts stays valid."""
    if os.path.exists(key_path):
        return
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    os.makedirs(os.path.dirname(os.path.abspath(key_path)), exist_ok=True)
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                   serialization.NoEncryption()))


def _generate_vapid_keys() -> dict:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization

    key = ec.generate_private_key(ec.SECP256R1())
    priv_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    pub_der = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return {
        "private_pem": priv_pem.decode(),
        "public_b64": base64.urlsafe_b64encode(pub_der).rstrip(b"=").decode(),
    }


@dataclass
class Settings:
    http_port: int = 8090
    smtp_port: int = 1025
    http_only: bool = False
    smtp_host: str = "127.0.0.1"
    db_path: str = "./data/mockpost.db"
    mockpost_url: str = "http://localhost:8090"
    # App -> SMTP port map, format "app-a:1025,app-b:1026"
    smtp_apps: dict = field(default_factory=dict)

    ftp_port: int = 2121
    ftps_port: int = 2122
    ftps_implicit_port: int = 2123
    sftp_port: int = 2222
    ftp_passive_ports: tuple = (30000, 30009)
    ftp_data_dir: str = "./data/ftp"
    sftp_data_dir: str = "./data/sftp"
    ftp_cert_path: str = "./data/ftp_cert.pem"
    ftp_key_path: str = "./data/ftp_key.pem"
    ssh_host_key_path: str = "./data/ssh_host_key"

    # Authentication is permissive by default: every channel takes the
    # credentials it is given, so no client needs changes. With
    # MOCKPOST_STRICT_AUTH each channel enforces its real authentication and
    # answers what the live API would (401/403/535) when it is missing.
    strict_auth: bool = False
    smtp_username: str = ""
    smtp_password: str = ""

    stripe_webhook_secret: str = field(default_factory=lambda: _random_secret("whsec_"))
    vapid_contact: str = "mailto:mockpost@test.local"
    vapid_keys: dict = field(default_factory=_generate_vapid_keys)
    oauth_jwt_key: str = field(default_factory=lambda: _random_secret())
    oauth_token_ttl_minutes: int = 60
    oauth_refresh_ttl_minutes: int = 60 * 24 * 7

    default_oauth_profile: dict = field(default_factory=lambda: {
        "google": {"sub": "fake-google-user-1", "email": "user@test.com", "email_verified": True, "name": "Fake User", "picture": "https://example.com/avatar.png"},
        "github": {"id": 424242, "login": "fakeuser", "name": "Fake User", "email": "user@test.com", "avatar_url": "https://example.com/avatar.png"},
        "facebook": {"id": "10234567890123456", "name": "Fake User", "email": "user@test.com", "picture": {"data": {"url": "https://example.com/avatar.png"}}},
        "x": {"id": "1234567890", "name": "Fake User", "username": "fakeuser", "email": "user@test.com", "profile_image_url": "https://example.com/avatar.png"},
    })

    # Per-app webhook secrets: app name -> {github, facebook, x}
    # Generated randomly on first use if the app does not define its own.
    social_webhook_secrets: dict = field(default_factory=dict)
    # Revoked OAuth JWTs (Google revoke): stateless, kept in-process.
    revoked_oauth_tokens: set = field(default_factory=set)

    def __post_init__(self) -> None:
        self.http_port = int(os.environ.get("MOCKPOST_HTTP_PORT", self.http_port))
        self.http_only = _flag("MOCKPOST_HTTP_ONLY", self.http_only)
        self.smtp_port = int(os.environ.get("MOCKPOST_SMTP_PORT", self.smtp_port))
        self.smtp_host = os.environ.get("MOCKPOST_SMTP_HOST", self.smtp_host)
        self.db_path = os.environ.get("MOCKPOST_DB_PATH", self.db_path)
        self.mockpost_url = os.environ.get("MOCKPOST_URL", self.mockpost_url)
        self.stripe_webhook_secret = os.environ.get("MOCKPOST_STRIPE_WEBHOOK_SECRET", self.stripe_webhook_secret)
        self.vapid_contact = os.environ.get("MOCKPOST_VAPID_CONTACT", self.vapid_contact)
        self.oauth_jwt_key = os.environ.get("MOCKPOST_OAUTH_JWT_KEY", self.oauth_jwt_key)
        self.strict_auth = _flag("MOCKPOST_STRICT_AUTH", self.strict_auth)
        self.smtp_username = os.environ.get("MOCKPOST_SMTP_USERNAME", self.smtp_username)
        self.smtp_password = os.environ.get("MOCKPOST_SMTP_PASSWORD", self.smtp_password)
        apps = os.environ.get("MOCKPOST_APPS", "")
        for item in apps.split(","):
            if ":" in item:
                name, port = item.rsplit(":", 1)
                if name and port.isdigit():
                    self.smtp_apps[name.strip()] = int(port)

        self.ftp_port = int(os.environ.get("MOCKPOST_FTP_PORT", self.ftp_port))
        self.ftps_port = int(os.environ.get("MOCKPOST_FTPS_PORT", self.ftps_port))
        self.ftps_implicit_port = int(os.environ.get("MOCKPOST_FTPS_IMPLICIT_PORT", self.ftps_implicit_port))
        self.sftp_port = int(os.environ.get("MOCKPOST_SFTP_PORT", self.sftp_port))
        self.ftp_passive_ports = _parse_port_range(
            os.environ.get("MOCKPOST_FTP_PASSIVE_PORTS", ""), self.ftp_passive_ports)
        self.ftp_data_dir = os.environ.get("MOCKPOST_FTP_DATA_DIR", self.ftp_data_dir)
        self.sftp_data_dir = os.environ.get("MOCKPOST_SFTP_DATA_DIR", self.sftp_data_dir)
        _generate_or_load_tls_cert(self.ftp_cert_path, self.ftp_key_path)
        _generate_or_load_ssh_host_key(self.ssh_host_key_path)


settings = Settings()


def utcnow() -> str:
    return _now()


def expire_at(minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
