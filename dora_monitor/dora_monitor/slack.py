import logging

import requests

log = logging.getLogger(__name__)


class SlackNotifier:
    def __init__(self, webhook_url: str, network_label: str = "", timeout: int = 10):
        self.webhook_url = webhook_url
        self.network_label = network_label
        self.timeout = timeout

    def _prefix(self) -> str:
        return f"[{self.network_label}] " if self.network_label else ""

    def send(self, text: str) -> None:
        body = {"text": f"{self._prefix()}{text}"}
        try:
            r = requests.post(self.webhook_url, json=body, timeout=self.timeout)
            if r.status_code >= 300:
                log.error("slack webhook failed: %s %s", r.status_code, r.text)
        except requests.RequestException as e:
            log.error("slack webhook error: %s", e)
