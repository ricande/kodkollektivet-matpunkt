# Kodkollektivet – Extern mätpunkt

En fristående mätpunkt för att kontrollera tillgängligheten hos Kodkollektivets publika webbtjänster.

Projektet använder GitHub Actions för att regelbundet genomföra kontroller från en extern nätverksmiljö.

## Syfte

Mätpunkten verifierar att utvalda publika tjänster går att nå via internet.

Genom att utföra kontrollerna utanför Kodkollektivets egen nätverksmiljö kan vi upptäcka tillgänglighetsproblem som inte nödvändigtvis framgår av lokala hälsokontroller.

## Så fungerar det

1. GitHub Actions startar en kontrollkörning enligt ett schema.
2. Kontrollmotorn läser `kontrollspec-publik.json`.
3. Endast uttryckligen definierade publika mål kontrolleras.
4. Resultaten sammanställs som JSON.
5. Den senaste mätningen publiceras på grenen `resultat`.

Kontrollerna är skrivskyddade och ändrar inte de tjänster som undersöks.

## Projektets filer

| Fil | Beskrivning |
|---|---|
| `motor.py` | Kontrollmotor för tillgänglighetsmätningar |
| `regler.py` | Validering av tillåtna kontrolltyper och mål |
| `kontrollspec-publik.json` | Definitioner av de publika kontrollerna |
| `.github/workflows/matpunkt.yml` | Schemaläggning och körning via GitHub Actions |

## Körning

Mätpunkten körs normalt var 15:e minut.

En manuell körning kan startas via:

**GitHub → Actions → matpunkt → Run workflow**

Kontrollresultaten publiceras i:

`resultat/senaste.json`

Resultatfilen innehåller bland annat kontrollernas identifierare, status, tidsstämpel och en kort beskrivning av utfallet.

## Kontrollstatus

| Status | Betydelse |
|---|---|
| `OK` | Kontrollen uppfyllde det förväntade resultatet |
| `FEL` | Kontrollen genomfördes men uppfyllde inte förväntningarna |
| `OKAND` | Kontrollen kunde inte genomföras eller avgöras |

Varje resultat gäller endast den funktion som den aktuella kontrollen faktiskt mäter.

## Begränsningar

- GitHub Actions erbjuder inte garanterad körning vid en exakt tidpunkt.
- Schemalagda arbetsflöden kan inaktiveras efter längre tids inaktivitet.
- En lyckad HTTP-kontroll bevisar inte att samtliga funktioner i en tjänst fungerar.
- Resultaten representerar tillgänglighet från GitHubs körmiljö och behöver inte motsvara upplevelsen från alla nätverk.
- Mätpunkten är ett komplement till annan övervakning och ersätter inte fullständiga funktions- eller säkerhetstester.

## Säkerhet

Mätpunkten arbetar enbart mot uttryckligen tillåtna kontrollmål.

Ingen administrativ åtkomst till de kontrollerade tjänsterna krävs.

Publika kontrollspecifikationer ska inte innehålla autentiseringsuppgifter, privata nycklar eller information om interna nätverksmiljöer.

## Om Kodkollektivet

Kodkollektivet är ett svenskt initiativ för gemensam mjukvaruutveckling, kunskapsdelning och öppen källkod.

Mer information finns på:

**https://kodkollektivet.se**
