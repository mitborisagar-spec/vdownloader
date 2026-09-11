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
def build_ydl_options(temp_dir, log_capture, url):

    # Get FFmpeg supplied by imageio-ffmpeg
    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()

    # Common options for all platforms
    ydl_opts = {
        'noplaylist': True,
        'format': 'bv*+ba/b',
        'merge_output_format': 'mp4',

        'outtmpl': os.path.join(
            temp_dir,
            '%(id)s.%(ext)s'
        ),

        'ffmpeg_location': ffmpeg_path,
        'logger': log_capture,
        'verbose': True,
        'retries': 2,
        'fragment_retries': 2,
        'continuedl': True,
    }

    # YouTube settings
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

def download_and_merge(url, temp_dir):

    log_capture = LogCapture()

    ydl_opts = build_ydl_options(
        temp_dir,
        log_capture,
        url
    )


    try:

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:

            # Download video + audio
            # FFmpeg automatically merges them
            info = ydl.extract_info(
                url,
                download=True
            )


            title = info.get(
                'title',
                'video'
            )


        # =================================================
        # SAFE FILE NAME
        # =================================================

        safe_title = ''.join(
            c
            for c in title
            if c.isalnum()
            or c in (' ', '_', '-')
        ).strip()


        filename = (
            safe_title[:80]
            or 'video'
        ) + '.mp4'


        # =================================================
        # FIND MERGED MP4
        # =================================================

        mp4_files = glob.glob(
            os.path.join(
                temp_dir,
                '*.mp4'
            )
        )


        if mp4_files:

            output_file = max(
                mp4_files,
                key=os.path.getsize
            )

        else:

            # Sometimes extension may differ.
            # Find any generated media file.
            media_files = [
                f
                for f in glob.glob(
                    os.path.join(
                        temp_dir,
                        '*'
                    )
                )
                if os.path.isfile(f)
            ]


            if not media_files:

                return None, None, {
                    'error': 'No downloaded file found.',
                    'ffmpeg': imageio_ffmpeg.get_ffmpeg_exe(),
                    'logs': log_capture.lines[-30:]
                }


            output_file = max(
                media_files,
                key=os.path.getsize
            )


        # =================================================
        # BASIC FILE CHECK
        # =================================================

        if not os.path.exists(output_file):

            return None, None, {
                'error': 'Output file does not exist.',
                'logs': log_capture.lines[-30:]
            }


        if os.path.getsize(output_file) == 0:

            return None, None, {
                'error': 'Output file is empty.',
                'logs': log_capture.lines[-30:]
            }


        return output_file, filename, None


    except Exception as e:

        return None, None, {

            'exception': str(e),

            'deno_found': (
                shutil.which('deno')
                is not None
            ),

            'deno_dir_exists': os.path.isdir(
                _deno_bin
            ),

            'ffmpeg_path': (
                imageio_ffmpeg.get_ffmpeg_exe()
            ),

            'logs': log_capture.lines[-30:]
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
