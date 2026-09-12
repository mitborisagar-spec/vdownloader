from flask import Flask, request, jsonify, send_file
import yt_dlp
import os
import shutil
import tempfile
import glob
import subprocess
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
      resources={r"/*": {"origins": "*"}},
    methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Accept"]
)

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

def _format_summary(info):
    """Return a compact, API-safe summary of extracted formats."""
    formats = info.get('formats') or []
    video = []
    audio = []
    muxed = []

    for f in formats:
        vcodec = f.get('vcodec')
        acodec = f.get('acodec')
        item = {
            'id': f.get('format_id'),
            'ext': f.get('ext'),
            'resolution': f.get('resolution'),
            'vcodec': vcodec,
            'acodec': acodec,
            'protocol': f.get('protocol'),
            'abr': f.get('abr'),
            'tbr': f.get('tbr'),
            'format_note': f.get('format_note'),
        }
        has_v = bool(vcodec and vcodec != 'none')
        has_a = bool(acodec and acodec != 'none')
        if has_v and has_a:
            muxed.append(item)
        elif has_v:
            video.append(item)
        elif has_a:
            audio.append(item)

    return {
        'total': len(formats),
        'video_only': len(video),
        'audio_only': len(audio),
        'muxed': len(muxed),
        'video_examples': video[-5:],
        'audio_examples': audio[-5:],
        'muxed_examples': muxed[-5:],
    }


def _log_format_debug(info, log_capture, attempt_label):
    summary = _format_summary(info)
    log_capture.debug(
        'Instagram formats [{}]: total={} video_only={} audio_only={} muxed={}'.format(
            attempt_label,
            summary['total'],
            summary['video_only'],
            summary['audio_only'],
            summary['muxed']
        )
    )
    for key in ('muxed_examples', 'audio_examples', 'video_examples'):
        for item in summary[key]:
            log_capture.debug(
                'FORMAT [{}]: id={} ext={} res={} vcodec={} acodec={} protocol={} abr={} tbr={} note={}'.format(
                    attempt_label,
                    item.get('id'), item.get('ext'), item.get('resolution'),
                    item.get('vcodec'), item.get('acodec'), item.get('protocol'),
                    item.get('abr'), item.get('tbr'), item.get('format_note')
                )
            )
    return summary


def _clear_temp_files(temp_dir):
    for path in glob.glob(os.path.join(temp_dir, '*')):
        try:
            if os.path.isfile(path):
                os.remove(path)
        except Exception:
            pass


def _download_once(url, temp_dir, log_capture, use_cookies=True, format_selector=None, attempt_label='unknown'):
    ydl_opts = build_ydl_options(
        temp_dir,
        log_capture,
        url,
        use_cookies=use_cookies,
        format_selector=format_selector
    )

    log_capture.debug(
        'Instagram attempt: cookies={} format={} yt-dlp={}'.format(
            use_cookies,
            format_selector or 'default',
            getattr(yt_dlp.version, '__version__', 'unknown')
        )
    )

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        summary = _log_format_debug(info, log_capture, attempt_label)

        if not summary['audio_only'] and not summary['muxed']:
            log_capture.warning(
                'No audio-bearing format exposed by Instagram in this attempt.'
            )

        processed = ydl.process_ie_result(
            info,
            download=True
        )

        return processed, info, summary


def download_and_merge(url, temp_dir):

    log_capture = LogCapture()

    try:
        if is_instagram(url):
            # Try muxed audio+video first. If Instagram exposes a separate
            # audio stream, fall back to normal video+audio selection.
            attempts = [
                (True, 'best[vcodec!=none][acodec!=none]/bv*+ba/b', 'cookies-muxed'),
                (True, 'bv*+ba/b', 'cookies-separate'),
                (False, 'best[vcodec!=none][acodec!=none]/bv*+ba/b', 'public-muxed'),
                (False, 'bv*+ba/b', 'public-separate'),
            ]

            last_summary = None
            for use_cookies, selector, label in attempts:
                _clear_temp_files(temp_dir)
                try:
                    processed, extracted, summary = _download_once(
                        url,
                        temp_dir,
                        log_capture,
                        use_cookies=use_cookies,
                        format_selector=selector,
                        attempt_label=label
                    )
                    last_summary = summary

                    # If the resulting file contains an audio-bearing format,
                    # this is the successful path. We still allow a downloaded
                    # muxed file even when format metadata is unusual.
                    if processed is not None:
                        
                        result = _finish_download(processed, temp_dir, log_capture, require_audio=True)
                        if result[0] is not None:
                            return result
                        log_capture.warning('Downloaded file did not contain an audio stream; trying next Instagram selector.')

                except Exception as attempt_error:
                    log_capture.error(
                        'Instagram attempt failed [{}]: {}'.format(
                            label, attempt_error
                        )
                    )

            return None, None, {
                'error': (
                    'Instagram did not expose a usable audio stream for this Reel. '
                    'The diagnostic below shows the formats Instagram returned to yt-dlp.'
                ),
                'format_summary': last_summary,
                'logs': log_capture.lines[-100:]
            }

        ydl_opts = build_ydl_options(
            temp_dir,
            log_capture,
            url
        )

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        return _finish_download(info, temp_dir, log_capture)

    except Exception as e:
        return None, None, {
            'exception': str(e),
            'deno_found': shutil.which('deno') is not None,
            'deno_dir_exists': os.path.isdir(_deno_bin),
            'ffmpeg_path': imageio_ffmpeg.get_ffmpeg_exe(),
            'logs': log_capture.lines[-100:]
        }


def _has_audio_stream(path):
    """Check the actual downloaded container, not just yt-dlp metadata."""
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    try:
        proc = subprocess.run(
            [ffmpeg, '-hide_banner', '-i', path, '-map', '0:a:0', '-f', 'null', '-'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30
        )
        text = (proc.stdout or '') + '\n' + (proc.stderr or '')
        return 'Audio:' in text
    except Exception:
        return False


def _finish_download(info, temp_dir, log_capture, require_audio=False):
    title = info.get('title', 'video') if isinstance(info, dict) else 'video'

    safe_title = ''.join(
        c for c in title
        if c.isalnum() or c in (' ', '_', '-')
    ).strip()

    filename = (safe_title[:80] or 'video') + '.mp4'

    mp4_files = glob.glob(os.path.join(temp_dir, '*.mp4'))
    if mp4_files:
        output_file = max(mp4_files, key=os.path.getsize)
    else:
        media_files = [
            f for f in glob.glob(os.path.join(temp_dir, '*'))
            if os.path.isfile(f)
        ]
        if not media_files:
            return None, None, {
                'error': 'No downloaded file found.',
                'ffmpeg': imageio_ffmpeg.get_ffmpeg_exe(),
                'logs': log_capture.lines[-100:]
            }
        output_file = max(media_files, key=os.path.getsize)

    if not os.path.exists(output_file):
        return None, None, {
            'error': 'Output file does not exist.',
            'logs': log_capture.lines[-100:]
        }

    if os.path.getsize(output_file) == 0:
        return None, None, {
            'error': 'Output file is empty.',
            'logs': log_capture.lines[-100:]
        }

    if require_audio and not _has_audio_stream(output_file):
        return None, None, {
            'error': 'Downloaded file contains no audio stream.',
            'logs': log_capture.lines[-100:]
        }

    return output_file, filename, None

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

@app.route("/", methods=["POST", "OPTIONS"])
def download():
    if request.method == "OPTIONS":
        return "", 204

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()

    if not url:
        return jsonify({
            "status": "error",
            "error": {
                "code": "URL is required."
            }
        }), 400

    proxy_url = request.host_url.rstrip("/") + "/stream?url=" + quote(
        url, safe=""
    )

    return jsonify({
        "status": "success",
        "url": proxy_url
    })


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
