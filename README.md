# LiveBot
Удобный и надёжный Telegram-бот, который позволяет общаться с людьми, даже если прямое сообщение затруднено из-за спамблока, приватности или других причин. Работает как безопасный мост: ты пишешь боту, он передает сообщение адресату, и наоборот. Идеально для анонимных обращений или управляемой коммуникации. Включает личный кабинет для пользователей (отправка сообщений, история, профиль) и мощную админ-панель (просмотр всех диалогов, быстрые ответы, блокировки, управление темами и рассылки). Простая настройка: просто укажи токен и ID админа в config.json.

#Russian
Запустить бота очень просто:
1. Создайте файл config.json и разместите его в той же папке, что и скрипт бота, со следующим содержимым:
    {
        "BOT_TOKEN": "ВАШ_ТОКЕН_БОТА",
        "ADMIN_ID": ВАШ_ID_В_TELEGRAM
    }
2.  Установите зависимости, откройте командную строку (терминал) и выполните команду:
    pip install python-telegram-bot
3.  Запуск бота: Ещё одна простая команда, и ваш бот готов к работе:
    python feedback_bot.py
​
Бот автоматически позаботится о создании базы данных `feedback.db` при первом запуске и зарегистрирует администратора, указанного в вашем конфигурационном файле.

#English
Telegram Intermediary Communication Bot

A convenient and reliable Telegram bot designed for seamless communication, even when direct messaging is hindered by spam blocks, privacy settings, or other issues. It acts as a secure bridge: you send a message to the bot, it delivers it to the recipient, and vice-versa. Ideal for anonymous inquiries or structured, controlled communication. Features include a user-friendly interface (for sending messages, viewing history, and managing profiles) and a robust admin panel (for overseeing all dialogues, quick replies, user blocking, topic management, and mass broadcasts). Simple to set up: just configure your bot token and admin ID in config.json.

Running a bot is very simple: 1. Create a config.json file and place it in the same folder as the bot script with the following contents: { "BOT_TOKEN": "YOUR_BOT_TOKEN", "ADMIN_ID": YOUR_ID_B_TELEGRAM } 2. Install the dependencies, open the command prompt (terminal) and run the command: pip install python-telegram-bot 3. Launch the bot: One more simple command and your bot is ready to go: python feedback_bot.py ​ The bot will automatically take care of creating the 'feedback.db' database at the first run and register the administrator specified in your configuration file.
