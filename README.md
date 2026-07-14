# kick-clip-hunter

Bot, který sleduje watchlist streamerů na [Kick](https://kick.com) a podle aktivity v chatu
detekuje potenciálně virální/vtipné momenty. Zachycený moment se nejdřív ukládá jako
timestamp + odkaz na VOD, s výhledem na automatické stříhání klipů v pozdější fázi.

## Stav projektu

Rané plánování / M0. Zatím žádný kód, jen založený repozitář.

## Architektura (plán)

```
Watchlist (streamers)
    -> Kick OAuth app + webhook subscription (chat.message.sent, livestream metadata)
    -> Webhook receiver (FastAPI)
    -> Detekční engine (rolling window: zpráv/s, frekvence emotů/klíčových slov)
    -> DB (SQLite): uložené momenty (kanál, čas, VOD odkaz, chat snippet, skóre)
    -> Dashboard/CLI pro správu watchlistu a přehled momentů
```

Fáze 2 (později, volitelně): worker s rolling bufferem streamu (ffmpeg + neoficiální
`kick.com/api/v2/channels/{slug}` playback URL), který z detekovaného momentu vystřihne
skutečný video klip.

## Stack

- Python
- FastAPI (webhook receiver, později dashboard)
- SQLite

## Roadmap

- [ ] M0 – registrace Kick dev app, OAuth flow, příjem prvního webhooku
- [ ] M1 – watchlist streamerů + ukládání příchozích chat zpráv do DB
- [ ] M2 – detekční heuristika (spike zpráv/s) + ukládání momentů s timestampem
- [ ] M3 – rozšíření o emote/keyword detekci, ladění prahů
- [ ] M4 – jednoduchý dashboard pro přehled zachycených momentů
- [ ] M5 – (volitelné) automatické video klipy přes m3u8 capture
