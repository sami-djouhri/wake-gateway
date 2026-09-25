FROM python:3.12-slim
# Sicherheitsstand des Basis-Image nachziehen. Ein Upstream-Image friert die Paketstaende
# vom Tag seines Baus ein, Debian-security ist regelmaessig weiter, und ein `--pull` holt
# nur ein neueres Bild derselben Verspaetung: gemessen am 2026-09-13 trug das aktuelle
# python:3.13-slim aus der Registry dieselben drei perl-CVEs wie das monatealte lokale.
# `upgrade`, nicht `dist-upgrade`: letzteres darf Pakete entfernen, um Konflikte zu loesen.
RUN apt-get update \
 && apt-get -y upgrade \
 && rm -rf /var/lib/apt/lists/*


WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/

# ★ Hier stand `COPY services.json /app/services.json`, und die Zeile war
# zweimal falsch. Im Betrieb tat sie nichts: beide Instanzen haengen ihre
# Registry zur Laufzeit ein (`./services.json:/app/services.json:ro`, gemessen
# 2026-09-11 an wake-api und wake-render auf beiden Wirten), der Mount
# ueberdeckt die kopierte Datei also immer. Draussen war sie ein sicherer
# Abbruch: die Registry beschreibt die Dienste EINES Hauses und steht deshalb
# in exclude.txt, weshalb das seit dem 2026-09-01 oeffentliche Repo bei jedem
# `docker build` mit "services.json: not found" abbrach, noch bevor ein Befehl
# lief. Hier faellt das nie auf, weil die Datei im Arbeitsbaum liegt.
# Wer ohne Mount betreibt, setzt REGISTRY_PATH oder legt services.json aus
# services.example.json an.

# Die Quelldateien kommen per rsync mit Gruppenrechten (660) vom kanonischen
# Host. Ohne Leserecht fuer alle koennte der unprivilegierte Dienstnutzer sein
# eigenes Programm nicht laden.
RUN chmod -R a+rX /app

# Unprivilegiert laufen: der Dienst braucht keinerlei Rechte am Host, der
# Docker-Zugriff laeuft ausschliesslich ueber den Socket-Proxy.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin wecker

# Ablage fuer den eigenen Zustand (Wartungsmodus). Sie MUSS hier im Image
# angelegt und uebereignet werden: Docker uebernimmt Eigentuemer und Rechte
# eines Image-Verzeichnisses nur dann in ein Named Volume, wenn das Volume
# beim Einhaengen leer ist. Legt man sie erst zur Laufzeit an, gehoert das
# frische Volume root, und der unprivilegierte Dienst bekommt beim Schreiben
# ein 'Permission denied', das erst auffaellt, wenn ein Neustart den
# vermeintlich gesetzten Schutz verschluckt hat.
RUN mkdir -p /var/lib/wake && chown 10001:10001 /var/lib/wake

USER 10001

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
