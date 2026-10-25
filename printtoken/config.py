"""Settings for the gateway.

Secrets live in ~/.env, the one place credentials are kept on this machine. config.toml holds
everything else and only NAMES the .env keys it needs, so it can be shared without leaking anything.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    """A setting is missing or unusable."""


@dataclass(frozen=True)
class Config:
    printer: str
    data_dir: Path
    bot_token: str
    allowed_tg_ids: frozenset[int]
    notify_chat_id: int | None
    pin_hash: str
    imap_host: str
    imap_user: str
    imap_password: str
    mail_enabled: bool = True
    poll_seconds: int = 30
    dmarc_accept: frozenset[str] = frozenset({"pass"})
    sender_allowlist: frozenset[str] = frozenset()
    token_ttl_min: int = 30
    max_outstanding: int = 3
    mints_per_hour: int = 3
    pin_failures_per_hour: int = 5
    prints_per_hour: int = 20
    page_cap: int = 20
    max_bytes_telegram: int = 20 * 1024 * 1024
    max_bytes_mail: int = 25 * 1024 * 1024
    queue_size: int = 10
    workers: int = 2

    @property
    def mail_local(self) -> str:
        return self.imap_user.lower().partition("@")[0]

    @property
    def mail_domain(self) -> str:
        return self.imap_user.lower().partition("@")[2]

    def address_for(self, tag: str) -> str:
        return f"{self.mail_local}+{tag}@{self.mail_domain}"


def _ids(value: str) -> frozenset[int]:
    out: set[int] = set()
    for part in value.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError as exc:
            raise ConfigError(f"not a Telegram id: {part!r}") from exc
    return frozenset(out)


def load(config_path: Path | str | None = None, env_path: Path | str | None = None,
         need_secrets: bool = True) -> Config:
    config_path = Path(config_path or os.environ.get("PRINT_TOKEN_CONFIG") or ROOT / "config.toml")
    env_path = Path(env_path or os.environ.get("PRINT_TOKEN_ENV") or Path.home() / ".env")
    if not config_path.exists():
        raise ConfigError(f"{config_path} not found. Copy config.example.toml to config.toml first.")
    with open(config_path, "rb") as fh:
        raw = tomllib.load(fh)
    env = dotenv_values(env_path) if env_path.exists() else {}
    tg, mail, lim = raw.get("telegram", {}), raw.get("mail", {}), raw.get("limits", {})

    def secret(section: dict, field_name: str, required: bool = need_secrets) -> str:
        key = section.get(field_name)
        value = (env.get(key) or "").strip() if key else ""
        if required and not value:
            raise ConfigError(f"{key or field_name} is not set in {env_path}")
        return value

    data_dir = Path(raw.get("data_dir", "data"))
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    notify = secret(tg, "notify_chat_env", required=False)
    # With email off, the gateway runs Telegram only and needs no mailbox password at all.
    mail_enabled = bool(mail.get("enabled", True))
    need_mail = need_secrets and mail_enabled
    return Config(
        printer=raw.get("printer", "DRYRUN"),
        data_dir=data_dir,
        bot_token=secret(tg, "token_env"),
        allowed_tg_ids=_ids(secret(tg, "allowed_ids_env")),
        notify_chat_id=int(notify) if notify else None,
        pin_hash=secret(tg, "pin_hash_env", required=False),     # /print stays off until a PIN is set
        imap_host=mail.get("imap_host", "imap.gmail.com"),
        imap_user=secret(mail, "user_env", required=need_mail),
        imap_password=secret(mail, "password_env", required=need_mail).replace(" ", ""),
        mail_enabled=mail_enabled,
        poll_seconds=int(mail.get("poll_seconds", 30)),
        dmarc_accept=frozenset(r.lower() for r in mail.get("dmarc_accept", ["pass"])),
        sender_allowlist=frozenset(a.lower() for a in mail.get("sender_allowlist", [])),
        token_ttl_min=int(lim.get("token_ttl_min", 30)),
        max_outstanding=int(lim.get("max_outstanding", 3)),
        mints_per_hour=int(lim.get("mints_per_hour", 3)),
        pin_failures_per_hour=int(lim.get("pin_failures_per_hour", 5)),
        prints_per_hour=int(lim.get("prints_per_hour", 20)),
        page_cap=int(lim.get("page_cap", 20)),
        max_bytes_telegram=int(lim.get("max_mb_telegram", 20)) * 1024 * 1024,
        max_bytes_mail=int(lim.get("max_mb_mail", 25)) * 1024 * 1024,
        queue_size=int(lim.get("queue_size", 10)),
        workers=int(lim.get("workers", 2)),
    )
