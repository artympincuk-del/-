# Telegram Roulette Bot

Казино-рулетка для Telegram на aiogram 3. Игроки получают стартовый баланс
фишек, делают ставки (цвет, чёт/нечет, диапазон, дюжина, число) и крутят
европейскую рулетку (0-36).

## Команды

- `/start` — регистрация и стартовый баланс
- `/play <ставка>` — сделать ставку (или `/play` без числа, бот спросит)
- `/balance` — текущий баланс
- `/top` — таблица лидеров
- `/help` — правила и выплаты
- `/cancel` — отменить текущую ставку

## Локальный запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # впиши свой BOT_TOKEN
python -m bot.main
```

## Деплой на Railway

1. Зайди на https://railway.app и залогинься через GitHub.
2. **New Project → Deploy from GitHub repo** → выбери этот репозиторий.
3. В настройках проекта (Variables) добавь переменную окружения:
   - `BOT_TOKEN` — токен бота от @BotFather
4. Railway автоматически соберёт проект (Nixpacks) и запустит команду из
   `railway.json` / `Procfile` (`python -m bot.main`).
5. После деплоя бот запускается через long polling — отдельный домен/порт
   не нужен.

Баланс игроков хранится в SQLite-файле рядом с процессом. На бесплатном
плане Railway диск эфемерный и сбрасывается при новом деплое — для
постоянного хранения подключи Railway Volume и укажи путь в переменной
`DB_PATH`.
