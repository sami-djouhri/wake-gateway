"""Erzeugt die nginx-Konfiguration des Weckers aus services.json.

Laeuft als kurzlebiger Init-Schritt vor nginx. Dadurch gibt es nur EINE
Wahrheit: wer einen Dienst in services.json eintraegt, bekommt automatisch
den passenden Server-Block, das Zugriffs-Log fuer die Leerlauf-Erkennung und
die Umleitung auf den Weck-Endpunkt.
"""

import json
import os
from pathlib import Path

REGISTRY_PATH = Path(os.getenv("REGISTRY_PATH", "/app/services.json"))
OUT_PATH = Path(os.getenv("NGINX_CONF_OUT", "/etc/nginx/conf.d/wake.conf"))
API_UPSTREAM = os.getenv("API_UPSTREAM", "wake-api:8000")

KOPF = """# ACHTUNG: automatisch erzeugt aus services.json, Aenderungen hier gehen
# beim naechsten Start verloren. Dienste in services.json pflegen.

# Docker-interner Resolver: die Ziel-Namen werden zur LAUFZEIT aufgeloest.
# Ohne das koennte nginx nicht starten, solange ein Zieldienst schlaeft.
resolver 127.0.0.11 valid=10s ipv6=off;

# Ueberwachungsverkehr wird nicht als Nutzung gewertet, sonst schliefe nie
# ein Dienst wieder ein.
map $remote_addr $echte_nutzung {
    default            1;
%(no_wake_map)s}

log_format nutzung '$time_iso8601 $remote_addr "$request" $status';

map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}
"""

BLOCK = """
# --- %(titel)s (Container %(container)s) ---
server {
    listen %(listen)d;
    server_name _;

    # Aenderungszeit dieser Datei = letzter echter Zugriff. Der Reaper liest
    # sie aus; deshalb kein Puffer, die Zeit muss sofort stimmen.
    access_log /var/log/wake/%(name)s.log nutzung if=$echte_nutzung;

    client_max_body_size 512m;

    # Pruefpfad fuer Ueberwachung und Container-Healthcheck: antwortet immer,
    # weckt nie und zaehlt nicht als Nutzung. Noetig, weil bei Ports auf
    # 127.0.0.1 die echte Absenderadresse durch Dockers Weiterleitung
    # verlorengeht: Ueberwachung und echte Besucher waeren sonst nicht zu
    # unterscheiden und der Dienst schliefe nie wieder ein.
    location = /_wake_health {
        access_log off;
        default_type text/plain;
        return 200 "wake-gateway ok: %(name)s\\n";
    }

    location / {
        set $ziel http://%(upstream)s;
        proxy_pass $ziel;

        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection $connection_upgrade;

        # Schlaeft der Dienst, ist der Name nicht aufloesbar und nginx meldet
        # 502: das ist unser Signal zum Wecken. Kurzer Timeout, damit der
        # Nutzer die Warteseite sofort sieht.
        proxy_connect_timeout 2s;
        proxy_read_timeout   300s;
        proxy_send_timeout   300s;
        error_page 502 504 = @wecken;
    }

    location @wecken {
        internal;
        # URI-Teil an der Variablen ist hier ausdruecklich gewollt: die
        # Anfrage soll auf den Weck-Endpunkt umgeschrieben werden.
        set $wecker http://%(api)s;
        proxy_pass $wecker/wake/%(name)s;
        proxy_set_header X-Original-URI $request_uri;
        proxy_set_header X-Client-IP    $remote_addr;
        proxy_connect_timeout 3s;
        # Abgeleitet aus `blockieren_s` des Dienstes, nicht fest verdrahtet:
        # wartet der Wecker laenger auf die Bereitschaft, als nginx auf ihn
        # wartet, schneidet nginx die Verbindung vorher ab. Der Besucher saehe
        # einen 504, obwohl der Dienst gleich da gewesen waere.
        proxy_read_timeout   %(weck_timeout)ds;
    }
}
"""


# Zustandspunkt des Weckers SELBST, unabhaengig von der Registry.
#
# ★ Warum ein eigener Port und nicht der eines Dienstes: bis 2026-09-19 zeigte
# der Container-Healthcheck auf 8091/_wake_health, also auf den Port eines
# einzelnen verwalteten Dienstes. Als der Owner-Entscheid vom 18.09. diesen und
# einen zweiten Dienst aus dem Weckbetrieb nahm, verschwand mit ihnen der
# Server-Block, auf den die Pruefung zielte. Der
# Wecker arbeitete einwandfrei weiter und galt trotzdem acht Stunden lang als
# unhealthy (FailingStreak 466). Ein Healthcheck, der an einem Registry-Eintrag
# haengt, misst nicht den Dienst, sondern die Registry.
#
# 8099 ist bewusst NICHT publiziert: die Pruefung laeuft im Container.
GESUNDHEIT = """
# --- Zustandspunkt des Weckers selbst (nicht aus services.json) ---
server {
    listen 8099;
    server_name _;
    access_log off;

    location = /_wake_health {
        default_type text/plain;
        return 200 "wake-gateway ok\\n";
    }

    location / {
        return 404;
    }
}
"""


def weck_timeout(svc: dict) -> int:
    """Wie lange nginx auf den Weck-Endpunkt warten darf.

    40 s war der Wert, als der Wecker immer sofort mit einer Warteseite
    antwortete. Wartet er stattdessen auf die Bereitschaft (`blockieren_s`),
    muss nginx laenger Geduld haben als er selbst, sonst kappt es die
    Verbindung kurz vor dem Ziel.
    """
    return max(40, int(svc.get("blockieren_s", 0) or 0) + 10)


def main() -> None:
    with REGISTRY_PATH.open(encoding="utf-8") as fh:
        data = json.load(fh)

    no_wake = data.get("no_wake_sources", [])
    no_wake_map = "".join(f'    {adr:<18} 0;\n' for adr in no_wake)

    teile = [KOPF % {"no_wake_map": no_wake_map}, GESUNDHEIT]
    for svc in data["services"]:
        teile.append(
            BLOCK
            % {
                "titel": svc.get("titel", svc["name"]),
                "container": svc["container"],
                "name": svc["name"],
                "listen": svc["listen"],
                "upstream": svc["upstream"],
                "api": API_UPSTREAM,
                "weck_timeout": weck_timeout(svc),
            }
        )

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text("".join(teile), encoding="utf-8")
    print(f"nginx-Konfiguration geschrieben: {OUT_PATH} ({len(data['services'])} Dienste)")


if __name__ == "__main__":
    main()
