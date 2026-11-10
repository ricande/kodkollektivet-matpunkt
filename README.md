# Extern mätpunkt (fas C)

Kontroller med räckvidd `publik-atkomst` kan inte mätas från kk-server, eftersom routern saknar
NAT-loopback. De mäts i stället utifrån, från GitHub Actions.

```
GitHub Actions (var 15:e min)            kk-server
  motor.py --extern                        extern_hamta.py (användartimer, var 5:e min)
  kontrollspec-publik.json   ──resultat──▶   extern.json  ──▶  motor.py (sandlåda)  ──▶ senaste.json
  → grenen "resultat": senaste.json          (https, ingen omdirigering, ≤ 512 kB,       (webbvyn, kk-status)
                                              bara id/status/orsak)
```

## Vad som skickas var

- **Till GitHub (publikt repo):**
  - `motor.py` och `regler.py`, som är samma granskade filer som i drift
  - `kontrollspec-publik.json`, som bara innehåller de publika kontrollerna: publika adresser och
    förväntade svar, men inga interna adresser, enheter eller portar mot 127.0.0.1
  - arbetsflödet `matpunkt.yml`

  Inga hemligheter används. Arbetsflödet har bara repots egen `GITHUB_TOKEN` med `contents: write`
  för grenen `resultat`.
- **Från GitHub:** `senaste.json` från grenen `resultat`. kk-server behandlar filen som **data**. Den kan
  bara visa ett utfall (OK, FEL eller OKAND med orsak) för en kontroll som redan står i den granskade
  `kontrollspec.json`. Den kan aldrig lägga till eller ändra kontroller. Okända id ignoreras.

## Statusar på kk-server

| Läge | Status för de publika kontrollerna |
|---|---|
| `extern.json` saknas eller är ogiltig | OKAND ("ingen extern mätning tillgänglig") |
| mätningen är äldre än 60 min | OKAND ("extern mätning är inaktuell") |
| kontrollen saknas i mätningen | OKAND ("synka kontrollspec-publik.json") |
| annars | mätpunktens OK, FEL eller OKAND med tiden för mätningen |

Uppstartsläget väntar aldrig på den externa mätpunkten. Ett FEL utifrån är ett observerat fel och
kan ge larm, precis som andra FEL.

## Sätta upp repot (ricande, en gång)

Filerna läggs fram av `publicera.sh` i
`/srv/kodkollektivet/systematlas/motor/extern/` på kk-server.

1. På GitHub: **New repository**
   - namn: till exempel `kodkollektivet-matpunkt`
   - synlighet: **Public** (Actions-minuter är gratis för publika repon)
   - kryssa i **Add a README file**
2. Hämta filerna till din dator:
   ```bash
   scp -r kk-server:/srv/kodkollektivet/systematlas/motor/extern ~/matpunkt
   ```
3. I repot: **Add file → Upload files**. Dra in `motor.py`, `regler.py` och
   `kontrollspec-publik.json` från `~/matpunkt`, och klicka på **Commit changes**.
4. **Add file → Create new file**
   - namn: `.github/workflows/matpunkt.yml`
   - innehåll: hela `~/matpunkt/matpunkt.yml`

   Klicka på **Commit changes**.
5. **Settings → Actions → General → Workflow permissions**: välj **Read and write permissions** och
   spara. Arbetsflödet begär bara `contents: write`.
6. **Actions → matpunkt → Run workflow**. Efter cirka en minut finns grenen `resultat` med
   `senaste.json`.
7. Ange adressen på kk-server. Byt `<konto>` och `<repo>` mot dina värden. Adressen är inte hemlig:
   ```bash
   ssh -t kk-server 'echo "https://raw.githubusercontent.com/<konto>/<repo>/resultat/senaste.json" > /srv/kodkollektivet/systematlas/extern-url'
   ```
   Installera sedan hämtningen med `intranet/systematlas/kontroll/installera-extern.sh`.

## När de publika kontrollerna ändras

Ladda upp den nya `kontrollspec-publik.json`, och `motor.py` och `regler.py` om de har ändrats, med
**Add file → Upload files**. Tills dess blir nya publika kontroller OKAND med orsaken
"synka kontrollspec-publik.json". Det är aldrig ett falskt OK.

## Kända begränsningar

- **GitHub stänger av schemalagda arbetsflöden efter 60 dagar utan aktivitet i repot.** Då blir
  mätningen inaktuell, och de publika kontrollerna blir OKAND. kk-status visar det. Åtgärden är
  **Actions → matpunkt → Enable workflow**, eller en commit i repot.
- GitHubs schema är ungefärligt. Körningar kan bli försenade eller utebli under hög last, och därför
  godtas mätningar upp till 60 minuter gamla.
- Mätpunkten står i ett datacenter (Azure). Den visar om tjänsterna nås från internet, inte hur det
  ser ut från ett visst hemnät.
- UDP (Jitsi 10000) och VLAN-isolering mäts inte här.
