# NOTES.md - phone-battery-monitor (handoff, aggiornato 2026-11-05)

## Contesto
- Repo: ~/repos/phone-battery-monitor (GitHub tommifi/phone-battery-monitor), Raspberry Pi 5 (Debian, Pi raggiungibile via Tailscale).
- Telefono: ZUK Z2 (ShenQi Z2-PLUS) attaccato alla porta USB del Pi: uhubctl -l 1 -p 1. Seriale USB 4a64b8f0, ADB Wi-Fi 192.168.100.115:5555.
- Il monitor (servizio utente battery-monitor.service) tiene la batteria tra 40 e 80%: spegne la porta all'80%, la riaccende al 40%.
- Il bot GramAddict e la sua sentinella (gramaddict-sentinel) girano sullo stesso telefono e si gestiscono in un'ALTRA chat.
- Il bot sta sempre in funzione: consumo 50-330 mA nelle giornate leggere, 400-700 mA in quelle pesanti.
- Batteria vecchia gonfia, sostituita il 18/10/2026 (capacita misurata circa 3,5 Ah).

## Scadenza
- Verso il 22/11/2026 (da confermare) Tommaso resta 45 giorni senza accesso diretto a PC e cellulare.
- Il Pi resta raggiungibile via Tailscale, ma serve un dispositivo da cui collegarsi: il recupero deve essere AUTOMATICO.
- Obiettivo: robustezza, non diagnosi. Ultimi 2 giorni prima della partenza: nessuna modifica.

## Stato del codice (ultimo commit 2567d2b su origin/main, 79 test pytest verdi, ruff e mypy puliti)
File: battery_monitor.py, test_battery_monitor.py, anomaly_snapshot.py, zuk_current_log.py, test_zuk_current_log.py.
Funzioni aggiunte nella sessione 26/10-05/11:
- _apply_initial_port_state: all'avvio legge il livello (4 tentativi, pausa 10 s) e accende la porta solo se sotto stop_level. Prima accendeva alla cieca.
- _verify_port_off: non riapplica lo spegnimento se il livello sta calando rispetto al riferimento (off_reference_level).
- last_level_log inizializzato a -LEVEL_LOG_INTERVAL_SECONDS, cosi la prima riga Level: esce subito dopo un boot.
- _ensure_usb_adb_only: legge sys.usb.state e, se non e "adb", esegue svc usb setFunction adb false e attende USB_SETTLE_SECONDS (8). Chiamata prima di ogni spegnimento.
- _maintain_usb_adb_only: stesso controllo ogni 600 s nel ciclo principale, rinuncia dopo 3 fallimenti consecutivi. Nota: il testo del WARNING dice "before port off" anche quando scatta dal controllo periodico (solo cosmetico).
- _check_not_charging: allarme Telegram (uno per episodio) se porta accesa, livello sotto stop_level, USB/AC powered true e status 3 o 4 per 80 cicli (circa 24 min). Non fa nessun recupero.
- zuk_current_log.py + timer utente zuk-current-log.timer (ogni 10 min): scrive ~/zuk_current_log.csv con Charge counter, livello, status, tempo CPU di healthd, uptime telefono, corrente media in mA (positivo = scarica). Unit systemd in ~/.config/systemd/user (fuori dal repo).
- Nei test: fixture autouse che disattivano _ensure_usb_adb_only e _check_not_charging. In coda a test_battery_monitor.py ci sono import con alias (_BM, _patch, _pytest) a meta file: da riordinare in testa al file.

Costanti principali: CHARGE_STOP_LEVEL 80, CHARGE_RESUME_LEVEL 40, CRITICAL_LEVEL 15, MAX_READ_FAILURES 3, GRACE_CYCLES 2, REAPPLY_MARGIN 2, REAPPLY_COOLDOWN_SECONDS 600, REAPPLY_MAX_ATTEMPTS 2, check_interval 15 s (cicli reali circa 18 s).

## Log e file utili
- ~/zuk_current_log.csv (orari locali, UTC-5, campo avg_ma con fine riga CRLF: usare gsub per togliere \r).
- ~/zuk_health_log.csv: una riga ogni 30 min dal 2/9 (orari in UTC).
- ~/zuk_anomaly_snapshots.log: blocchi con dumpsys battery, processi, CPU.
- Journal: journalctl --user -u battery-monitor ; eventi USB: journalctl _TRANSPORT=kernel | grep "usb 1-1".

## Fatti accertati
1. Con identita USB 2b4c:101b (funzione mtp,adb) uhubctl -a off NON tiene: il telefono si ri-enumera entro 1 s e riprende corrente. Provato a mano il 2/11.
2. Con 18d1:4ee7 (funzione solo adb) lo spegnimento tiene: il telefono resta enumerato senza corrente. Provato il 28/10 e il 2/11.
3. svc usb setFunction adb false (sintassi singolare, secondo argomento false) porta da 101b a 4ee7 senza riavvio.
4. Dopo ogni reboot del telefono la funzione torna mtp,adb (identita 101b). Ora il monitor la corregge entro 10 minuti (provato una volta, 2/11).
5. Dal 3 al 5/11: 5 cicli completi 80%/40%, nessun re-applying, nessun allarme not-charging. Lo stop a 80% ha tenuto ogni volta.
6. Il recupero automatico di ADB Wi-Fi dopo un reboot (_restore_wifi_adb via USB + adb tcpip) funziona end to end: dopo reboot normale circa 7 minuti con monitor cieco. atx-agent e uiautomator sono ripartiti da soli (verificato una volta).
7. Cavi: i due cavi con dati danno circa 0,45-0,47 A a 4,92-4,97 V (porta dati standard, Max charging current 500000, USB powered true). Il cavo spesso senza dati da 1,64 A a 4,51 V (AC powered true, 1500000), ma il Pi non vede il telefono: niente ADB via USB, niente recupero con tcpip. Ora e collegato un cavo CON dati.
8. Il Charge counter (microAh) dumpsys battery e una misura diretta affidabile della corrente.
9. Il Pi ha un RTC e orologio sincronizzato: gli orari dei log sono affidabili. I riavvii del 28/10 mattina li aveva ordinati Tommaso.
10. healthd (processo di sistema del telefono) consuma CPU in modo crescente con l'uptime del telefono (0,1% dopo reboot, 18% a 42 h, 69% a 91 h) ma era gia costante prima del guasto del 1/11: non dimostrato come causa.

## Problemi aperti
A. Guasto del 1/11 NON spiegato: con porta accesa e cavo connesso, USB powered true ma status 3 (non carica) da circa 12:00 locali a 70% fino a 0% (il telefono e arrivato a 0 e si e spento). Nessun evento USB, nessun uhubctl, nessun Fail-safe. Non riprodotto dopo il cambio cavo. Bilancio energetico gia al limite quella mattina (-60/-70 mA con 0,45 A in ingresso). Nessun recupero automatico ancora implementato, solo l'allarme Telegram.
B. Evento del 5/11 ore 09:53 (locale): "Power loss detected! Battery level: 40%" con porta accesa da 21 minuti (resume alle 09:32), poi 09:55 allarme sentinella "atx-agent not responding (phone unreachable via adb), manual intervention required (automatic reboot disabled)" e 09:56 "USB state is ''". Telefono poi risultato connesso e funzionante. IPOTESI NON VERIFICATA: is_ac_or_usb_powered restituisce False anche quando dumpsys battery fallisce (ADB momentaneamente irraggiungibile), quindi _check_power_loss scambia un guasto ADB per perdita di alimentazione ed esegue stop_heavy_services (force-stop di atx-agent e uiautomator e schermo spento), che puo aver causato o aggravato l'allarme della sentinella. Stesso effetto visto il 2/11 durante lo scambio cavo. Da verificare nel journal (righe "Unable to fetch battery status", "unreadable", Re-enabling ADB) tra 09:45 e 10:05 del 5/11.
C. Bilancio energetico: con circa 0,45 A e giornate pesanti (500-700 mA) il livello puo scendere anche a porta accesa. Opzioni: alzare la finestra (es. 60-90 di giorno e 40-80 di notte), ridurre il carico del bot, oppure cavo solo alimentazione (perde il recupero ADB via USB).
D. restart_heavy_services si limita a loggare: i servizi li rialza la sentinella (altra chat).

## Decisioni prese
- Una variabile alla volta; annotare date dei cambi hardware (cavo cambiato il 2/11 ore 18:53; terzo cavo con dati attualmente in uso).
- Niente reboot periodico a calendario per ora. Idea preferita: reboot del telefono A CONDIZIONE (status 3 persistente con porta alimentata, o healthd alto), tetto di uno ogni 24 h, allarme Telegram.
- Non cambiare le soglie 40/80 prima di aver visto alcuni giorni di dati.
- Se compaiono popup mtp-probe/sr0 sul desktop del Pi: kill -9 di pcmanfm --desktop e rilancio con setsid nohup pcmanfm --desktop.

## Prossimi passi (in ordine)
1. Verificare l'ipotesi B nel journal del 5/11 (09:45-10:05). Se confermata: correggere _check_power_loss/is_ac_or_usb_powered in modo che una lettura ADB fallita NON conti come perdita di alimentazione (distinguere "false" da "unknown"), con test, commit e push. Se non confermata: cercare la vera causa.
2. Recupero automatico del guasto A: ciclo di porta (uhubctl -a cycle -d 5, provato meccanicamente il 2/11, telefono rientrato a 18d1:4ee7 status 2) e poi, se non basta, reboot del telefono, con tetti e pause, allarme Telegram e test.
3. Decidere il bilancio energetico (punto C) guardando alcuni giorni di zuk_current_log.csv.
4. Prova generale prima della partenza: reboot del telefono, reboot del Pi, stop del servizio, perdita momentanea di ADB; controllare che tutto torni da solo (monitor, ADB Wi-Fi, USB adb-only, bot).
5. Riordinare gli import in test_battery_monitor.py (togliere gli alias a meta file).
6. Rivedere gli allarmi Telegram: verificare che TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID siano nell'ambiente del servizio.

## Comando di riepilogo periodico
    journalctl --user -u battery-monitor -S "3 days ago" --no-pager | grep -E "not charging|USB powered but|forcing adb|not applied|re-applying|stopping charge|resuming charge|Fail-safe|Power loss" | cut -c1-130 | tail -25

## Preferenze di lavoro di Tommaso
- Rispondere in italiano; codice, log e docstring in inglese, con logger e docstring per ogni funzione e classe.
- Un comando o gruppo di comandi alla volta, NUMERATO (ripartire da 1 nella nuova chat), mirato a consumare pochi token.
- File Python scritti con heredoc quotato (<< 'PYEOF') e Path.write_text; per modifiche piccole patch mirate con assert text.count(old) == 1. Attenzione: dentro l'heredoc usare "\n" singoli, mai "\\n".
- Controlli prima di ogni commit: uv run ruff format . && uv run ruff check . && uv run mypy battery_monitor.py test_battery_monitor.py && uv run pytest -q. Si usa uv.
- Includere sempre i comandi git (add, commit, push, git log --oneline -1) e, se serve, il riavvio: systemctl --user restart battery-monitor.
- Se una domanda o un'idea e tecnicamente sbagliata, contraddire e argomentare; distinguere sempre fatti da ipotesi.
- Niente domande a scelta multipla o quiz. Preparare un NOTES.md quando la chat e satura.

## Aggiornamento sessione 05/11 sera
- Chiusi: ipotesi B (falso Power loss da lettura ADB fallita: confermata, fix in 5a13cc4, stato None), recupero not-charging (5564c30: ciclo porta con set_charging dopo 80 cicli, adb reboot dopo 120, tetto 24 h in memoria), import test riordinati (c97970d), Telegram verificato (infra.env, sent: True).
- Punto C chiuso: soglie 40/80 invariate. Solo il 31/10 ha avuto bilancio a zero per ~15 h; dal 2/11 margine 150-300 mA.
- Prova 1 (ADB Wi-Fi staccato 60 s) superata dal vivo. Restano: reboot telefono (livello >55%), reboot Pi, simulazione not-charging.
- La sentinella NON si chiama gramaddict-sentinel: e gramaddict_test.py, lanciato ogni 5 min da gramaddict-service-test.timer.
- Sudoers: la regola e in /etc/sudoers riga 60, solo uhubctl on/off (non cycle).
- RISCHIO CHIUSO (si gestisce qui: la sopravvivenza del bot e di questa chat): gramaddict.service ha ExecStartPre=check_phone_clock.py che esce 1 se ADB non risponde per 45 s, Restart=on-failure, RestartSec=10, StartLimitBurst=3/5min. Con ADB giu per >3-4 min durante un riavvio del bot l'unit finisce in failed e non si rialza. Proposte: StartLimitIntervalSec=0 o RestartSec lungo.
- Fix applicato il 05/11: drop-in ~/.config/systemd/user/gramaddict.service.d/survival.conf (StartLimitIntervalSec=0, RestartSec=60), fuori dal repo. Verificato: enabled, Linger=yes, sentinella e bot in *.wants.

## Aggiornamento sessione 07/11 (prove generali)
Prove superate dal vivo:
- Reboot del Pi (15:10): servizi attivi dopo 2 minuti, ADB USB e Wi-Fi subito disponibili, il monitor ha rilevato il livello con 4 tentativi e acceso la porta alle 15:11:03. Il bot e ripartito da solo, atx-agent e uiautomator vivi.
- Reboot del telefono (15:15:02): ADB Wi-Fi tornato alle 15:16:02 (circa 60 s, non 7 minuti), 3 letture "unreadable" senza falso Power loss (fix 5a13cc4 confermato). sys.usb.state corretto da mtp,adb a adb alle 15:21:05 (circa 6 min). Bot rialzato da solo entro le 15:25.
- Spegnimento del Pi di 15 minuti (non pianificato, circa 15:40-15:53): al ritorno ADB e porta ripristinati da soli, livello 52%.
- Simulazione not-charging (dumpsys battery set status 3 alle 15:57:06): alle 16:17:11 log "USB powered but status 3 for 80 cycles", alle 16:17:12 ciclo porta (off 6 s, poi on), messaggio Telegram RICEVUTO. Con 80 cicli sono passati circa 20 min, quindi i cicli reali durano circa 15 s. Stato falso tolto a mano con dumpsys battery reset (status 2, livello 54%).
Note:
- send_telegram_message logga solo gli errori, il successo non viene loggato: l'unica prova di invio e il messaggio ricevuto.
- Non provata dal vivo: seconda fase (adb reboot a 120 cicli), coperta solo dai test pytest.
- Non provato se il ciclo porta basterebbe in un guasto reale come quello del 1/11 (causa ancora ignota).
- Bilancio energetico: livello 52% sia alle 15:56 sia alle 16:23, giornata con bot attivo a margine quasi zero con circa 450 mA in ingresso. Soglie 40/80 invariate.

## Prossimi passi aggiornati
1. Fix _ensure_usb_adb_only (riga 302 circa) e _maintain_usb_adb_only (riga 589 circa): se getprop sys.usb.state e vuoto (lettura fallita) NON lanciare svc usb setFunction, trattarlo come sconosciuto. Con test, commit, push.
2. Messaggio WARNING cosmetico: "before port off" anche dal controllo periodico.
3. Riassunto di 2-3 giorni di zuk_current_log.csv per giudicare il margine energetico, senza cambiare le soglie.
4. Ultima verifica a freddo (stop servizio e riavvio) solo se si cambia codice. Ultimi 2 giorni prima del 22/11: nessuna modifica.

## Aggiornamento sessione 07/11 sera
- Chiusi: fix sys.usb.state vuoto o lettura ADB fallita (328b237): _ensure_usb_adb_only ora restituisce None (sconosciuto), non lancia svc usb setFunction e _maintain_usb_adb_only non lo conta come fallimento. WARNING reso neutro. 88 test pytest verdi, ruff e mypy puliti. Servizio riavviato col codice nuovo alle 16:38, ADB rientrato in circa 16 s da solo.
- Non provato dal vivo: il ramo "USB state unreadable; adb-only check skipped" (coperto solo dai test).
- Aperto: punto C, margine energetico. Guardare zuk_current_log.csv dopo 2-3 giorni (9-10/11) senza cambiare le soglie.
- Calendario: ultimi 2 giorni prima del 22/11 nessuna modifica. Se non emergono altri problemi, dopo il riepilogo energetico si congela il codice.
- 07/11 sera: scrcpy dava "Multiple (2) ADB devices" per il doppio device (USB + Wi-Fi) dopo il cavo con dati. Fix fuori dal repo: /usr/local/share/applications/scrcpy.desktop ora usa scrcpy -s 192.168.100.115:5555 (backup .bak nella stessa cartella; Pi-Apps puo riscriverlo). Da terminale: scrcpy -e. Il messaggio "adb reverse failed" e innocuo (fallback su adb forward). Il device USB non va rimosso: serve al recupero ADB Wi-Fi.
