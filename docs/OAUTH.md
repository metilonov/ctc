# Google Drive OAuth 2.0

OAuth не настроен за владельца: нужны его Google Cloud проект и согласие аккаунта.
Используется scope `drive.file`: бот видит файлы/папки, созданные этим приложением.
API key и service account не заменяют здесь пользовательский OAuth.

1. В Google Cloud Console создать проект, включить Google Drive API.
2. Настроить Google Auth Platform / consent screen. Для внешнего приложения
   в режиме Testing добавить свой Google-аккаунт как test user.
3. Создать OAuth client типа **Desktop app**, скачать JSON в `client_secret.json`.
4. На личном компьютере установить зависимости проекта. Создать `.env` с
   TOKEN_ENCRYPTION_KEY (Fernet) и DATA_DIR. Это должен быть тот же ключ, который
   затем будет установлен в секретах хостинга.
5. Выполнить:

   ```sh
   python -m drive.auth --client-secret client_secret.json
   ```

6. Откроется системный браузер. Выбрать свой аккаунт и разрешить доступ. Callback
   принимается только на 127.0.0.1 со случайным локальным портом; state/PKCE
   обрабатывает google-auth-oauthlib. Не копировать коды авторизации в чат.
7. В DATA_DIR появится `google-token.enc`. Это зашифрованные access/refresh credentials.
   Перенести файл на приватный постоянный том хостинга через доступный защищённый
   механизм загрузки. Не включать его в Docker-образ, Git или архив исходников.
8. Ключ хранить отдельно в secrets/env. На Linux файл создаётся с mode 0600;
   на Windows дополнительно защитить каталог ACL своей учётной записи.

При обновлении consent/config или отзыве доступа повторить OAuth для **того же**
аккаунта. Остановить бот, атомарно заменить токен, запустить, нажать «Продолжить»
у задач с error. Refresh выполняется автоматически в отдельном потоке.
В режиме Testing refresh token внешнего приложения с Drive scope может истекать
через 7 дней: для длительного размещения изучите правила публикации Google.

Папки: `<DRIVE_FOLDER_NAME>/<job ID>/<путь из торрента>`. ID задачи предотвращает
смешивание двух торрентов с одинаковым именем. Никаких permissions.create/публичных
ссылок бот не вызывает. Drive должен иметь достаточно места для полного результата.
Не заменяйте аккаунт под работающей очередью: создайте для другого аккаунта новый DATA_DIR.

Протокол: 8 МиБ chunks (кратно 256 КиБ), сохранение session URI, status PUT после
перезапуска, фактический offset из Range; стабильный ID до create; GET size+MD5
после окончания. Session URI является секретом и хранится в приватной SQLite.
Истёкшие сессии пересоздаются с тем же file ID. Ошибки HTTP приводят к конечным
повторам с сохранением локальных данных, а не удалению файлов.

Источники: [OAuth Desktop](https://developers.google.com/identity/protocols/oauth2/native-app),
[Drive resumable uploads](https://developers.google.com/workspace/drive/api/guides/manage-uploads),
[Drive generateIds](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/generateIds).
