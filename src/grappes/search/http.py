"""Fournisseur générique `type: http` : toute API HTTP décrite par un fichier YAML.

Couvre : authentification (en-tête, paramètre d'URL, Bearer, Basic, jeton OAuth fourni en variable
d'environnement), GET/POST, corps JSON ou formulaire, gabarits (`{sujet}`, `{mots_cles}`, `{limite}`,
`{page}`, `{decalage}`, `{curseur}`), pagination (page, décalage, curseur, lien « suivant »),
réponses JSON/XML/CSV, chemins vers les champs et transformations simples. Voir
`config/recherche/exemples/` et `docs/utilisateur.md`.

Sécurité : pas de redirection suivie, taille de réponse bornée, lien « suivant » limité à
l'origine configurée, aucun secret dans le journal.
"""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from grappes.search.base import (
    ProviderStop,
    ProviderUnavailable,
    SearchContext,
    SearchError,
    SearchProvider,
    register_provider,
)
from grappes.search.build import build_candidate
from grappes.search.formats import decode
from grappes.search.models import Candidate
from grappes.search.paths import first, select

MAX_BYTES = 2_000_000
MAX_PAGES = 20
_PLACEHOLDER = re.compile(r"\{(sujet|mots_cles|limite|page|decalage|curseur)\}")
_NUMERIC = {"limite", "page", "decalage"}


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes


Transport = Callable[[str, str, Mapping[str, str], bytes | None, float, int], Response]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any):
        return None  # une redirection n'est jamais suivie : elle est rendue comme réponse 3xx


def urllib_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout: float,
    max_bytes: int,
) -> Response:
    """Transport par défaut (bibliothèque standard). Seuls http et https sont admis."""
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise SearchError("schéma d'URL non autorisé (http ou https uniquement)")
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as resp:
            data, status, hdrs = resp.read(max_bytes + 1), resp.status, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        data, status, hdrs = exc.read(max_bytes + 1), exc.code, dict(exc.headers or {})
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SearchError(f"erreur réseau : {type(exc).__name__}") from exc
    if len(data) > max_bytes:
        raise SearchError(f"réponse trop volumineuse (> {max_bytes} octets)")
    return Response(status, hdrs, data)


def render(template: Any, values: Mapping[str, str]) -> Any:
    """Gabarit récursif ; remplace seulement les marques connues (aucune évaluation)."""
    if isinstance(template, str):
        whole = _PLACEHOLDER.fullmatch(template)
        if (
            whole
            and whole.group(1) in _NUMERIC
            and values.get(whole.group(1), "").lstrip("-").isdigit()
        ):
            return int(values[whole.group(1)])
        return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), ""), template)
    if isinstance(template, Mapping):
        return {str(k): render(v, values) for k, v in template.items()}
    if isinstance(template, list):
        return [render(v, values) for v in template]
    return template


def _same_origin(a: str, b: str) -> bool:
    pa, pb = urllib.parse.urlsplit(a), urllib.parse.urlsplit(b)
    return (pa.scheme, pa.hostname, pa.port) == (pb.scheme, pb.hostname, pb.port)


@register_provider
class HttpProvider(SearchProvider):
    type = "http"

    def __init__(self, provider_id: str, options: Mapping[str, Any], ctx: SearchContext,
                 transport: Transport | None = None):  # fmt: skip
        super().__init__(provider_id, options, ctx)
        self.transport = transport or urllib_transport

    # --- authentification -----------------------------------------------------------------

    def _auth(self) -> tuple[dict[str, str], dict[str, str]]:
        """(en-têtes, paramètres d'URL) d'authentification. Le journal n'enregistre que l'URL sans
        ces paramètres, et masque en plus toute valeur secrète."""
        a = self.options.get("auth") or {"type": "none"}
        if not isinstance(a, Mapping):
            raise SearchError("`auth` doit être un mapping")
        kind = a.get("type", "none")
        if kind == "none":
            return {}, {}
        if kind == "header":
            value = self.ctx.env_secret(a.get("env"))
            return {str(a.get("name", "Authorization")): f"{a.get('prefix', '')}{value}"}, {}
        if kind == "query":
            return {}, {str(a.get("name", "key")): self.ctx.env_secret(a.get("env"))}
        if kind in (
            "bearer",
            "oauth_env",
        ):  # jeton OAuth obtenu ailleurs et fourni par l'environnement
            return {"Authorization": f"Bearer {self.ctx.env_secret(a.get('env'))}"}, {}
        if kind == "basic":
            user, pwd = (
                self.ctx.env_secret(a.get("user_env")),
                self.ctx.env_secret(a.get("password_env")),
            )
            token = base64.b64encode(f"{user}:{pwd}".encode()).decode()
            return {"Authorization": f"Basic {token}"}, {}
        raise SearchError(f"type d'authentification inconnu : {kind!r}")

    def _robots(self, base_url: str):
        """Respect de robots.txt pour les flux publics (RSS) : `respecter_robots: true` dans le YAML."""
        from grappes.fetch.robots import (
            RobotsCache,
        )  # import tardif : `grappes.fetch` importe ce module

        ua = str((self.options.get("headers") or {}).get("User-Agent", "grappes-recherche/1"))

        def fetch(u: str) -> tuple[int, bytes]:
            try:
                self.ctx.throttle()
                r = self.transport("GET", u, {"User-Agent": ua}, None, 15.0, 500_000)
                self.ctx.journal.log(self.id, "-", f"GET {u}", str(r.status))
                return r.status, r.body
            except Exception as exc:  # noqa: BLE001 - robots illisible = prudence
                self.ctx.journal.log(self.id, "-", f"GET {u}", f"erreur {type(exc).__name__}")
                return 0, b""

        return RobotsCache(fetch, ua)

    def _espacer(self, delai_s: float) -> None:
        """Délai minimal entre deux appels de CE fournisseur (`delai_entre_appels_s`), pour les services qui limitent le débit
        (ex. GDELT : un appel toutes les 5 secondes). Sans option, aucun délai."""
        if delai_s <= 0:
            return
        attente = getattr(self, "_dernier_appel", 0.0) + delai_s - time.monotonic()
        if attente > 0:
            time.sleep(attente)
        self._dernier_appel = time.monotonic()

    # --- recherche -------------------------------------------------------------------------

    def search(self, keyword: str, limit: int) -> Iterable[Candidate]:
        o = self.options
        # Flux sans requête (RSS : toujours les mêmes éléments quel que soit le mot-clé) : lu une seule fois par
        # exécution, avec son propre plafond de résultats.
        if o.get("sans_mots_cles"):
            if getattr(self, "_deja_lu", False):
                return
            self._deja_lu = True
        limit = int(o.get("limite_resultats", limit))
        base_url = o.get("url")
        if not isinstance(base_url, str) or urllib.parse.urlsplit(base_url).scheme not in (
            "http",
            "https",
        ):
            raise ProviderUnavailable("`url` http(s) manquante ou invalide")
        method = str(o.get("method", "GET")).upper()
        if method not in ("GET", "POST"):
            raise ProviderUnavailable(f"méthode non gérée : {method}")
        auth_headers, auth_params = self._auth()
        robots = self._robots(base_url) if o.get("respecter_robots") and self.ctx.execute else None
        pag = o.get("pagination") or {}
        kind = pag.get("type", "none")
        if kind not in ("none", "page", "offset", "cursor", "next_link"):
            raise ProviderUnavailable(f"pagination inconnue : {kind!r}")
        max_pages = max(1, min(int(pag.get("max_pages", 1 if kind == "none" else 3)), MAX_PAGES))
        resp_spec = o.get("response") or {}
        fmt = str(resp_spec.get("format", "json"))
        fields = o.get("fields") or {}
        quota = o.get("quota")
        page = int(pag.get("start", 1 if kind == "page" else 0)) if kind == "page" else 1
        offset = int(pag.get("start", 0)) if kind == "offset" else 0
        step = int(pag.get("size", limit))
        cursor, next_url = "", None
        produced = 0
        for _ in range(max_pages):
            values = {"sujet": self.ctx.sujet or keyword, "mots_cles": keyword,
                      "limite": str(min(limit, step) if kind != "none" else limit),
                      "page": str(page), "decalage": str(offset), "curseur": cursor}  # fmt: skip
            params = {
                k: v for k, v in ((k, render(t, values)) for k, t in (o.get("params") or {}).items())
                if v != "" or not _PLACEHOLDER.search(str(o["params"][k]))
            }  # fmt: skip
            if kind == "page" and pag.get("param"):
                params[str(pag["param"])] = str(page)
            if kind == "offset" and pag.get("param"):
                params[str(pag["param"])] = str(offset)
            if kind == "cursor" and pag.get("param") and cursor:
                params[str(pag["param"])] = cursor
            if pag.get("size_param"):
                params[str(pag["size_param"])] = str(step)
            target = next_url or base_url
            if next_url:
                params = {}
            log_url = target + (
                ("?" if "?" not in target else "&") + urllib.parse.urlencode(params)
                if params
                else ""
            )
            full_params = {**params, **auth_params}
            url = target + (
                ("?" if "?" not in target else "&") + urllib.parse.urlencode(full_params)
                if full_params
                else ""
            )
            headers = {"Accept": "application/json, application/xml, text/csv, */*;q=0.1",
                       "User-Agent": "grappes-recherche/1", **{str(k): render(v, values) for k, v in (o.get("headers") or {}).items()},
                       **auth_headers}  # fmt: skip
            body = None
            if method == "POST" and o.get("body"):
                b = o["body"]
                content = render(b.get("content", {}), values)
                if b.get("type", "json") == "form":
                    body = urllib.parse.urlencode({k: str(v) for k, v in content.items()}).encode()
                    headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
                else:
                    body = json.dumps(content).encode()
                    headers.setdefault("Content-Type", "application/json")
            if not self.ctx.execute:
                self.ctx.journal.log(self.id, keyword, f"{method} {log_url}", "dry-run")
                return
            if robots is not None and not robots.check(url)[0]:
                raise ProviderUnavailable(
                    f"robots.txt interdit {urllib.parse.urlsplit(url).path or '/'} : flux non lu"
                )
            self.ctx.take(self.id, quota)
            self.ctx.throttle()
            self._espacer(float(o.get("delai_entre_appels_s", 0) or 0))
            resp = self.transport(
                method, url, headers, body, float(o.get("timeout_s", 20)), MAX_BYTES
            )
            self.ctx.journal.log(self.id, keyword, f"{method} {log_url}", str(resp.status))
            if resp.status in (403, 429):
                raise ProviderStop(f"{self.id} : HTTP {resp.status}, arrêt pour ce cycle")
            if resp.status >= 300:
                raise SearchError(f"{self.id} : HTTP {resp.status}")
            root = decode(resp.body, fmt, delimiter=str(resp_spec.get("delimiter", ",")))
            nodes = select(root, resp_spec.get("items"))
            if not nodes:
                return
            for item in nodes:
                cand = build_candidate(self.id, keyword, item, fields, o)
                if cand is not None:
                    yield cand
                    produced += 1
                    if produced >= limit:
                        return
            if kind == "page":
                page += 1
            elif kind == "offset":
                offset += step
            elif kind == "cursor":
                cursor = str(first(root, pag.get("cursor_path")) or "")
                if not cursor:
                    return
            elif kind == "next_link":
                link = first(root, pag.get("next_path"))
                if not isinstance(link, str) or not _same_origin(link, base_url):
                    return  # lien absent, ou hors de l'origine configurée : jamais suivi
                next_url = link
            else:
                return
