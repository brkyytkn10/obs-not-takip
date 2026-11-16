# obs-not-takip

YTÜ OBS'de yeni girilen notları Telegram'a bildiren bot (GitHub Actions, saatte bir).

Secrets: `OBS_USER`, `OBS_PASS`, `TG_TOKEN`, `TG_CHAT_ID`.
Modlar: `discover` (site yapısını kaydeder), `run` (notları kontrol eder). Notlar yalnızca actions/cache'te tutulur.
