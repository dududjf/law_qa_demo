import os
import json
import time
import requests
import urllib3
from flask import Flask, request, jsonify, render_template, send_file, session
from gtts import gTTS
import tempfile
from pydub import AudioSegment
import io
import random, threading
from functools import wraps

from flask_sqlalchemy import SQLAlchemy
import datetime
from datetime import timedelta

urllib3.disable_warnings()
app = Flask(__name__)
app.secret_key = 'abcd'
# 配置参数
VOICE_SERVER_URL = "http://106.13.229.118:9061/v1/audio/transcriptions"
QA_URL = 'https://192.168.252.132:38443/apiaccess/modelmate/north/machine/v1/conversations/completions'
DOC_URL = 'https://192.168.252.132:38443/apiaccess/modelmate/north/machine/v1/documents/download'
API_KEY = '2006|sk-AMCBBnuSqDBi4iXj09eKXOGKIxSXukgd'
USER_ID = '1753323987000104291'
# ASSISTANT_ID = '68cd08aad147429cb1aab72126425ae7' 之前的
ASSISTANT_ID = '68cd08aad147429cb1aab72126425ae7'
CONVERSATION_ID = '3f47f47085f747cc916f38329f82e54b'

GT_CID_URL = 'https://192.168.252.132:38443/apiaccess/modelmate/north/machine/v1/conversations/add'

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///chatbot.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# 确保目录存在
os.makedirs('docs', exist_ok=True)
os.makedirs('audio_responses', exist_ok=True)

conversation_counter = 0
conversation_lock = threading.Lock()

app.permanent_session_lifetime = timedelta(hours=24)  # 设置为24小时


# 对话历史模型
class ConversationHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), nullable=False)
    conversation_id = db.Column(db.String(100), nullable=False)
    title = db.Column(db.String(255))  # 对话标题
    json_data = db.Column(db.Text)  # 存储整个会话 JSON（messages 列表）
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)


# 创建数据库表 - 关键修复部分
with app.app_context():
    db.create_all()  # 确保所有模型对应的表都被创建

VALID_ACCOUNTS = {
    'user1': '123456',
    'user2': '123456',
    'user3': '123456'
}

from flask import request, jsonify
import jwt  # 需要安装 PyJWT 库
from datetime import datetime, timedelta


# 生成 token（登录成功时）
@app.route('/login', methods=['POST'])
def login():
    username = request.json.get('username')
    password = request.json.get('password')
    if username in VALID_ACCOUNTS and VALID_ACCOUNTS[username] == password:
        # 生成 24 小时有效的 token
        token = jwt.encode(
            {
                'username': username,
                'exp': datetime.utcnow() + timedelta(hours=24)  # 过期时间
            },
            app.config['SECRET_KEY'],  # 密钥，需保密
            algorithm='HS256'
        )
        return jsonify({'success': True, 'token': token})
    return jsonify({'success': False, 'error': '用户名或密码错误'})


# 验证 token 的装饰器（保护需要登录的接口）


def token_required(f):
    @wraps(f)  # 关键：保留原函数的元数据（如函数名）
    def wrapper(*args, **kwargs):
        token = None
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]

        if not token:
            return jsonify({'error': '未提供 token'}), 401

        try:
            payload = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
            current_user = payload['username']
        except jwt.ExpiredSignatureError:
            return jsonify({'error': 'token 已过期，请重新登录'}), 401
        except jwt.InvalidTokenError:
            return jsonify({'error': '无效的 token'}), 401

        return f(current_user, *args, **kwargs)

    return wrapper


@app.route('/')
def index():
    return render_template('index.html')


# 新增获取匿名用户历史接口（用于调试，实际可去掉）
@app.route('/get_anonymous_history', methods=['GET'])
def get_anonymous_history():
    return jsonify({"message": "匿名历史存储在客户端"})


# 修改ask接口，不需要登录验证
@app.route('/ask', methods=['POST'])
def ask_question():
    global CONVERSATION_ID, conversation_counter
    data = request.json
    if not data or 'question' not in data:
        return jsonify({'error': '未提供问题'}), 400

    question = data['question']
    with conversation_lock:
        conversation_counter += 1
        if conversation_counter >= 1:  # 达到10次，刷新 conversationId
            new_id = refresh_conversationid()
            if new_id:
                CONVERSATION_ID = new_id
            conversation_counter = 0  # 重置计数器

    print(conversation_counter)
    qa_headers = {
        'Authorization': f'Bearer {API_KEY}',
    }

    qa_data = {
        'userId': USER_ID,
        'assistantId': ASSISTANT_ID,
        'conversationId': CONVERSATION_ID,
        'question': question,
    }

    try:
        # 发起流式请求
        response = requests.post(
            QA_URL,
            headers=qa_headers,
            json=qa_data,
            stream=True,
            verify=False
        )
        response.raise_for_status()

        # 直接转发流式数据（不做额外处理）
        def generate():
            for line in response.iter_lines():
                if line:  # 过滤空行
                    # 保留原始格式（data: ...），直接转发给前端
                    yield line.decode('utf-8') + '\n'

        # 返回流式响应
        return app.response_class(
            generate(),
            mimetype='text/event-stream'
        )

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# 新增合并历史记录接口（登录时使用）
@app.route('/merge_anonymous_history', methods=['POST'])
@token_required
def merge_anonymous_history():
    data = request.json
    username = session['username']

    if not data or 'history' not in data:
        return jsonify({'error': '未提供历史记录'}), 400

    try:
        # 将匿名历史记录保存到数据库
        for item in data['history']:
            new_history = ConversationHistory(
                username=username,
                conversation_id=item.get('conversation_id'),
                question=item.get('question'),
                answer=item.get('answer')
            )
            db.session.add(new_history)
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


# 登出接口
@app.route('/logout', methods=['POST'])
def logout():
    session.pop('username', None)
    return jsonify({'success': True})


# 检查登录状态接口
@app.route('/check_login', methods=['GET'])
def check_login():
    if 'username' in session:
        return jsonify({
            'logged_in': True,
            'username': session['username']
        })
    return jsonify({'logged_in': False})


@app.route('/verify_token', methods=['GET'])
@token_required  # 使用已有的token验证装饰器
def verify_token(current_user):
    # 如果token有效，返回用户信息
    return jsonify({
        'username': current_user,
        'is_authenticated': True
    })


# 保存对话历史接口（需要登录）
@app.route('/save_history', methods=['POST'])
@token_required
def save_history(current_user):
    data = request.json  # 前端传来的整个对话对象
    conversation_id = data.get('id')
    # 检查是否已存在该对话记录
    existing_history = ConversationHistory.query.filter_by(
        username=current_user,
        conversation_id=conversation_id
    ).first()
    if existing_history:
        # 存在则更新记录
        existing_history.title = data.get('title')
        existing_history.json_data = json.dumps(data)

    else:
        new_history = ConversationHistory(
            username=current_user,
            conversation_id=data.get('id'),
            title=data.get('title'),
            json_data=json.dumps(data),  # 存储完整 JSON
        )
        print(new_history.json_data)
        db.session.add(new_history)
    db.session.commit()
    return jsonify({'success': True})


# 获取对话历史接口（需要登录）
# 修改get_history接口

# 获取对话历史接口（需要登录）
@app.route('/get_history', methods=['GET'])
@token_required
def get_history(current_user):
    histories = ConversationHistory.query.filter_by(username=current_user).order_by(
        ConversationHistory.timestamp.desc()
    ).all()

    result = []
    for h in histories:
        try:
            # 还原 JSON 对话结构
            conv = json.loads(h.json_data)
            result.append(conv)
        except Exception:
            # 如果 json_data 有问题，就返回最基本信息
            result.append({
                "id": h.conversation_id,
                "title": h.title,
                "messages": [],
                "timestamp": h.timestamp.isoformat()
            })

    return jsonify(result)


@app.route('/transcribe', methods=['POST'])
def transcribe_audio():
    if 'audio' not in request.files:
        return jsonify({'error': '未提供音频文件'}), 400

    audio_file = request.files['audio']

    try:
        # 读取原始音频文件
        audio_data = audio_file.read()

        # 尝试自动识别格式并转换为WAV，不限制输入格式
        try:
            # 使用pydub的自动格式识别功能
            audio = AudioSegment.from_file(io.BytesIO(audio_data))

            # 统一转换为标准WAV格式参数
            audio = audio.set_channels(1)  # 单声道
            audio = audio.set_frame_rate(16000)  # 16kHz采样率
            audio = audio.set_sample_width(2)  # 16位深度

            # 导出为WAV格式到内存
            wav_buffer = io.BytesIO()
            audio.export(wav_buffer, format="wav")
            wav_buffer.seek(0)  # 重置缓冲区指针

        except Exception as e:
            return jsonify({'error': f"音频转换失败: 无法处理此格式 - {str(e)}"}), 500

        # 将转换后的WAV文件发送到语音接口
        files = {
            'file': ('converted.wav', wav_buffer, 'audio/wav')
        }

        response = requests.post(
            VOICE_SERVER_URL,
            files=files,
            timeout=30
        )

        # 返回语音接口响应
        if response.status_code == 200:
            return response.json()
        else:
            return jsonify({
                'error': "语音接口返回错误",
                'status_code': response.status_code,
                'details': response.text
            }), response.status_code

    except Exception as e:
        return jsonify({'error': f"请求处理失败: {str(e)}"}), 500


@app.route('/get_new_conversationid', methods=['GET'])
def refresh_conversationid():
    qa_headers = {
        'Authorization': f'Bearer {API_KEY}',
    }

    causal = str(random.randint(10000000, 99999999))
    qa_data = {
        'userId': USER_ID,
        'assistantId': ASSISTANT_ID,
        'conversationName': causal,
    }
    try:
        # 发起流式请求
        response = requests.post(
            GT_CID_URL,
            headers=qa_headers,
            json=qa_data,
            verify=False
        )
        if response.status_code == 200:
            res_json = response.json()
            new_id = res_json.get('data', {}).get('conversationId')
            print(f"✅ Conversation ID 已刷新：{new_id}")
            return new_id
        else:
            print(f"刷新 Conversation ID 失败: {response.text}")
            return None

    except Exception as e:
        print(f"刷新 Conversation ID 异常: {str(e)}")
        return None


@app.route('/download_doc/<dataset_id>/<doc_id>/<doc_name>')
def download_document(dataset_id, doc_id, doc_name):
    try:
        doc_headers = {
            'Authorization': f'Bearer {API_KEY}',
        }

        params = {
            'userId': USER_ID,
            'knowledgeBaseId': dataset_id,
            'documentId': doc_id,
        }

        response = requests.get(DOC_URL, headers=doc_headers, params=params, verify=False, stream=True)
        response.raise_for_status()

        temp_file = tempfile.NamedTemporaryFile(delete=False)
        for chunk in response.iter_content(chunk_size=8192):
            temp_file.write(chunk)
        temp_file.close()

        return send_file(temp_file.name, as_attachment=True, download_name=doc_name)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


import edge_tts
import asyncio
import tempfile

import os
import tempfile
import uuid  # 用于生成唯一文件名

# 获取当前 Python 文件（app.py）所在的目录的绝对路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 音频文件保存目录的绝对路径
AUDIO_DIR = os.path.join(BASE_DIR, 'audio_responses')

# 确保音频目录存在
os.makedirs(AUDIO_DIR, exist_ok=True)


# 修改 text_to_speech 接口
@app.route('/text_to_speech', methods=['POST'])
def text_to_speech():
    data = request.json
    if not data or 'text' not in data:
        return jsonify({'error': '未提供文本内容'}), 400

    text = data['text']
    try:
        # 生成唯一文件名（避免临时文件路径问题）
        filename = f"audio_{uuid.uuid4().hex}.mp3"
        # 音频文件的绝对路径
        audio_path = os.path.join(AUDIO_DIR, filename)

        print(f"开始生成语音，保存路径：{audio_path}")  # 打印日志

        # 生成语音
        async def generate_speech():
            communicate = edge_tts.Communicate(
                text=text,
                voice="en-US-AriaNeural"
            )
            await communicate.save(audio_path)

        asyncio.run(generate_speech())

        # 验证文件是否生成
        if not os.path.exists(audio_path):
            raise Exception(f"语音生成成功，但文件未找到：{audio_path}")

        # 返回音频URL（使用文件名即可，因为 get_audio 会用绝对路径查找）
        return jsonify({'audio_url': f'/audio/{filename}'})

    except Exception as e:
        print(f"语音生成失败：{str(e)}")
        # 清理可能的空文件
        if 'audio_path' in locals() and os.path.exists(audio_path):
            os.remove(audio_path)
        return jsonify({'error': f"语音生成失败: {str(e)}"}), 500


import os
import time
import threading
# 在现有导入语句中添加 Response
from flask import Flask, request, jsonify, send_file, after_this_request, Response


@app.route('/audio/<path:filename>')
def get_audio(filename):
    audio_path = os.path.join(AUDIO_DIR, filename)

    if not os.path.exists(audio_path):
        return jsonify({'error': f'音频文件不存在: {audio_path}'}), 404

    # 自定义文件发送函数，确保发送后释放句柄
    def send_audio_file():
        with open(audio_path, 'rb') as f:
            yield from f

    # 延迟删除函数（5秒后尝试删除）
    def delayed_delete():
        time.sleep(5)  # 等待5秒，确保前端已加载完成
        try:
            if os.path.exists(audio_path):
                os.remove(audio_path)
                print(f"延迟删除成功: {audio_path}")
        except Exception as e:
            # 若仍删除失败，记录日志，交给定时任务处理
            print(f"延迟删除失败: {str(e)}，将由定时任务清理")

    # 启动延迟删除线程
    threading.Thread(target=delayed_delete, daemon=True).start()

    # 发送文件（使用生成器方式避免句柄占用）
    return Response(
        send_audio_file(),
        mimetype="audio/mpeg",
        headers={"Content-Length": str(os.path.getsize(audio_path))}
    )


# 在app.py中添加分段语音生成接口
@app.route('/text_to_speech_stream', methods=['POST'])
def text_to_speech_stream():
    data = request.json
    if not data or 'text' not in data:
        return jsonify({'error': '未提供文本内容'}), 400

    text = data['text']
    try:
        # 生成唯一文件名
        filename = f"audio_{uuid.uuid4().hex}.mp3"
        audio_path = os.path.join(AUDIO_DIR, filename)

        # 异步生成语音并流式返回进度
        async def generate_and_stream():
            communicate = edge_tts.Communicate(text=text, voice="en-US-AriaNeural")
            with open(audio_path, 'wb') as f:
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        f.write(chunk["data"])
                        # 每写入1024字节就返回一次进度
                        if len(chunk["data"]) >= 1024:
                            yield f"data: {json.dumps({'status': 'processing', 'filename': filename})}\n\n"
            yield f"data: {json.dumps({'status': 'completed', 'filename': filename})}\n\n"

        return app.response_class(
            generate_and_stream(),
            mimetype='text/event-stream'
        )

    except Exception as e:
        print(f"语音生成失败：{str(e)}")
        return jsonify({'error': f"语音生成失败: {str(e)}"}), 500


if __name__ == '__main__':
    # 注意：生产环境需要使用 proper WSGI 服务器和有效的SSL证书
    # app.run(ssl_context='adhoc', debug=True)  # adhoc 仅用于测试
    # app.run(debug=True, host='0.0.0.0', port=5000)
    ssl_context = (
        "D:/360MoveData/Users/韩琪琪/Desktop/judgemodel/ssl_certs/localhost.crt",
        "D:/360MoveData/Users/韩琪琪/Desktop/judgemodel/ssl_certs/localhost.key"
    )
    # 启动 HTTPS 服务（host 设为 0.0.0.0 允许局域网访问）
    app.run(
        host="0.0.0.0",
        port=5000,
        ssl_context=ssl_context,
        debug=True  # 生产环境移除 debug=True
    )
