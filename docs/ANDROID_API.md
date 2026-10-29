# Будущий API Cloud Torrent Client (проект контракта)

HTTP API **пока не реализован**. Независимый Service отделяет UI от планировщика;
Android-клиент может получить отдельный FastAPI слой без дублирования движка.

| Метод | Путь | Назначение |
|---|---|---|
| POST | /v1/torrents | magnet/HTTPS input, Idempotency-Key, ответ 202 с job_id |
| POST | /v1/torrents/metadata | bounded multipart .torrent, ≤8 МиБ |
| GET | /v1/tasks | пагинация только собственных задач |
| GET | /v1/tasks/{id} | статус, total/selected/sent bytes, скорости, reservations |
| GET | /v1/tasks/{id}/files | пагинация дерева, допустимость каждого файла |
| PUT | /v1/tasks/{id}/selection | список indices, version/ETag для конкурентных правок |
| POST | /v1/tasks/{id}/start | проверить выбор и бюджет, запустить |
| POST | /v1/tasks/{id}/pause | остановить admission и текущие операции |
| POST | /v1/tasks/{id}/resume | возобновить; сброс ошибок только явно |
| DELETE | /v1/tasks/{id} | отменить, освободить локальные данные |
| GET | /v1/storage | used/reserved/limit/free, disk_blocked |
| GET | /v1/events | SSE с ограничением частоты и Last-Event-ID |

Пример ответа задачи:

```json
{"id":"aabbccddeeff","state":"running","total_bytes":159987531776,
 "workspace_limit_bytes":10737418240,"selected_files":45,
 "sent_bytes":4294967296,"download_rate":1048576,"upload_rate":2097152,
 "eta_seconds":null,"version":3}
```

До открытия сети: HTTPS, короткие access tokens, refresh rotation, привязка owner_id
в каждой строке/запросе и изоляция каталогов, rate limits, аудит без секретов,
optimistic locking, схемы входа/выхода. Telegram ID не является паролем API.
Android OAuth должен использовать системный браузер и PKCE; нельзя встраивать
серверный Fernet key или bot token в APK. Google-подключения привязываются к tenant,
а не к глобальной текущей однопользовательской vault.
