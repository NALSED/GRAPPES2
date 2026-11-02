from grappes.canon import canonical_domain, canonical_handle, canonical_url


def test_handle():
    assert canonical_handle("@Foo_Bar") == "foo_bar"
    assert canonical_handle("  FOO ") == "foo"


def test_domain():
    assert canonical_domain("WWW.Exemple.TEST") == "exemple.test"
    assert canonical_domain("https://www.exemple.test:8080/a/b") == "exemple.test"
    assert canonical_domain("exemple.test/chemin") == "exemple.test"


def test_url_nettoyage():
    u = "HTTPS://WWW.Exemple.test/Soupe/?utm_source=x&fbclid=1&id=3#frag"
    assert canonical_url(u) == "https://exemple.test/Soupe?id=3"


def test_url_slash_final_et_racine():
    assert canonical_url("https://exemple.test/") == "https://exemple.test"
    assert canonical_url("https://exemple.test/a/b/") == "https://exemple.test/a/b"


def test_url_sans_schema():
    assert canonical_url("www.exemple.test/a") == "https://exemple.test/a"


def test_url_vide_et_idempotence():
    assert canonical_url("  ") == ""
    u = canonical_url("https://www.exemple.test/a/?utm_x=1&b=2")
    assert canonical_url(u) == u


def test_link_target():
    from grappes.canon import link_target

    assert link_target("https://t.me/Canal_A/123") == "telegram:canal_a"
    assert link_target("t.me/s/canal_a") == "telegram:canal_a"
    assert link_target("https://www.x.com/Compte_A/status/1") == "x:compte_a"
    assert link_target("https://twitter.com/compte_a") == "x:compte_a"
    assert link_target("https://www.instagram.com/compte_c/") == "instagram:compte_c"
    assert link_target("https://www.Exemple.org/p?utm=1") == "web:exemple.org"
    # cibles non identifiables : rien n'est cité, jamais web:t.me / web:x.com / ...
    for ignore in (
        "https://t.me/+AbCdEf", "https://t.me/joinchat/xyz", "https://t.me/c/123/4", "https://t.me/",
        "https://t.co/AbC123", "https://x.com/i/web/status/1", "https://x.com/search?q=a",
        "https://www.instagram.com/p/DEMO0001/", "https://www.instagram.com/reel/DEMO/", "",
    ):  # fmt: skip
        assert link_target(ignore) == "", ignore
