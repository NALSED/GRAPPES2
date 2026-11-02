"""Fournisseur générique `type: mcp` : client d'un serveur MCP externe (stdio ou HTTP).

Distinct du serveur MCP en lecture seule de grappes (`grappes.mcp`, qui sert vers l'extérieur).
La configuration donne le transport, l'outil à appeler, la correspondance des arguments
(gabarits `{sujet}`, `{mots_cles}`, `{limite}`) et celle du résultat vers le format commun (mêmes `response`
et `fields` que pour `type: http`).

Sécurité : le résultat d'un outil est une donnée non fiable. Seul un JSON est lu (contenu
structuré, ou texte JSON) ; on n'en extrait que des champs typés et nettoyés. Il n'est jamais
exécuté, jamais pris pour une instruction, jamais transmis à un modèle.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterable, Mapping
from typing import Any

from grappes.search.base import (
    ProviderUnavailable,
    SearchError,
    SearchProvider,
    register_provider,
)
from grappes.search.build import build_candidate
from grappes.search.http import render
from grappes.search.models import Candidate
from grappes.search.paths import select

MAX_RESULT_CHARS = 2_000_000


def _result_to_root(result: Any) -> Any:
    """Résultat d'outil MCP -> structure JSON (contenu structuré, sinon texte JSON)."""
    if getattr(result, "is_error", False):
        raise SearchError("le serveur MCP a signalé une erreur")
    structured = getattr(result, "structured_content", None)
    # un outil qui renvoie une chaîne est enveloppé en {"result": "<texte>"} : on lit alors le texte
    wrapped_text = (
        isinstance(structured, Mapping)
        and set(structured) == {"result"}
        and isinstance(structured["result"], str)
    )
    if structured and not wrapped_text:
        return structured
    texts = [c.text for c in getattr(result, "content", []) if getattr(c, "type", "") == "text"]
    raw = "".join(texts)
    if not raw or len(raw) > MAX_RESULT_CHARS:
        raise SearchError("résultat MCP vide ou trop volumineux")
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise SearchError("résultat MCP non JSON (ignoré)") from exc


@register_provider
class McpProvider(SearchProvider):
    type = "mcp"

    def search(self, keyword: str, limit: int) -> Iterable[Candidate]:
        o = self.options
        tool = o.get("tool")
        transport = o.get("transport") or {}
        if not isinstance(tool, str) or not tool or not isinstance(transport, Mapping):
            raise ProviderUnavailable("`tool` et `transport` sont obligatoires")
        kind = transport.get("type")
        if kind == "stdio" and not transport.get("command"):
            raise ProviderUnavailable("transport stdio : `command` manquante")
        if kind == "http" and not str(transport.get("url", "")).startswith(("http://", "https://")):
            raise ProviderUnavailable("transport http : `url` http(s) manquante")
        if kind not in ("stdio", "http"):
            raise ProviderUnavailable(f"transport inconnu : {kind!r} (stdio, http)")
        headers = {
            str(h): self.ctx.env_secret(v) for h, v in (transport.get("headers_env") or {}).items()
        }
        env = {str(n): self.ctx.env_secret(n) for n in transport.get("env") or []}
        values = {
            "sujet": self.ctx.sujet or keyword,
            "mots_cles": keyword,
            "limite": str(limit),
            "page": "1",
            "decalage": "0",
            "curseur": "",
        }
        arguments = render(o.get("arguments") or {}, values)
        what = transport.get("command") or transport.get("url")
        if not self.ctx.execute:
            self.ctx.journal.log(self.id, keyword, f"mcp {kind} {what} outil={tool}", "dry-run")
            return
        self.ctx.take(self.id, o.get("quota"))
        self.ctx.throttle()
        timeout = float(o.get("timeout_s", 30))
        try:
            result = asyncio.run(
                asyncio.wait_for(
                    self._call(kind, transport, headers, env, tool, arguments, timeout), timeout
                )
            )
        except TimeoutError as exc:
            self.ctx.journal.log(
                self.id, keyword, f"mcp {kind} {what} outil={tool}", "délai dépassé"
            )
            raise SearchError(f"{self.id} : délai dépassé ({timeout:g} s)") from exc
        except SearchError:
            raise
        except Exception as exc:
            self.ctx.journal.log(
                self.id, keyword, f"mcp {kind} {what} outil={tool}", f"erreur {type(exc).__name__}"
            )
            raise SearchError(f"{self.id} : appel MCP impossible ({type(exc).__name__})") from exc
        self.ctx.journal.log(self.id, keyword, f"mcp {kind} {what} outil={tool}", "ok")
        resp_spec = o.get("response") or {}
        root = _result_to_root(result)
        produced = 0
        for item in select(root, resp_spec.get("items")):
            cand = build_candidate(self.id, keyword, item, o.get("fields") or {}, o)
            if cand is not None:
                yield cand
                produced += 1
                if produced >= limit:
                    return

    @staticmethod
    async def _call(kind, transport, headers, env, tool, arguments, timeout) -> Any:
        from mcp import ClientSession

        if kind == "stdio":
            from mcp.client.stdio import StdioServerParameters, stdio_client

            params = StdioServerParameters(
                command=str(transport["command"]),
                args=[str(a) for a in transport.get("args") or []],
                env={**{k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}, **env},
                cwd=transport.get("cwd"),
            )
            async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
                await session.initialize()
                return await session.call_tool(tool, arguments, read_timeout_seconds=timeout)
        import httpx2
        from mcp.client.streamable_http import streamable_http_client

        async with (
            httpx2.AsyncClient(headers=headers, timeout=timeout) as client,
            streamable_http_client(str(transport["url"]), http_client=client) as streams,
            ClientSession(streams[0], streams[1]) as session,
        ):
            await session.initialize()
            return await session.call_tool(tool, arguments, read_timeout_seconds=timeout)
