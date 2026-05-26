from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import anthropic
import os
from datetime import datetime

app = Flask(__name__)
CORS(app)

# Получаем API ключ из переменных окружения
API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

if not API_KEY:
    print("⚠️  ВНИМАНИЕ: ANTHROPIC_API_KEY не установлен!")
    print("Установите его командой: $env:ANTHROPIC_API_KEY='your-api-key'")

client = anthropic.Anthropic(api_key=API_KEY) if API_KEY else None

# История сообщений для контекста
conversation_history = []

@app.route('/')
def index():
    return send_from_directory('.', 'chat_gui.html')

@app.route('/api/chat', methods=['POST'])
def chat():
    try:
        if not client:
            return jsonify({
                'error': 'API ключ не настроен. Установите ANTHROPIC_API_KEY'
            }), 500

        data = request.json
        user_message = data.get('message', '')

        if not user_message:
            return jsonify({'error': 'Сообщение не может быть пустым'}), 400

        # Добавляем сообщение пользователя в историю
        conversation_history.append({
            "role": "user",
            "content": user_message
        })

        # Отправляем запрос к Claude API через OmniRoute
        # Пробуем разные форматы моделей
        try:
            response = client.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=4096,
                messages=conversation_history,
                system="Ты полезный AI-ассистент. Отвечай на русском языке, если пользователь пишет на русском."
            )
        except Exception as e:
            print(f"First attempt failed: {e}")
            # Пробуем альтернативный формат
            response = client.messages.create(
                model="gpt-4",
                max_tokens=4096,
                messages=conversation_history,
                system="Ты полезный AI-ассистент. Отвечай на русском языке, если пользователь пишет на русском."
            )

        # Получаем ответ
        assistant_message = response.content[0].text

        # Добавляем ответ ассистента в историю
        conversation_history.append({
            "role": "assistant",
            "content": assistant_message
        })

        # Ограничиваем историю последними 20 сообщениями
        if len(conversation_history) > 20:
            conversation_history[:] = conversation_history[-20:]

        return jsonify({
            'response': assistant_message,
            'timestamp': datetime.now().isoformat()
        })

    except anthropic.APIError as e:
        print(f"API Error: {str(e)}")
        print(f"Error type: {type(e)}")
        return jsonify({
            'error': f'API Error: {str(e)}'
        }), 500
    except Exception as e:
        print(f"Server Error: {str(e)}")
        print(f"Error type: {type(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'error': f'Server Error: {str(e)}'
        }), 500

@app.route('/api/clear', methods=['POST'])
def clear_history():
    conversation_history.clear()
    return jsonify({'status': 'История очищена'})

@app.route('/api/status', methods=['GET'])
def status():
    return jsonify({
        'api_configured': bool(API_KEY),
        'messages_count': len(conversation_history)
    })

if __name__ == '__main__':
    print("Starting Claude Chat server...")
    print(f"API key: {'Configured' if API_KEY else 'Not configured'}")
    print("Open: http://localhost:5000")
    app.run(debug=True, port=5000)
