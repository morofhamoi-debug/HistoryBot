# История

Компактный проект для сохранения доступной истории Telegram Business‑сообщений и отслеживания удалений.
Backend: FastAPI + aiogram 3 + Supabase. Frontend: HTML/CSS/JS (Telegram Mini App).
Всё развёртывается на Render и настраивается с iPhone через Safari.

## ⚠️ Ограничения Telegram Business API

- Бот сохраняет **только те сообщения**, которые Telegram передал через `business_message`. Сообщения, удалённые до их получения ботом, восстановить **невозможно**.
- **Исчезающие / самоуничтожающиеся медиа** не гарантируют доступ к файлу. В интерфейсе показывается «Файл недоступен».
- `deleted_business_messages` **не содержит точного времени удаления**. В уведомлении различаются «время отправки» и «время обнаружения удаления».
- Бот не может читать чаты, в которых не присутствует, и не обходит ограничения платформы.

## 1. Создание бота в @BotFather

1. Откройте `@BotFather`.
2. `/newbot` → имя `История` → username (например, `istoriya_bot`).
3. Сохраните `BOT_TOKEN` (в формате `1234567890:AAH...`).
4. Позже добавьте Mini App: `/mybots` → выберите бота → **Bot Settings → Menu Button → Configure Menu Button** → URL вашего Render-сервиса.

## 2. GitHub-репозиторий (с iPhone)

1. Откройте [github.com](https://github.com) в Safari, войдите.
2. Создайте репозиторий `istoriya` (Private рекомендуется).
3. Загрузите 10 файлов через **Add file → Upload files** или создайте каждый через **Create new file**:
   - `main.py`
   - `index.html`
   - `app.js`
   - `style.css`
   - `schema.sql`
   - `requirements.txt`
   - `render.yaml`
   - `.env.example`
   - `.gitignore`
   - `README.md`

## 3. Supabase

1. Зарегистрируйтесь на [supabase.com](https://supabase.com), создайте проект.
2. **SQL Editor → New query** → вставьте весь `schema.sql` → **Run**.
3. **Storage → New bucket** → имя `history-media` → **Public bucket: OFF** (обязательно приватный).
4. **Settings → API** скопируйте:
   - `Project URL` → в `SUPABASE_URL`
   - `service_role` key → в `SUPABASE_SERVICE_ROLE_KEY`
   - ⚠️ Не используйте `anon` key.

## 4. Render

1. [render.com](https://render.com) → **New → Web Service** → подключите GitHub-репозиторий.
2. Render автоматически подхватит `render.yaml`. Если нет — задайте вручную:
   - Runtime: **Python**
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
3. **Environment** → добавьте переменные:
   - `BOT_TOKEN` — из @BotFather
   - `ADMIN_ID` — `5208615220`
   - `SUPABASE_URL` — из Supabase
   - `SUPABASE_SERVICE_ROLE_KEY` — из Supabase (service_role)
   - `WEBHOOK_SECRET` — любая случайная строка (например, `istoriya_2024_secret_xyz`)
   - `MINI_APP_URL` — публичный URL Render, например `https://istoriya.onrender.com`
   - `STORAGE_BUCKET` — `history-media`
4. **Create Web Service** → дождитесь статуса **Live**.

## 5. Установка webhook

После деплоя откройте в Safari (подставив свои значения):
