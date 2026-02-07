"""
SnapDownloader Backend - Snapchat Video Downloader API
A Flask-based backend for extracting and downloading Snapchat videos.
"""

import os
import re
import uuid
import shutil
import tempfile
import subprocess
import threading
import time
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, request, jsonify, Response, send_file
from flask_cors import CORS
import yt_dlp
import requests

# ============== App Configuration ==============
app = Flask(__name__)
CORS(app, resources={
    r"/api/*": {
        "origins": ["http://localhost:3000", "https://snapdown.cc", "*"],
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization"]
    }
})

# Configuration
TEMP_DIR = os.path.join(tempfile.gettempdir(), 'snapdownloader')
MAX_FILE_AGE_HOURS = 1  # Auto-cleanup files older than this
CHUNK_SIZE = 8192  # Download chunk size
REQUEST_TIMEOUT = 30  # Timeout for external requests

# Ensure temp directory exists
os.makedirs(TEMP_DIR, exist_ok=True)

# ============== Utility Functions ==============

def cleanup_old_files():
    """Remove files older than MAX_FILE_AGE_HOURS from temp directory."""
    try:
        cutoff = datetime.now() - timedelta(hours=MAX_FILE_AGE_HOURS)
        for filename in os.listdir(TEMP_DIR):
            filepath = os.path.join(TEMP_DIR, filename)
            if os.path.isfile(filepath):
                file_time = datetime.fromtimestamp(os.path.getmtime(filepath))
                if file_time < cutoff:
                    os.remove(filepath)
                    app.logger.info(f"Cleaned up old file: {filename}")
    except Exception as e:
        app.logger.error(f"Cleanup error: {e}")

def start_cleanup_thread():
    """Start background cleanup thread."""
    def cleanup_loop():
        while True:
            cleanup_old_files()
            time.sleep(3600)  # Run every hour
    
    thread = threading.Thread(target=cleanup_loop, daemon=True)
    thread.start()

def is_valid_snapchat_url(url: str) -> bool:
    """Validate if URL is a valid Snapchat URL."""
    patterns = [
        r'snapchat\.com/spotlight/',
        r'snapchat\.com/add/',
        r'snapchat\.com/t/',
        r'snapchat\.com/p/',
        r'snapchat\.com/discover/',
        r'snapchat\.com/story/',
        r'snapchat\.com/@[\w.-]+/spotlight/',
        r'story\.snapchat\.com/',
        r'snap\.com/',
        r'snapchat\.com/unlock/',
        r'web\.snapchat\.com/',
        r't\.snapchat\.com/',
    ]
    return any(re.search(pattern, url, re.IGNORECASE) for pattern in patterns)

def get_safe_filename(title: str, ext: str = 'mp4') -> str:
    """Generate a safe filename from title."""
    # Remove invalid characters
    safe_title = re.sub(r'[^\w\s-]', '', title)
    safe_title = re.sub(r'\s+', '_', safe_title)
    safe_title = safe_title[:50] or 'snapchat_video'
    return f"{safe_title}.{ext}"

def extract_username_from_url(url: str) -> str:
    """Extract username from Snapchat URL."""
    # Pattern for story.snapchat.com/s/USERNAME/...
    match = re.search(r'story\.snapchat\.com/s/([A-Za-z0-9_.-]+)', url, re.IGNORECASE)
    if match:
        return match.group(1)
    
    # Pattern for snapchat.com/@USERNAME/spotlight/...
    match = re.search(r'snapchat\.com/@([A-Za-z0-9_.-]+)', url, re.IGNORECASE)
    if match:
        return match.group(1)
    
    # Pattern for snapchat.com/add/USERNAME
    match = re.search(r'snapchat\.com/add/([A-Za-z0-9_.-]+)', url, re.IGNORECASE)
    if match:
        return match.group(1)
    
    # Pattern for snapchat.com/story/USERNAME/...
    match = re.search(r'snapchat\.com/story/([A-Za-z0-9_.-]+)', url, re.IGNORECASE)
    if match:
        return match.group(1)
    
    # Pattern for t.snapchat.com or web.snapchat.com with username
    match = re.search(r'(?:t|web)\.snapchat\.com/([A-Za-z0-9_.-]+)', url, re.IGNORECASE)
    if match and match.group(1) not in ['s', 'p', 't', 'spotlight', 'discover', 'story', 'add', 'unlock']:
        return match.group(1)
    
    return None

def get_ffmpeg_path():
    """Get FFmpeg path - check if available in PATH or local."""
    # Check if ffmpeg is in PATH
    if shutil.which('ffmpeg'):
        return 'ffmpeg'
    
    # Check common Windows locations
    common_paths = [
        r'C:\ffmpeg\bin\ffmpeg.exe',
        r'C:\Program Files\ffmpeg\bin\ffmpeg.exe',
        os.path.join(os.path.dirname(__file__), 'ffmpeg', 'ffmpeg.exe'),
    ]
    
    for path in common_paths:
        if os.path.exists(path):
            return path
    
    return None

# ============== yt-dlp Configuration ==============

def get_yt_dlp_options(extract_only: bool = True):
    """Get yt-dlp options for extraction."""
    options = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'no_check_certificate': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Referer': 'https://www.snapchat.com/',
        },
        'socket_timeout': REQUEST_TIMEOUT,
        'retries': 3,
    }
    
    if extract_only:
        options['skip_download'] = True
    
    # Add FFmpeg location if available
    ffmpeg = get_ffmpeg_path()
    if ffmpeg:
        options['ffmpeg_location'] = os.path.dirname(ffmpeg) if ffmpeg != 'ffmpeg' else None
    
    return options

def extract_video_info(url: str) -> dict:
    """Extract video information using yt-dlp."""
    try:
        options = get_yt_dlp_options(extract_only=True)
        
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
            
            if not info:
                return {'success': False, 'error': 'Could not extract video information'}
            
            # Process formats
            formats = []
            if 'formats' in info and info['formats']:
                for fmt in info['formats']:
                    format_info = {
                        'format_id': fmt.get('format_id', 'unknown'),
                        'url': fmt.get('url', ''),
                        'ext': fmt.get('ext', 'mp4'),
                        'quality': fmt.get('quality', 0),
                        'filesize': fmt.get('filesize'),
                        'width': fmt.get('width'),
                        'height': fmt.get('height'),
                        'fps': fmt.get('fps'),
                        'vcodec': fmt.get('vcodec'),
                        'acodec': fmt.get('acodec'),
                        'protocol': fmt.get('protocol', ''),
                    }
                    
                    # Mark HLS streams for conversion
                    if 'm3u8' in format_info['url'] or format_info['protocol'] in ['m3u8', 'm3u8_native', 'hls']:
                        format_info['needs_conversion'] = True
                        format_info['original_ext'] = format_info['ext']
                        format_info['ext'] = 'mp4'
                    else:
                        format_info['needs_conversion'] = False
                    
                    formats.append(format_info)
            
            # Get best URL
            best_url = info.get('url', '')
            if not best_url and formats:
                # Prefer direct MP4 over HLS
                direct_formats = [f for f in formats if not f.get('needs_conversion')]
                if direct_formats:
                    best_url = max(direct_formats, key=lambda x: x.get('height', 0) or 0)['url']
                else:
                    best_url = formats[0]['url']
            
            # Get uploader - try yt-dlp fields first, then extract from URL
            uploader = info.get('uploader') or info.get('channel') or info.get('creator')
            if not uploader:
                uploader = extract_username_from_url(url)
            
            return {
                'success': True,
                'mediaUrl': best_url,
                'mediaType': 'video',
                'title': info.get('title') or info.get('description', '')[:50] or 'Snapchat Video',
                'duration': info.get('duration'),
                'thumbnail': info.get('thumbnail'),
                'uploader': uploader,
                'view_count': info.get('view_count'),
                'upload_date': info.get('upload_date'),
                'description': info.get('description'),
                'formats': formats,
            }
            
    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)
        if 'Video unavailable' in error_msg:
            return {'success': False, 'error': 'This video is unavailable or private'}
        elif 'Unable to extract' in error_msg:
            return {'success': False, 'error': 'Could not extract video. The URL might be invalid or the video is private.'}
        else:
            return {'success': False, 'error': f'Download error: {error_msg}'}
    except Exception as e:
        app.logger.error(f"Extraction error: {e}")
        return {'success': False, 'error': f'Failed to extract video: {str(e)}'}

# ============== API Routes ==============

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    ffmpeg_available = get_ffmpeg_path() is not None
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'ffmpeg_available': ffmpeg_available,
        'temp_dir': TEMP_DIR,
    })

@app.route('/api/test-connection', methods=['GET'])
def test_connection():
    """Test connection endpoint for frontend."""
    return jsonify({
        'success': True,
        'message': 'Backend is running',
        'version': '1.0.0',
    })

@app.route('/api/extract', methods=['POST'])
def extract_video():
    """Extract video information from Snapchat URL."""
    try:
        data = request.get_json()
        
        if not data or 'url' not in data:
            return jsonify({'success': False, 'error': 'URL is required'}), 400
        
        url = data['url'].strip()
        
        if not url:
            return jsonify({'success': False, 'error': 'URL cannot be empty'}), 400
        
        if not is_valid_snapchat_url(url):
            return jsonify({'success': False, 'error': 'Please enter a valid Snapchat URL'}), 400
        
        result = extract_video_info(url)
        
        if result['success']:
            return jsonify(result)
        else:
            return jsonify(result), 400
            
    except Exception as e:
        app.logger.error(f"Extract API error: {e}")
        return jsonify({'success': False, 'error': 'An unexpected error occurred'}), 500

@app.route('/api/formats', methods=['POST'])
def get_formats():
    """Get available video formats."""
    try:
        data = request.get_json()
        
        if not data or 'url' not in data:
            return jsonify({'success': False, 'error': 'URL is required'}), 400
        
        url = data['url'].strip()
        result = extract_video_info(url)
        
        if result['success']:
            return jsonify({
                'success': True,
                'formats': result.get('formats', []),
                'title': result.get('title', 'Snapchat Video'),
            })
        else:
            return jsonify(result), 400
            
    except Exception as e:
        app.logger.error(f"Formats API error: {e}")
        return jsonify({'success': False, 'error': 'An unexpected error occurred'}), 500

@app.route('/api/download', methods=['GET'])
def download_video():
    """Proxy download video file."""
    try:
        video_url = request.args.get('url')
        filename = request.args.get('filename', 'snapchat_video.mp4')
        original_url = request.args.get('original_url', '')
        
        if not video_url:
            return jsonify({'success': False, 'error': 'Video URL is required'}), 400
        
        # Set headers for the request
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': '*/*',
            'Accept-Encoding': 'identity',
            'Referer': original_url or 'https://www.snapchat.com/',
        }
        
        # Stream the video content
        response = requests.get(video_url, headers=headers, stream=True, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        
        # Get content type and length
        content_type = response.headers.get('Content-Type', 'video/mp4')
        content_length = response.headers.get('Content-Length')
        
        def generate():
            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                if chunk:
                    yield chunk
        
        # Build response headers
        headers = {
            'Content-Type': content_type,
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Access-Control-Expose-Headers': 'Content-Disposition',
        }
        
        if content_length:
            headers['Content-Length'] = content_length
        
        return Response(
            generate(),
            headers=headers,
            mimetype=content_type
        )
        
    except requests.exceptions.RequestException as e:
        app.logger.error(f"Download request error: {e}")
        return jsonify({'success': False, 'error': 'Failed to download video'}), 500
    except Exception as e:
        app.logger.error(f"Download API error: {e}")
        return jsonify({'success': False, 'error': 'An unexpected error occurred'}), 500

@app.route('/api/convert', methods=['POST'])
def convert_video():
    """Convert M3U8/HLS stream to MP4."""
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({'success': False, 'error': 'Request data is required'}), 400
        
        video_url = data.get('url', '')
        original_url = data.get('originalUrl', '')
        filename = data.get('filename', 'snapchat_video.mp4')
        
        if not video_url:
            return jsonify({'success': False, 'error': 'Video URL is required'}), 400
        
        # Check if FFmpeg is available
        ffmpeg_path = get_ffmpeg_path()
        if not ffmpeg_path:
            return jsonify({
                'success': False, 
                'error': 'FFmpeg is not installed. Please install FFmpeg to convert HLS streams.'
            }), 500
        
        # Generate unique file ID
        file_id = str(uuid.uuid4())
        output_path = os.path.join(TEMP_DIR, f"{file_id}.mp4")
        
        # Ensure filename ends with .mp4
        if not filename.endswith('.mp4'):
            filename = filename.rsplit('.', 1)[0] + '.mp4'
        
        # Build FFmpeg command
        cmd = [
            ffmpeg_path,
            '-y',  # Overwrite output
            '-i', video_url,  # Input URL
            '-c', 'copy',  # Copy streams without re-encoding
            '-bsf:a', 'aac_adtstoasc',  # Fix AAC audio
            '-movflags', '+faststart',  # Enable fast start for web playback
            output_path
        ]
        
        # Add headers if needed
        if original_url:
            cmd.insert(1, '-headers')
            cmd.insert(2, f'Referer: {original_url}\r\nUser-Agent: Mozilla/5.0\r\n')
        
        # Run FFmpeg
        app.logger.info(f"Running FFmpeg conversion for {file_id}")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300  # 5 minute timeout
        )
        
        if result.returncode != 0:
            app.logger.error(f"FFmpeg error: {result.stderr}")
            
            # Try with re-encoding as fallback
            cmd_reencode = [
                ffmpeg_path,
                '-y',
                '-i', video_url,
                '-c:v', 'libx264',
                '-c:a', 'aac',
                '-preset', 'fast',
                '-movflags', '+faststart',
                output_path
            ]
            
            if original_url:
                cmd_reencode.insert(1, '-headers')
                cmd_reencode.insert(2, f'Referer: {original_url}\r\nUser-Agent: Mozilla/5.0\r\n')
            
            result = subprocess.run(
                cmd_reencode,
                capture_output=True,
                text=True,
                timeout=600  # 10 minute timeout for re-encoding
            )
            
            if result.returncode != 0:
                return jsonify({
                    'success': False,
                    'error': 'Video conversion failed. Please try a different quality.'
                }), 500
        
        if not os.path.exists(output_path):
            return jsonify({'success': False, 'error': 'Conversion failed - output file not created'}), 500
        
        return jsonify({
            'success': True,
            'fileId': file_id,
            'filename': filename,
            'message': 'Video converted successfully'
        })
        
    except subprocess.TimeoutExpired:
        return jsonify({'success': False, 'error': 'Conversion timed out. The video might be too long.'}), 500
    except Exception as e:
        app.logger.error(f"Convert API error: {e}")
        return jsonify({'success': False, 'error': f'Conversion failed: {str(e)}'}), 500

@app.route('/api/download-converted/<file_id>', methods=['GET'])
def download_converted(file_id):
    """Download a converted video file."""
    try:
        # Validate file_id format (UUID)
        try:
            uuid.UUID(file_id)
        except ValueError:
            return jsonify({'success': False, 'error': 'Invalid file ID'}), 400
        
        file_path = os.path.join(TEMP_DIR, f"{file_id}.mp4")
        
        if not os.path.exists(file_path):
            return jsonify({'success': False, 'error': 'File not found or expired'}), 404
        
        # Get filename from query param or use default
        filename = request.args.get('filename', f'snapchat_video_{file_id[:8]}.mp4')
        
        return send_file(
            file_path,
            mimetype='video/mp4',
            as_attachment=True,
            download_name=filename
        )
        
    except Exception as e:
        app.logger.error(f"Download converted API error: {e}")
        return jsonify({'success': False, 'error': 'Failed to download file'}), 500

@app.route('/api/info', methods=['GET'])
def api_info():
    """Get API information and capabilities."""
    return jsonify({
        'name': 'SnapDownloader API',
        'version': '1.0.0',
        'description': 'Snapchat Video Downloader Backend',
        'endpoints': {
            '/health': 'GET - Health check',
            '/api/test-connection': 'GET - Test connection',
            '/api/extract': 'POST - Extract video info',
            '/api/formats': 'POST - Get video formats',
            '/api/download': 'GET - Download video',
            '/api/convert': 'POST - Convert M3U8 to MP4',
            '/api/download-converted/<file_id>': 'GET - Download converted file',
        },
        'supported_urls': [
            'snapchat.com/spotlight/*',
            'snapchat.com/@username/spotlight/*',
            'story.snapchat.com/*',
            'snapchat.com/story/*',
            't.snapchat.com/*',
            'web.snapchat.com/*',
        ],
        'ffmpeg_available': get_ffmpeg_path() is not None,
    })

# ============== Error Handlers ==============

@app.errorhandler(404)
def not_found(e):
    return jsonify({'success': False, 'error': 'Endpoint not found'}), 404

@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({'success': False, 'error': 'Method not allowed'}), 405

@app.errorhandler(500)
def internal_error(e):
    return jsonify({'success': False, 'error': 'Internal server error'}), 500

# ============== Main ==============

if __name__ == '__main__':
    # Start cleanup thread
    start_cleanup_thread()
    
    # Get port from environment or use default
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('DEBUG', 'false').lower() == 'true'
    
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║               SnapDownloader Backend v1.0.0                  ║
╠══════════════════════════════════════════════════════════════╣
║  Server running at: http://localhost:{port}                    ║
║  FFmpeg available: {str(get_ffmpeg_path() is not None).ljust(40)}║
║  Temp directory: {TEMP_DIR[:42].ljust(42)}║
╚══════════════════════════════════════════════════════════════╝
    """)
    
    app.run(host='0.0.0.0', port=port, debug=debug)
