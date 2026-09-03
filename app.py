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

    ydl_opts = {'format': 'best', 'noplaylist': True}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

            video_url = info.get('url')

            # Fallback 1: some extractors (e.g. Twitter/X) only populate requested_downloads
            if not video_url:
                rd = info.get('requested_downloads')
                if rd and len(rd) > 0:
                    video_url = rd[0].get('url')

            # Fallback 2: pick the last (usually highest quality/combined) format
            if not video_url:
                formats = info.get('formats')
                if formats:
                    video_url = formats[-1].get('url')

            if not video_url:
                return jsonify({
                    'status': 'error',
                    'error': {'code': 'Could not extract a direct video URL for this link.'}
                }), 500

            return jsonify({'status': 'success', 'url': video_url})

    except Exception as e:
        return jsonify({'status': 'error', 'error': {'code': str(e)}}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
