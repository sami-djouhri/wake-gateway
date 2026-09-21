"""wake-gateway: Torwaechter vor schlafenden Web-Diensten auf node1.

Der Wecker uebernimmt die veroeffentlichten Ports der on-demand-Dienste. Solange
ein Dienst laeuft, reicht nginx die Anfragen einfach durch. Ist er aus, laeuft
nginx in einen 502 und leitet intern hierher um: dieser Dienst startet den
Container und liefert eine Warteseite, die sich selbst neu laedt. Der Nutzer
ruft also nur die gewohnte Adresse auf, einschalten muss er nichts.

Umgekehrt beobachtet der Reaper, wie lange ein Dienst nicht mehr genutzt wurde,
und legt ihn nach `idle_timeout_s` wieder schlafen. Als Nutzungsspur dient die
Zugriffs-Logdatei, die nginx pro Dienst schreibt. Gelesen wird ihr INHALT, nicht
nur ihr Aenderungszeitpunkt: es zaehlt die juengste Zeile, deren Statuscode einen
echten Zugriff belegt (siehe `_zaehlt_als_nutzung`). Das kostet im Anfragepfad
nichts und haelt diesen Dienst aus der Verbindung heraus, solange alles laeuft.

Zwei Arten von Verkehr sind ausgenommen, an zwei verschiedenen Merkmalen:

* Ueberwachung (blackbox-exporter auf host) an der ABSENDERADRESSE, schon in
  nginx (`$echte_nutzung`). Sie weckt nichts und landet gar nicht erst in der
  Spur. Sonst haette die Minutentakt-Probe jeden Dienst dauerhaft wachgehalten.
* Abgewiesene und gescheiterte Anfragen am STATUSCODE, hier beim Auswerten.
  Wer 401 bekommt, hat den Dienst nicht benutzt, und die 503 der eigenen
  Warteseite haette einen Dienst allein durchs Warten wachgehalten.

Drei Grenzen schuetzen den Wirt davor, dass Weckverkehr allein den gesamten
Speichergewinn auffrisst (`grenzen` in der Registry):

* `max_wach` deckelt, wie viele verwaltete Dienste gleichzeitig laufen duerfen.
  Ist der Deckel erreicht, wird der am laengsten unbenutzte Dienst schlafen
  gelegt, bevor der neue startet, dasselbe Verdraengungs-Muster wie beim
  Arbiter der schweren Rollen. Wer gerade genutzt wird (`schutz_s`), ist davon
  ausgenommen; ist niemand verdraengbar, wartet der neue Dienst, statt einen
  Besucher aus einer laufenden Sitzung zu werfen.
* `weck_budget` begrenzt Startvorgaenge je `weck_fenster_s`. Gezaehlt werden nur
  echte Starts, nicht Anfragen, sonst wuerde die sich selbst neu ladende
  Warteseite ihr eigenes Budget aufbrauchen.
* `reserve_mb` ist der Speicherboden des Wirts: nach dem Start muss so viel
  frei BLEIBEN, sonst startet nichts. Was ein Dienst dafuer veranschlagt, steht
  als `bedarf_mb` bei ihm selbst.

Warum die dritte Grenze noetig ist: `max_wach` zaehlt Dienste, nicht Gewicht.
Ein WordPress samt Datenbank und ein kiwix zaehlen gleich, brauchen aber sehr
unterschiedlich viel. Ohne Speicherpruefung startet der Wecker auch dann noch,
wenn der Wirt schon am Anschlag ist, es gaebe weder Fehlermeldung noch
Aufschub, der OOM-Killer entschiede. Der Arbiter der Spiel-Rollen auf Node .18
rechnet aus demselben Grund mit `min_free_mb`; hier ist es dieselbe Idee.

Gemessen wird `MemAvailable` aus /proc/meminfo. In einem Docker-Container ohne
eigenen Speicher-Namensraum sind das die Werte des WIRTS, genau die gesuchte
Groesse (nachgeprueft auf node1: Wert im Container identisch zum Host). Bewusst
OHNE Swap: dass noch Auslagerungsspeicher frei ist, ist kein Grund, einen
weiteren Dienst zu starten, node1 hat davon schon mehrere GB in Benutzung.

Ist der Speicher knapp, wird nach derselben Politik verdraengt wie beim
Anzahl-Deckel. Bleibt es zu eng, bekommt der Besucher eine Warteseite und der
Dienst startet NICHT. Nach aussen bleibt die Begruendung bewusst vage ("stark
ausgelastet"), die Shops sind oeffentlich erreichbar, Innenzustand des Wirts
gehoert dort nicht hin. Im Log steht die genaue Zahl.

Die Grenzen wirken bewusst NICHT je Absenderadresse: hinter Tunnel und
veroeffentlichtem localhost-Port tragen alle Besucher dieselbe Quelle, ein
IP-Limit traefe echte Nutzung genauso wie Missbrauch.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

REGISTRY_PATH = Path(os.getenv("REGISTRY_PATH", "/app/services.json"))
LOG_DIR = Path(os.getenv("WAKE_LOG_DIR", "/var/log/wake"))
# Eigenes Verzeichnis fuer eigenen Zustand. NICHT LOG_DIR: dort liegen die
# nginx-Zugriffsspuren, und die sind bewusst read-only eingehaengt, der Wecker
# liest sie nur. Ein Schreibversuch dorthin scheitert (Errno 30), und zwar erst
# zur Laufzeit und ohne dass der Aufrufer es merkt.
STATE_DIR = Path(os.getenv("WAKE_STATE_DIR", "/var/lib/wake"))
DOCKER_API = os.getenv("DOCKER_API", "http://wake-socket-proxy:2375")
REAP_INTERVAL_S = int(os.getenv("REAP_INTERVAL_S", "60"))
# Abstand zwischen zwei Bereitschaftsfragen waehrend des Wartens. Kurz genug,
# dass niemand unnoetig haengt, lang genug, dass ein startender Dienst nicht
# nebenbei noch die Docker-API bedienen muss.
BEREIT_ABFRAGE_S = float(os.getenv("BEREIT_ABFRAGE_S", "1.0"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("wake-gateway")

app = FastAPI(title="wake-gateway", docs_url=None, redoc_url=None)

# Zeitpunkt des letzten Weckens je Dienst. Schuetzt einen gerade startenden
# Dienst davor, vom Reaper sofort wieder gestoppt zu werden, bevor der erste
# echte Zugriff im nginx-Log gelandet ist.
_woken_at: dict[str, float] = {}
_registry: dict = {}

# Zeitpunkte der letzten tatsaechlichen Startvorgaenge, fuer `weck_budget`.
_starts: list[float] = []

# Dienste im WARTUNGSMODUS: kein Auto-Off, keine Verdraengung. Gedacht fuer den Fall,
# dass jemand an einem Dienst arbeitet, ohne den Schalter muesste er im Minutentakt
# eine Seite aufrufen, damit ihm der Dienst nicht unter den Haenden weggeraeumt wird.
# Beim game-arbiter heisst dieselbe Mechanik "geschuetzt" (im Spiele-Dashboard
# "Reserviert", beim Windows-Lab ebenfalls "Wartungsmodus"), getrennte Systeme,
# gleiche Zusage: was geschuetzt ist, bleibt stehen.
# Der Zustand liegt neben den Zugriffsspuren auf Platte, damit ein Neustart des
# Weckers ihn nicht verliert: ein still verfallener Wartungsmodus waere schlimmer
# als keiner, weil niemand mit dem Verfall rechnet.
WARTUNG_PATH = STATE_DIR / "wartung.json"
_wartung: set[str] = set()


def wartung_laden() -> None:
    global _wartung
    try:
        with WARTUNG_PATH.open(encoding="utf-8") as fh:
            _wartung = set(json.load(fh))
    except (OSError, ValueError):
        _wartung = set()


def wartung_sichern() -> bool:
    """True, wenn der Zustand wirklich auf Platte liegt.

    Der Rueckgabewert ist keine Formsache: schlaegt das Speichern fehl, gilt der
    Schalter nur bis zum naechsten Neustart. Wer ihn gesetzt hat, muss das
    erfahren -- sonst verlaesst er sich auf einen Schutz, den es nach dem
    naechsten Recreate nicht mehr gibt.
    """
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = WARTUNG_PATH.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(sorted(_wartung), fh)
        tmp.replace(WARTUNG_PATH)
        return True
    except OSError as exc:
        log.error("Wartungsmodus nicht speicherbar (%s): %s", WARTUNG_PATH, exc)
        return False


def in_wartung(name: str) -> bool:
    """Zur Laufzeit geschaltet ODER in der Registry dauerhaft so hinterlegt."""
    if name in _wartung:
        return True
    svc = service(name)
    return bool(svc and svc.get("wartung"))


def load_registry() -> dict:
    """Liest die Registry neu ein. Aenderungen wirken beim naechsten Zugriff."""
    with REGISTRY_PATH.open(encoding="utf-8") as fh:
        data = json.load(fh)
    return {
        "no_wake_sources": set(data.get("no_wake_sources", [])),
        "grenzen": data.get("grenzen", {}),
        "services": {s["name"]: s for s in data["services"]},
    }


def grenze(name: str, default: int) -> int:
    """Ein Wert aus dem `grenzen`-Block, 0 heisst ueberall 'keine Grenze'."""
    wert = _registry.get("grenzen", {}).get(name, default)
    return int(wert) if isinstance(wert, (int, float)) else default


def service(name: str) -> dict | None:
    return _registry.get("services", {}).get(name)


def container_kette(svc: dict) -> list[str]:
    """Alle Container eines Dienstes in Startreihenfolge.

    Begleiter zuerst: ein WordPress ohne seine Datenbank waere zwar erreichbar,
    wuerde den Besucher aber mit einem Verbindungsfehler begruessen.
    """
    return list(svc.get("begleiter", [])) + [svc["container"]]


async def container_zustand(
    client: httpx.AsyncClient, container: str
) -> tuple[bool, bool]:
    """(laeuft, bereit) fuer einen Container.

    Die beiden Werte sind nicht dasselbe, und das ist der ganze Punkt: ein
    Container gilt Docker gegenueber als `Running`, sobald sein erster Prozess
    startet. Ein WordPress braucht danach noch Sekunden, ein kiwix mit grossem
    Bestand fast eine Minute, bis es die erste Anfrage beantwortet. Wer nur
    `Running` fragt, haelt einen bootenden Dienst fuer fertig.

    `bereit` heisst deshalb: laeuft UND sein Healthcheck steht auf `healthy`.
    Hat der Container gar keinen Healthcheck, gibt es keine bessere Auskunft
    als `Running`, dann gilt er als bereit. Sonst wuerde ein Warten darauf nie
    enden. Ein Dienst, der wirklich beobachtbar sein soll, braucht also einen
    Healthcheck; ohne ihn faellt der Wecker auf das alte Verhalten zurueck.
    """
    resp = await client.get(f"/containers/{container}/json")
    if resp.status_code == 404:
        return False, False
    resp.raise_for_status()
    state = resp.json().get("State", {})
    if not state.get("Running"):
        return False, False
    # Fehlt der Healthcheck, fehlt der ganze Health-Block, nicht bloss der Wert.
    status = state.get("Health", {}).get("Status")
    return True, status in (None, "healthy")


async def container_running(client: httpx.AsyncClient, container: str) -> bool:
    laeuft, _ = await container_zustand(client, container)
    return laeuft


async def dienst_laeuft(client: httpx.AsyncClient, svc: dict) -> bool:
    """Ein Dienst laeuft nur, wenn alle seine Container laufen.

    Bewusst NUR `Running`, nicht `bereit`: an dieser Antwort haengen der
    Start-Entscheid, die Verdraengung und der Reaper. Wuerde hier ein bootender
    Dienst als "laeuft nicht" gelten, liefe die sich selbst neu ladende
    Warteseite in einen zweiten Startversuch und verbrauchte dabei das
    `weck_budget`. Nach wenigen Sekunden waere es aufgebraucht und der Dienst
    haette sich selbst ausgesperrt. Fuer die Belegung des Wirts zaehlt ohnehin
    `Running`: Speicher braucht ein Container auch, waehrend er hochfaehrt.
    """
    for name in container_kette(svc):
        if not await container_running(client, name):
            return False
    return True


async def dienst_bereit(client: httpx.AsyncClient, svc: dict) -> bool:
    """Antwortet der Dienst schon? Alle Container muessen bereit sein."""
    for name in container_kette(svc):
        _, bereit = await container_zustand(client, name)
        if not bereit:
            return False
    return True


async def start_container(client: httpx.AsyncClient, container: str) -> None:
    resp = await client.post(f"/containers/{container}/start")
    # 304 = lief bereits; fuer uns ein Erfolg, nicht ein Fehler.
    if resp.status_code not in (204, 304):
        resp.raise_for_status()


async def stop_container(client: httpx.AsyncClient, container: str) -> None:
    resp = await client.post(f"/containers/{container}/stop", params={"t": 20})
    if resp.status_code not in (204, 304):
        resp.raise_for_status()


async def dienst_starten(client: httpx.AsyncClient, svc: dict) -> None:
    for name in container_kette(svc):
        if not await container_running(client, name):
            await start_container(client, name)


async def dienst_stoppen(client: httpx.AsyncClient, svc: dict) -> None:
    # Umgekehrte Reihenfolge: erst der Dienst, dann seine Datenbank.
    for name in reversed(container_kette(svc)):
        if await container_running(client, name):
            await stop_container(client, name)


async def auf_bereitschaft_warten(
    client: httpx.AsyncClient, svc: dict, grenze_s: float
) -> bool:
    """Haelt die Anfrage, bis der Dienst antwortet. True, wenn er es tat.

    Die Warteseite ist ein `<meta refresh>` und wirkt nur in einem Browser, der
    sie auch anzeigt. Ein Aufruf per Programm, ein abgeschicktes Formular oder
    eine Anwendung, die ihre Daten selbst nachlaedt, bekommt stattdessen einen
    503 mit HTML im Rumpf und bricht ab. Fuer solche Dienste gibt es diesen
    zweiten Weg: nicht antworten, sondern warten, und erst weiterleiten, wenn
    wirklich jemand hinter der Adresse steht.

    Der Preis ist eine gehaltene Verbindung, deshalb ist es kein Vorgabewert,
    sondern steht je Dienst in der Registry (`blockieren_s`). Wer es setzt,
    muss auch einen Healthcheck haben: ohne ihn gilt ein Container schon beim
    Start als bereit, und das Warten endet, bevor der Dienst antwortet.
    """
    ende = time.monotonic() + grenze_s
    while time.monotonic() < ende:
        await asyncio.sleep(BEREIT_ABFRAGE_S)
        try:
            if await dienst_bereit(client, svc):
                return True
        except Exception as exc:  # pragma: no cover - Diagnosepfad
            log.warning("Bereitschaft von %s nicht lesbar: %s", svc["name"], exc)
            return False
    return False


# Wie weit ans Ende der Zugriffsspur wir schauen. 64 KB sind rund 800 Zeilen --
# mehr als jeder Leerlauf-Zeitraum hergibt. Findet sich darin kein zaehlender
# Zugriff, liegt der letzte jedenfalls weiter zurueck als jedes `idle_timeout_s`,
# und genau das ist die gesuchte Antwort.
LOG_FENSTER_BYTES = 64 * 1024


def _zaehlt_als_nutzung(status: int) -> bool:
    """Belegt dieser Statuscode, dass jemand den Dienst tatsaechlich benutzt hat?

    ★★ WER NIE HINEINKOMMT, IST KEIN NUTZER. Am 2026-09-06 hielten drei Shops alle
    drei Plaetze besetzt (`max_wach`), kiwix bekam ueber 20 Minuten nur 503 und das
    Wissens-Portal suchte ohne seine groesste Quelle. Die drei Shops hatte aber
    niemand benutzt: in der Spur standen ausschliesslich Anfragen, die **401**
    bekamen. Eine Probe von aussen sah damit genauso aus wie ein Besucher.

    Ausgenommen sind deshalb 401/403 (abgewiesen, der Inhalt wurde nie geliefert)
    und alles ab 500 -- darunter faellt besonders die **503 der eigenen Warteseite**:
    solange ein Dienst hochfaehrt, schreibt jeder Neuladeversuch eine Zeile, und die
    haette den Dienst allein durchs Warten wachgehalten.

    404 zaehlt bewusst mit: der Dienst hat geantwortet, und ein Mensch, der sich
    vertippt, ist trotzdem da.
    """
    return status not in (401, 403) and status < 500


def _spur_auswerten(text: str) -> float | None:
    """Juengster zaehlender Zeitpunkt aus einem Stueck Zugriffsspur.

    Format (siehe render_nginx.py): ``$time_iso8601 $remote_addr "$request" $status``
    Gelesen werden nur erstes und letztes Feld; ein Request mit Leerzeichen darin
    verschiebt also nichts.

    Rueckgabe None heisst "nicht auswertbar" und ist NICHT dasselbe wie "kein
    Zugriff": der Aufrufer faellt dann auf die Aenderungszeit zurueck, damit ein
    geaendertes Logformat nicht dazu fuehrt, dass reihenweise benutzte Dienste
    schlafen gelegt werden.
    """
    juengster = 0.0
    verstanden = 0
    for zeile in text.splitlines():
        felder = zeile.split()
        if len(felder) < 2:
            continue
        try:
            status = int(felder[-1])
            wann = datetime.fromisoformat(felder[0]).timestamp()
        except ValueError:
            continue
        verstanden += 1
        if _zaehlt_als_nutzung(status) and wann > juengster:
            juengster = wann
    if not verstanden:
        return None
    return juengster


def last_seen(name: str) -> float:
    """Letzter echter Zugriff: neuester Zeitpunkt aus Zugriffsspur und Weckvorgang.

    Der Weckzeitpunkt zaehlt bewusst mit: wer einen Dienst aufweckt, wartet auf ihn,
    und in dieser Zeit steht in der Spur nur die 503 der Warteseite. Ohne das legte
    der Reaper einen startenden Dienst schlafen, waehrend jemand davor sitzt.
    """
    stamps = [_woken_at.get(name, 0.0)]
    log_file = LOG_DIR / f"{name}.log"
    try:
        with log_file.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - LOG_FENSTER_BYTES))
            roh = fh.read().decode("utf-8", "replace")
        aus_spur = _spur_auswerten(roh)
        if aus_spur is None:
            # Unverstaendliches Format: altes Verhalten, aber laut.
            log.warning("Zugriffsspur von %s nicht auswertbar, nutze Aenderungszeit", name)
            aus_spur = log_file.stat().st_mtime
        stamps.append(aus_spur)
    except FileNotFoundError:
        pass
    except OSError as exc:  # pragma: no cover - Diagnosepfad
        log.warning("Zugriffsspur von %s nicht lesbar: %s", name, exc)
    return max(stamps)


def budget_frei(jetzt: float) -> bool:
    """Ist im laufenden Fenster noch ein Startvorgang erlaubt?"""
    budget = grenze("weck_budget", 0)
    if not budget:
        return True
    fenster = grenze("weck_fenster_s", 60)
    _starts[:] = [t for t in _starts if jetzt - t < fenster]
    return len(_starts) < budget


def speicher_frei_mb() -> int | None:
    """Freier Speicher des Wirts in MB, oder None wenn nicht lesbar.

    `MemAvailable` statt `MemFree`: der Kernel rechnet dort ein, was aus Puffern
    und Zwischenspeicher zurueckgeholt werden kann, ohne dass etwas auslagert.
    Genau das ist die Frage vor einem Start. Ist die Datei nicht lesbar, gibt es
    None, der Aufrufer laesst den Start dann zu, statt am Messfehler zu haengen.
    """
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for zeile in fh:
                if zeile.startswith("MemAvailable:"):
                    return int(zeile.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def speicher_reicht(svc: dict, frei: int | None) -> bool:
    """Bleibt nach dem Start dieses Dienstes noch `reserve_mb` uebrig?

    Ohne `bedarf_mb` und ohne `reserve_mb` ist die Pruefung aus, eine Registry
    ohne die neuen Felder verhaelt sich damit exakt wie vorher. Ist der
    Messwert nicht lesbar, wird nicht blockiert: eine kaputte Messung darf
    keinen Dienst aussperren.
    """
    bedarf = int(svc.get("bedarf_mb", 0) or 0)
    reserve = grenze("reserve_mb", 0)
    if not bedarf and not reserve:
        return True
    if frei is None:
        return True
    return frei - bedarf >= reserve


async def wache_dienste(
    client: httpx.AsyncClient, ausser: str | None = None
) -> list[str]:
    """Namen aller verwalteten Dienste, die gerade laufen."""
    wach = []
    for name, svc in _registry.get("services", {}).items():
        if name == ausser:
            continue
        try:
            if await dienst_laeuft(client, svc):
                wach.append(name)
        except Exception as exc:  # pragma: no cover - Diagnosepfad
            log.warning("Zustand von %s nicht lesbar: %s", name, exc)
    return wach


async def platz_schaffen(
    client: httpx.AsyncClient, svc: dict, jetzt: float
) -> str:
    """Haelt Anzahl-Deckel UND Speicherboden ein. Rueckgabe: warum es nicht geht.

    'ok'  , der Dienst darf starten
    'voll': `max_wach` erreicht und alle Plaetze in Benutzung
    'eng' , der Wirt hat zu wenig Speicher, und nichts Verdraengbares mehr da

    Beide Gruende teilen sich dieselbe Politik: verdraengt wird der am laengsten
    unbenutzte Dienst, aber nur wenn er seit `schutz_s` niemanden mehr bedient
    hat. Sonst wuerde ein Aufruf von aussen einen Besucher aus einer laufenden
    Sitzung werfen. Solange noch jemand verdraengbar ist, wird weiter Platz
    gemacht, bei Speichermangel also so lange, bis es reicht.
    """
    deckel = grenze("max_wach", 0)
    wach = await wache_dienste(client, ausser=svc["name"])
    schutz = grenze("schutz_s", 900)
    verdraengbar = sorted(
        (
            n
            for n in wach
            # Wartungsmodus schlaegt die Leerlauf-Regel: gerade WEIL an so einem
            # Dienst niemand "surft", saehe er hier wie der beste Kandidat aus.
            if jetzt - last_seen(n) >= schutz and not in_wartung(n)
        ),
        key=last_seen,
    )

    while True:
        zu_viele = bool(deckel) and len(wach) >= deckel
        frei = speicher_frei_mb()
        eng = not speicher_reicht(svc, frei)
        if not zu_viele and not eng:
            return "ok"

        if not verdraengbar:
            if eng:
                # Keine Nachricht nach aussen: der fehlende Speicher liegt oft
                # gar nicht an den verwalteten Diensten (auf node1 haelt allein
                # obsidian-memory mehrere GB). Dann ist hier nichts zu holen und
                # die Zahl im Log ist der einzige Hinweis darauf, warum ein
                # Dienst nicht mehr hochkommt.
                log.warning(
                    "Speicher zu knapp fuer %s: %s MB frei, Bedarf %s MB + "
                    "Reserve %s MB, nichts mehr verdraengbar, Start abgelehnt",
                    svc["name"],
                    frei,
                    svc.get("bedarf_mb", 0),
                    grenze("reserve_mb", 0),
                )
                return "eng"
            log.info(
                "Deckel %d erreicht und alle Plaetze in Benutzung, %s wartet",
                deckel,
                svc["name"],
            )
            return "voll"

        opfer = verdraengbar.pop(0)
        grund = (
            "Speicher knapp (%s MB frei)" % frei
            if eng
            else "Deckel %d erreicht" % deckel
        )
        log.info(
            "%s, lege %s schlafen, damit %s starten kann",
            grund,
            opfer,
            svc["name"],
        )
        try:
            await dienst_stoppen(client, _registry["services"][opfer])
        except Exception as exc:
            log.error("Verdraengen von %s fehlgeschlagen: %s", opfer, exc)
            continue
        wach.remove(opfer)


def warteseite(svc: dict, ziel: str, zustand: str = "startet") -> str:
    """Die Seite, die ein Besucher sieht, solange sein Dienst nicht antwortet.

    `startet` = faehrt hoch · `schlaeft` = aus, wird auch nicht geweckt
    (Ueberwachung) · `wartet` = darf gerade nicht starten, weil der Deckel
    erreicht ist oder das Weck-Budget aufgebraucht · `eng` = der Wirt hat zu
    wenig Speicher.

    Der Unterschied zwischen `wartet` und `eng` ist fuer den Besucher wichtig:
    beim Deckel wird gleich ein Platz frei, bei Speichermangel kann es dauern.
    Die Ursache selbst bleibt draussen, diese Seiten sind oeffentlich.
    """
    titel = svc.get("titel", svc["name"])
    if zustand == "schlaeft":
        kopf = f"{titel} schläft"
        text = (
            "Dieser Dienst läuft nur bei Bedarf und ist gerade ausgeschaltet. "
            "Ein Aufruf im Browser startet ihn automatisch."
        )
        refresh = ""
    elif zustand == "wartet":
        kopf = f"{titel} reiht sich ein"
        text = (
            "Gerade sind andere Dienste in Benutzung. Dieser startet, sobald "
            "ein Platz frei wird. Diese Seite lädt sich von selbst neu."
        )
        refresh = f'<meta http-equiv="refresh" content="15; url={ziel}">'
    elif zustand == "eng":
        kopf = f"{titel} muss kurz warten"
        text = (
            "Der Server ist gerade stark ausgelastet, deshalb startet dieser "
            "Dienst noch nicht. Diese Seite lädt sich von selbst neu, sobald "
            "wieder Luft ist, geht es weiter."
        )
        # Seltener neu laden als beim Deckel: hier wird nicht in Sekunden ein
        # Platz frei, und jeder Versuch kostet den Wirt eine Runde Arbeit.
        refresh = f'<meta http-equiv="refresh" content="30; url={ziel}">'
    else:
        kopf = f"{titel} wird gestartet"
        text = (
            "Der Dienst läuft nur bei Bedarf und fährt gerade hoch. "
            "Diese Seite lädt sich von selbst neu, bitte einen Moment."
        )
        refresh = f'<meta http-equiv="refresh" content="3; url={ziel}">'
    return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{refresh}
<title>{kopf}</title>
<style>
  body {{ margin:0; min-height:100vh; display:flex; align-items:center;
         justify-content:center; background:#14161a; color:#e8e6e3;
         font-family:system-ui,-apple-system,"Segoe UI",sans-serif; }}
  .karte {{ max-width:26rem; padding:2.5rem; text-align:center; }}
  h1 {{ font-size:1.35rem; font-weight:600; margin:0 0 .75rem; }}
  p {{ line-height:1.6; color:#a8a29e; margin:0; }}
  .puls {{ width:2.5rem; height:2.5rem; margin:0 auto 1.5rem; border-radius:50%;
          border:2px solid #3f4650; border-top-color:#8ab4f8;
          animation:dreh 1s linear infinite; }}
  @keyframes dreh {{ to {{ transform:rotate(360deg); }} }}
</style>
</head>
<body>
  <div class="karte">
    {'' if zustand == "schlaeft" else '<div class="puls"></div>'}
    <h1>{kopf}</h1>
    <p>{text}</p>
  </div>
</body>
</html>"""


@app.on_event("startup")
async def startup() -> None:
    global _registry
    _registry = load_registry()
    # Das Log-Verzeichnis ist ein geteiltes Volume und wird nur gelesen,
    # anlegen ist bloss der Notnagel, falls nginx noch nie geschrieben hat.
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    wartung_laden()
    if _wartung:
        log.info("Wartungsmodus aktiv fuer: %s", ", ".join(sorted(_wartung)))
    log.info(
        "Registry geladen: %d Dienste (%s)",
        len(_registry["services"]),
        ", ".join(_registry["services"]),
    )
    asyncio.create_task(reaper())


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "dienste": len(_registry.get("services", {}))}


@app.get("/status")
async def status() -> JSONResponse:
    """Maschinenlesbarer Zustand aller verwalteten Dienste."""
    jetzt = time.time()
    frei = speicher_frei_mb()
    out = []
    async with httpx.AsyncClient(base_url=DOCKER_API, timeout=10) as client:
        for name, svc in _registry.get("services", {}).items():
            try:
                laeuft = await dienst_laeuft(client, svc)
                bereit = await dienst_bereit(client, svc) if laeuft else False
            except Exception as exc:  # pragma: no cover - Diagnosepfad
                out.append({"name": name, "fehler": str(exc)})
                continue
            gesehen = last_seen(name)
            out.append(
                {
                    "name": name,
                    "container": svc["container"],
                    "laeuft": laeuft,
                    # Der Unterschied zu `laeuft` ist die eigentliche Auskunft:
                    # steht hier dauerhaft false, waehrend `laeuft` true ist,
                    # dann bootet der Dienst nicht mehr, sondern sein
                    # Healthcheck schlaegt fehl. Ohne dieses Feld sieht beides
                    # von aussen gleich aus.
                    "bereit": bereit,
                    "leerlauf_s": int(jetzt - gesehen) if gesehen else None,
                    "idle_timeout_s": svc.get("idle_timeout_s", 0),
                    "bedarf_mb": svc.get("bedarf_mb", 0),
                    # Wuerde dieser Dienst JETZT starten duerfen? Beantwortet
                    # beim Nachsehen die Frage, warum eine Seite haengt.
                    "startbar": speicher_reicht(svc, frei),
                    # Ohne dieses Feld koennte eine Oberflaeche den Schalter
                    # anbieten, ohne seinen Zustand zu kennen, und ihn falsch
                    # herum beschriften.
                    "wartung": in_wartung(name),
                }
            )
    fenster = grenze("weck_fenster_s", 60)
    return JSONResponse(
        {
            "dienste": out,
            "speicher": {
                "frei_mb": frei,
                "reserve_mb": grenze("reserve_mb", 0),
            },
            "grenzen": {
                "max_wach": grenze("max_wach", 0),
                "wach": sum(1 for d in out if d.get("laeuft")),
                "schutz_s": grenze("schutz_s", 900),
                "weck_budget": grenze("weck_budget", 0),
                "weck_fenster_s": fenster,
                "starts_im_fenster": sum(1 for t in _starts if jetzt - t < fenster),
            },
        }
    )


@app.post("/wartung/{name}")
async def wartung_schalten(name: str, an: int = 1) -> JSONResponse:
    """Wartungsmodus an/aus: der Dienst wird nicht mehr automatisch schlafen gelegt
    und nicht verdraengt, bis der Schalter faellt.

    Absichtlich ohne eigenes Token: der Wecker ist nur aus dem LAN erreichbar (auf
    node1 haelt er die Dienst-Ports, auf host spricht ihn nur das dev-portal ueber
    das Docker-Netz an), und wer hier hinkommt, kann die Dienste ohnehin direkt
    starten und stoppen. Ein zweites Geheimnis waere Theater, keine Sicherheit.
    Der Schalter ist bewusst NICHT selbstverfallend: eine Frist, die im falschen
    Moment ablaeuft, ist schlimmer als eine, an die man sich erinnern muss.
    """
    global _registry
    _registry = load_registry()
    if service(name) is None:
        return JSONResponse({"fehler": f"Unbekannter Dienst: {name}"}, status_code=404)
    if an:
        _wartung.add(name)
    else:
        _wartung.discard(name)
    dauerhaft = wartung_sichern()
    log.info("Wartungsmodus fuer %s %s", name, "AN" if an else "AUS")
    antwort = {"dienst": name, "wartung": in_wartung(name), "dauerhaft": dauerhaft}
    if not dauerhaft:
        antwort["warnung"] = (
            "Der Schalter gilt, konnte aber nicht gespeichert werden, nach einem "
            "Neustart des Weckers ist er weg."
        )
    return JSONResponse(antwort)


def _rueckweg(request: Request) -> str:
    """Wohin die Warteseite zurueckkehrt, nachdem der Dienst wach ist.

    ★★ `X-Original-URI` ALLEIN REICHT NICHT, WENN DER WECKER HINTER EINEM
    PRAEFIX-PFAD SITZT. Auf node1 haelt der Wecker die Host-Ports selbst, dort
    ist die Anfrage-URI vollstaendig und alles stimmt. Auf host laeuft der
    einzige Dienst (schach) ueber das dev-portal, und dessen
    `proxy_pass $schach_upstream/` schneidet das Praefix ab: beim Wecker kommt
    `/` an statt `/schach/`. Die Warteseite trug damit `refresh url=/` und warf
    den Spieler nach drei Sekunden auf die Portal-Startseite. Es sieht nicht
    nach einem Fehler aus, sondern nach einer Weiterleitung, die so gemeint
    ist, und der Dienst dahinter war zu dem Zeitpunkt laengst wach.

    Das Praefix steht im Header, den das Portal ohnehin schon setzt. Es wird
    nur vorangestellt, wenn es fehlt: schneidet ein Aufrufer kuenftig nichts
    ab, ist die URI bereits vollstaendig, und ein zweites `/schach` davor
    ergaebe `/schach/schach/`.
    """
    ziel = request.headers.get("X-Original-URI", "/")
    praefix = request.headers.get("X-Forwarded-Prefix", "").rstrip("/")
    if praefix and not ziel.startswith(praefix + "/") and ziel != praefix:
        ziel = praefix + ziel
    return ziel


@app.get("/wake/{name}")
@app.post("/wake/{name}")
async def wake(name: str, request: Request) -> HTMLResponse:
    """Wird von nginx aufgerufen, wenn der eigentliche Dienst nicht antwortet."""
    global _registry
    _registry = load_registry()
    svc = service(name)
    if svc is None:
        return HTMLResponse(f"Unbekannter Dienst: {name}", status_code=404)

    ziel = _rueckweg(request)
    quelle = request.headers.get("X-Client-IP", "")

    # Ueberwachung weckt nicht und zaehlt nicht als Nutzung. Antwort mit 200,
    # damit der gewollte Schlafzustand nicht als Ausfall alarmiert wird.
    if quelle in _registry.get("no_wake_sources", set()):
        return HTMLResponse(
            warteseite(svc, ziel, zustand="schlaeft"),
            status_code=200,
            headers={"X-Wake-State": "sleeping", "Cache-Control": "no-store"},
        )

    jetzt = time.time()
    try:
        async with httpx.AsyncClient(base_url=DOCKER_API, timeout=30) as client:
            if not await dienst_laeuft(client, svc):
                # Erst die Grenzen, dann der Start. Ein abgewiesener Versuch
                # gilt NICHT als Nutzung, sonst haelt blosses Anklopfen die
                # Uhr des Reapers am Laufen.
                if not budget_frei(jetzt):
                    log.info(
                        "Weck-Budget (%d/%d s) aufgebraucht, %s wartet",
                        grenze("weck_budget", 0),
                        grenze("weck_fenster_s", 60),
                        name,
                    )
                    return HTMLResponse(
                        warteseite(svc, ziel, zustand="wartet"),
                        status_code=503,
                        headers={
                            "Retry-After": "15",
                            "X-Wake-State": "throttled",
                            "Cache-Control": "no-store",
                        },
                    )
                platz = await platz_schaffen(client, svc, jetzt)
                if platz != "ok":
                    eng = platz == "eng"
                    return HTMLResponse(
                        warteseite(
                            svc, ziel, zustand="eng" if eng else "wartet"
                        ),
                        status_code=503,
                        headers={
                            "Retry-After": "30" if eng else "15",
                            "X-Wake-State": "lowmem" if eng else "queued",
                            "Cache-Control": "no-store",
                        },
                    )
                log.info(
                    "wecke %s (%s)", name, ", ".join(container_kette(svc))
                )
                _woken_at[name] = jetzt
                _starts.append(jetzt)
                await dienst_starten(client, svc)
            else:
                # Laeuft schon: der Aufruf ist echte Nutzung.
                _woken_at[name] = jetzt

            blockieren = float(svc.get("blockieren_s", 0) or 0)
            if blockieren > 0:
                # War der Dienst schon vor dem Warten bereit und nginx kam
                # trotzdem nicht durch, liegt der Fehler nicht am Schlaf. Eine
                # Weiterleitung waere dann eine Schleife: sie schickte den
                # Aufrufer auf genau die Adresse zurueck, die ihn hierher
                # geschickt hat. In dem Fall ist die Warteseite ehrlicher.
                if not await dienst_bereit(client, svc) and await (
                    auf_bereitschaft_warten(client, svc, blockieren)
                ):
                    log.info("%s ist bereit, leite weiter auf %s", name, ziel)
                    # 307 statt 302: nur der erhaelt Methode und Rumpf. Ein
                    # abgeschicktes Formular wuerde sonst als GET wiederholt
                    # und der Inhalt waere weg.
                    return RedirectResponse(
                        ziel,
                        status_code=307,
                        headers={
                            "X-Wake-State": "ready",
                            "Cache-Control": "no-store",
                        },
                    )
    except Exception as exc:
        log.error("Wecken von %s fehlgeschlagen: %s", name, exc)
        return HTMLResponse(
            f"<h1>{svc.get('titel', name)} lässt sich nicht starten</h1><p>{exc}</p>",
            status_code=502,
        )

    return HTMLResponse(
        warteseite(svc, ziel),
        status_code=503,
        headers={
            "Retry-After": "3",
            "X-Wake-State": "starting",
            "Cache-Control": "no-store",
        },
    )


async def reaper() -> None:
    """Legt Dienste schlafen, die laenger als vereinbart nicht genutzt wurden."""
    global _registry
    while True:
        await asyncio.sleep(REAP_INTERVAL_S)
        try:
            _registry = load_registry()
            jetzt = time.time()
            async with httpx.AsyncClient(base_url=DOCKER_API, timeout=30) as client:
                for name, svc in _registry.get("services", {}).items():
                    timeout = svc.get("idle_timeout_s", 0)
                    if not timeout:
                        continue
                    if in_wartung(name):
                        continue
                    # Frisch geweckte Dienste bekommen ihre Startzeit zugestanden.
                    geweckt = _woken_at.get(name, 0.0)
                    if jetzt - geweckt < svc.get("start_timeout_s", 90):
                        continue
                    leerlauf = jetzt - last_seen(name)
                    if leerlauf < timeout:
                        continue
                    if not await dienst_laeuft(client, svc):
                        continue
                    log.info(
                        "lege %s schlafen (%d s ohne Zugriff)", name, int(leerlauf)
                    )
                    await dienst_stoppen(client, svc)
        except Exception as exc:  # pragma: no cover - Dauerlaeufer nie sterben lassen
            log.error("Reaper-Durchlauf fehlgeschlagen: %s", exc)
