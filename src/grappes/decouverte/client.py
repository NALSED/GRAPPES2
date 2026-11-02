"""Client HTTP JSON avec cache disque : une requête identique ne retourne jamais au réseau (« collecter une fois »).

Poli : User-Agent identifiable, délai minimal entre deux requêtes, nouvelle tentative bornée sur 429 et 5xx.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from grappes import avancement

UA = "grappes/0.1 (decouverte; landes_martin@yahoo.fr) python-urllib"


class DecouverteError(RuntimeError):
    pass


class Http:
    def __init__(self, cache_dir: str | Path, delai_s: float = 1.0, offline: bool = False, ttl_jours: int | None = None):
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.delai_s, self.offline, self.ttl_jours = delai_s, offline, ttl_jours
        self._last = 0.0
        self.appels_reseau = 0

    def _path(self, url: str) -> Path:
        return self.cache / f"{hashlib.sha256(url.encode()).hexdigest()[:24]}.json"

    def _perime(self, p: Path) -> bool:
        """Réponse plus vieille que `ttl_jours` (les registres changent) ; hors ligne, on garde ce qu'on a."""
        return (
            self.ttl_jours is not None
            and not self.offline
            and time.time() - p.stat().st_mtime > self.ttl_jours * 86400
        )

    def get_json(self, url: str) -> object:
        p = self._path(url)
        if p.exists() and not self._perime(p):
            return json.loads(p.read_text(encoding="utf-8"))
        if self.offline:
            raise DecouverteError("hors ligne : requête absente du cache")
        for essai in range(4):
            attente = self._last + self.delai_s - time.monotonic()
            if attente > 0:
                time.sleep(attente)
            self._last = time.monotonic()
            self.appels_reseau += 1
            avancement.signaler("appel", prise="découverte", hote=urllib.parse.urlsplit(url).netloc)
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=100) as r:
                    data = json.loads(r.read())
                p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                return data
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and essai < 3:
                    time.sleep(10 * (essai + 1))
                    continue
                raise DecouverteError(f"HTTP {exc.code} : {url[:120]}") from exc
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                if essai < 3:
                    time.sleep(10 * (essai + 1))
                    continue
                raise DecouverteError(f"réseau : {type(exc).__name__}") from exc
        raise DecouverteError("épuisé")  # pragma: no cover

    def get_text(self, url: str, encoding: str = "utf-8-sig") -> str:
        """Fichier texte (CSV) téléchargé une seule fois, gardé sur disque comme les réponses JSON."""
        p = self._path(url).with_suffix(".txt")
        if p.exists() and not self._perime(p):
            return p.read_text(encoding="utf-8")
        if self.offline:
            raise DecouverteError("hors ligne : fichier absent du cache")
        avancement.signaler("appel", prise="découverte", hote=urllib.parse.urlsplit(url).netloc)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=300) as r:
                texte = r.read().decode(encoding, errors="replace").lstrip("\ufeff")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DecouverteError(f"réseau : {type(exc).__name__}") from exc
        p.write_text(texte, encoding="utf-8")
        return texte

    def sparql(self, query: str) -> list[dict[str, str]]:
        url = "https://query.wikidata.org/sparql?" + urllib.parse.urlencode({"query": query, "format": "json"})
        rows = []
        for b in self.get_json(url)["results"]["bindings"]:  # type: ignore[index]
            rows.append({
                k: (v["value"].rsplit("/", 1)[-1] if v["value"].startswith("http://www.wikidata.org/entity/") else v["value"])
                for k, v in b.items()
            })  # fmt: skip
        return rows

    def wd_search(self, text: str, limit: int = 8, langue: str = "fr") -> list[dict]:
        url = "https://www.wikidata.org/w/api.php?" + urllib.parse.urlencode({
            "action": "wbsearchentities", "search": text, "language": langue, "uselang": langue,
            "limit": str(limit), "type": "item", "format": "json",
        })  # fmt: skip
        return self.get_json(url).get("search", [])  # type: ignore[union-attr]
