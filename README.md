# Claude Chat GUI

Веб-интерфейс для общения с Claude AI через API.

## Установка

1. Установите зависимости:
```powershell
pip install -r requirements.txt
```

2. Установите ваш API ключ Anthropic:
```powershell
$env:ANTHROPIC_API_KEY='your-api-key-here'
```

Чтобы получить API ключ:
- Зайдите на https://console.anthropic.com/
- Создайте новый API ключ в разделе API Keys

## Запуск

1. Запустите сервер:
```powershell
python server.py
```

2. Откройте браузер и перейдите на:
```
http://localhost:5000
```

## Возможности

- ✅ Реальное общение с Claude API
- ✅ Сохранение истории разговора
- ✅ Современный UI с анимациями
- ✅ Индикатор статуса подключения
- ✅ Очистка истории
- ✅ Поддержка многострочных сообщений
- ✅ Автоматическая прокрутка

## Используемые технологии

- **Backend**: Flask (Python)
- **Frontend**: HTML, CSS, JavaScript
- **AI**: Claude API (Anthropic)
- **Model**: Claude Sonnet 4.6

## Устранение неполадок

### Ошибка "API ключ не настроен"
Убедитесь, что вы установили переменную окружения `ANTHROPIC_API_KEY`

### Ошибка "Не удалось подключиться к серверу"
Проверьте, что сервер запущен командой `python server.py`

### Порт 5000 занят
Измените порт в файле `server.py` (последняя строка) и в `chat_gui.html` (переменная `API_URL`)
