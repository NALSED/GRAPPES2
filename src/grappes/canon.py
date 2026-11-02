"""Canonicalisation des URLs, handles et domaines. Aucune résolution réseau."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PARAMS = {"fbclid"}


def canonical_handle(handle: str) -> str:
    """Minuscules, sans `@` initial."""
    return handle.strip().lstrip("@").casefold()


def canonical_domain(host: str) -> str:
    """Hôte seul, minuscules, sans `www.` ni port."""
    h = host.strip().casefold()
    if "//" in h:
        h = urlsplit(h).hostname or ""
    h = h.split("/", 1)[0].split(":", 1)[0]
    return h.removeprefix("www.")


_SECOND_LEVEL = {"co", "com", "org", "net", "gov", "ac", "edu"}  # « co.uk », « com.au »...


def site_key(host: str) -> str:
    """Éditeur d'un site : domaine enregistrable, approché sans liste externe (les deux derniers libellés, trois
    avec un second niveau « co/com/org/net/gov/ac/edu » sous un code pays). `agency.lebonbon.fr` et `lebonbon.fr`
    sont le même éditeur ; `bbc.co.uk` reste lui-même. Approximation documentée (D58) : elle ne sert qu'à ne pas
    compter deux fois la même voix."""
    labels = canonical_domain(host).split(".")
    if len(labels) <= 2:
        return ".".join(labels)
    keep = 3 if labels[-2] in _SECOND_LEVEL and len(labels[-1]) == 2 else 2
    return ".".join(labels[-keep:])


_TME_HOSTS = {"t.me", "telegram.me"}
_X_HOSTS = {"x.com", "twitter.com", "mobile.twitter.com"}
_IG_HOSTS = {"instagram.com"}
_SHORTENERS = {"t.co"}
# Plateformes sociales dont un lien ne désigne pas une source identifiable (profil non reconnu sans les
# lire) : un lien vers elles ne crée aucune entité `web:<plateforme>` (constaté sur les premières
# données réelles : pieds de page de sites d'actualité).
_OPAQUE_PLATFORMS = {
    "facebook.com", "fb.com", "tiktok.com", "linkedin.com", "youtube.com", "youtu.be", "bsky.app",
    "pinterest.com", "snapchat.com", "threads.net", "whatsapp.com", "wa.me", "tumblr.com", "vk.com",
    "discord.com", "twitch.tv", "reddit.com", "mastodon.social",
}  # fmt: skip  # cible inconnaissable sans résolution réseau : jamais citée
_TME_RESERVED = {"joinchat", "c", "addstickers", "share", "iv", "proxy", "socks", "login"}
_X_RESERVED = {
    "i", "home", "hashtag", "search", "intent", "share", "explore", "settings", "messages",
    "notifications", "compose", "login", "signup", "tos", "privacy", "about", "account",
}  # fmt: skip
_IG_RESERVED = {
    "p", "reel", "reels", "tv", "explore", "stories", "accounts", "direct", "about",
    "developer", "legal", "web",
}  # fmt: skip


def link_target(url: str) -> str:
    """Clé canonique (`ecosysteme:ident`) de l'élément cité par un lien ; `""` si aucun.

    * `t.me/<canal>[/<id>]`, `t.me/s/<canal>` -> `telegram:canal` ;
    * `x.com|twitter.com/<compte>[/...]` -> `x:compte` ;
    * `instagram.com/<compte>` -> `instagram:compte` ;
    * autre site -> `web:domaine`.

    Rien n'est cité (`""`) quand la cible ne peut pas être identifiée sans résolution réseau ni
    ré-identification : invitations Telegram (`t.me/+...`, `joinchat`), messages de groupes
    privés (`t.me/c/...`), publications Instagram (`/p/...`, `/reel/...`), raccourcisseur `t.co`,
    pages d'outils de plateforme (`x.com/i/...`, `x.com/search`...). Aucun lien ne devient donc
    une entité `web:t.me`, `web:x.com` ou `web:instagram.com`.
    """
    url = url.strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    host = (parts.hostname or "").casefold().removeprefix("www.")
    if not host:
        return ""
    segments = [seg for seg in parts.path.split("/") if seg]
    first = segments[0] if segments else ""
    if host in _SHORTENERS or any(host == h or host.endswith("." + h) for h in _OPAQUE_PLATFORMS):
        return ""
    if host in _TME_HOSTS:
        if first.casefold() == "s" and len(segments) > 1:  # aperçu web d'un canal public
            first = segments[1]
        if not first or first.startswith("+") or first.casefold() in _TME_RESERVED | {"s"}:
            return ""
        handle = canonical_handle(first)
        return f"telegram:{handle}" if handle else ""
    if host in _X_HOSTS:
        if not first or first.casefold() in _X_RESERVED:
            return ""
        return f"x:{canonical_handle(first)}"
    if host in _IG_HOSTS:
        if not first or first.casefold() in _IG_RESERVED:
            return ""
        return f"instagram:{canonical_handle(first)}"
    return f"web:{host}"


_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)


def extract_urls(text: str) -> list[str]:
    """URLs `http(s)://` d'un texte libre (légende, message), sans ponctuation finale."""
    return [u.rstrip(".,;:!?") for u in _URL_RE.findall(text)]


def canonical_url(url: str) -> str:
    """Schéma + hôte en minuscules, sans `www.`, `utm_*`, `fbclid`, fragment ni `/` final."""
    url = url.strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    host = (parts.hostname or "").casefold().removeprefix("www.")
    if parts.port:
        host = f"{host}:{parts.port}"
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.casefold().startswith("utm_") and k.casefold() not in _TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.casefold(), host, path, urlencode(query), ""))
