"""Options of the journal app (same sources as the other apps: env HTH_*, options.json)."""

from __future__ import annotations

from typing import Literal

from ha_trading_helper.config import Settings

# OKX serves each account from one domain: EEA accounts (Spain included) answer
# "API key doesn't exist" (50119) on www.okx.com.
OkxDomain = Literal["www.okx.com", "eea.okx.com", "my.okx.com", "us.okx.com"]

# Everything is valued in this currency: the user's own choice, and the quote of their pairs.
REFERENCE = "USDC"


class JournalSettings(Settings):
    base_topic: str = "okx_journal"
    okx_domain: OkxDomain = "www.okx.com"

    # MariaDB (Home Assistant app "core-mariadb"). Without db_host: SQLite in data_dir.
    db_host: str = ""
    db_port: int = 3306
    db_name: str = "okx_journal"
    db_user: str = ""
    db_password: str = ""

    sync_minutes: int = 5
    timezone: str = "Europe/Madrid"  # for showing times; days are always UTC
    web_host: str = "0.0.0.0"  # noqa: S104  # nosec B104 - reached through HA ingress only
    web_port: int = 8099
    mqtt_enabled: bool = True

    @property
    def okx_base_url(self) -> str:
        return f"https://{self.okx_domain}"

    @property
    def okx_ready(self) -> bool:
        return all((self.okx_api_key, self.okx_api_secret, self.okx_passphrase))

    def db_url(self) -> str:
        if not self.db_host:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            return f"sqlite:///{self.data_dir / 'journal.db'}"
        from sqlalchemy.engine import URL

        url = URL.create(
            "mysql+pymysql",
            username=self.db_user or None,
            password=self.db_password or None,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
            query={"charset": "utf8mb4"},
        )
        return url.render_as_string(hide_password=False)
