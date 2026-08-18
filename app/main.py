"""wake-gateway — Torwaechter vor schlafenden Web-Diensten auf node1.

Der Wecker uebernimmt die veroeffentlichten Ports der on-demand-Dienste. Solange
ein Dienst laeuft, reicht nginx die Anfragen einfach durch. Ist er aus, laeuft
nginx in einen 502 und leitet intern hierher um: dieser Dienst startet den
Container und liefert eine Warteseite, die sich selbst neu laedt. Der Nutzer
ruft also nur die gewohnte Adresse auf — einschalten muss er nichts.

Umgekehrt beobachtet der Reaper, wie lange ein Dienst nicht mehr genutzt wurde,
und legt ihn nach `idle_timeout_s` wieder schlafen. Als Nutzungsspur dient die
Zugriffs-Logdatei, die nginx pro Dienst schreibt: deren Aenderungszeitpunkt ist
der letzte echte Zugriff. Das kostet im Anfragepfad nichts und haelt diesen
Dienst aus der Verbindung heraus, solange alles laeuft.

Ueberwachungsverkehr (blackbox-exporter auf host) ist bewusst ausgenommen —
er weckt nichts und gilt nicht als Nutzung. Sonst haette die Minutentakt-Probe
jeden Dienst dauerhaft wachgehalten und on-demand waere wirkungslos.
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

REGISTRY_PATH = Path(os.getenv("REGISTRY_PATH", "/app/services.json"))
LOG_DIR = Path(os.getenv("WAKE_LOG_DIR", "/var/log/wake"))
DOCKER_API = os.getenv("DOCKER_API", "http://wake-socket-proxy:2375")
REAP_INTERVAL_S = int(os.getenv("REAP_INTERVAL_S", "60"))

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


def load_registry() -> dict:
    """Liest die Registry neu ein. Aenderungen wirken beim naechsten Zugriff."""
    with REGISTRY_PATH.open(encoding="utf-8") as fh:
        data = json.load(fh)
    return {
        "no_wake_sources": set(data.get("no_wake_sources", [])),
        "services": {s["name"]: s for s in data["services"]},
    }


def service(name: str) -> dict | None:
    return _registry.get("services", {}).get(name)


def container_kette(svc: dict) -> list[str]:
    """Alle Container eines Dienstes in Startreihenfolge.

    Begleiter zuerst: ein WordPress ohne seine Datenbank waere zwar erreichbar,
    wuerde den Besucher aber mit einem Verbindungsfehler begruessen.
    """
    return list(svc.get("begleiter", [])) + [svc["container"]]


async def container_running(client: httpx.AsyncClient, container: str) -> bool:
    resp = await client.get(f"/containers/{container}/json")
    if resp.status_code == 404:
        return False
    resp.raise_for_status()
    return bool(resp.json().get("State", {}).get("Running"))


async def dienst_laeuft(client: httpx.AsyncClient, svc: dict) -> bool:
    """Ein Dienst laeuft nur, wenn alle seine Container laufen."""
    for name in container_kette(svc):
        if not await container_running(client, name):
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


def last_seen(name: str) -> float:
    """Letzter echter Zugriff: neuester Zeitpunkt aus nginx-Log und Weckvorgang."""
    stamps = [_woken_at.get(name, 0.0)]
    log_file = LOG_DIR / f"{name}.log"
    try:
        stamps.append(log_file.stat().st_mtime)
    except FileNotFoundError:
        pass
    return max(stamps)


def warteseite(svc: dict, ziel: str, schlaeft_nur: bool = False) -> str:
    titel = svc.get("titel", svc["name"])
    if schlaeft_nur:
        kopf = f"{titel} schläft"
        text = (
            "Dieser Dienst läuft nur bei Bedarf und ist gerade ausgeschaltet. "
            "Ein Aufruf im Browser startet ihn automatisch."
        )
        refresh = ""
    else:
        kopf = f"{titel} wird gestartet"
        text = (
            "Der Dienst läuft nur bei Bedarf und fährt gerade hoch. "
            "Diese Seite lädt sich von selbst neu — bitte einen Moment."
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
    {'' if schlaeft_nur else '<div class="puls"></div>'}
    <h1>{kopf}</h1>
    <p>{text}</p>
  </div>
</body>
</html>"""


@app.on_event("startup")
async def startup() -> None:
    global _registry
    _registry = load_registry()
    # Das Log-Verzeichnis ist ein geteiltes Volume und wird nur gelesen —
    # anlegen ist bloss der Notnagel, falls nginx noch nie geschrieben hat.
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
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
    out = []
    async with httpx.AsyncClient(base_url=DOCKER_API, timeout=10) as client:
        for name, svc in _registry.get("services", {}).items():
            try:
                laeuft = await dienst_laeuft(client, svc)
            except Exception as exc:  # pragma: no cover - Diagnosepfad
                out.append({"name": name, "fehler": str(exc)})
                continue
            gesehen = last_seen(name)
            out.append(
                {
                    "name": name,
                    "container": svc["container"],
                    "laeuft": laeuft,
                    "leerlauf_s": int(jetzt - gesehen) if gesehen else None,
                    "idle_timeout_s": svc.get("idle_timeout_s", 0),
                }
            )
    return JSONResponse({"dienste": out})


@app.get("/wake/{name}")
@app.post("/wake/{name}")
async def wake(name: str, request: Request) -> HTMLResponse:
    """Wird von nginx aufgerufen, wenn der eigentliche Dienst nicht antwortet."""
    global _registry
    _registry = load_registry()
    svc = service(name)
    if svc is None:
        return HTMLResponse(f"Unbekannter Dienst: {name}", status_code=404)

    ziel = request.headers.get("X-Original-URI", "/")
    quelle = request.headers.get("X-Client-IP", "")

    # Ueberwachung weckt nicht und zaehlt nicht als Nutzung. Antwort mit 200,
    # damit der gewollte Schlafzustand nicht als Ausfall alarmiert wird.
    if quelle in _registry.get("no_wake_sources", set()):
        return HTMLResponse(
            warteseite(svc, ziel, schlaeft_nur=True),
            status_code=200,
            headers={"X-Wake-State": "sleeping", "Cache-Control": "no-store"},
        )

    _woken_at[name] = time.time()
    try:
        async with httpx.AsyncClient(base_url=DOCKER_API, timeout=30) as client:
            if not await dienst_laeuft(client, svc):
                log.info(
                    "wecke %s (%s)", name, ", ".join(container_kette(svc))
                )
                await dienst_starten(client, svc)
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
