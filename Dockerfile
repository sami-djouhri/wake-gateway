FROM python:3.14-slim

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
USER 10001

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
