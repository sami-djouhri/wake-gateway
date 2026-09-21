#!/usr/bin/env bash
# wartung.sh: Wartungsmodus des on-demand-Weckers schalten und nachsehen.
#
#   ./wartung.sh status            was laeuft, was schlaeft, was steht offen
#   ./wartung.sh schach an         Dienst wachhalten, solange daran gearbeitet wird
#   ./wartung.sh schach aus        wieder freigeben
#
# Wozu. Beim Messen an einem on-demand-Dienst legt der Wecker den Container
# mitten in der Arbeit schlafen, weil dabei niemand "surft" und der Dienst
# deshalb als Verdraengungskandidat am besten aussieht. In der Schach-Sitzung am
# 2026-09-05 ist das dreimal passiert, einmal mitten in einem Testlauf.
#
# Der Wartungsmodus dagegen existiert im Wecker seit dem 2026-08-23, war aber
# nur ueber einen von Hand gebauten Aufruf im Container erreichbar. ★ Der Port
# ist dabei 8000, nicht 8080; das musste jedes Mal gesucht werden.
#
# Zwei Wirte, ein Skript: host spricht seinen Wecker ueber den Container an,
# node1 ueber ssh. Welcher Wirt gemeint ist, wird NICHT hier gepflegt, sondern
# bei jedem Aufruf aus den Registries beider Wecker gelesen. Eine Liste im
# Skript waere schon jetzt falsch: der Auftrag nannte sechs Dienste auf node1,
# gemessen sind es sieben (db-konsole ist dazugekommen).
set -uo pipefail

node1="${WARTUNG_node1:-user@192.0.2.10}"
SSH_KEY="${WARTUNG_SSH_KEY:-$HOME/.ssh/id_node1}"
WIRTE="host node1"

# Das Programm laeuft IM Container (dort ist der Wecker auf 127.0.0.1:8000
# erreichbar) und wird ueber stdin hineingereicht. Ueber ssh traegt derselbe
# stdin bis in den entfernten Container, deshalb braucht es keine zweite,
# geschachtelte Zitierweise.
read -r -d '' PROGRAMM <<'PY'
import json, os, sys, urllib.error, urllib.request

pfad = os.environ["WG_PFAD"]
methode = os.environ["WG_METHODE"]
anfrage = urllib.request.Request("http://127.0.0.1:8000" + pfad, method=methode)
try:
    with urllib.request.urlopen(anfrage, timeout=15) as antwort:
        sys.stdout.write(antwort.read().decode())
except urllib.error.HTTPError as fehler:
    sys.stdout.write(fehler.read().decode())
    sys.exit(3)
PY

# api <wirt> <pfad> [methode] -> JSON auf stdout, rc != 0 wenn unerreichbar
api() {
  local wirt="$1" pfad="$2" methode="${3:-GET}"
  local innen="docker exec -e WG_PFAD='$pfad' -e WG_METHODE='$methode' -i wake-api python -"
  if [ "$wirt" = host ]; then
    printf '%s' "$PROGRAMM" | timeout 40 bash -c "$innen" 2>/dev/null
  else
    printf '%s' "$PROGRAMM" | timeout 40 ssh -i "$SSH_KEY" \
      -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=8 \
      "$node1" "$innen" 2>/dev/null
  fi
}

# Liest /status aller Wirte einmal ein. Ergebnis in ZUSTAND_<wirt>, plus
# UNERREICHBAR fuer die, die nicht geantwortet haben. Ein stiller Ausfall waere
# hier besonders tueckisch: ein nicht abgefragter Wirt sieht in der Zusammen-
# fassung genauso aus wie einer ohne offenen Wartungsmodus.
UNERREICHBAR=""
lage_holen() {
  local wirt roh
  for wirt in $WIRTE; do
    roh="$(api "$wirt" /status GET)"
    if [ -z "$roh" ] || ! printf '%s' "$roh" | python3 -c 'import json,sys; json.load(sys.stdin)' 2>/dev/null; then
      UNERREICHBAR="$UNERREICHBAR $wirt"
      roh=""
    fi
    printf -v "ZUSTAND_${wirt}" '%s' "$roh"
  done
}

# Sucht einen Dienstnamen in den eingelesenen Registries.
wirt_von_dienst() {
  local gesucht="$1" wirt roh treffer=""
  for wirt in $WIRTE; do
    roh="$(eval printf '%s' "\"\$ZUSTAND_${wirt}\"")"
    [ -n "$roh" ] || continue
    if printf '%s' "$roh" | WG_NAME="$gesucht" python3 -c '
import json, os, sys
name = os.environ["WG_NAME"]
lage = json.load(sys.stdin)
sys.exit(0 if any(d.get("name") == name for d in lage.get("dienste", [])) else 1)'; then
      treffer="$treffer $wirt"
    fi
  done
  printf '%s' "${treffer# }"
}

alle_dienste() {
  local wirt roh
  for wirt in $WIRTE; do
    roh="$(eval printf '%s' "\"\$ZUSTAND_${wirt}\"")"
    [ -n "$roh" ] || continue
    printf '%s' "$roh" | WG_WIRT="$wirt" python3 -c '
import json, os, sys
for d in json.load(sys.stdin).get("dienste", []):
    print(os.environ["WG_WIRT"] + "/" + str(d.get("name")))'
  done
}

status_zeigen() {
  local wirt roh offen_gesamt=0
  echo "Wartungsmodus des on-demand-Weckers"
  echo
  for wirt in $WIRTE; do
    roh="$(eval printf '%s' "\"\$ZUSTAND_${wirt}\"")"
    if [ -z "$roh" ]; then
      printf '  %-8s nicht erreichbar\n\n' "$wirt"
      continue
    fi
    printf '  %s\n' "$wirt"
    printf '%s' "$roh" | python3 -c '
import json, sys

lage = json.load(sys.stdin)
for d in lage.get("dienste", []):
    if d.get("fehler"):
        print("    %-14s FEHLER  %s" % (d.get("name"), d["fehler"]))
        continue
    lauf = "wach" if d.get("laeuft") else "schlaeft"
    if d.get("laeuft") and d.get("bereit") is False:
        lauf = "wach, nicht bereit"
    leer = d.get("leerlauf_s")
    if leer is None:
        seit = "nie benutzt"
    elif leer < 90:
        seit = "%d s ungenutzt" % leer
    elif leer < 5400:
        seit = "%d min ungenutzt" % (leer // 60)
    elif leer < 172800:
        seit = "%d h ungenutzt" % (leer // 3600)
    else:
        seit = "%d Tage ungenutzt" % (leer // 86400)
    marke = "WARTUNG AN" if d.get("wartung") else ""
    print(("    %-14s %-20s %-18s %s" % (d.get("name"), lauf, seit, marke)).rstrip())
sp = lage.get("speicher", {})
gr = lage.get("grenzen", {})
print("    %s frei, Boden %s MB, %s von %s wach" % (
    str(sp.get("frei_mb")) + " MB", sp.get("reserve_mb"),
    gr.get("wach"), gr.get("max_wach")))'
    echo
  done

  # Die eigentliche Auskunft: steht irgendwo noch ein Schalter offen?
  local offen=""
  for wirt in $WIRTE; do
    roh="$(eval printf '%s' "\"\$ZUSTAND_${wirt}\"")"
    [ -n "$roh" ] || continue
    offen="$offen $(printf '%s' "$roh" | WG_WIRT="$wirt" python3 -c '
import json, os, sys
for d in json.load(sys.stdin).get("dienste", []):
    if d.get("wartung"):
        print(os.environ["WG_WIRT"] + "/" + str(d.get("name")), end=" ")')"
  done
  offen="$(printf '%s' "$offen" | tr -s ' ' | sed 's/^ //;s/ $//')"

  if [ -n "$offen" ]; then
    offen_gesamt=$(printf '%s' "$offen" | wc -w)
    echo "★ OFFEN: $offen_gesamt Dienst(e) im Wartungsmodus, schlafen nicht von selbst ein:"
    echo "      $offen"
    echo "  Der Schalter laeuft nicht ab. Wieder freigeben mit:"
    for eintrag in $offen; do
      echo "      $0 ${eintrag#*/} aus"
    done
  elif [ -n "$UNERREICHBAR" ]; then
    echo "Auf den erreichbaren Wirten kein Wartungsmodus offen."
  else
    echo "Kein Wartungsmodus offen."
  fi

  if [ -n "$UNERREICHBAR" ]; then
    echo
    echo "⚠ Nicht abgefragt:${UNERREICHBAR}. Fuer diese(n) Wirt(e) ist die Aussage"
    echo "  oben unvollstaendig, ein offener Wartungsmodus waere dort nicht sichtbar."
    return 2
  fi
  return 0
}

schalten() {
  local dienst="$1" an="$2" wirt roh
  wirt="$(wirt_von_dienst "$dienst")"
  if [ -z "$wirt" ]; then
    echo "Unbekannter Dienst: $dienst" >&2
    echo "Bekannt sind:" >&2
    alle_dienste | sed 's/^/  /' >&2
    [ -n "$UNERREICHBAR" ] && echo "  (${UNERREICHBAR# } nicht erreichbar, dortige Dienste fehlen in der Liste)" >&2
    return 1
  fi
  if [ "$(printf '%s' "$wirt" | wc -w)" -gt 1 ]; then
    echo "Der Name '$dienst' existiert auf mehreren Wirten: $wirt" >&2
    echo "Das Skript kann nicht raten, welcher gemeint ist." >&2
    return 1
  fi

  roh="$(api "$wirt" "/wartung/${dienst}?an=${an}" POST)"
  if [ -z "$roh" ]; then
    echo "Wecker auf $wirt hat nicht geantwortet." >&2
    return 1
  fi

  printf '%s' "$roh" | WG_WIRT="$wirt" WG_AN="$an" WG_SELBST="$0" python3 -c '
import json, os, sys

antwort = json.load(sys.stdin)
if "fehler" in antwort:
    print(antwort["fehler"], file=sys.stderr)
    sys.exit(1)

wirt = os.environ["WG_WIRT"]
dienst = antwort.get("dienst")
selbst = os.environ["WG_SELBST"]
if antwort.get("wartung"):
    print("Wartungsmodus AN fuer %s/%s." % (wirt, dienst))
    print()
    print("★ Der Schalter ist dauerhaft: er ueberlebt einen Neustart des Weckers")
    print("  und laeuft NICHT von selbst ab. Solange er steht, wird %s" % dienst)
    print("  weder automatisch schlafen gelegt noch bei knappem Speicher")
    print("  verdraengt, also ist on-demand fuer diesen Dienst ausgehebelt.")
    print()
    print("  Nach der Arbeit wieder freigeben:  %s %s aus" % (selbst, dienst))
    if not antwort.get("dauerhaft", True):
        print()
        print("⚠ %s" % antwort.get("warnung", "Konnte nicht gespeichert werden."))
        print("  Der Schalter gilt also nur bis zum naechsten Neustart des Weckers.")
else:
    print("Wartungsmodus AUS fuer %s/%s." % (wirt, dienst))
    print("Der Dienst folgt wieder seiner Leerlaufzeit und kann verdraengt werden.")
'
}

hilfe() {
  cat <<'TEXT'
wartung.sh: Wartungsmodus des on-demand-Weckers (host und node1)

  wartung.sh status           Lage beider Wirte, offene Schalter am Ende
  wartung.sh <dienst> an      Dienst wachhalten, solange daran gearbeitet wird
  wartung.sh <dienst> aus     wieder freigeben

Der Wirt wird aus dem Dienstnamen abgeleitet, es gibt keine Liste zu pflegen.
Der Wartungsmodus ist dauerhaft und laeuft nicht ab; was offen steht, zeigt
"status".
TEXT
}

case "${1:-status}" in
  status|--status|"") lage_holen; status_zeigen ;;
  -h|--help|hilfe)    hilfe ;;
  *)
    dienst="$1"
    aktion="${2:-}"
    if ! printf '%s' "$dienst" | grep -qE '^[A-Za-z0-9_-]+$'; then
      echo "Ungueltiger Dienstname: $dienst" >&2
      exit 1
    fi
    case "$aktion" in
      an|ein|1)   lage_holen; schalten "$dienst" 1 ;;
      aus|ab|0)   lage_holen; schalten "$dienst" 0 ;;
      "")         echo "Fehlt: an oder aus. Siehe $0 --help" >&2; exit 1 ;;
      *)          echo "Unbekannte Aktion: $aktion (an|aus)" >&2; exit 1 ;;
    esac
    ;;
esac
