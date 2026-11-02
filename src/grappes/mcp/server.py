"""Serveur MCP en lecture seule (SDK `mcp` 2.x, `MCPServer`, transport stdio).

Variables d'environnement : GRAPPES_DB (défaut donnees/grappes.db), GRAPPES_CONFIG (défaut config).
Lancement : `python -m grappes.mcp.server`.
"""

from __future__ import annotations

import os

from mcp.server.mcpserver import MCPServer

from grappes.config import load_config
from grappes.mcp.queries import Queries, open_readonly


def create_server(db_path: str | None = None, config_dir: str | None = None) -> MCPServer:
    cfg = load_config(config_dir or os.environ.get("GRAPPES_CONFIG", "config"))
    path = db_path or os.environ.get("GRAPPES_DB", "donnees/grappes.db")
    open_readonly(path).close()  # échec immédiat si la base est absente
    server = MCPServer("grappes")

    def call(method: str, *args):
        """Les outils synchrones tournent dans des threads : une connexion en lecture seule par appel."""
        conn = open_readonly(path)
        try:
            return getattr(Queries(conn, cfg), method)(*args)
        finally:
            conn.close()

    @server.tool()
    def list_grappes() -> list[dict]:
        """Grappes disponibles (id, label, nombre d'entités)."""
        return call("list_grappes")

    @server.tool()
    def get_grappe(grappe_id: str) -> dict:
        """Détail d'une grappe : ancres, oppositions, statistiques."""
        return call("get_grappe", grappe_id)

    @server.tool()
    def top_sources(grappe_id: str, limit: int = 10, ecosystem: str | None = None) -> list[dict]:
        """Sources les plus influentes d'une grappe (limit plafonné à 100)."""
        return call("top_sources", grappe_id, limit, ecosystem)

    @server.tool()
    def search_entities(
        query: str, grappe_id: str | None = None, min_confidence: float = 0, limit: int = 20
    ) -> list[dict]:
        """Recherche d'entités par nom ou clé canonique."""
        return call("search_entities", query, grappe_id, min_confidence, limit)

    @server.tool()
    def get_entity(entity_id: str) -> dict:
        """Fiche d'une entité avec ses appartenances."""
        return call("get_entity", entity_id)

    @server.tool()
    def explain_assignment(entity_id: str, grappe_id: str) -> dict:
        """Preuves d'une appartenance : termes, citations, validations."""
        return call("explain_assignment", entity_id, grappe_id)

    @server.tool()
    def get_validated_refs(grappe_id: str, limit: int = 20) -> list[dict]:
        """Éléments validés par recoupement pour une grappe."""
        return call("get_validated_refs", grappe_id, limit)

    @server.tool()
    def compare_grappes(a: str, b: str) -> dict:
        """Recouvrement et oppositions entre deux grappes."""
        return call("compare_grappes", a, b)

    return server


def main() -> None:
    create_server().run()


if __name__ == "__main__":
    main()
