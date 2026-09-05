from flask import Flask, request, jsonify, Response, stream_with_context
import yt_dlp
import requests
import os
import shutil
from flask_cors import CORS
from urllib.parse import quote

app = Flask(__name__)
CORS(app)

SECRET_COOKIES_PATH = '/etc/secrets/cookies.txt'
WRITABLE_COOKIES_PATH = '/tmp/cookies.txt'


def get_writable_cookies_path():
    if os.path.exists(SECRET_COOKIES_PATH):
        try:
            shutil.copyfile(SECRET_COOKIES_PATH, WRITABLE_COOKIES_PATH)
            return WRITABLE_COOKIES_PATH
        except Exception:
            return None
    return None


def is_youtube(url):
    return 'youtube.com' in url or 'youtu.be' in url


def pick_best_format(formats):
    if not formats:
        return None

    combined = [
        f for f in formats
        if f.get('vcodec') not in (None, 'none')
        and f.get('acodec') not in (None, 'none')
        and f.get('url')
    ]
    if combined:
        combined.sort(key=lambda f: f.get('height') or 0)
        return combined[-1]['url']

    video_only = [f for f in formats if f.get('vcodec') not in (None, 'none') and f.get('url')]
    if video_only:
        video_only.sort(key=lambda f: f.get('height') or 0)
        return video_only[-1]['url']

    for f in reversed(formats):
        if f.get('url'):
            return f['url']

    return None


def extract_video_url(url):
    ydl_opts = {'noplaylist': True}

    if is_youtube(url):
        cookies_path = get_writable_cookies_path()
        if cookies_path:
            ydl_opts['cookiefile'] = cookies_path

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        # process=False stops yt-dlp BEFORE it runs format selection, which
        # is exactly the step that raises "Requested format is not
        # available". We deliberately do NOT call process_ie_result
        # afterwards (that would just run the same selection step
        # manually and hit the same error) - instead we read the raw,
        # already-extracted 'formats' list directly and pick one ourselves.
        info = ydl.extract_info(url, download=False, process=False)

        # Some extractors nest the real data one level down.
        if info.get('_type') == 'url_transparent' or 'formats' not in info:
            inner = info.get('entries')
            if inner:
                try:
                    info = next(iter(inner)) if not isinstance(inner, dict) else inner
                except Exception:
                    pass

        video_url = info.get('url')

        if not video_url:
            formats = info.get('formats') or []
            video_url = pick_best_format(formats)

        title = info.get('title') or 'video'
        safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '_', '-')).strip()
        filename = (safe_title[:80] or 'video') + '.mp4'

        return video_url, filename


@app.route('/', methods=['POST'])
def download():
    data = request.json
    url = data.get('url')
    if not url:
        return jsonify({'status': 'error', 'error': {'code': 'URL missing'}}), 400

    proxy_url = request.host_url.rstrip('/') + '/stream?url=' + quote(url, safe='')
    return jsonify({'status': 'success', 'url': proxy_url})


@app.route('/stream', methods=['GET'])
def stream():
    original_url = request.args.get('url')
    if not original_url:
        return jsonify({'status': 'error', 'error': {'code': 'URL missing'}}), 400

    try:
        video_url, filename = extract_video_url(original_url)

        if not video_url:
            return jsonify({
                'status': 'error',
                'error': {'code': 'Could not extract a direct video URL for this link.'}
            }), 500

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                           '(KHTML, like Gecko) Chrome/120.0 Safari/537.36',
            'Referer': 'https://twitter.com/'
        }

        upstream = requests.get(video_url, headers=headers, stream=True, timeout=30)
        upstream.raise_for_status()

        def generate():
            for chunk in upstream.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk

        return Response(
            stream_with_context(generate()),
            content_type=upstream.headers.get('Content-Type', 'video/mp4'),
            headers={'Content-Disposition': 'attachment; filename="' + filename + '"'}
        )

    except Exception as e:
        return jsonify({'status': 'error', 'error': {'code': str(e)}}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
