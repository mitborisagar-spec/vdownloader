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

# Extensions/markers that mean "this is not actually a video" - YouTube's
# storyboard/scrubbing-preview sprite sheets are the classic culprit.
NON_VIDEO_EXTS = {'webp', 'mhtml', 'jpg', 'jpeg', 'png', 'gif'}


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


def is_real_video_format(f):
    if not f.get('url'):
        return False
    ext = (f.get('ext') or '').lower()
    if ext in NON_VIDEO_EXTS:
        return False
    format_id = (f.get('format_id') or '').lower()
    note = (f.get('format_note') or '').lower()
    if 'storyboard' in note or format_id.startswith('sb'):
        return False
    # A real video/audio format must have at least a video OR audio codec -
    # storyboards and thumbnails have neither.
    vcodec = f.get('vcodec')
    acodec = f.get('acodec')
    if (vcodec in (None, 'none')) and (acodec in (None, 'none')):
        return False
    return True


def pick_best_format(formats):
    if not formats:
        return None

    valid = [f for f in formats if is_real_video_format(f)]
    if not valid:
        return None

    combined = [f for f in valid if f.get('vcodec') not in (None, 'none') and f.get('acodec') not in (None, 'none')]
    if combined:
        combined.sort(key=lambda f: f.get('height') or 0)
        return combined[-1]['url']

    video_only = [f for f in valid if f.get('vcodec') not in (None, 'none')]
    if video_only:
        video_only.sort(key=lambda f: f.get('height') or 0)
        return video_only[-1]['url']

    # Only audio-only formats remain - still better than nothing.
    return valid[-1]['url']


def extract_video_url(url):
    ydl_opts = {'noplaylist': True}

    if is_youtube(url):
        cookies_path = get_writable_cookies_path()
        if cookies_path:
            ydl_opts['cookiefile'] = cookies_path

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False, process=False)

        if info.get('_type') == 'url_transparent' or 'formats' not in info:
            inner = info.get('entries')
            if inner:
                try:
                    info = next(iter(inner)) if not isinstance(inner, dict) else inner
                except Exception:
                    pass

        formats = info.get('formats') or []
        video_url = pick_best_format(formats)

        if not video_url:
            top_url = info.get('url')
            if top_url and is_real_video_format({'url': top_url, 'ext': info.get('ext'),
                                                  'vcodec': info.get('vcodec'),
                                                  'acodec': info.get('acodec'),
                                                  'format_note': info.get('format_note'),
                                                  'format_id': info.get('format_id')}):
                video_url = top_url

        title = info.get('title') or 'video'
        safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '_', '-')).strip()
        filename = (safe_title[:80] or 'video') + '.mp4'

        debug = None
        if not video_url:
            debug = {
                'format_count': len(formats),
                'sample': [
                    {
                        'format_id': f.get('format_id'),
                        'ext': f.get('ext'),
                        'vcodec': f.get('vcodec'),
                        'acodec': f.get('acodec'),
                        'has_url': bool(f.get('url')),
                        'note': f.get('format_note'),
                    }
                    for f in formats[:8]
                ],
                'top_level_url_present': bool(info.get('url')),
                'top_level_ext': info.get('ext'),
            }

        return video_url, filename, debug


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
        video_url, filename, debug = extract_video_url(original_url)

        if not video_url:
            return jsonify({
                'status': 'error',
                'error': {'code': 'Could not extract a direct video URL for this link.'},
                'debug': debug
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
