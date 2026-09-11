from flask import Flask, request, jsonify, send_file
import yt_dlp
import os
import shutil
import tempfile
import glob
import imageio_ffmpeg
from flask_cors import CORS
from urllib.parse import quote


# =========================================================
# DENO PATH
# =========================================================

_deno_bin = '/opt/render/project/.deno/bin'

if os.path.isdir(_deno_bin):
    os.environ['PATH'] = (
        _deno_bin
        + os.pathsep
        + os.environ.get('PATH', '')
    )


# =========================================================
# FLASK APP
# =========================================================

app = Flask(__name__)
CORS(app)


# =========================================================
# COOKIE SETTINGS
# =========================================================

SECRET_COOKIES_PATH = '/etc/secrets/cookies.txt'
WRITABLE_COOKIES_PATH = '/tmp/cookies.txt'


def get_writable_cookies_path():
    if os.path.exists(SECRET_COOKIES_PATH):
        try:
            shutil.copyfile(
                SECRET_COOKIES_PATH,
                WRITABLE_COOKIES_PATH
            )
            return WRITABLE_COOKIES_PATH
        except Exception:
            return None

    return None


# =========================================================
# YOUTUBE CHECK
# =========================================================

def is_youtube(url):
    url = url.lower()

    return (
        'youtube.com' in url
        or 'youtu.be' in url
    )


# =========================================================
# LOG CAPTURE
# =========================================================

class LogCapture:

    def __init__(self):
        self.lines = []

    def debug(self, msg):
        self.lines.append(str(msg))

    def warning(self, msg):
        self.lines.append(
            'WARNING: ' + str(msg)
        )

    def error(self, msg):
        self.lines.append(
            'ERROR: ' + str(msg)
        )


# =========================================================
# BUILD YT-DLP OPTIONS
# =========================================================

def is_instagram(url):
    url = url.lower()
    return 'instagram.com' in url or 'instagr.am' in url


def build_ydl_options(temp_dir, log_capture, url, use_cookies=True, format_selector=None):

    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    url_lower = url.lower()

    ydl_opts = {
        'noplaylist': True,
        # The selector is supplied per attempt so Instagram can be tried
        # as both a single muxed file and as separate video + audio.
        'format': format_selector or 'bv*+ba/b',
        'merge_output_format': 'mp4',
        'outtmpl': os.path.join(temp_dir, '%(id)s.%(ext)s'),
        'ffmpeg_location': ffmpeg_path,
        'logger': log_capture,
        'verbose': True,
        'retries': 3,
        'fragment_retries': 3,
        'continuedl': True,
    }

    # Instagram currently relies heavily on browser-like requests.
    # Use the exported Instagram cookies when available, but allow a
    # second logged-out attempt if cookies expose fewer formats.
    if is_instagram(url_lower):
        ydl_opts['http_headers'] = {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/151.0.0.0 Safari/537.36'
            ),
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://www.instagram.com/',
        }

        if use_cookies:
            cookies_path = get_writable_cookies_path()
            if cookies_path:
                ydl_opts['cookiefile'] = cookies_path

        return ydl_opts

    # YouTube / Facebook
    if is_youtube(url):
        cookies_path = get_writable_cookies_path()
        if cookies_path:
            ydl_opts['cookiefile'] = cookies_path

        ydl_opts['extractor_args'] = {
            'youtubepot-bgutilhttp': {
                'base_url': [
                    'https://vdownloader-pot.onrender.com'
                ]
            },
            'youtube': {
                'getpot_bgutil_baseurl': [
                    'https://vdownloader-pot.onrender.com'
                ]
            }
        }

    return ydl_opts


# =========================================================
# MAIN DOWNLOAD + MERGE FUNCTION
# =========================================================

def _print_format_debug(info):
    print('')
    print('===== FORMAT DEBUG =====')

    formats = info.get('formats') or []

    for f in formats:
        print(
            'FORMAT: {} | EXT: {} | RES: {} | VCODEC: {} | ACODEC: {} | PROTO: {} | TBR: {}'.format(
                f.get('format_id'),
                f.get('ext'),
                f.get('resolution'),
                f.get('vcodec'),
                f.get('acodec'),
                f.get('protocol'),
                f.get('tbr')
            )
        )

    print('===== END FORMAT DEBUG =====')
    print('')

    video_formats = [
        f for f in formats
        if f.get('vcodec') and f.get('vcodec') != 'none'
    ]

    audio_formats = [
        f for f in formats
        if f.get('acodec') and f.get('acodec') != 'none'
        and (not f.get('vcodec') or f.get('vcodec') == 'none')
    ]

    muxed_formats = [
        f for f in formats
        if f.get('vcodec') and f.get('vcodec') != 'none'
        and f.get('acodec') and f.get('acodec') != 'none'
    ]

    print('VIDEO FORMATS:', len(video_formats))
    print('AUDIO-ONLY FORMATS:', len(audio_formats))
    print('MUXED VIDEO+AUDIO FORMATS:', len(muxed_formats))

    if audio_formats:
        best_audio = max(
            audio_formats,
            key=lambda x: (x.get('abr') or 0, x.get('tbr') or 0)
        )
        print(
            'BEST AUDIO:',
            best_audio.get('format_id'),
            '| ACODEC:',
            best_audio.get('acodec'),
            '| ABR:',
            best_audio.get('abr')
        )
    elif muxed_formats:
        best_muxed = max(
            muxed_formats,
            key=lambda x: (x.get('height') or 0, x.get('tbr') or 0)
        )
        print(
            'BEST MUXED:',
            best_muxed.get('format_id'),
            '| ACODEC:',
            best_muxed.get('acodec')
        )
    else:
        print('WARNING: NO AUDIO FORMAT FOUND')

    return bool(audio_formats or muxed_formats)


def _download_once(url, temp_dir, log_capture, use_cookies=True, format_selector=None):
    ydl_opts = build_ydl_options(
        temp_dir,
        log_capture,
        url,
        use_cookies=use_cookies,
        format_selector=format_selector
    )

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

        if is_instagram(url):
            _print_format_debug(info)

        info = ydl.process_ie_result(
            info,
            download=True
        )

        return info, info


def _clear_temp_files(temp_dir):
    for f in glob.glob(os.path.join(temp_dir, '*')):
        try:
            if os.path.isfile(f):
                os.remove(f)
        except Exception:
            pass


def download_and_merge(url, temp_dir):

    log_capture = LogCapture()

    try:
        if is_instagram(url):
            # Instagram can expose a Reel as a muxed file in one response
            # and as separate streams in another. Try every useful selector
            # before declaring the Reel video-only.
            attempts = [
                (True, 'best'),
                (True, 'bv*+ba/b'),
                (False, 'best'),
                (False, 'bv*+ba/b'),
            ]

            info = None

            for use_cookies, selector in attempts:
                _clear_temp_files(temp_dir)
                log_capture.lines.append(
                    'Instagram attempt: cookies={} format={}'.format(
                        use_cookies, selector
                    )
                )

                try:
                    info, extracted = _download_once(
                        url,
                        temp_dir,
                        log_capture,
                        use_cookies=use_cookies,
                        format_selector=selector
                    )
                    if info is not None:
                        break
                except Exception as attempt_error:
                    log_capture.lines.append(
                        'Instagram attempt failed: ' + str(attempt_error)
                    )
                    info = None

            if info is None:
                return None, None, {
                    'error': (
                        'Instagram could not provide a downloadable audio/video combination. '
                        'This Reel may expose only video media to yt-dlp.'
                    ),
                    'logs': log_capture.lines[-80:]
                }

        else:
            ydl_opts = build_ydl_options(
                temp_dir,
                log_capture,
                url
            )

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(
                    url,
                    download=True
                )

        title = info.get(
            'title',
            'video'
        )

        safe_title = ''.join(
            c for c in title
            if c.isalnum() or c in (' ', '_', '-')
        ).strip()

        filename = (
            safe_title[:80] or 'video'
        ) + '.mp4'

        mp4_files = glob.glob(
            os.path.join(temp_dir, '*.mp4')
        )

        if mp4_files:
            output_file = max(
                mp4_files,
                key=os.path.getsize
            )
        else:
            media_files = [
                f for f in glob.glob(os.path.join(temp_dir, '*'))
                if os.path.isfile(f)
            ]

            if not media_files:
                return None, None, {
                    'error': 'No downloaded file found.',
                    'ffmpeg': imageio_ffmpeg.get_ffmpeg_exe(),
                    'logs': log_capture.lines[-80:]
                }

            output_file = max(
                media_files,
                key=os.path.getsize
            )

        if not os.path.exists(output_file):
            return None, None, {
                'error': 'Output file does not exist.',
                'logs': log_capture.lines[-80:]
            }

        if os.path.getsize(output_file) == 0:
            return None, None, {
                'error': 'Output file is empty.',
                'logs': log_capture.lines[-80:]
            }

        return output_file, filename, None

    except Exception as e:
        return None, None, {
            'exception': str(e),
            'deno_found': shutil.which('deno') is not None,
            'deno_dir_exists': os.path.isdir(_deno_bin),
            'ffmpeg_path': imageio_ffmpeg.get_ffmpeg_exe(),
            'logs': log_capture.lines[-80:]
        }


# =========================================================
# HOME API
# =========================================================

@app.route('/', methods=['POST'])
def download():

    try:

        data = request.get_json(
            silent=True
        ) or {}

        url = data.get('url')


        if not url:

            return jsonify({

                'status': 'error',

                'error': {
                    'code': 'URL missing'
                }

            }), 400


        url = str(url).strip()


        if not url:

            return jsonify({

                'status': 'error',

                'error': {
                    'code': 'URL missing'
                }

            }), 400


        # Same API structure as before
        proxy_url = (
            request.host_url.rstrip('/')
            + '/stream?url='
            + quote(
                url,
                safe=''
            )
        )


        return jsonify({

            'status': 'success',

            'url': proxy_url

        })


    except Exception as e:

        return jsonify({

            'status': 'error',

            'error': {
                'code': str(e)
            }

        }), 500


# =========================================================
# STREAM / DOWNLOAD ENDPOINT
# =========================================================

@app.route('/stream', methods=['GET'])
def stream():

    original_url = request.args.get(
        'url'
    )


    if not original_url:

        return jsonify({

            'status': 'error',

            'error': {
                'code': 'URL missing'
            }

        }), 400


    # Create temporary directory
    temp_dir = tempfile.mkdtemp(
        prefix='vdownloader_'
    )


    try:

        # =================================================
        # DOWNLOAD + MERGE
        # =================================================

        output_file, filename, debug = (
            download_and_merge(
                original_url,
                temp_dir
            )
        )


        # =================================================
        # ERROR
        # =================================================

        if not output_file:

            shutil.rmtree(
                temp_dir,
                ignore_errors=True
            )


            return jsonify({

                'status': 'error',

                'error': {
                    'code': (
                        debug.get(
                            'exception',
                            'Could not download and merge the video.'
                        )
                        if debug
                        else
                        'Could not download and merge the video.'
                    )
                },

                'debug': debug

            }), 500


        # =================================================
        # SEND MERGED MP4
        # =================================================

        response = send_file(

            output_file,

            mimetype='video/mp4',

            as_attachment=True,

            download_name=filename,

            max_age=0

        )


        # =================================================
        # CLEAN TEMP FILE AFTER DOWNLOAD
        # =================================================

        @response.call_on_close
        def cleanup():

            shutil.rmtree(
                temp_dir,
                ignore_errors=True
            )


        return response


    except Exception as e:

        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )


        return jsonify({

            'status': 'error',

            'error': {
                'code': str(e)
            }

        }), 500


# =========================================================
# HEALTH CHECK
# =========================================================

@app.route('/health', methods=['GET'])
def health():

    try:

        ffmpeg_path = (
            imageio_ffmpeg.get_ffmpeg_exe()
        )

        ffmpeg_exists = os.path.exists(
            ffmpeg_path
        )


        return jsonify({

            'status': 'ok',

            'ffmpeg': ffmpeg_exists,

            'ffmpeg_path': ffmpeg_path,

            'deno': (
                shutil.which('deno')
                is not None
            )

        })


    except Exception as e:

        return jsonify({

            'status': 'error',

            'error': str(e)

        }), 500


# =========================================================
# RUN
# =========================================================

if __name__ == '__main__':

    app.run(
        host='0.0.0.0',
        port=5000
    )
