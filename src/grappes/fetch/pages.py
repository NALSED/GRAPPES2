"""`PageFetcher` : récupère une page publique en respectant les règles générales, la normalise au
format web existant (`WebFetcher`) et signale tout refus avec son motif. Voir `grappes.fetch`."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit

from grappes.autonomy import Autonomy
from grappes.fetch.html import extract
from grappes.fetch.robots import RobotsCache
from grappes.fetch.rules import Rules
from grappes.search.base import Journal
from grappes.search.http import Response, Transport, urllib_transport
from grappes.search.safe import clean_text, clean_url

MAX_REDIRECTS = 3
MAX_LINKS = 100
MAX_TEXT = 20_000
_REDIRECT = {301, 302, 303, 307, 308}
_LOGIN_PATH = re.compile(
    r"/(log-?in|sign-?in|connexion|authentification|auth|account/login)(/|$|\?)", re.IGNORECASE
)
_CAPTCHA = re.compile(
    r"captcha|are you a robot|verify you are human|vérifiez que vous êtes un humain|unusual traffic",
    re.IGNORECASE,
)


@dataclass(slots=True)
class FetchResult:
    url: str
    ok: bool
    status: str  # code HTTP, ou court motif (« refusé », « robots », « réseau »...)
    reason: str = ""  # explication lisible en cas de refus
    record: dict | None = (
        None  # enregistrement au format web (voir docs/conception/formats_json.md)
    )
    links: list[str] = field(default_factory=list)  # liens sortants http(s) validés
    domain_blocked: bool = False  # 403/429/captcha : plus aucune requête vers ce domaine ce cycle


class PageFetcher:
    def __init__(
        self,
        autonomy: Autonomy,
        journal: Journal | None = None,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        allow_private: bool = False,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.autonomy = autonomy
        self.journal = journal or Journal(None)
        self.transport = transport or urllib_transport
        self.sleep, self.monotonic, self.now = sleep, monotonic, now
        self.rules = Rules(
            autonomy.interdits_domaines,
            autonomy.interdits_motifs,
            autonomy.reseaux_non_lus,
            allow_private,
        )
        self.robots = RobotsCache(self._raw_get, autonomy.user_agent)
        self.blocked: set[str] = set()  # domaines bloqués pour ce cycle (403, 429, captcha)
        self._last: dict[str, float] = {}  # dernière requête par hôte
        self.requests = 0

    # --- réseau -----------------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        # aucun cookie, aucune authentification : seulement un User-Agent identifiable
        return {
            "User-Agent": self.autonomy.user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
        }

    def _wait(self, host: str, extra_delay: float = 0.0) -> None:
        """Délai d'au moins `delai_domaine_s` (>= 1 s) entre deux requêtes vers un même hôte."""
        gap = max(self.autonomy.delai_domaine_s, extra_delay)
        last = self._last.get(host)
        if last is not None:
            wait = last + gap - self.monotonic()
            if wait > 0:
                self.sleep(wait)
        self._last[host] = self.monotonic()

    def _raw_get(self, url: str) -> tuple[int, bytes]:
        """GET sans redirection pour robots.txt ; (0, b"") si erreur réseau."""
        host = urlsplit(url).netloc
        self._wait(host)
        self.requests += 1
        try:
            resp = self.transport("GET", url, self._headers(), None, 15.0, 500_000)
        except Exception as exc:  # noqa: BLE001 - toute erreur réseau = robots indisponible
            self.journal.log("pages", "", f"GET {url}", f"erreur {type(exc).__name__}")
            return 0, b""
        self.journal.log("pages", "", f"GET {url}", str(resp.status))
        return resp.status, resp.body

    # --- récupération -----------------------------------------------------------------------

    def fetch(self, url: str) -> FetchResult:
        """Récupère `url` (redirections suivies, chaque étape revérifiée). Ne lève jamais."""
        current, hops = url, 0
        while True:
            refus = self._refuse(current)
            if refus:
                return FetchResult(url, False, "refusé", refus)
            host = (urlsplit(current).hostname or "").casefold()
            allowed, crawl_delay = self.robots.check(current)
            if not allowed:
                return FetchResult(
                    url, False, "robots", "robots.txt : visite interdite ou illisible"
                )
            self._wait(urlsplit(current).netloc, crawl_delay)
            self.requests += 1
            try:
                resp: Response = self.transport(
                    "GET", current, self._headers(), None, 20.0, self.autonomy.max_octets_page
                )
            except Exception as exc:  # noqa: BLE001 - réseau, taille, schéma : jamais fatal
                self.journal.log("pages", "", f"GET {current}", f"erreur {type(exc).__name__}")
                return FetchResult(
                    url, False, "réseau", f"échec de la requête ({type(exc).__name__})"
                )
            self.journal.log("pages", "", f"GET {current}", str(resp.status))
            if resp.status in (403, 429):
                self.blocked.add(host)
                return FetchResult(
                    url,
                    False,
                    str(resp.status),
                    "refus du serveur (403/429) : arrêt sur ce domaine",
                    domain_blocked=True,
                )
            if resp.status in _REDIRECT:
                hops += 1
                target = resp.headers.get("Location") or resp.headers.get("location") or ""
                if hops > MAX_REDIRECTS or not target:
                    return FetchResult(
                        url, False, str(resp.status), "redirection invalide ou trop longue"
                    )
                current = urljoin(current, target)
                continue
            if resp.status == 401:
                return FetchResult(
                    url, False, "401", "connexion requise : ignoré (aucun compte n'est utilisé)"
                )
            if resp.status >= 400:
                return FetchResult(url, False, str(resp.status), "page introuvable ou en erreur")
            return self._normalize(url, current, resp)

    def _refuse(self, url: str) -> str | None:
        refus = self.rules.check_url(url)
        if refus:
            return refus
        host = (urlsplit(url).hostname or "").casefold()
        if host in self.blocked:
            return "domaine bloqué pour ce cycle (403/429/captcha rencontré)"
        return None

    def _normalize(self, asked: str, final: str, resp: Response) -> FetchResult:
        host = (urlsplit(final).hostname or "").casefold()
        ctype = (
            resp.headers.get("Content-Type") or resp.headers.get("content-type") or ""
        ).casefold()
        if ctype and not ctype.startswith(("text/html", "application/xhtml")):
            return FetchResult(
                asked, False, str(resp.status), f"type non HTML ignoré ({ctype.split(';')[0]})"
            )
        page = extract(resp.body, ctype, final)
        if _LOGIN_PATH.search(urlsplit(final).path) or (
            page.has_password_field and len(page.text) < 600
        ):
            return FetchResult(
                asked, False, "connexion", "connexion requise : ignoré (aucun compte n'est utilisé)"
            )
        if _CAPTCHA.search(page.text[:2000] + " " + page.title):
            self.blocked.add(host)
            return FetchResult(
                asked,
                False,
                "captcha",
                "captcha ou contrôle anti-robot : non contourné",
                domain_blocked=True,
            )
        if page.noindex:
            return FetchResult(
                asked, False, "noindex", "la page demande à ne pas être indexée : ignorée"
            )
        # --- champs typés et nettoyés : tout le reste est jeté ---
        title = clean_text(page.title, 200)
        description = clean_text(page.description, 500)
        refus = self.rules.check_text(f"{final} {title} {description}")
        if refus:
            return FetchResult(asked, False, "refusé", refus)
        links: list[str] = []
        anchors: list[dict] = []
        for href, anchor in page.links:
            u = clean_url(href)
            if u and u not in links and len(links) < MAX_LINKS:
                links.append(u)
                anchors.append({"href": u, "anchor": clean_text(anchor, 100)})
        canonical = clean_url(page.canonical) or clean_url(final)
        lang = re.sub(r"[^A-Za-z-]", "", page.lang)[:10]
        stamp = self.now().strftime("%Y-%m-%dT%H:%M:%SZ")
        record = {
            "url": clean_url(final) or final,
            "canonical_url": canonical,
            "fetched_at": stamp,
            "title": title,
            "text": clean_text(
                f"{description}. {page.text}" if description else page.text, MAX_TEXT
            ),
            "lang": lang,
            "site_name": clean_text(page.site_name, 100),
            "links": anchors,
        }
        return FetchResult(asked, True, str(resp.status), "", record, links)
