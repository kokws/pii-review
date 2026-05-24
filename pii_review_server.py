# pii_review_server.py — Standalone Flask server for PII Review.
# Serves the FE at /pii_review and exposes the analyze/apply BE under
# /api/pii-review/*. Local-only by design; no public tunneling.
import logging
import os
import socket
import sys
import time
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler

from dotenv import load_dotenv
from flask import Flask, redirect, url_for
from waitress import serve

from modules.pii_review import PiiReview

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Env file: load .env from project root if present. PII_REVIEW_ENV_PATH
# overrides for non-standard deployments. See .env.example for keys.
ENV_PATH = os.environ.get(
    "PII_REVIEW_ENV_PATH",
    os.path.join(BASE_DIR, '.env'),
)
LOG_BASENAME = "pii_review"
LOG_DIR = os.path.join(BASE_DIR, 'logs')

# Poppler PATH prepend — the viz_data extractor calls `pdftotext -bbox-layout`
# (poppler-only flag). On Windows + git-bash, an xpdf-derived pdftotext often
# wins PATH and silently produces zero words (it has no -bbox-layout flag),
# which corrupts viz_data.lite.json. If POPPLER_BIN_DIR is set, prepend it
# so the real poppler binaries win the PATH lookup. On Linux/macOS this is
# usually unnecessary — poppler is the standard pdftotext.
_POPPLER_BIN = os.getenv('POPPLER_BIN_DIR', '').strip()
if _POPPLER_BIN and os.path.isdir(_POPPLER_BIN):
    os.environ['PATH'] = _POPPLER_BIN + os.pathsep + os.environ.get('PATH', '')

# Load environment variables
load_dotenv(ENV_PATH)
PII_REVIEW_PORT = int(os.getenv("PII_REVIEW_PORT", "5000"))
FLASK_SERVER_HOST = os.getenv("FLASK_SERVER_HOST", "localhost")

COMPUTER_NAME = socket.gethostname()


class SGTRotatingHandler(TimedRotatingFileHandler):
    """Singapore Time (UTC+8) daily log rotation. Override the rollover
    boundary so log files cut at SGT midnight regardless of host TZ."""
    def __init__(self, filename, **kwargs):
        self.utc_offset = 8 * 3600  # SGT = UTC+8
        os.makedirs(LOG_DIR, exist_ok=True)
        super().__init__(
            filename=os.path.join(LOG_DIR, f"{filename}.log"),
            when='midnight',
            interval=1,
            utc=True,
            backupCount=7,
        )

    def computeRollover(self, currentTime):
        return super().computeRollover(currentTime - self.utc_offset)

    def getRolloverFileName(self, default_name):
        sgt_date = time.strftime("%Y-%m-%d", time.gmtime(self.rolloverAt + self.utc_offset))
        return f"{self.baseFilename}.{sgt_date}"

    def getFilesToDelete(self):
        return []


class PiiReviewServer:
    def __init__(
        self,
        flask_port: int = PII_REVIEW_PORT,
        flask_host: str = FLASK_SERVER_HOST,
        debug: bool = False,
    ):
        self.flask_port = flask_port
        self.flask_host = flask_host
        self.debug = debug

        self.log = logging.getLogger("PiiReview")
        self.log.setLevel(logging.INFO)
        handler = SGTRotatingHandler(LOG_BASENAME)
        handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        ))
        self.log.addHandler(handler)
        self.log.info("=" * 80)
        self.log.info(f"PII Review server initializing on {COMPUTER_NAME}")
        self.log.info(f"Port: {self.flask_port}   Host: {self.flask_host}")
        self.log.info("=" * 80)

        self.app = Flask(__name__, static_folder='static', template_folder='templates')
        self.app.config['TEMPLATES_AUTO_RELOAD'] = True
        self.app.jinja_env.auto_reload = True
        self.app.secret_key = os.urandom(24)

        sys.excepthook = self.handle_exception

        # Root → redirect to the PII Review UI.
        @self.app.route('/')
        @self.app.route('/home')
        def root_redirect():
            return redirect(url_for('pii_review_page')) if 'pii_review_page' in self.app.view_functions else redirect('/pii_review')

        self.server_start_time = datetime.now()

        self.pii_review = PiiReview(self.app, self.log, BASE_DIR)
        self.pii_review.add_routes()

    def start_server(self):
        try:
            self.log.info(f"Starting waitress server on {self.flask_host}:{self.flask_port}")
            serve(self.app, host=self.flask_host, port=self.flask_port)
        except Exception as e:
            self.log.critical(f"Server failed to start: {e}")
            sys.exit(1)

    def handle_exception(self, exc_type, exc_value, exc_traceback):
        self.log.error("Unhandled exception", exc_info=(exc_type, exc_value, exc_traceback))


if __name__ == "__main__":
    server = PiiReviewServer()
    server.start_server()
