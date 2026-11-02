"""Règles de visite : INTERDITS (éditables), réseaux sociaux, adresses privées, schémas."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

from grappes.canon import link_target
from grappes.semantic.scoring import normalize


def _host_matches(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def is_private_host(host: str, resolve: bool = True) -> bool:
    """Vrai pour localhost, une adresse privée/locale/réservée, ou un nom qui y mène."""
    host = host.strip("[]").casefold()
    if host in ("localhost", "localhost.localdomain") or host.endswith(
        (".localhost", ".local", ".internal")
    ):
        return True
    try:
        ips = [ipaddress.ip_address(host)]
    except ValueError:
        if not resolve:
            return False
        try:
            ips = [ipaddress.ip_address(i[4][0]) for i in socket.getaddrinfo(host, None)]
        except (OSError, ValueError):
            return False  # nom introuvable : la requête échouera d'elle-même
    return any(
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
        or ip.is_unspecified
        for ip in ips
    )  # fmt: skip


@dataclass(frozen=True, slots=True)
class Rules:
    interdits_domaines: tuple[str, ...] = ()
    interdits_motifs: tuple[str, ...] = ()
    reseaux: tuple[str, ...] = ()  # réseaux sociaux : citations seulement
    allow_private: bool = False  # tests seulement (faux serveur local)

    def check_url(self, url: str) -> str | None:
        """`None` si l'URL peut être visitée ; sinon le motif du refus."""
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return f"schéma non autorisé ({parts.scheme or 'aucun'})"
        host = (parts.hostname or "").casefold().removeprefix("www.")
        if not host:
            return "URL sans hôte"
        if parts.username or parts.password:
            return "identifiants dans l'URL : connexion interdite"
        if _host_matches(host, self.reseaux) or link_target(url).split(":", 1)[0] in (
            "telegram", "x", "instagram"
        ):  # fmt: skip
            return "réseau social : lu seulement par un adaptateur officiel (ses liens sont des citations)"
        if _host_matches(host, self.interdits_domaines):
            return "domaine interdit"
        if not self.allow_private and is_private_host(host):
            return "adresse privée ou locale"
        return self.check_text(url)

    def check_text(self, text: str) -> str | None:
        """Motifs interdits dans l'URL, le titre ou la description (casse et accents ignorés)."""
        low = normalize(text)
        for motif in self.interdits_motifs:
            if normalize(motif) in low:
                return f"motif interdit « {motif} »"
        return None
