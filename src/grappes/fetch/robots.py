"""robots.txt : lu une fois par hôte et par exécution, respecté strictement."""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

# fetch(url) -> (statut HTTP ou 0 si erreur réseau, corps)
Fetch = Callable[[str], tuple[int, bytes]]


class RobotsCache:
    def __init__(self, fetch: Fetch, user_agent: str):
        self._fetch = fetch
        self._token = user_agent.split("/")[0].split()[0] if user_agent.strip() else "*"
        self._cache: dict[tuple[str, str], RobotFileParser | bool] = {}

    def _load(self, scheme: str, netloc: str) -> RobotFileParser | bool:
        status, body = self._fetch(f"{scheme}://{netloc}/robots.txt")
        if status in (404, 410):
            return True  # pas de robots.txt : rien n'est interdit
        if status == 200:
            rp = RobotFileParser()
            rp.parse(body.decode("utf-8", "replace").splitlines())
            return rp
        return False  # 401/403/429/5xx/erreur : par prudence, on ne visite pas cet hôte

    def check(self, url: str) -> tuple[bool, float]:
        """(autorisé ?, délai demandé en secondes par `Crawl-delay`)."""
        parts = urlsplit(url)
        key = (parts.scheme, parts.netloc)
        if key not in self._cache:
            self._cache[key] = self._load(*key)
        rp = self._cache[key]
        if rp is True:
            return True, 0.0
        if rp is False:
            return False, 0.0
        delay = rp.crawl_delay(self._token) or rp.crawl_delay("*") or 0
        return rp.can_fetch(self._token, url), float(delay)
