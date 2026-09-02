from flask import Flask, request, jsonify
import yt_dlp
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

@app.route('/', methods=['POST'])
def download():
    data = request.json
    url = data.get('url')
    if not url:
        return jsonify({'status': 'error', 'error': {'code': 'URL missing'}}), 400

    ydl_opts = {'format': 'best'}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            video_url = info.get('url')
            return jsonify({'status': 'success', 'url': video_url})
    except Exception as e:
        return jsonify({'status': 'error', 'error': {'code': str(e)}}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

