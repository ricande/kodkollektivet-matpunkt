#!/usr/bin/env python3
"""Systematlasens kontrollmotor: kör kontrollerna i kontrollspec.json mot driften och rapporterar.

    motor.py --spec kontrollspec.json --resultat <katalog> [--enhetsstatus <fil>] [--uppstart]
             [--las hoppa|vanta] [--vanta-sekunder N] [--intervall S]

Lägen:
  normal (standard)   en runda; ett fel är FEL direkt
  --uppstart          efter omstart: varje kontroll får vara UNDER_UPPSTART i sitt fönster (uppstart_max)
                      och provas igen var --intervall sekund; kvarstående fel efter fönstret blir FEL med
                      väntetiden. Resultatet skrivs efter varje runda.
Spärr (en körning i taget, gemensam för alla lägen):
  --las hoppa         (standard för timern) annan körning pågår -> loggas, exit 0
  --las vanta         vänta högst --vanta-sekunder, sedan exit 3 med tydligt meddelande
  --uppstart          väntar alltid på spärren (uppstartsläget har företräde: timern hoppar över)
Resultat (atomiskt: tmp-fil, fsync, os.replace):
  <katalog>/senaste.json och <katalog>/historik/<tid>.json (de BEHALL senaste sparas)
Status: OK, FEL, OKAND, BLOCKERAD (bara när ett objekt i kraver har FEL), UNDER_UPPSTART.
Exit: 0 utan FEL, 1 med minst ett FEL (BLOCKERAD och OKAND är inte FEL), 2 ogiltig spec, 3 spärren upptagen.

Användarenheter (systemd-enhet, timer-senast-lyckad) läses ur en statusfil (--enhetsstatus, standard
<resultat>/enheter.json) som hjälparen enhetsstatus.py skriver utanför sandlådan. Motorn har ingen
förbindelse med ricandes tjänstehanterare: den skulle kunna starta tjänster utanför sandlådan.
Saknas filen eller är den äldre än ENHETSSTATUS_MAX_ALDER blir kontrollen OKAND (UNDER_UPPSTART inom
uppstartsfönstret) – aldrig FEL.

Publik åtkomst (räckvidd publik-atkomst) kan inte mätas från kk-server. Med --extern kör motorn BARA de
kontrollerna, från en extern mätpunkt (GitHub Actions, se verktyg/extern/). På kk-server läses deras
resultat ur extern.json (--extern-resultat, standard <resultat>/extern.json), som hjälparen
extern_hamta.py hämtar. Saknas den eller är mätningen äldre än EXTERN_MAX_ALDER blir de OKAND.

Säkerhet: bara de sex typerna; varje kontroll granskas med regler.py innan den körs; HTTP följer inte
omdirigeringar och läser högst MAX_SVAR byte; namn slås bara upp om de finns i allowlistan; inga
underprocesser. Bara Pythons standardbibliotek.
"""
import argparse
import datetime as dt
import fcntl
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from regler import ENHET, granska_kontroll, tillatna_fran_spec  # noqa: E402

FORMAT = 1
TIDSGRANS = 5            # sekunder per anrop
MAX_SVAR = 64 * 1024     # byte som läses av ett HTTP-svar
BEHALL = 50              # sparade körningar i historik/
ENHETSSTATUS_MAX_ALDER = 300   # sekunder
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
ENHETER = {"fil": None}         # sätts av main(); läses om varje runda
EXTERN = {"lage": False, "fil": None}
EXTERN_MAX_ALDER = 3600         # sekunder: GitHubs schema är ungefärligt (körning var 15:e min, ibland senare)
CA_FIL = os.environ.get("KONTROLL_CA_FIL")                       # bara för tester (egen CA)
STATUSAR = ("OK", "FEL", "OKAND", "BLOCKERAD", "UNDER_UPPSTART")


def nu():
    return dt.datetime.now(dt.timezone.utc).astimezone()


# ---------------------------------------------------------------- kontrolltyper

class IngenOmdirigering(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None          # 30x blir ett HTTPError med koden – följs aldrig


def _oppnare():
    ctx = ssl.create_default_context(cafile=CA_FIL) if CA_FIL else ssl.create_default_context()
    return urllib.request.build_opener(IngenOmdirigering, urllib.request.HTTPSHandler(context=ctx),
                                       urllib.request.ProxyHandler({}))


def k_http(k, tillatna):
    req = urllib.request.Request(k["mal"], method="GET", headers={"User-Agent": "systematlas-kontroll/1"})
    if k.get("vard"):
        req.add_header("Host", k["vard"])
    try:
        with _oppnare().open(req, timeout=TIDSGRANS) as r:
            r.read(MAX_SVAR)
            kod = r.status
    except urllib.error.HTTPError as e:
        kod = e.code
    except (urllib.error.URLError, OSError) as e:
        orsak = getattr(e, "reason", e)
        return "FEL", f"inget svar ({orsak})"
    if kod in k["forvantat"]["status"]:
        return "OK", f"HTTP {kod}"
    return "FEL", f"HTTP {kod}, väntade {k['forvantat']['status']}"


def k_tcp(k, tillatna):
    vard, port = k["mal"].rsplit(":", 1)
    try:
        with socket.create_connection((vard, int(port)), timeout=TIDSGRANS):
            return "OK", "porten är öppen"
    except OSError as e:
        return "FEL", f"ingen anslutning ({e.strerror or e})"


def k_dns(k, tillatna):
    try:
        adresser = sorted({a[4][0] for a in socket.getaddrinfo(k["mal"], None, proto=socket.IPPROTO_TCP)})
    except OSError as e:
        return "FEL", f"uppslaget misslyckades ({e})"
    if k["forvantat"]["adress"] in adresser:
        return "OK", f"{k['mal']} -> {', '.join(adresser)}"
    return "FEL", f"{k['mal']} -> {', '.join(adresser) or 'inget'}, väntade {k['forvantat']['adress']}"


class Vanta(Exception):
    """Mätunderlaget saknas just nu (t.ex. enhetsstatus inte skriven än): OKAND, eller UNDER_UPPSTART
    inom uppstartsfönstret – aldrig FEL."""


def _enhet(enhet):
    if not ENHET.match(enhet):
        raise ValueError("ogiltigt enhetsnamn")
    fil = ENHETER["fil"]
    try:
        data = json.loads(Path(fil).read_text(encoding="utf-8"))
        alder = time.time() - float(data["tid_unix"])
    except Exception:
        raise Vanta("enhetsstatus saknas (hjälparen enhetsstatus.py har inte skrivit filen)")
    if alder > ENHETSSTATUS_MAX_ALDER or alder < -60:
        raise Vanta(f"enhetsstatus är inaktuell ({alder / 60:.0f} min gammal)")
    try:
        nuvarande = BOOT_ID.read_text().strip()
    except OSError:
        nuvarande = None
    if nuvarande and data.get("boot_id") and data["boot_id"] != nuvarande:
        raise Vanta("enhetsstatus är från före omstarten")
    p = (data.get("enheter") or {}).get(enhet)
    if p is None:
        raise Vanta("enheten finns inte i enhetsstatus")
    return p


def k_systemd_enhet(k, tillatna):
    p = _enhet(k["mal"])
    if p.get("LoadState") == "not-found":
        return "FEL", "enheten finns inte"
    if p.get("ActiveState") == "active":
        return "OK", f"aktiv ({p.get('SubState', '')})"
    return "FEL", f"inte aktiv ({p.get('ActiveState', '?')}/{p.get('SubState', '?')})"


def k_timer_senast_lyckad(k, tillatna):
    p = _enhet(k["mal"])
    if p.get("LoadState") == "not-found":
        return "FEL", "enheten finns inte"
    maxi = k["forvantat"]["max_alder_minuter"]
    senaste = p.get("Senaste")
    if senaste:
        # Journalen: tål omstart och att enheten körs just nu (systemctl show nollställs då).
        alder = (time.time() - float(senaste["tid_unix"])) / 60
        if senaste.get("resultat") != "done":
            return "FEL", f"senaste körningen misslyckades ({senaste.get('resultat')}) för {alder:.0f} min sedan"
        if alder > maxi:
            return "FEL", f"senaste lyckade körning för {alder:.0f} min sedan, gräns {maxi} min"
        return "OK", f"lyckades för {alder:.0f} min sedan (journalen)"
    ts = p.get("ExecMainExitTimestamp", "")
    if not ts.startswith("@"):
        if p.get("ActiveState") == "activating":
            raise Vanta("körs just nu och har ingen tidigare körning i journalen")
        return "FEL", "har inte körts (varken enligt systemd eller journalen)"
    alder = (time.time() - int(ts[1:])) / 60
    if p.get("Result") != "success":
        return "FEL", f"senaste körningen misslyckades ({p.get('Result')})"
    if alder > maxi:
        return "FEL", f"senaste lyckade körning för {alder:.0f} min sedan, gräns {maxi} min"
    return "OK", f"lyckades för {alder:.0f} min sedan"


def k_certifikat(k, tillatna):
    ctx = ssl.create_default_context(cafile=CA_FIL) if CA_FIL else ssl.create_default_context()
    vard, _, port = k["mal"].partition(":")
    try:
        with socket.create_connection((vard, int(port or 443)), timeout=TIDSGRANS) as s:
            with ctx.wrap_socket(s, server_hostname=vard) as t:
                cert = t.getpeercert()
    except (OSError, ssl.SSLError) as e:
        return "FEL", f"TLS misslyckades ({e})"
    slut = dt.datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), dt.timezone.utc)
    dagar = (slut - nu()).days
    if dagar >= k["forvantat"]["min_dagar"]:
        return "OK", f"giltigt {dagar} dagar till"
    return "FEL", f"giltigt bara {dagar} dagar till, krav {k['forvantat']['min_dagar']}"


TYPER = {"http": k_http, "tcp": k_tcp, "dns": k_dns, "systemd-enhet": k_systemd_enhet,
         "timer-senast-lyckad": k_timer_senast_lyckad, "certifikat": k_certifikat}


def extern_resultat(k):
    """Resultatet från den externa mätpunkten för en publik kontroll (extern.json).
    Saknas underlaget blir det OKAND direkt (även i uppstartsläget): att mätpunkten inte har svarat
    säger inget om uppstarten, och uppstartsläget ska inte vänta på GitHub."""
    try:
        data = json.loads(Path(EXTERN["fil"]).read_text(encoding="utf-8"))
        assert data.get("format") == FORMAT
        slut = dt.datetime.fromisoformat(data["slut"])
    except Exception:
        return "OKAND", "ingen extern mätning tillgänglig (publik åtkomst kan inte mätas från kk-server)"
    alder = (nu() - slut).total_seconds()
    if alder > EXTERN_MAX_ALDER or alder < -300:
        return "OKAND", f"extern mätning är inaktuell ({alder / 60:.0f} min gammal)"
    post = next((x for x in data.get("kontroller", []) if isinstance(x, dict) and x.get("id") == k["id"]), None)
    if post is None:
        return "OKAND", "kontrollen saknas i den externa mätningen (synka kontrollspec-publik.json till GitHub-repot)"
    status = post.get("status") if post.get("status") in ("OK", "FEL", "OKAND") else "OKAND"
    return status, f"extern mätning {slut.strftime('%H:%M')}: {str(post.get('orsak', ''))[:200]}"


def kor_en(k, tillatna):
    """En kontroll, utan hänsyn till beroenden. Returnerar (status, orsak)."""
    if k.get("rackvidd") == "publik-atkomst" and not EXTERN["lage"]:
        return extern_resultat(k)
    fel = granska_kontroll(k, {}, tillatna, kontrollera_lyssnar=False)
    if fel or k.get("typ") not in TYPER:
        return "OKAND", "avvisad av reglerna: " + "; ".join(fel or ["okänd typ"])
    if k["typ"] in ("http", "certifikat", "dns"):
        vard = urlsplit(k["mal"]).hostname if k["typ"] == "http" else k["mal"].split(":")[0]
        if vard not in tillatna["lokala"] | tillatna["interna_natadresser"] | tillatna["interna_namn"]:
            return "OKAND", "avvisad: värden finns inte i allowlistan"
    try:
        return TYPER[k["typ"]](k, tillatna)
    except Vanta as e:
        return "VANTA", str(e)
    except Exception as e:                      # en trasig kontroll får aldrig stoppa de andra
        return "OKAND", f"kontrollen kunde inte genomföras ({type(e).__name__}: {e})"


# ---------------------------------------------------------------- körningen

def objektstatus(resultat):
    """objekt -> FEL om någon av dess kontroller är FEL (används bara för BLOCKERAD)."""
    ut = {}
    for r in resultat.values():
        if r["status"] == "FEL":
            ut[r["objekt"]] = "FEL"
    return ut


def runda(kontroller, tillatna, resultat, start, uppstart):
    """Kör alla kontroller som inte är avgjorda. Kontroller vars kraver har FEL blir BLOCKERAD;
    alla andra körs, även om relaterade objekt har fel."""
    # Ordning: objekt som andra kräver körs först (djup i kraver-grafen), så att BLOCKERAD bygger på
    # färska resultat. Validatorn har redan stoppat cykler; djupet begränsas ändå för säkerhets skull.
    kraver_av = {}
    for k in kontroller:
        kraver_av.setdefault(k["objekt"], set()).update(k.get("kraver") or [])

    def djup(o, sett=()):
        if o in sett or len(sett) > 50:
            return 0
        return 1 + max((djup(x, sett + (o,)) for x in kraver_av.get(o, ())), default=-1)

    ordning = sorted(kontroller, key=lambda k: (djup(k["objekt"]), k["objekt"], k["id"]))
    for k in ordning:
        tidigare = resultat.get(k["id"])
        if tidigare and tidigare["status"] in ("OK", "FEL", "OKAND", "BLOCKERAD") and uppstart:
            continue                            # avgjord i en tidigare runda
        felaktiga = objektstatus(resultat)
        blockerare = [x for x in k.get("kraver", []) if felaktiga.get(x) == "FEL"]
        t0 = time.monotonic()
        if blockerare:
            status, orsak = "BLOCKERAD", "blockerad av " + ", ".join(f"{x} (FEL)" for x in blockerare)
        else:
            status, orsak = kor_en(k, tillatna)
        vantat = round(time.monotonic() - start, 1)
        if status == "VANTA":
            # Inget mätunderlag: inom uppstartsfönstret provas igen, annars OKAND – aldrig FEL.
            status = "UNDER_UPPSTART" if uppstart and vantat < k.get("uppstart_max", 600) else "OKAND"
        elif uppstart and status == "FEL" and vantat < k.get("uppstart_max", 600):
            status, orsak = "UNDER_UPPSTART", f"{orsak} – provas igen (fönster {k.get('uppstart_max', 600)} s)"
        elif uppstart and status == "FEL":
            orsak = f"{orsak} – kvarstår efter {vantat:.0f} s uppstartsfönster"
        resultat[k["id"]] = {
            "id": k["id"], "objekt": k["objekt"], "typ": k["typ"], "rackvidd": k["rackvidd"], "mal": k["mal"],
            "status": status, "orsak": orsak, "tid": nu().isoformat(timespec="seconds"),
            "varaktighet_ms": round((time.monotonic() - t0) * 1000), "vantat_s": vantat if uppstart else 0,
        }


def rapport(spec, resultat, lage, start_tid, klar):
    rader = sorted(resultat.values(), key=lambda r: (r["objekt"], r["id"]))
    antal = {s: sum(1 for r in rader if r["status"] == s) for s in STATUSAR}
    t = spec["tackning"]
    sammanfattning = (f"{t['med_kontroller']} av {t['inventerade_objekt']} inventerade objekt har kontroller "
                      f"({t['utan_kontroller']} utan, inte verifierade). {len(rader)} kontroller: "
                      + ", ".join(f"{s} {n}" for s, n in antal.items()) + ".")
    return {"format": FORMAT, "atlas_version": spec.get("atlas_version", "okand"), "lage": lage, "klar": klar,
            "start": start_tid, "slut": nu().isoformat(timespec="seconds"), "tackning": t, "antal": antal,
            "sammanfattning": sammanfattning, "kontroller": rader,
            "utan_kontroller": [u["objekt"] for u in spec.get("utan_kontroller", [])]}


def skriv_atomiskt(fil, data):
    fil = Path(fil)
    tmp = fil.with_name(f".{fil.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, fil)
    fd = os.open(fil.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def spara(katalog, data, slutlig):
    skriv_atomiskt(katalog / "senaste.json", data)
    if slutlig:
        hist = katalog / "historik"
        hist.mkdir(exist_ok=True)
        namn = data["start"].replace(":", "").replace("+", "p") + f"-{data['lage']}.json"
        skriv_atomiskt(hist / namn, data)
        for gammal in sorted(hist.glob("*.json"))[:-BEHALL]:
            gammal.unlink()


def ta_sparr(katalog, lage, vanta_sekunder):
    fd = os.open(katalog / ".sparr", os.O_RDWR | os.O_CREAT, 0o644)
    if lage == "uppstart":
        fcntl.flock(fd, fcntl.LOCK_EX)            # uppstartsläget väntar alltid
        return fd
    slut = time.monotonic() + (vanta_sekunder if lage == "vanta" else 0)
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= slut:
                os.close(fd)
                return None
            time.sleep(0.2)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--spec", type=Path, required=True)
    ap.add_argument("--resultat", type=Path, required=True)
    ap.add_argument("--enhetsstatus", type=Path, help="statusfil från enhetsstatus.py (standard: <resultat>/enheter.json)")
    ap.add_argument("--extern", action="store_true", help="extern mätpunkt: kör bara kontroller med räckvidd publik-atkomst")
    ap.add_argument("--extern-resultat", type=Path, help="resultat från den externa mätpunkten (standard: <resultat>/extern.json)")
    ap.add_argument("--uppstart", action="store_true")
    ap.add_argument("--las", choices=("hoppa", "vanta"), default="hoppa")
    ap.add_argument("--vanta-sekunder", type=float, default=60)
    ap.add_argument("--intervall", type=float, default=15)
    a = ap.parse_args()

    a.resultat.mkdir(parents=True, exist_ok=True)
    ENHETER["fil"] = a.enhetsstatus or (a.resultat / "enheter.json")
    EXTERN["lage"] = a.extern
    EXTERN["fil"] = a.extern_resultat or (a.resultat / "extern.json")
    lage = "extern" if a.extern else ("uppstart" if a.uppstart else "normal")
    fd = ta_sparr(a.resultat, "uppstart" if a.uppstart else a.las, a.vanta_sekunder)
    if fd is None:
        if a.las == "hoppa":
            print("En annan kontrollkörning pågår (t.ex. uppstartsläget). Hoppar över den här körningen.")
            return 0
        print(f"STOPP: en annan kontrollkörning pågår fortfarande efter {a.vanta_sekunder:.0f} s. Försök igen senare.")
        return 3
    try:
        try:
            spec = json.loads(a.spec.read_text(encoding="utf-8"))
            assert spec.get("format") == FORMAT and isinstance(spec.get("kontroller"), list)
        except Exception as e:
            print(f"STOPP: ogiltig kontrollspecifikation {a.spec} ({e})")
            return 2
        tillatna = tillatna_fran_spec(spec)
        kontroller = spec["kontroller"]
        if a.extern:
            kontroller = [k for k in kontroller if k.get("rackvidd") == "publik-atkomst"]
        start, start_tid = time.monotonic(), nu().isoformat(timespec="seconds")
        resultat = {}
        while True:
            runda(kontroller, tillatna, resultat, start, a.uppstart)
            vantar = [r for r in resultat.values() if r["status"] == "UNDER_UPPSTART"]
            klar = not (a.uppstart and vantar)
            data = rapport(spec, resultat, lage, start_tid, klar)
            spara(a.resultat, data, klar)
            if klar:
                break
            time.sleep(a.intervall)
        print(data["sammanfattning"])
        for r in data["kontroller"]:
            if r["status"] != "OK":
                print(f"  {r['status']:<10} {r['id']} ({r['rackvidd']}): {r['orsak']}")
        return 1 if data["antal"]["FEL"] else 0
    finally:
        os.close(fd)


if __name__ == "__main__":
    sys.exit(main())
