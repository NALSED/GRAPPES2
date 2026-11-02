"""Bilan des économies de jetons : aucune valeur inventée, source vide ou illisible signalée."""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outils"))
import bilan_jetons as bj

NOW = datetime.fromisoformat("2026-10-11T10:00:00+04:00")
RTK = json.dumps(
    {
        "summary": {"total_commands": 12, "total_input": 4000, "total_output": 1000, "total_saved": 3000, "avg_savings_pct": 75.0},
        "daily": [
            {"date": "2026-10-05", "commands": 5, "input_tokens": 1000, "output_tokens": 400, "saved_tokens": 600, "savings_pct": 60.0},
            {"date": "2026-10-08", "commands": 7, "input_tokens": 3000, "output_tokens": 600, "saved_tokens": 2400, "savings_pct": 80.0},
        ],
    }
)  # fmt: skip


def rapport(journal_text: str | None, rtk_project=None, rtk_account=None, **kw):
    j = (
        bj.parse_journal(journal_text)
        if journal_text is not None
        else bj.load_journal(Path("/inexistant/journal"))
    )
    return bj.build_report(
        j,
        bj.parse_rtk(rtk_project) if rtk_project else bj.Rtk(error="absent pour le test"),
        bj.parse_rtk(rtk_account) if rtk_account else bj.Rtk(error="absent pour le test"),
        generated=NOW,
        **kw,
    )


# --- journal ---------------------------------------------------------------------------------------


def test_journal_vide():
    r = rapport("", RTK)
    assert "est vide : **0 appel**" in r and "Période couverte" not in r
    assert "| 2026-10-05 | non disponible | 5 | 600 |" in r  # sans journal : appels absents, pas 0


def test_journal_absent_ou_illisible_le_dit_et_continue(tmp_path):
    r = rapport(None, RTK)
    assert "Source illisible" in r and "Aucun chiffre" in r
    assert "Total consolidé : 3 000 jetons" in r  # la source RTK continue de compter


def test_ligne_malformee_signalee_et_non_comptee():
    texte = "2026-10-05T09:00:00+04:00 | tâche A\nceci n'est pas une ligne\n2026-10-xx | cassée\n\n2026-10-05T10:00:00+04:00 | tâche B\n"
    j = bj.parse_journal(texte)
    assert len(j.entries) == 2 and [n for n, _ in j.bad_lines] == [2, 3]
    r = rapport(texte, RTK)
    assert "Appels enregistrés : **2**" in r and "Lignes illisibles ignorées : **2**" in r
    assert "ligne 2 : `ceci n'est pas une ligne`" in r


def test_periode_sans_appel_vaut_zero_et_hors_periode_est_absent():
    texte = "2026-10-05T09:00:00+04:00 | a\n2026-10-08T09:00:00+04:00 | b\n2026-10-08T10:00:00+04:00 | c\n"
    r = rapport(texte, RTK)
    assert "| 2026-10-05 | 1 | 5 |" in r
    assert "| 2026-10-06 | 0 | non disponible |" in r  # journal couvert, aucun appel ce jour-là
    assert "| 2026-10-07 | 0 |" in r
    assert "| 2026-10-08 | 2 | 7 |" in r
    assert "(4 jour(s), dont 2 avec au moins un appel)" in r
    r2 = rapport("2026-10-08T09:00:00+04:00 | b\n", RTK)
    assert "| 2026-10-05 | non disponible | 5 |" in r2  # avant la première ligne : donnée absente


def test_volume_en_jetons_jamais_estime():
    r = rapport("2026-10-05T09:00:00+04:00 | a\n", RTK)
    assert "Volume estimé en jetons : **non disponible**" in r
    assert "aucune estimation n'est faite" in r


# --- RTK -------------------------------------------------------------------------------------------


def test_rtk_illisible_ou_vide_signale():
    assert "JSON RTK illisible" in bj.parse_rtk("pas du json").error
    assert "JSON RTK illisible" in bj.parse_rtk('{"summary": {}}').error
    assert (
        "aucune commande"
        in bj.parse_rtk('{"summary": {"total_commands": 0, "total_saved": 0}}').error
    )
    r = rapport("2026-10-05T09:00:00+04:00 | a\n", "pas du json")
    assert "Total consolidé : **non disponible**" in r and "JSON RTK illisible" in r


def test_rtk_absent_run_rtk_ne_plante_pas(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("rtk")

    monkeypatch.setattr(subprocess, "run", boom)
    assert "rtk indisponible" in bj.run_rtk(project=True).error
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 3, "", "err")
    )
    assert "code 3" in bj.run_rtk(project=False).error


def test_rtk_est_toujours_presente_comme_une_estimation():
    r = rapport("", RTK, RTK)
    assert "ESTIMATION, pas une mesure" in r and "**pas** l'économie totale" in r
    assert "jetons estimés économisés" in r


def test_total_consolide_et_marge():
    r = rapport("2026-10-05T09:00:00+04:00 | a\n", RTK, ratio_low=3, ratio_high=5)
    assert "Total consolidé : 3 000 jetons estimés économisés" in r
    # 3000 jetons RTK = 12 000 octets ; /5 = 2 400 ; /3 = 4 000
    assert "de 2 400 à 4 000 jetons" in r and "hypothèse" in r
    assert "minimum chiffrable" in r


def test_marge_configurable():
    assert "de 3 000 à 3 000 jetons" in rapport("", RTK, ratio_low=4, ratio_high=4)


# --- commande ---------------------------------------------------------------------------------------


def test_commande_regenere_le_rapport(tmp_path, capsys):
    (tmp_path / "j.log").write_text("2026-10-05T09:00:00+04:00 | a\n", encoding="utf-8")
    (tmp_path / "rtk.json").write_text(RTK, encoding="utf-8")
    out = tmp_path / "ECONOMIES.md"
    args = [
        "--journal",
        str(tmp_path / "j.log"),
        "--rtk-json",
        str(tmp_path / "rtk.json"),
        "--rtk-json-compte",
        str(tmp_path / "rtk.json"),
        "--out",
        str(out),
    ]
    assert bj.main(args) == 0 and out.exists()
    first = out.read_text("utf-8")
    assert bj.main(args) == 0
    assert (
        out.read_text("utf-8").split("\n", 3)[3] == first.split("\n", 3)[3]
    )  # hors horodatage : identique
    assert bj.main([*args, "--ratio-bas", "6", "--ratio-haut", "2"]) == 2


def test_rtk_fichier_illisible_ne_plante_pas(tmp_path):
    out = tmp_path / "e.md"
    args = [
        "--journal",
        str(tmp_path / "absent.log"),
        "--rtk-json",
        str(tmp_path / "absent.json"),
        "--rtk-json-compte",
        str(tmp_path / "absent.json"),
        "--out",
        str(out),
    ]
    assert bj.main(args) == 0
    texte = out.read_text("utf-8")
    assert "Source illisible" in texte and "fichier RTK illisible" in texte
    assert "Total consolidé : **non disponible**" in texte


@pytest.mark.parametrize(
    "ligne",
    ["", "sans séparateur", "| description sans date", "2026-13-45T00:00:00 | date impossible"],
)
def test_lignes_degenerees(ligne):
    j = bj.parse_journal(ligne)
    assert j.entries == [] and (len(j.bad_lines) == (1 if ligne else 0))
