# Autonomie eerst: van opdracht tot geverifieerde oplevering

Status: gepland, uitvoering niet gestart. Canonieke planning en afhankelijkheden:
[`autonomy-first.json`](autonomy-first.json). Datum: 2026-09-19.

De gebruiker geeft betrouwbare seriële voortgang voorrang boven parallelisme.
De opdracht voor deze intake is uitsluitend: zet de besproken richting om in taken.
De runtime, productcode, live projecten en publicatie blijven buiten deze stap.

## Gewenste keten

Oorspronkelijke opdracht → verkenning → productdoel en relevante architectuur →
uitvoerbare taken → bouwen/controleren/herstellen → afzonderlijk leveren →
doelaudit en volgende geschikte taak binnen hetzelfde mandaat.

Visie bepaalt richting en relevantie, maar geeft geen onbeperkte toestemming om
features te verzinnen. Architectuur bindt alleen de relevante grenzen, besluiten
en kwaliteitskenmerken. De taak beschrijft waarneembaar gedrag en passend bewijs.
Een leeg backlog, groene test of onderzoeksdocument bewijst niet dat het
oorspronkelijke productdoel is bereikt.

## Werkvolgorde

| Taak | Resultaat | Wacht op |
| --- | --- | --- |
| autonomy-01 | Begrensd runmandaat, traceerbare uitkomsten en governing architectuurcontract | Geen voorganger |
| autonomy-02 | Onderzoek van ruwe opdracht naar hergebruikte of nieuwe uitvoerbare taken | 01 |
| autonomy-03 | Duurzame seriële projectrunner die volgende taken zelf oppakt | 01 |
| autonomy-04 | Herstel, procesbewaking, cumulatief budget en begrensde reparatie | 03 |
| autonomy-05 | Inhoudelijke productreview en bewijs per oorspronkelijke uitkomst | 01, 02 |
| autonomy-06 | Echte doelaudit en controleerbare ochtendrapportage | 03, 05 |
| autonomy-07 | Nette Git-state en volledige afzonderlijke publicatie per taak | 04, 06 |
| autonomy-08 | Herhaalbare volledige keten met geïnjecteerde fouten | 02, 04, 06, 07 |
| autonomy-09 | Werkelijke onbegeleide nachtrun met modelworkers en live bewijs | 08 |
| go-project-template:autonomy-template-01 | Draagbare onboarding met bewezen stackversie | stack 09 |

Volgnummers geven de aanbevolen seriële uitvoering aan; native dependency-edges
leggen de werkelijke prerequisites vast. Alle edges eisen geverifieerd
releasebewijs. autonomy-01 kan als eerste worden opgepakt na uitvoeringsautorisatie.
De overige stacktaken verwijzen naar de nog draft architectuurbrief
`autonomous-campaign`; autonomie-01 legt de governing besluiten vast voordat
die taken implementeerbaar worden. Dit is geen fictieve architectuurgoedkeuring.

De huidige classificatie markeert autonomy-04 en autonomy-08 als foundational.
De bestaande named-human-evidence gate blijft gelden waar de effectieve
classificatie dit vereist. De intake is geen menselijke uitvoering/architectuurapproval;
bij latere uitvoering moet toepasselijke sessieautoriteit en werkelijk scope-impact
worden beoordeeld. Er is nu geen extra toestemming nodig om deze planning vast te leggen.

## Bestaande garanties behouden

Herbruik de completed ABC-lifecycle, outcome-tracking, adviespromotie, semantische
taakdecompositie, architecture lane en v0.3.33 task-design-readiness. Deze taken
gaan over ontbrekende verbindingen en de volledige keten; ze heropenen historische
releases niet en bouwen geen tweede test-, besluit- of taakadministratie.

Eén builder en één taakwerkplek tegelijk. Verse workers krijgen actuele canonieke
context en gerichte feedback; chatgeschiedenis is geen vereiste. Fouten worden
gerepareerd binnen mandaat en budget. Echte productbesluiten blijven zichtbaar.
Een taakspecifieke blokkade mag onafhankelijk werk niet stoppen; een onbekend
publicatie-effect of onveilige repository-state kan de hele schrijfketen stoppen.

Alle producttaken behouden een eigen releaseplicht. Het geregistreerde Astra High
profiel hergebruikt de eerdere gebruikerskeuze voor dit brononderhoud; de catalogus
moet vóór uitvoering beschikbaarheid bewijzen. Dit introduceert geen automatische
modelwissel of standaardmodel in nieuwe templateprojecten.

Scopebestanden voor productrelease/pin zijn per taak opgenomen. Canonieke `.go`
mutaties horen bij de controller; een scope-entry geeft de worker geen toestemming
om gekopieerde control-state te wijzigen. Werkplekken, eigenaarschap en bewijs
blijven aan de bestaande runtimecontroles onderworpen.

## Bewijs en grenzen

Task JSON bevat acceptatie, scope, concrete beoogde testcommando's en afhankelijkheden.
Nieuwe testnamen zijn uit te voeren deliverables van die taken, geen nu behaalde checks.
Lokale validators bewijzen alleen dat de planning coherent is.

autonomy-08 gebruikt deterministische adapters en echte processen/Git-fouten.
autonomy-09 moet afzonderlijk werkelijk modelgebruik, meerdere opleveringen,
herstel, gemeten onbegeleide looptijd en gebruikersinterventies aantonen. De
achtuursambitie is geen verplicht nutteloos doorwerken nadat het doel klaar is.
Een handmatig door de agent gestuurde reeks is geen bewijs van automatische
projectvoortzetting.

Voor de liveproef staan testproject, representatief werk, budget, profielen en
publicatie/deploymentdoel nog open. Hergebruik bestaande bevoegdheid waar die
concreet bestaat; veronderstel geen toestemming om Hearthhold of saves te wijzigen.
Geen externe accounts, productieomgeving of gebruiksbudget wordt nu ingesteld.

De template blijft een starter. Haar eigen onderhoudstaak is lokaal en mag niet
als uitvoerbare onderhoudsbacklog in een gepubliceerde starter terechtkomen.
Pinupdate v0.3.32 → v0.3.33 tijdens intake is uitsluitend de verplichte freshness
preflight; het bewijst geen implementatie van deze autonomieplannen.

Het eerdere [parallelontwerp](project-orchestration.md) blijft bewaard als
geparkeerde uitbreiding. Geen parallelle builders, inter-worker berichtenbus,
multi-host scheduling, nieuwe sessieadapter, GitHub Actions of globale skillwijziging
maken deel uit van deze campagne.
