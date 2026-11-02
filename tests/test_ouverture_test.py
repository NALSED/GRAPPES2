"""Le jeu test ne s'ouvre qu'une fois : une seconde exécution est refusée et consignée (brûlage)."""

import json

from grappes.autocal import test_final as tf


def test_seconde_ouverture_refusee_et_consignee(tmp_path, monkeypatch):
    j = tmp_path / "test_ouvert.json"
    j.write_text(json.dumps({"ouvert_le_utc": "2026-01-01T00:00:00+00:00", "reouvertures": []}))
    monkeypatch.setattr(tf, "JOURNAL", j)
    assert tf.ouvrir() == 2
    assert len(json.loads(j.read_text())["reouvertures"]) == 1


def test_proba_platt_monotone():
    ab = (1.0, -4.0)
    assert tf.proba(0.0, ab) < tf.proba(3.0, ab) < 1.0
