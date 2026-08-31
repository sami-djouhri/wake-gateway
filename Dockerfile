FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/
COPY services.json /app/services.json

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
# frische Volume root — und der unprivilegierte Dienst bekommt beim Schreiben
# ein 'Permission denied', das erst auffaellt, wenn ein Neustart den
# vermeintlich gesetzten Schutz verschluckt hat.
RUN mkdir -p /var/lib/wake && chown 10001:10001 /var/lib/wake

USER 10001

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
