import json
import logging

from dora_monitor.discord import DiscordNotifier
from dora_monitor.slack import SlackNotifier

log = logging.getLogger(__name__)


class Notifier:
    """Fan-out aggregator over Slack and/or Discord providers.

    Either provider may be `None`. In `dry_run` mode no providers are
    contacted; alerts are printed once regardless of how many would have
    been configured.
    """

    def __init__(
        self,
        *,
        slack: SlackNotifier | None = None,
        discord: DiscordNotifier | None = None,
        network_label: str = "",
        dry_run: bool = False,
        dry_debug: bool = False,
    ):
        self.slack = slack
        self.discord = discord
        self.network_label = network_label
        self.dry_run = dry_run
        self.dry_debug = dry_debug

    def _prefix(self) -> str:
        return f"[{self.network_label}] " if self.network_label else ""

    def send(self, text: str) -> None:
        if self.dry_run:
            print(f"[DRY-RUN] {self._prefix()}{text}")
            return
        if self.slack:
            self.slack.send(text)
        if self.discord:
            self.discord.send(text)

    def send_heartbeat(
        self,
        slack_blocks: list[dict],
        discord_embed: dict,
        fallback: str,
    ) -> None:
        if self.dry_run:
            print(f"[DRY-RUN] {self._prefix()}{fallback}")
            if self.dry_debug:
                print("[DRY-RUN slack blocks JSON]")
                print(json.dumps(slack_blocks, indent=2))
                print("[DRY-RUN discord embed JSON]")
                print(json.dumps(discord_embed, indent=2))
            return
        if self.slack:
            self.slack.send_blocks(slack_blocks, fallback)
        if self.discord:
            self.discord.send_embed(discord_embed, fallback)
