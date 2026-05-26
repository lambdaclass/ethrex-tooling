import logging
import re

import requests

from dora_monitor.slack import _split_on_lines

log = logging.getLogger(__name__)

# Discord webhook hard limit on `content` is 2000. Reserve headroom for the
# network-label prefix and the "(i/n)" series marker we may append.
_MAX_TEXT = 1900

# Translate the Slack `:shortcode:` tokens we actually emit (see slack.py /
# checks.py) into unicode glyphs so Discord renders something meaningful.
# Unknown shortcodes pass through unchanged; we only rewrite the keys here.
_EMOJI_MAP = {
    ":warning:": "⚠️",
    ":red_circle:": "\U0001f534",
    ":large_green_circle:": "\U0001f7e2",
    ":large_yellow_circle:": "\U0001f7e1",
    ":large_orange_circle:": "\U0001f7e0",
    ":white_circle:": "⚪",
    ":fork_and_knife:": "\U0001f374",
    ":white_check_mark:": "✅",
    ":turtle:": "\U0001f422",
    ":zap:": "⚡",
    ":package:": "\U0001f4e6",
    ":fire:": "\U0001f525",
    ":rocket:": "\U0001f680",
    ":mag:": "\U0001f50d",
    ":desktop_computer:": "\U0001f5a5️",
}
_EMOJI_RE = re.compile(r":[a-z0-9_+-]+:")

# Slack mrkdwn uses single asterisks for bold; Discord markdown reads that
# as italic. Rewrite `*X*` → `**X**` while leaving anything already doubled
# or sitting inside a backtick span alone.
_BOLD_RE = re.compile(r"(?<![\*`])\*([^*\n`]+?)\*(?!\*)")


def to_discord_markdown(text: str) -> str:
    """Translate Slack-flavored text (mrkdwn + `:shortcode:` emoji) to Discord
    markdown. Idempotent for already-Discord-formatted strings."""
    text = _EMOJI_RE.sub(lambda m: _EMOJI_MAP.get(m.group(0), m.group(0)), text)
    text = _BOLD_RE.sub(r"**\1**", text)
    return text


class DiscordNotifier:
    def __init__(self, webhook_url: str, network_label: str = "", timeout: int = 10):
        self.webhook_url = webhook_url
        self.network_label = network_label
        self.timeout = timeout

    def _prefix(self) -> str:
        return f"[{self.network_label}] " if self.network_label else ""

    def _post(self, payload: dict, what: str) -> None:
        try:
            r = requests.post(self.webhook_url, json=payload, timeout=self.timeout)
            if r.status_code == 429:
                retry = r.headers.get("Retry-After", "?")
                log.error("discord rate-limited (429, retry-after=%s); %s dropped", retry, what)
            elif r.status_code >= 300:
                log.error("discord webhook (%s) failed: %s %s", what, r.status_code, r.text[:200])
        except requests.RequestException as e:
            log.error("discord webhook (%s) error: %s", what, e)

    def send(self, text: str) -> None:
        body = to_discord_markdown(f"{self._prefix()}{text}")
        if len(body) <= _MAX_TEXT:
            self._post({"content": body}, "alert")
            return
        chunks = _split_on_lines(body, _MAX_TEXT)
        total = len(chunks)
        for i, chunk in enumerate(chunks, 1):
            self._post({"content": f"{chunk}\n*({i}/{total})*"}, "alert")

    def send_embed(self, embed: dict, fallback: str) -> None:
        """Post a Discord rich embed. `fallback` is sent as the `content`
        field so notifications and screen readers still get a textual digest.
        """
        prefix = self._prefix()
        content = f"{prefix}{fallback}"
        if len(content) > _MAX_TEXT:
            content = content[: _MAX_TEXT - 1] + "…"
        self._post({"content": content, "embeds": [embed]}, "embed")
