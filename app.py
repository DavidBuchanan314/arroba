"""Demo PDS app."""
import logging
import os
from urllib.parse import urljoin

from flask import make_response, redirect, request
import google.cloud.logging

logger = logging.getLogger(__name__)
logging.basicConfig()
logging.getLogger().setLevel(logging.DEBUG)
for logger in ('google.cloud', 'oauthlib', 'requests', 'requests_oauthlib',
               'urllib3'):
  logging.getLogger(logger).setLevel(logging.INFO)
# logging.getLogger('lexrpc').setLevel(logging.INFO)

from flask import Flask
from google.cloud import ndb
import lexrpc.flask_server

from arroba import server
from arroba import xrpc_identity, xrpc_repo, xrpc_server, xrpc_sync

os.environ.setdefault('APPVIEW_HOST', 'api.bsky-sandbox.dev')
os.environ.setdefault('BGS_HOST', 'bgs.bsky-sandbox.dev')
os.environ.setdefault('PLC_HOST', 'plc.bsky-sandbox.dev')
os.environ.setdefault('REPO_DID', open('repo_did').read().strip())
os.environ.setdefault('REPO_HANDLE', open('repo_handle').read().strip())
if os.environ.get('GAE_ENV') == 'standard':
    os.environ.setdefault('REPO_PASSWORD', open('repo_password').read().strip())
    os.environ.setdefault('REPO_TOKEN', open('repo_token').read().strip())

    logging_client = google.cloud.logging.Client()
    logging_client.setup_logging(log_level=logging.DEBUG)
else:
    os.environ.setdefault('REPO_PASSWORD', 'sooper-sekret')
    os.environ.setdefault('REPO_TOKEN', 'towkin')


app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ['REPO_TOKEN']
app.json.compact = False

# redirect app.bsky.* XRPCs to sandbox AppView
# https://atproto.com/blog/federation-developer-sandbox#bluesky-app-view
#
# WARNING: this only works for GETs, but we're doing it for POSTs too. should be
# ok as long as client apps don't send us app.bsky POSTs. we'll see.
@app.route(f'/xrpc/app.bsky.<nsid_rest>', methods=['GET', 'OPTIONS'])
def proxy_appview(nsid_rest=None):
    if request.method == 'GET':
        resp = redirect(urljoin('https://' + os.environ['APPVIEW_HOST'],
                                request.full_path))
    else:
        resp = make_response('')

    resp.headers.update(lexrpc.flask_server.RESPONSE_HEADERS)
    return resp

server.init()
lexrpc.flask_server.init_flask(server.server, app)


ndb_client = ndb.Client()

def ndb_context_middleware(wsgi_app):
    """WSGI middleware to add an NDB context per request.

    Copied from oauth_dropins.webutil.flask_util.
    """
    def wrapper(environ, start_response):
        with ndb_client.context():
            return wsgi_app(environ, start_response)

    return wrapper


app.wsgi_app = ndb_context_middleware(app.wsgi_app)
