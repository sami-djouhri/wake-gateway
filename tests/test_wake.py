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
from datetime import datetime
from pathlib import Path

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


# --- Bereitschaft: laeuft ist nicht dasselbe wie antwortet ---------------

class FakeAntwort:
    def __init__(self, state, status_code=200):
        self.status_code = status_code
        self._state = state

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %d" % self.status_code)

    def json(self):
        return {"State": self._state}


class ZustandsClient:
    """Docker-API-Ersatz, der je Container einen State liefert."""

    def __init__(self, zustaende):
        self.zustaende = zustaende
        self.abfragen = 0

    async def get(self, pfad):
        self.abfragen += 1
        name = pfad.split("/")[2]
        if name not in self.zustaende:
            return FakeAntwort({}, status_code=404)
        wert = self.zustaende[name]
        # Eine Liste bedeutet: bei jeder Abfrage der naechste Zustand.
        if isinstance(wert, list):
            wert = wert.pop(0) if len(wert) > 1 else wert[0]
        return FakeAntwort(wert)


def test_container_zustand():
    print("container_zustand: Running gegen Health")
    import importlib
    m = importlib.reload(main)
    c = ZustandsClient({
        "ohne_check": {"Running": True},
        "gesund": {"Running": True, "Health": {"Status": "healthy"}},
        "startet": {"Running": True, "Health": {"Status": "starting"}},
        "krank": {"Running": True, "Health": {"Status": "unhealthy"}},
        "aus": {"Running": False},
    })
    def lauf(n):
        return asyncio.run(m.container_zustand(c, n))
    pruefe(lauf("ohne_check") == (True, True),
           "ohne Healthcheck gilt Running als bereit (Altbestand)")
    pruefe(lauf("gesund") == (True, True), "healthy ist bereit")
    pruefe(lauf("startet") == (True, False),
           "starting laeuft, ist aber NICHT bereit")
    pruefe(lauf("krank") == (True, False), "unhealthy laeuft, ist nicht bereit")
    pruefe(lauf("aus") == (False, False), "gestoppt ist weder noch")
    pruefe(lauf("gibtsnicht") == (False, False), "unbekannter Container: 404")


def test_dienst_bereit_kette():
    print("dienst_bereit: die ganze Kette muss antworten")
    import importlib
    m = importlib.reload(main)
    c = ZustandsClient({
        "app": {"Running": True, "Health": {"Status": "healthy"}},
        "db": {"Running": True, "Health": {"Status": "starting"}},
    })
    svc = {"name": "x", "container": "app", "begleiter": ["db"]}
    pruefe(asyncio.run(m.dienst_laeuft(c, svc)) is True,
           "beide Container laufen, also laeuft der Dienst")
    pruefe(asyncio.run(m.dienst_bereit(c, svc)) is False,
           "die noch startende Datenbank macht den Dienst unbereit")


def test_warten_auf_bereitschaft():
    print("auf_bereitschaft_warten")
    import importlib
    m = importlib.reload(main)
    m.BEREIT_ABFRAGE_S = 0.01
    # Erst startend, dann gesund: das Warten muss den Wechsel bemerken.
    c = ZustandsClient({
        "app": [
            {"Running": True, "Health": {"Status": "starting"}},
            {"Running": True, "Health": {"Status": "healthy"}},
        ],
    })
    svc = {"name": "x", "container": "app"}
    pruefe(asyncio.run(m.auf_bereitschaft_warten(c, svc, 2.0)) is True,
           "wird der Dienst waehrend des Wartens gesund, endet das Warten mit True")

    m2 = importlib.reload(main)
    m2.BEREIT_ABFRAGE_S = 0.01
    c2 = ZustandsClient({"app": {"Running": True, "Health": {"Status": "starting"}}})
    pruefe(asyncio.run(m2.auf_bereitschaft_warten(c2, svc, 0.05)) is False,
           "kommt er nicht hoch, laeuft die Frist ab und der Aufrufer bekommt False")


def test_nutzung_am_statuscode():
    """Nur wer wirklich hineinkam, haelt einen Dienst wach.

    Vorgeschichte: am 2026-09-06 belegten drei Shops alle drei Plaetze, obwohl in
    ihrer Zugriffsspur ausschliesslich Anfragen mit 401 standen. kiwix bekam
    daneben ueber 20 Minuten nur 503.
    """
    print("Nutzung am Statuscode")
    pruefe(main._zaehlt_als_nutzung(200) is True, "200 zaehlt")
    pruefe(main._zaehlt_als_nutzung(302) is True, "302 zaehlt (Weiterleitung nach dem Login)")
    pruefe(main._zaehlt_als_nutzung(404) is True, "404 zaehlt: der Dienst hat geantwortet")
    pruefe(main._zaehlt_als_nutzung(401) is False, "401 zaehlt nicht: abgewiesen")
    pruefe(main._zaehlt_als_nutzung(403) is False, "403 zaehlt nicht: abgewiesen")
    pruefe(main._zaehlt_als_nutzung(503) is False, "503 zaehlt nicht: das ist die eigene Warteseite")
    pruefe(main._zaehlt_als_nutzung(500) is False, "500 zaehlt nicht: geliefert wurde nichts")


def test_spur_auswerten():
    print("Zugriffsspur auswerten")
    echt = '2026-09-06T07:00:00+00:00 192.0.2.10 "GET / HTTP/1.1" 200'
    abgewiesen = '2026-09-06T08:00:00+00:00 192.0.2.10 "GET / HTTP/1.1" 401'
    warteseite = '2026-09-06T09:00:00+00:00 192.0.2.10 "GET / HTTP/1.1" 503'

    wert = main._spur_auswerten("\n".join([echt, abgewiesen, warteseite]))
    erwartet = datetime.fromisoformat("2026-09-06T07:00:00+00:00").timestamp()
    pruefe(wert == erwartet,
           "die juengste ZAEHLENDE Zeile gewinnt, nicht die juengste ueberhaupt")

    pruefe(main._spur_auswerten("\n".join([abgewiesen, warteseite])) == 0.0,
           "eine Spur aus lauter Abweisungen belegt keinen Zugriff")

    pruefe(main._spur_auswerten("") is None,
           "leere Eingabe ist nicht auswertbar (der Aufrufer faellt auf die Aenderungszeit zurueck)")
    pruefe(main._spur_auswerten("voellig anderes Format\nnoch eine Zeile") is None,
           "unverstaendliches Format ebenso, statt stillschweigend 'nie benutzt'")

    halb = "kaputte zeile ohne alles\n" + echt
    pruefe(main._spur_auswerten(halb) == erwartet,
           "eine unlesbare Zeile macht nicht die ganze Spur unlesbar")


def test_last_seen_liest_inhalt(tmpdir=None):
    """last_seen darf sich nicht mehr auf die Aenderungszeit verlassen."""
    print("last_seen gegen eine echte Datei")
    import importlib
    import tempfile

    verzeichnis = tempfile.mkdtemp()
    m = importlib.reload(main)
    m.LOG_DIR = Path(verzeichnis)
    m._woken_at.clear()

    spur = Path(verzeichnis) / "shop.log"
    echt = datetime.fromisoformat("2026-09-06T07:00:00+00:00").timestamp()
    spur.write_text(
        '2026-09-06T07:00:00+00:00 10.0.0.1 "GET / HTTP/1.1" 200\n'
        '2026-09-06T08:00:00+00:00 10.0.0.1 "GET / HTTP/1.1" 401\n'
    )
    pruefe(m.last_seen("shop") == echt,
           "die 401 eine Stunde spaeter verschiebt den letzten Zugriff nicht")

    pruefe(m.last_seen("gibtsnicht") == 0.0,
           "ohne Spur gibt es keinen Zugriff")

    nur_abweisungen = Path(verzeichnis) / "leer.log"
    nur_abweisungen.write_text('2026-09-06T08:00:00+00:00 10.0.0.1 "GET / HTTP/1.1" 401\n')
    pruefe(m.last_seen("leer") == 0.0,
           "eine Spur aus lauter 401 zaehlt wie gar keine")

    m._woken_at["leer"] = 12345.0
    pruefe(m.last_seen("leer") == 12345.0,
           "der Weckzeitpunkt zaehlt weiter mit, sonst stirbt ein startender Dienst")


def test_weck_timeout():
    print("render_nginx.weck_timeout")
    sys.path.insert(0, "/app")
    import render_nginx
    pruefe(render_nginx.weck_timeout({}) == 40,
           "ohne blockieren_s bleibt es beim bisherigen Wert")
    pruefe(render_nginx.weck_timeout({"blockieren_s": 20}) == 40,
           "kurzes Warten aendert nichts, 40 s reichen dafuer")
    pruefe(render_nginx.weck_timeout({"blockieren_s": 90}) == 100,
           "langes Warten hebt den nginx-Timeout darueber (sonst 504 kurz vor dem Ziel)")


class FakeRequest:
    """Nur die Header, mehr liest `_rueckweg` nicht."""

    def __init__(self, **header):
        self.headers = {k.replace("_", "-"): v for k, v in header.items()}


def test_rueckweg_hinter_praefix():
    print("_rueckweg")
    # Der node1-Fall: der Wecker haelt den Host-Port selbst, kein Praefix.
    pruefe(main._rueckweg(FakeRequest(X_Original_URI="/buch/42")) == "/buch/42",
           "ohne Praefix bleibt die URI unveraendert (node1-Fall)")
    pruefe(main._rueckweg(FakeRequest()) == "/",
           "ohne jeden Header bleibt es beim bisherigen Rueckfall")
    # Der host-Fall: das dev-portal hat `/schach` abgeschnitten. Ohne die
    # Korrektur zeigte die Warteseite auf `/` und damit auf die Portal-Startseite.
    pruefe(main._rueckweg(
        FakeRequest(X_Original_URI="/", X_Forwarded_Prefix="/schach")) == "/schach/",
        "abgeschnittenes Praefix wird wieder vorangestellt")
    pruefe(main._rueckweg(
        FakeRequest(X_Original_URI="/api/zug", X_Forwarded_Prefix="/schach")) == "/schach/api/zug",
        "auch ein tieferer Pfad bekommt das Praefix zurueck")
    # Schneidet ein Aufrufer kuenftig nichts ab, darf nichts verdoppelt werden.
    pruefe(main._rueckweg(
        FakeRequest(X_Original_URI="/schach/", X_Forwarded_Prefix="/schach")) == "/schach/",
        "ein bereits vollstaendiger Pfad wird nicht verdoppelt")
    pruefe(main._rueckweg(
        FakeRequest(X_Original_URI="/schachmatt", X_Forwarded_Prefix="/schach")) == "/schach/schachmatt",
        "Namensgleichheit am Anfang ist keine Praefix-Uebereinstimmung")


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
        test_container_zustand,
        test_dienst_bereit_kette,
        test_warten_auf_bereitschaft,
        test_nutzung_am_statuscode,
        test_spur_auswerten,
        test_last_seen_liest_inhalt,
        test_weck_timeout,
        test_rueckweg_hinter_praefix,
    ):
        fn()
    print()
    if pruefe.fehler:
        print("%d Pruefung(en) fehlgeschlagen" % pruefe.fehler)
        sys.exit(1)
    print("alle Pruefungen bestanden")
