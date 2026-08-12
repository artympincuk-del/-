# Telegram AI Assistant Bot

AI-ассистент для Telegram на aiogram 3, использующий Groq (бесплатный API,
модели Llama 3.1 / 3.3). Есть дневной бесплатный лимит сообщений и докупка
дополнительных сообщений за Telegram Stars.

## Команды

- `/start` — приветствие
- `/model` — выбрать модель: быстрая (Llama 3.1 8B, с бесплатным лимитом)
  или премиум (Llama 3.3 70B, только за докупленные сообщения)
- `/balance` — остаток дневного лимита и докупленных сообщений
- `/buy` — купить пакет сообщений за Telegram Stars
- `/reset` — сбросить историю диалога
- `/help` — подробности

Любое обычное текстовое сообщение (не команда) отправляется модели, ответ
приходит в чат.

## Локальный запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # впиши свои BOT_TOKEN и GROQ_API_KEY
python -m bot.main
```

Ключ Groq берётся на https://console.groq.com/keys (бесплатно, без карты).

## Деплой на Railway

1. Зайди на https://railway.app и залогинься через GitHub.
2. **New Project → Deploy from GitHub repo** → выбери этот репозиторий и ветку.
3. В настройках проекта (Variables) добавь:
   - `BOT_TOKEN` — токен бота от @BotFather
   - `GROQ_API_KEY` — ключ от console.groq.com
4. Railway автоматически соберёт проект (Nixpacks) и запустит команду из
   `railway.json` / `Procfile` (`python -m bot.main`).

Баланс/лимиты игроков хранятся в SQLite-файле рядом с процессом. На
бесплатном плане Railway диск эфемерный и сбрасывается при новом деплое —
для постоянного хранения подключи Railway Volume и укажи путь в переменной
`DB_PATH`.
