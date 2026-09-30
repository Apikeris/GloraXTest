# GloraX

Приложение для проверки знаний о проектах GloraX. Flask, PostgreSQL, административная панель и фоновый сбор данных.

## Запуск

Установите зависимости и заполните `.env` по примеру `.env.example`. Для `SECRET_KEY` задайте случайную строку не короче 32 символов.

```bash
pip install -r requirements.txt
docker compose up -d db
python scripts/migrate.py
flask --app app create-admin --username admin
flask --app app enqueue-refresh
```

Запустите приложение и обработчик заданий в разных терминалах:

```bash
flask --app app run --port 5001
python -m glorax.worker
```

Приложение: http://127.0.0.1:5001. Вход администратора: `/admin/login`.

На Render используется команда `python scripts/start_render.py`. Она запускает сервер, миграции и обработчик заданий.
