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
            shutil.copyfile(SECRET_COOKIES_PATH, WRITABLE_COOKIES_PATH)
            return WRITABLE_COOKIES_PATH
        except Exception:
            return None
    return None

# =========================================================
# HELPERS & UTILITIES
# =========================================================
def is_youtube(url):
    url = url.lower()
    return 'youtube.com' in url or 'youtu.be' in url

def is_instagram(url):
    url = url.lower()
    return 'instagram.com' in url or 'instagr.am' in url

# =========================================================
# LOG CAPTURE
# =========================================================
class LogCapture:
    def __init__(self):
        self.lines = []

    def debug(self, msg):
        self.lines.append(str(msg))

    def warning(self, msg):
        self.lines.append('WARNING: ' + str(msg))

    def error(self, msg):
        self.lines.append('ERROR: ' + str(msg))

# =========================================================
# BUILD YT-DLP OPTIONS
# =========================================================
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

    if is_instagram(url_lower):
        ydl_opts['http_headers'] = {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/151.0.0.0 Safari/537.36'
            ),
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://instagram.com',
        }

        if use_cookies:
            cookies_path = get_writable_cookies_path()
            if cookies_path:
                ydl_opts['cookiefile'] = cookies_path

        return ydl_opts

    if is_youtube(url):
        cookies_path = get_writable_cookies_path()
        if cookies_path:
            ydl_opts['cookiefile'] = cookies_path

        ydl_opts['extractor_args'] = {
            'youtubepot-bgutilhttp': {
                'base_url': ['https://onrender.com']
            },
            'youtube': {
                'getpot_bgutil_baseurl': ['https://onrender.com']
            }
        }

    return ydl_opts

# =========================================================
# FORMAT DEBUG & DIAGNOSTICS
# =========================================================
def _print_format_debug(info):
    print('\n===== FORMAT DEBUG =====')
    formats = info.get('formats') or []

    for f in formats:
        print(
            'FORMAT: {} | EXT: {} | RES: {} | VCODEC: {} | ACODEC: {} | PROTO: {} | TBR: {}'.format(
                f.get('format_id'), f.get('ext'), f.get('resolution'),
                f.get('vcodec'), f.get('acodec'), f.get('protocol'), f.get('tbr')
            )
        )
    print('===== END FORMAT DEBUG =====\n')

    video_formats = [f for f in formats if f.get('vcodec') and f.get('vcodec') != 'none']
    audio_formats = [f for f in formats if f.get('acodec') and f.get('acodec') != 'none' and (not f.get('vcodec') or f.get('vcodec') == 'none')]
    muxed_formats = [f for f in formats if f.get('vcodec') and f.get('vcodec') != 'none' and f.get('acodec') and f.get('acodec') != 'none']

    print('VIDEO FORMATS:', len(video_formats))
    print('AUDIO-ONLY FORMATS:', len(audio_formats))
    print('MUXED VIDEO+AUDIO FORMATS:', len(muxed_formats))

    return bool(audio_formats or muxed_formats)

def _download_once(url, temp_dir, log_capture, use_cookies=True, format_selector=None):
    ydl_opts = build_ydl_options(temp_dir, log_capture, url, use_cookies=use_cookies, format_selector=format_selector)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if is_instagram(url):
            _print_format_debug(info)
        processed_info = ydl.process_ie_result(info, download=True)
        return processed_info

def _clear_temp_files(temp_dir):
    for f in glob.glob(os.path.join(temp_dir, '*')):
        try:
            if os.path.isfile(f):
                os.remove(f)
        except Exception:
            pass

# =========================================================
# FIXED DOWNLOAD + MERGE STRATEGY WITH FALLBACKS
# =========================================================
def download_and_merge(url, temp_dir):
    log_capture = LogCapture()
    
    try:
        if is_instagram(url):
            # Try 4 standard format strategies to find the audio stream natively
            attempts = [
                (True, 'bestvideo+bestaudio/best'),
                (False, 'bestvideo+bestaudio/best'),
                (True, 'best'),
                (False, 'best'),
            ]

            for use_cookies, selector in attempts:
                _clear_temp_files(temp_dir)
                log_capture.lines.append(f"Instagram attempt: cookies={use_cookies} format={selector}")
                
                try:
                    info = _download_once(url, temp_dir, log_capture, use_cookies=use_cookies, format_selector=selector)
                    
                    # Gather download tracking artifacts
                    files = glob.glob(os.path.join(temp_dir, '*'))
                    valid_files = [f for f in files if not f.endswith('.part') and not f.endswith('.ytdl')]
                    
                    if valid_files:
                        acodec = info.get('acodec')
                        # If audio stream is validated inside the track output file, return early
                        if acodec and acodec != 'none':
                            return valid_files[0], info
                except Exception as e:
                    log_capture.lines.append(f"Attempt failed: {str(e)}")
                    continue

            # =========================================================
            # EMERGENCY FALLBACK: EXPLICIT DUAL-MANIFEST INTERCEPTION
            # =========================================================
            # Standard selectors dropped audio tracks. We manually grab separate audio & video IDs
            _clear_temp_files(temp_dir)
            log_capture.lines.append("Triggering emergency manual stream-pairing loop.")
            
            ydl_opts = build_ydl_options(temp_dir, log_capture, url, use_cookies=True, format_selector='all')
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                formats = info.get('formats', [])
                
                video_urls = [f for f in formats if f.get('vcodec') != 'none' and f.get('acodec') == 'none']
                audio_urls = [f for f in formats if f.get('acodec') != 'none']
                
                if video_urls and audio_urls:
                    best_video = max(video_urls, key=lambda x: x.get('tbr', 0) or x.get('height', 0))
                    best_audio = max(audio_urls, key=lambda x: x.get('tbr', 0) or x.get('abr', 0))
                    
                    # Force combine target stream tokens
                    target_format = f"{best_video['format_id']}+{best_audio['format_id']}"
                    info = _download_once(url, temp_dir, log_capture, use_cookies=True, format_selector=target_format)
                    
                    files = glob.glob(os.path.join(temp_dir, '*'))
                    valid_files = [f for f in files if not f.endswith('.part') and not f.endswith('.ytdl')]
                    if valid_files:
                        return valid_files[0], info

        # YouTube, Facebook, or Fallback Engine standard execution path
        _clear_temp_files(temp_dir)
        info = _download_once(url, temp_dir, log_capture, use_cookies=True)
        files = glob.glob(os.path.join(temp_dir, '*'))
        valid_files = [f for f in files if not f.endswith('.part') and not f.endswith('.ytdl')]
        if valid_files:
            return valid_files[0], info
            
        raise Exception("No processed video outputs captured after parsing available protocols.")

    except Exception as e:
        print("CRITICAL ENGINE ERROR:", str(e))
        print("\n".join(log_capture.lines))
        return None, None

# =========================================================
# API ENDPOINT
# =========================================================
@app.route('/download', methods=['GET'])
def download_api():
    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'Missing source URL parameter'}), 400
        # Initialize a secure isolated container for the conversion execution
    temp_dir = tempfile.mkdtemp(prefix='render_ytdlp_')
    
    try:
        file_path, info = download_and_merge(url, temp_dir)
        
        if not file_path or not os.path.exists(file_path):
            return jsonify({
                'error': 'Failed to process video or capture audio stream from the platform.'
            }), 500
            
        # Clean up name format for headers
        title = info.get('title', 'instagram_reel')
        safe_filename = f"{quote(title[:50])}.mp4"
        
        # Stream the finalized multiplexed file directly back to consumer
        return send_file(
            file_path,
            as_attachment=True,
            download_name=safe_filename,
            mimetype='video/mp4'
        )

    except Exception as e:
        return jsonify({'error': str(e)}), 500
        
    # Temporary files within the execution block are preserved until after response delivery,
    # you can run a clean up cron script or clean up here manually if using custom return strategies.

if __name__ == '__main__':
    # Ensure Render environment standard binding ports are met
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
