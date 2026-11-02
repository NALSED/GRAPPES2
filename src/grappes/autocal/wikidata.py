"""Client Wikidata (SPARQL et API publique) avec cache disque : sert à fabriquer la référence « silver standard »
(supervision distante, Mintz et al. 2009) et à juger les résultats. Jamais utilisé comme fournisseur du système
(règle de non-circularité de DECISION_AUTOCAL.md).

Poli : un User-Agent identifiable, 2 s entre deux requêtes, nouvelle tentative bornée sur 429 et 5xx. Chaque réponse
est mise en cache par empreinte de la requête : un second appel identique ne touche pas au réseau.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SPARQL = "https://query.wikidata.org/sparql"
API = "https://www.wikidata.org/w/api.php"
UA = "grappes-autocal/0.1 (https://github.com/; landes_martin@yahoo.fr) python-urllib"
DELAY_S = 2.0


class WikidataError(RuntimeError):
    pass


class Wikidata:
    def __init__(self, cache_dir: str | Path, offline: bool = False):
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.offline = offline
        self._last = 0.0
        self.network_calls = 0

    def _path(self, kind: str, payload: str) -> Path:
        return self.cache / f"{kind}-{hashlib.sha256(payload.encode()).hexdigest()[:24]}.json"

    def _get(self, url: str) -> bytes:
        for attempt in range(4):
            wait = self._last + DELAY_S - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.network_calls += 1
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Accept": "application/json"}
            )
            try:
                with urllib.request.urlopen(req, timeout=100) as r:
                    return r.read()
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(10 * (attempt + 1))
                    continue
                raise WikidataError(f"HTTP {exc.code} : {url[:120]}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < 3:
                    time.sleep(10 * (attempt + 1))
                    continue
                raise WikidataError(f"réseau : {type(exc).__name__}") from exc
        raise WikidataError("épuisé")  # pragma: no cover

    def sparql(self, query: str) -> list[dict[str, str]]:
        """Lignes `{variable: valeur}` d'une requête SPARQL (valeurs brutes ; les QID sont réduits à `Q123`)."""
        p = self._path("sparql", query)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        if self.offline:
            raise WikidataError("hors ligne : requête SPARQL absente du cache")
        raw = self._get(SPARQL + "?" + urllib.parse.urlencode({"query": query, "format": "json"}))
        rows = []
        for b in json.loads(raw)["results"]["bindings"]:
            rows.append(
                {
                    k: (
                        v["value"].rsplit("/", 1)[-1]
                        if v["value"].startswith("http://www.wikidata.org/entity/")
                        else v["value"]
                    )
                    for k, v in b.items()
                }
            )
        p.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        return rows

    def api(self, **params: str) -> dict:
        """Appel `w/api.php` (JSON), mis en cache."""
        params = {**params, "format": "json"}
        key = json.dumps(params, sort_keys=True)
        p = self._path("api", key)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        if self.offline:
            raise WikidataError("hors ligne : appel API absent du cache")
        data = json.loads(self._get(API + "?" + urllib.parse.urlencode(params)))
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data

    def search(self, text: str, limit: int = 5, language: str = "fr") -> list[dict]:
        """`wbsearchentities` : [{id, label, description}]."""
        d = self.api(
            action="wbsearchentities", search=text, language=language, uselang=language,
            limit=str(limit), type="item",
        )  # fmt: skip
        return d.get("search", [])
