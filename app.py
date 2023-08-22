"""Demo PDS app."""
from datetime import datetime, timedelta
import json
import logging
import os
from pathlib import Path
from urllib.parse import urljoin

from Crypto.PublicKey import ECC
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from flask import Flask, make_response, redirect, request
import google.cloud.logging
from google.cloud import ndb
import lexrpc.flask_server
import requests

from arroba import jwt_monkeypatch as jwt

logger = logging.getLogger(__name__)
logging.basicConfig()
logging.getLogger().setLevel(logging.DEBUG)

for module in ('google.cloud', 'oauthlib', 'requests', 'requests_oauthlib',
               'urllib3'):
  logging.getLogger(module).setLevel(logging.INFO)

from arroba.repo import Repo
from arroba import server
from arroba.datastore_storage import DatastoreStorage
from arroba import xrpc_repo, xrpc_server, xrpc_sync

USER_AGENT = 'Arroba PDS (https://arroba-pds.appspot.com/)'

os.environ.setdefault('APPVIEW_HOST', 'api.bsky-sandbox.dev')
os.environ.setdefault('BGS_HOST', 'bgs.bsky-sandbox.dev')
os.environ.setdefault('PLC_HOST', 'plc.bsky-sandbox.dev')
os.environ.setdefault('PDS_HOST', open('pds_host').read().strip())
# Alternative: include these as env vars in app.yaml
# https://cloud.google.com/appengine/docs/standard/python/config/appref#Python_app_yaml_Includes
os.environ.setdefault('REPO_PRIVKEY', open('privkey.pem').read().strip())
os.environ.setdefault('REPO_PASSWORD', open('repo_password').read().strip())
os.environ.setdefault('REPO_TOKEN', open('repo_token').read().strip())

did_docs = list(Path(__file__).parent.glob('did:plc:*.json'))
assert len(did_docs) == 1, f'Expected one DID doc file; got {did_docs}'
os.environ.setdefault('REPO_DID', did_docs[0].name.removesuffix('.json'))
with open(did_docs[0]) as f:
  handle = json.load(f)['alsoKnownAs'][0].removeprefix('at://').strip('/')
os.environ.setdefault('REPO_HANDLE', handle)

# https://cloud.google.com/appengine/docs/flexible/python/runtime#environment_variables
is_prod = 'GAE_INSTANCE' in os.environ
if is_prod:
    # prod App Engine
    logging_client = google.cloud.logging.Client()
    logging_client.setup_logging(log_level=logging.DEBUG)
else:
    # local
    os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = os.path.join(
        os.path.dirname(__file__), 'fake_user_account.json')
    os.environ.setdefault('CLOUDSDK_CORE_PROJECT', 'app')
    os.environ.setdefault('DATASTORE_DATASET', 'app')
    os.environ.setdefault('GOOGLE_CLOUD_PROJECT', 'app')
    os.environ.setdefault('DATASTORE_EMULATOR_HOST', 'localhost:8089')

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ['REPO_TOKEN']
app.json.compact = False

# https://atproto.com/specs/xrpc#inter-service-authentication-temporary-specification
# https://atproto.com/specs/cryptography
privkey_bytes = server.key = load_pem_private_key(
    os.environ['REPO_PRIVKEY'].encode(), password=None)
jwt_raw = {
    'iss': os.environ['REPO_DID'],
    'aud': f'did:web:{os.environ["APPVIEW_HOST"]}',
    'alg': 'ES256',  # p256
    'exp': int((datetime.now() + timedelta(days=7)).timestamp()),  # 😎
}
APPVIEW_JWT = jwt.encode(jwt_raw, privkey_bytes, algorithm='ES256')
APPVIEW_HEADERS = {
      'User-Agent': USER_AGENT,
      'Authorization': f'Bearer {APPVIEW_JWT}',
}

@app.route('/xrpc/app.bsky.actor.getPreferences', methods=['OPTIONS'])
def options_preferences():
    return '', lexrpc.flask_server.RESPONSE_HEADERS

@app.get('/xrpc/app.bsky.actor.getPreferences')
def get_preferences():
    return {
        'preferences': [],
    }, lexrpc.flask_server.RESPONSE_HEADERS

@app.post('/xrpc/app.bsky.actor.putPreferences')
def put_preferences():
    return {}, lexrpc.flask_server.RESPONSE_HEADERS

# proxy all other app.bsky.* XRPCs to sandbox AppView
# https://atproto.com/blog/federation-developer-sandbox#bluesky-app-view
@app.route(f'/xrpc/app.bsky.<nsid_rest>', methods=['OPTIONS'])
def cors_preflight(nsid_rest=None):
    return '', lexrpc.flask_server.RESPONSE_HEADERS

@app.route(f'/xrpc/com.atproto.identity.resolveHandle', methods=['GET', 'POST'])
@app.route(f'/xrpc/app.bsky.<nsid_rest>', methods=['GET', 'POST'])
def proxy_appview(nsid_rest=None):
    logger.info(f'JWT raw: {jwt_raw}')
    url = urljoin('https://' + os.environ['APPVIEW_HOST'], request.full_path)
    logger.info(f'requests.{request.method} {url} {APPVIEW_HEADERS}')
    resp = requests.request(request.method, url, headers=APPVIEW_HEADERS)
    logger.info(f'Received {resp.status_code}: {"" if resp.ok else resp.text[:500]}')
    logger.info(resp.json())
    return resp.content, resp.status_code, {
      **lexrpc.flask_server.RESPONSE_HEADERS,
      **resp.headers,
    }

lexrpc.flask_server.init_flask(server.server, app)

server.key = ECC.import_key(os.environ['REPO_PRIVKEY'])

ndb_client = ndb.Client()

with ndb_client.context():
    server.storage = DatastoreStorage()
    if is_prod:
        server.repo = server.storage.load_repo(did=os.environ['REPO_DID'])
    else:
        server.repo = Repo.create(server.storage, os.environ['REPO_DID'],
                                  server.key, handle=os.environ['REPO_HANDLE'])

server.repo.callback = xrpc_sync.enqueue_commit
if server.repo.handle != os.environ['REPO_HANDLE']:
    logger.warning(f"$REPO_HANDLE is {os.environ['REPO_HANDLE']} but loaded repo's handle is {server.repo.handle} !")

def ndb_context_middleware(wsgi_app):
    """WSGI middleware to add an NDB context per request.

    Copied from oauth_dropins.webutil.flask_util.
    """
    def wrapper(environ, start_response):
        with ndb_client.context():
            return wsgi_app(environ, start_response)

    return wrapper


app.wsgi_app = ndb_context_middleware(app.wsgi_app)


@app.get('/liveness_check')
@app.get('/readiness_check')
def health_check():
  """App Engine Flex health checks.

  https://cloud.google.com/appengine/docs/flexible/reference/app-yaml?tab=python#updated_health_checks
  """
  return 'OK'


@app.get('/')
def homepage():
  return """\
<!DOCTYPE html>
<html>
<body>
<h1>Arroba PDS</h1>
<p>This is a demo <a href="https://atproto.com/">AT Protocol</a> <a href="https://atproto.com/blog/federation-developer-sandbox">federation sandbox</a> <a href="https://atproto.com/guides/applications#applications-model">PDS</a> based on <a href="https://github.com/snarfed/arroba">arroba</a>.</p>
</body>
</html>
"""
