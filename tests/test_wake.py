"""Tests fuer die Grenzen des Weckers — Anzahl-Deckel und Speicherboden.

Laeuft ohne Testframework, weil das Abbild keins enthaelt und der Dienst sonst
nur drei Abhaengigkeiten hat. Aufruf (aus dem Projektverzeichnis):

    docker run --rm -v "$PWD/tests:/tests:ro" -v "$PWD/services.json:/app/services.json:ro" \
        wake-gateway-api python /tests/test_wake.py

Der Schwerpunkt liegt auf `platz_schaffen`: dort entscheidet sich, ob ein
Dienst startet, wartet oder verdraengt — und dort kann ein Fehler entweder den
Wirt ueberlasten oder Besucher grundlos aussperren.
"""

import asyncio
import sys

sys.path.insert(0, "/app")
import main  # noqa: E402


class FakeClient:
    """Ersetzt die Docker-API. Merkt sich, was gestoppt wurde."""

    def __init__(self, laufend):
        self.laufend = set(laufend)
        self.gestoppt = []


async def fake_dienst_laeuft(client, svc):
    return svc["container"] in client.laufend


async def fake_dienst_stoppen(client, svc):
    client.gestoppt.append(svc["name"])
    client.laufend.discard(svc["container"])


def registry(dienste, **grenzen):
    return {
        "no_wake_sources": set(),
        "grenzen": grenzen,
        "services": {d["name"]: d for d in dienste},
    }


def dienst(name, bedarf=0):
    return {
        "name": name,
        "container": name,
        "titel": name,
        "bedarf_mb": bedarf,
    }


def aufbau(dienste, laufend, frei, jetzt=10_000.0, letzte=None, **grenzen):
    """Setzt den Modulzustand fuer einen Durchlauf und liefert den Fake-Client."""
    main._registry = registry(dienste, **grenzen)
    main.dienst_laeuft = fake_dienst_laeuft
    main.dienst_stoppen = fake_dienst_stoppen
    main.speicher_frei_mb = lambda: frei
    # last_seen liest sonst Logdateien; hier zaehlt nur der gesetzte Zeitpunkt.
    letzte = letzte or {}
    main.last_seen = lambda n: letzte.get(n, 0.0)
    return FakeClient(laufend)


def pruefe(bedingung, text):
    if not bedingung:
        print("  FEHLGESCHLAGEN: %s" % text)
        pruefe.fehler += 1
    else:
        print("  ok: %s" % text)


pruefe.fehler = 0


# --- speicher_reicht: die reine Rechnung ---------------------------------

def test_speicher_reicht():
    print("speicher_reicht")
    main._registry = registry([], reserve_mb=700)
    pruefe(
        main.speicher_reicht(dienst("a", 800), 2000) is True,
        "2000 frei, 800 Bedarf, 700 Boden -> darf starten",
    )
    pruefe(
        main.speicher_reicht(dienst("a", 800), 1400) is False,
        "1400 frei ist zu wenig (600 blieben uebrig, Boden ist 700)",
    )
    pruefe(
        main.speicher_reicht(dienst("a", 800), 1500) is True,
        "Grenzfall: genau der Boden bleibt uebrig -> erlaubt",
    )
    pruefe(
        main.speicher_reicht(dienst("a", 800), None) is True,
        "unlesbarer Messwert sperrt niemanden aus",
    )
    main._registry = registry([])
    pruefe(
        main.speicher_reicht(dienst("a"), 10) is True,
        "ohne bedarf_mb und ohne reserve_mb ist die Pruefung aus (Altbestand)",
    )
    # Nur ein Boden ohne Bedarf: der Dienst zaehlt als gewichtslos, der Boden gilt.
    main._registry = registry([], reserve_mb=700)
    pruefe(
        main.speicher_reicht(dienst("a"), 500) is False,
        "Boden allein greift auch ohne bedarf_mb",
    )


# --- platz_schaffen: Deckel, Speicher, Verdraengung -----------------------

def test_platz_ok():
    print("platz_schaffen: freie Bahn")
    c = aufbau([dienst("neu", 800)], laufend=[], frei=5000,
               max_wach=3, reserve_mb=700)
    erg = asyncio.run(main.platz_schaffen(c, dienst("neu", 800), 10_000.0))
    pruefe(erg == "ok", "nichts laeuft, Speicher reicht -> ok")
    pruefe(c.gestoppt == [], "dabei wird nichts gestoppt")


def test_deckel_verdraengt():
    print("platz_schaffen: Deckel erreicht, aeltester weicht")
    d = [dienst(n) for n in ("a", "b", "c", "neu")]
    c = aufbau(d, laufend=["a", "b", "c"], frei=9000,
               letzte={"a": 1000.0, "b": 5000.0, "c": 9000.0},
               max_wach=3, schutz_s=900, reserve_mb=700)
    erg = asyncio.run(main.platz_schaffen(c, d[3], 10_000.0))
    pruefe(erg == "ok", "nach dem Verdraengen darf der neue starten")
    pruefe(c.gestoppt == ["a"], "es weicht der am laengsten unbenutzte (a)")


def test_deckel_alle_in_benutzung():
    print("platz_schaffen: Deckel erreicht, aber alle in Benutzung")
    d = [dienst(n) for n in ("a", "b", "c", "neu")]
    # Alle drei wurden gerade eben bedient -> keiner ist verdraengbar.
    c = aufbau(d, laufend=["a", "b", "c"], frei=9000,
               letzte={"a": 9800.0, "b": 9850.0, "c": 9900.0},
               max_wach=3, schutz_s=900, reserve_mb=700)
    erg = asyncio.run(main.platz_schaffen(c, d[3], 10_000.0))
    pruefe(erg == "voll", "niemand wird aus einer laufenden Sitzung geworfen")
    pruefe(c.gestoppt == [], "und nichts wird gestoppt")


def test_speicher_knapp_verdraengt():
    print("platz_schaffen: Deckel frei, aber Speicher knapp")
    d = [dienst("alt", 800), dienst("neu", 800)]
    # Nur ein Dienst wach (Deckel 3 nicht erreicht), aber 1200 MB frei:
    # 1200 - 800 = 400 < 700 Boden -> es muss trotzdem Platz gemacht werden.
    zustand = {"frei": 1200}
    c = aufbau(d, laufend=["alt"], frei=None,
               letzte={"alt": 1000.0}, max_wach=3, schutz_s=900, reserve_mb=700)
    # Nach dem Stoppen gibt der Wirt Speicher frei:
    async def stoppen(client, svc):
        client.gestoppt.append(svc["name"])
        client.laufend.discard(svc["container"])
        zustand["frei"] = 3000
    main.dienst_stoppen = stoppen
    main.speicher_frei_mb = lambda: zustand["frei"]
    erg = asyncio.run(main.platz_schaffen(c, d[1], 10_000.0))
    pruefe(erg == "ok", "Speichermangel loest dieselbe Verdraengung aus wie der Deckel")
    pruefe(c.gestoppt == ["alt"], "der unbenutzte Dienst weicht")


def test_speicher_knapp_nichts_zu_holen():
    print("platz_schaffen: Speicher knapp, nichts verdraengbar")
    d = [dienst("neu", 800)]
    c = aufbau(d, laufend=[], frei=900,
               max_wach=3, schutz_s=900, reserve_mb=700)
    erg = asyncio.run(main.platz_schaffen(c, d[0], 10_000.0))
    pruefe(erg == "eng", "der Start wird abgelehnt statt den Wirt zu ueberlasten")
    pruefe(c.gestoppt == [], "es gibt nichts zu stoppen (Last liegt woanders)")


def test_speicher_knapp_geschuetzter_dienst():
    print("platz_schaffen: Speicher knapp, der einzige Wache ist in Benutzung")
    d = [dienst("aktiv", 800), dienst("neu", 800)]
    c = aufbau(d, laufend=["aktiv"], frei=900,
               letzte={"aktiv": 9900.0}, max_wach=3, schutz_s=900, reserve_mb=700)
    erg = asyncio.run(main.platz_schaffen(c, d[1], 10_000.0))
    pruefe(erg == "eng", "ein benutzter Dienst wird auch bei Speichernot nicht geworfen")
    pruefe(c.gestoppt == [], "Sitzung bleibt unangetastet")


def test_altbestand_ohne_neue_felder():
    print("platz_schaffen: Registry ohne die neuen Felder verhaelt sich wie frueher")
    d = [dienst(n) for n in ("a", "neu")]
    c = aufbau(d, laufend=["a"], frei=50, letzte={"a": 1000.0}, max_wach=3)
    erg = asyncio.run(main.platz_schaffen(c, d[1], 10_000.0))
    pruefe(erg == "ok", "ohne reserve_mb blockiert auch ein leerer Wirt nicht")
    pruefe(c.gestoppt == [], "und es wird nichts verdraengt")


def test_kein_deckel_gesetzt():
    print("platz_schaffen: max_wach = 0 heisst 'keine Grenze'")
    d = [dienst(n) for n in ("a", "b", "c", "d", "neu")]
    c = aufbau(d, laufend=["a", "b", "c", "d"], frei=9000, max_wach=0)
    erg = asyncio.run(main.platz_schaffen(c, d[4], 10_000.0))
    pruefe(erg == "ok", "vier wache Dienste sind ohne Deckel kein Hindernis")


# --- speicher_frei_mb gegen die echte Datei ------------------------------

def test_meminfo_echt():
    print("speicher_frei_mb gegen das echte /proc/meminfo")
    # Bewusst die ungepatchte Fassung: der Test soll die echte Datei lesen.
    import importlib
    frisch = importlib.reload(main)
    wert = frisch.speicher_frei_mb()
    pruefe(isinstance(wert, int) and wert > 0,
           "liefert eine plausible Zahl (%s MB)" % wert)
    pruefe(wert < 1024 * 1024, "und keinen unsinnig grossen Wert")


def test_warteseite_zustaende():
    print("Warteseite")
    import importlib
    frisch = importlib.reload(main)
    svc = {"name": "x", "titel": "Beispiel"}
    eng = frisch.warteseite(svc, "/", zustand="eng")
    pruefe("ausgelastet" in eng, "der enge Fall nennt keine Innenwerte")
    pruefe("MB" not in eng and "Speicher" not in eng,
           "kein Speicherstand auf einer oeffentlich erreichbaren Seite")
    pruefe('content="30;' in eng, "laedt seltener neu als der Deckel-Fall")
    wartet = frisch.warteseite(svc, "/", zustand="wartet")
    pruefe('content="15;' in wartet, "Deckel-Fall laedt nach 15 s neu")
    schlaeft = frisch.warteseite(svc, "/", zustand="schlaeft")
    pruefe("refresh" not in schlaeft, "die Schlaf-Seite laedt gar nicht neu")


if __name__ == "__main__":
    for fn in (
        test_speicher_reicht,
        test_platz_ok,
        test_deckel_verdraengt,
        test_deckel_alle_in_benutzung,
        test_speicher_knapp_verdraengt,
        test_speicher_knapp_nichts_zu_holen,
        test_speicher_knapp_geschuetzter_dienst,
        test_altbestand_ohne_neue_felder,
        test_kein_deckel_gesetzt,
        test_meminfo_echt,
        test_warteseite_zustaende,
    ):
        fn()
    print()
    if pruefe.fehler:
        print("%d Pruefung(en) fehlgeschlagen" % pruefe.fehler)
        sys.exit(1)
    print("alle Pruefungen bestanden")
