# vitadock-garmin-sync

Synchroniseert metingen uit de Medisana **VitaDock+** cloud naar **Garmin Connect**:

| VitaDock module | Garmin Connect |
|---|---|
| `targetscales` (weegschaal) | Gewicht + lichaamssamenstelling (vet %, water %, spiermassa, botmassa, BMR, BMI) |
| `cardiodocks` (bloeddrukmeter) | Bloeddruk + hartslag |

Draait op elke Linux-machine met Python ≥ 3.10, als systemd-timer of als daemon.

## Hoe het werkt (reverse engineering)

De VitaDock+ app (v2.9.42, `de.medisana.vitadockplus`) is een Unity/IL2CPP-app. Uit
`global-metadata.dat` + `libil2cpp.so` zijn de klassen `API`, `DataConnection` en
`Trinity.Helper.AuthorizationBuilder` gehaald. De app gebruikt de VitaDock Online API:

1. `POST https://cloud.vitadock.com/auth/unauthorizedaccesses`: OAuth 1.0a request-token,
   ondertekend met HMAC-SHA256 en de consumer key/secret die in de app zitten.
2. `GET /portalv2/desiredaccessrights/request?oauth_token=…`: het web-portaal
   (Spring Security formulier `j_username`/`j_password`). Na inloggen redirect het naar
   `vitadock2://callback?…&oauth_verifier=…`. Deze tool doorloopt dat formulier zonder browser.
3. `POST /auth/accesses/verify`: access-token + secret. Deze verlopen niet (tot logout),
   en worden opgeslagen in `state.json`.
4. `GET /data/<module>?start=&max=&date_since=`: JSON-lijst met metingen, gepagineerd per 100.

Garmin-uploads gaan via [`garminconnect`](https://github.com/cyberjunky/python-garminconnect):
gewicht als FIT-bestand (`weight_scale` bericht), bloeddruk via de bloeddruk-API.

## Installatie (Linux)

Eén regel op de Linux-host (systemd, Debian/Ubuntu/Fedora/Arch/Alpine/openSUSE):

```bash
curl -fsSL https://raw.githubusercontent.com/DGTLMagician/vitadockGarminSync/main/install.sh | sudo bash
```

Het script:

- maakt systeemgebruiker `vitadock-sync` aan en installeert in `/opt/vitadock-garmin-sync`
  (Python ≥ 3.12; op oudere distro's wordt Python 3.12 via `uv` binnen die map geplaatst);
- vraagt de VitaDock consumer key/secret (zie hieronder) en zet die in
  `/etc/vitadock-garmin-sync.env` (root, `600`);
- laat je eenmalig inloggen bij VitaDock en Garmin (incl. MFA). Wachtwoorden worden niet
  opgeslagen, alleen tokens in `/var/lib/vitadock-garmin-sync` (`700`);
- activeert `vitadock-garmin-sync.timer` (elke 30 minuten, met hardening in de service-unit).

Opnieuw draaien = updaten; configuratie en tokens blijven behouden. Handig daarna:

```bash
sudo vitadock-garmin-sync sync --dry-run
journalctl -u vitadock-garmin-sync -f
```

Liever een langlopend proces (bijv. in Docker)? Gebruik `vitadock-garmin-sync run --interval 30`.

### VitaDock consumer key/secret

De OAuth consumer key en secret zitten in de VitaDock+ app, maar staan bewust niet in
deze repository. Haal ze zelf uit de app:

```bash
adb shell pm path de.medisana.vitadockplus        # pad naar base.apk
adb pull <pad>/base.apk && unzip -o base.apk assets/bin/Data/Managed/Metadata/global-metadata.dat
grep -aoE "[A-Za-z0-9]{64}" assets/bin/Data/Managed/Metadata/global-metadata.dat | sort -u
```

Eén van de 64-tekenstrings is de key en één de secret. Het juiste paar geeft HTTP 200 op
`POST https://cloud.vitadock.com/auth/unauthorizedaccesses`.

## Configuratie

Zie [deploy/vitadock-garmin-sync.env.example](deploy/vitadock-garmin-sync.env.example). Belangrijkste opties:

- `VDGS_SYNC_SINCE`: sla bij de eerste sync metingen vóór deze datum over (handig als Garmin
  de historie al heeft, bijvoorbeeld via Health Connect/Health Sync).
- `VDGS_TIMESTAMPS`: `utc` (standaard) of `wallclock`. Als de tijden in Garmin precies een of twee
  uur verschoven zijn, zet dit op `wallclock`.
- `VITADOCK_EMAIL`/`VITADOCK_PASSWORD`: optioneel; alleen nodig als de service zelf opnieuw moet
  kunnen inloggen wanneer het opgeslagen token wordt ingetrokken.

De status (tokens, al gesynchroniseerde meting-id's) staat in `$STATE_DIRECTORY/state.json`
en `$STATE_DIRECTORY/garmin/`. Metingen worden nooit dubbel geüpload; elke run kijkt 2 dagen
terug om laat binnengekomen metingen alsnog mee te nemen.

## Ontwikkelen

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/pytest
```
