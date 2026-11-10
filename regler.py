"""Gemensamma regler för kontroller: används av validera.py (i CI) och av kontrollmotorn (före varje
anrop). Samma kod på båda ställena, så att en kontroll som underkänns i CI aldrig kan köras i drift.

    from regler import las_tillatna, granska_kontroll, kraver_cykler
    fel = granska_kontroll(kontroll, objekt, tillatna)    # lista med felbeskrivningar, tom = godkänd
"""
import re
from pathlib import Path
from urllib.parse import urlsplit

ENHET = re.compile(r"^[a-z0-9][a-z0-9@._-]{0,80}\.(service|timer|socket)$")
STANDARD_UPPSTART = 600   # sekunder: hur länge en kontroll får vara UNDER UPPSTART efter en omstart


def tillatna_fran_spec(spec):
    """Allowlistan i den form granska_kontroll vill ha, ur kontrollspec.json (motorn har ingen PyYAML)."""
    return {k: set(spec.get("tillatna", {}).get(k) or []) for k in ("lokala", "interna_natadresser", "interna_namn")}


def las_tillatna(rot):
    import yaml
    data = yaml.safe_load((Path(rot) / "verktyg/kontroll/tillatna.yaml").read_text(encoding="utf-8"))
    return {k: set(data.get(k) or []) for k in ("lokala", "interna_natadresser", "interna_namn")}


def _lyssnar_pa(objekt, vard, port):
    """Står host:port i objektets lyssnar-lista (fritext, t.ex. '127.0.0.1:3300 (HTTP)')?"""
    monster = re.compile(rf"(^|[\s\[]){re.escape(vard)}:{port}(\b|/)")
    return any(monster.search(str(r)) for r in objekt.get("lyssnar") or [])


def _vard_och_port(mal):
    m = re.fullmatch(r"([0-9.]+|[a-z0-9.-]+):(\d{1,5})", mal)
    if not m:
        return None, None
    return m.group(1), int(m.group(2))


def _rackvidd_for_vard(vard, tillatna):
    if vard in tillatna["lokala"]:
        return "lokal-tjanst"
    if vard in tillatna["interna_natadresser"] or vard in tillatna["interna_namn"]:
        return "intern-atkomst"
    return None


def granska_kontroll(k, objekt, tillatna, kontrollera_lyssnar=True):
    """Semantiska regler utöver schemat. Returnerar en lista med fel (tom = godkänd).
    kontrollera_lyssnar=False används av motorn: lyssnar finns inte i specifikationen och är redan
    kontrollerad i CI. Allt annat (mål, räckvidd, tecken, enhetsnamn) kontrolleras igen."""
    fel = []
    typ, mal, rackvidd = k.get("typ"), str(k.get("mal", "")), k.get("rackvidd")
    # Samma teckenmängd som schemat – inga blanksteg, citattecken eller skaltecken (; | ` $ < > \ ( ) { } *).
    if not mal or re.search(r"[^A-Za-z0-9:/._@?=&%-]", mal):
        fel.append("målet innehåller otillåtna tecken")
    if k.get("vard") and k["vard"] not in tillatna["interna_namn"]:
        fel.append(f"vard {k['vard']!r} finns inte bland interna_namn i tillatna.yaml")

    if rackvidd == "publik-atkomst":
        # Mäts utifrån (extern mätpunkt, verktyg/extern/), inte från kk-server (NAT-loopback saknas).
        # Bara Kodkollektivets publika namn och https.
        if typ == "http":
            u = urlsplit(mal)
            if u.scheme != "https" or u.username or u.password or u.fragment:
                fel.append("publik http-kontroll ska vara https:// utan inloggning eller fragment")
            elif (u.hostname or "") not in tillatna["interna_namn"]:
                fel.append(f"värden {u.hostname!r} finns inte bland namnen i tillatna.yaml")
        elif typ == "tcp":
            vard, port = _vard_och_port(mal)
            if vard is None or vard not in tillatna["interna_namn"]:
                fel.append("publik tcp-kontroll ska vara namn:port med ett namn ur tillatna.yaml")
        elif typ == "certifikat":
            if mal not in tillatna["interna_namn"]:
                fel.append(f"namnet {mal!r} finns inte bland namnen i tillatna.yaml")
        else:
            fel.append("publik-atkomst stöds bara för http, tcp och certifikat")
        if k.get("vard"):
            fel.append("vard får inte anges för publik åtkomst")
        return fel

    if typ == "http":
        u = urlsplit(mal)
        if u.scheme not in ("http", "https") or u.username or u.password or u.fragment:
            fel.append("http-målet ska vara http:// eller https:// utan inloggning eller fragment")
            return fel
        vard = u.hostname or ""
        try:
            port = u.port or (443 if u.scheme == "https" else 80)
        except ValueError:
            return fel + ["ogiltig port i http-målet"]
        vantad = _rackvidd_for_vard(vard, tillatna)
        if vantad is None:
            fel.append(f"värden {vard!r} finns inte i tillatna.yaml")
        elif vantad != rackvidd:
            fel.append(f"räckvidden ska vara {vantad} för {vard}")
        if (kontrollera_lyssnar and vard in tillatna["lokala"] | tillatna["interna_natadresser"]
                and not _lyssnar_pa(objekt, vard, port)):
            fel.append(f"{vard}:{port} står inte i objektets lyssnar")
    elif typ == "tcp":
        vard, port = _vard_och_port(mal)
        if vard is None or not 1 <= port <= 65535:
            return fel + ["tcp-målet ska vara värd:port"]
        vantad = _rackvidd_for_vard(vard, tillatna)
        if vantad is None:
            fel.append(f"värden {vard!r} finns inte i tillatna.yaml")
        elif vantad != rackvidd:
            fel.append(f"räckvidden ska vara {vantad} för {vard}")
        if (kontrollera_lyssnar and vard in tillatna["lokala"] | tillatna["interna_natadresser"]
                and not _lyssnar_pa(objekt, vard, port)):
            fel.append(f"{vard}:{port} står inte i objektets lyssnar")
    elif typ in ("dns", "certifikat"):
        if mal not in tillatna["interna_namn"]:
            fel.append(f"namnet {mal!r} finns inte bland interna_namn i tillatna.yaml")
        if rackvidd != "intern-atkomst":
            fel.append("räckvidden ska vara intern-atkomst (mätt från kk-server via lokal DNS)")
    elif typ in ("systemd-enhet", "timer-senast-lyckad"):
        if not ENHET.match(mal):
            fel.append("målet ska vara ett enhetsnamn (.service, .timer eller .socket)")
        if typ == "timer-senast-lyckad" and not mal.endswith(".service"):
            fel.append("timer-senast-lyckad gäller tjänsten som timern startar (.service)")
        if rackvidd != "lokal-tjanst":
            fel.append("räckvidden ska vara lokal-tjanst för enheter")
    if k.get("vard") and typ != "http":
        fel.append("vard får bara anges för http")
    return fel


def kraver_cykler(kraver_av):
    """kraver_av: objekt-id -> mängd objekt-id som dess kontroller kräver. Returnerar en cykel eller None."""
    farg = {}

    def besok(n, vag):
        farg[n] = 1
        for m in sorted(kraver_av.get(n, ())):
            if farg.get(m) == 1:
                return vag + [n, m]
            if farg.get(m) is None:
                c = besok(m, vag + [n])
                if c:
                    return c
        farg[n] = 2
        return None

    for n in sorted(kraver_av):
        if farg.get(n) is None:
            c = besok(n, [])
            if c:
                return c[c.index(c[-1]):]
    return None
