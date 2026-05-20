import logging

import requests

log = logging.getLogger(__name__)

# Slack mrkdwn text limit per message is 4000 chars. Keep some headroom for
# the network-label prefix and the "(i/n)" series marker we may append.
_MAX_TEXT = 3800


class SlackNotifier:
    def __init__(self, webhook_url: str, network_label: str = "", timeout: int = 10):
        self.webhook_url = webhook_url
        self.network_label = network_label
        self.timeout = timeout

    def _prefix(self) -> str:
        return f"[{self.network_label}] " if self.network_label else ""

    def _post(self, text: str) -> None:
        try:
            r = requests.post(self.webhook_url, json={"text": text}, timeout=self.timeout)
            if r.status_code == 429:
                retry = r.headers.get("Retry-After", "?")
                log.error("slack rate-limited (429, retry-after=%s); alert dropped", retry)
            elif r.status_code >= 300:
                log.error("slack webhook failed: %s %s", r.status_code, r.text[:200])
        except requests.RequestException as e:
            log.error("slack webhook error: %s", e)

    def send(self, text: str) -> None:
        body = f"{self._prefix()}{text}"
        if len(body) <= _MAX_TEXT:
            self._post(body)
            return

        chunks = _split_on_lines(body, _MAX_TEXT)
        total = len(chunks)
        for i, chunk in enumerate(chunks, 1):
            self._post(f"{chunk}\n_({i}/{total})_")


def _split_on_lines(text: str, limit: int) -> list[str]:
    """Split text on newline boundaries into chunks of at most `limit` chars.

    Falls back to hard slicing for any single line longer than `limit` so a
    pathological input still gets through rather than being dropped.
    """
    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0
    for line in text.split("\n"):
        # Hard-slice a single oversized line into limit-sized pieces.
        if len(line) > limit:
            if buf:
                chunks.append("\n".join(buf))
                buf, buf_len = [], 0
            for i in range(0, len(line), limit):
                chunks.append(line[i : i + limit])
            continue
        add = len(line) + (1 if buf else 0)
        if buf_len + add > limit:
            chunks.append("\n".join(buf))
            buf, buf_len = [line], len(line)
        else:
            buf.append(line)
            buf_len += add
    if buf:
        chunks.append("\n".join(buf))
    return chunks
