import os
import sys
import logging
import time
from dotenv import load_dotenv

# Automatically load the .env file
load_dotenv()

# Adjust sys.path to work around local imports
current_dir = os.path.dirname(os.path.abspath(__file__))
src_dir = os.path.dirname(current_dir)
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)
if src_dir not in sys.path:
    sys.path.insert(0, src_dir)

# Install the Twisted asyncioreactor
from twisted.internet import asyncioreactor
asyncioreactor.install()
from twisted.internet import reactor

from pumice_server.service import SyncServiceServicer
from pumice_server.grpc_web_resource import SyncServiceResource, RootResource
from twisted.web.wsgi import WSGIResource
from twisted.web.server import Site
from pumice_server.web import create_pyramid_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("pumice_server.main")

from twisted.internet import task

def clean_temp_files(data_dir: str, max_age_seconds=3600):
    tmp_dir = os.path.join(data_dir, "tmp")
    if not os.path.exists(tmp_dir):
        return
    
    now = time.time()
    count = 0
    for root, dirs, files in os.walk(tmp_dir):
        for file in files:
            file_path = os.path.join(root, file)
            try:
                stat = os.stat(file_path)
                age = now - stat.st_mtime
                if age > max_age_seconds:
                    os.remove(file_path)
                    count += 1
            except Exception:
                pass
    if count > 0:
        logger.info(f"Garbage Collector: Cleaned up {count} expired temporary files.")

def build_connection_url(db_type: str, data_dir: str) -> str:
    host = os.getenv("DB_HOST", "127.0.0.1")
    user = os.getenv("DB_USER", "")
    password = os.getenv("DB_PASSWORD", "")
    db_name = os.getenv("DB_NAME", "obsidian_sync")
    auth = f"{user}:{password}@" if user else ""

    if db_type == "sqlite":
        db_name_env = os.getenv("DB_NAME", "sync_metadata.db")
        db_path = db_name_env if os.path.isabs(db_name_env) else os.path.join(data_dir, db_name_env)
        return f"sqlite:///{db_path}"
    elif db_type in ("mysql", "mariadb"):
        port = os.getenv("DB_PORT", "3306")
        return f"mysql+pymysql://{auth}{host}:{port}/{db_name}"
    elif db_type == "postgresql":
        port = os.getenv("DB_PORT", "5432")
        return f"postgresql+psycopg2://{auth}{host}:{port}/{db_name}"
    elif db_type == "cubrid":
        port = os.getenv("DB_PORT", "33000")
        return f"cubrid://{auth}{host}:{port}/{db_name}"
    else:
        return ""


def main():
    import argparse
    from pumice_server.repository import SqlAlchemyMetadataRepository

    env_http_port = int(os.getenv("HTTP_PORT", "8080"))
    env_data_dir = os.path.expanduser(os.getenv("DATA_DIR", "~/.obsidian-sync-server"))
    env_db_type = os.getenv("DB_TYPE", "sqlite")

    parser = argparse.ArgumentParser(description="Obsidian gRPC Sync Server")
    parser.add_argument("--http-port", type=int, default=env_http_port, help="HTTP/Pyramid server port")
    parser.add_argument("--data-dir", type=str, default=env_data_dir, help="Data directory")
    parser.add_argument("--db-type", type=str, default=env_db_type,
                        choices=["sqlite", "mysql", "mariadb", "postgresql", "cubrid"],
                        help="Database backend type")
    args = parser.parse_args()

    db_type = args.db_type.lower()
    url = build_connection_url(db_type, args.data_dir)
    repository = SqlAlchemyMetadataRepository(url)
    logger.info(f"Using SQLAlchemy metadata repository ({db_type}).")

    # An admin account must exist from the moment the server is deployed -- there's no
    # self-service signup (accounts are only ever created by an admin, via
    # POST /api/admin/users/create), so without this there would be no way to log in at all.
    admin_user = os.getenv("ADMIN_USER")
    admin_password = os.getenv("ADMIN_PASSWORD")
    if not admin_user or not admin_password:
        logger.error(
            "ADMIN_USER and ADMIN_PASSWORD must both be set before the server can start -- "
            "there is no other way to provision the first account. Set them in .env and restart."
        )
        sys.exit(1)

    existing = repository.get_user_by_username(admin_user)
    if not existing:
        import hashlib
        salt = os.urandom(16)
        key = hashlib.pbkdf2_hmac('sha256', admin_password.encode('utf-8'), salt, 100000)
        pw_hash = salt.hex() + ":" + key.hex()
        repository.create_user(
            username=admin_user,
            password_hash=pw_hash,
            name="System Admin",
            email=admin_user,
            is_admin=True
        )
        logger.info(f"Initialized admin user '{admin_user}' in database.")

    def startup():
        # Create the Pyramid WSGI app (web UI + admin/publish API only now -- the sync RPCs
        # are handled natively below, no WSGI bridge involved for those anymore)
        pyramid_app = create_pyramid_app(repository, args.data_dir)

        # Wrap Pyramid with the same CORS behavior the whole server used to get from the
        # single shared middleware, so the web UI's responses are unaffected by the split.
        class CORSWSGIMiddleware:
            def __init__(self, app):
                self.app = app
            def __call__(self, environ, start_response):
                if environ.get("REQUEST_METHOD") == "OPTIONS":
                    origin = environ.get("HTTP_ORIGIN", "*")
                    headers = [
                        ("Access-Control-Allow-Origin", origin),
                        ("Access-Control-Allow-Methods", "POST, GET, OPTIONS, PUT, DELETE"),
                        ("Access-Control-Allow-Headers", "authorization, content-type, x-grpc-web, x-user-agent, grpc-timeout, obs-token, obs-id, obs-path, obs-hash, x-device-name, x-user-name"),
                        ("Access-Control-Allow-Credentials", "true"),
                        ("Access-Control-Expose-Headers", "grpc-status, grpc-message"),
                        ("Content-Length", "0"),
                        ("Content-Type", "text/plain")
                    ]
                    start_response("204 No Content", headers)
                    return []
                def custom_start_response(status, headers, exc_info=None):
                    headers = [h for h in headers if not h[0].lower().startswith("access-control-")]
                    origin = environ.get("HTTP_ORIGIN", "*")
                    headers.extend([
                        ("Access-Control-Allow-Origin", origin),
                        ("Access-Control-Allow-Credentials", "true"),
                        ("Access-Control-Expose-Headers", "grpc-status, grpc-message")
                    ])
                    return start_response(status, headers, exc_info)
                return self.app(environ, custom_start_response)

        final_pyramid_app = CORSWSGIMiddleware(pyramid_app)

        # Pyramid/web-UI requests still go through the WSGI thread pool (each occupies one
        # thread for its duration). The sync RPCs no longer do -- SyncServiceResource drives
        # SyncServiceServicer's async methods directly off the reactor's event loop.
        reactor.suggestThreadPoolSize(50)
        pyramid_resource = WSGIResource(reactor, reactor.getThreadPool(), final_pyramid_app)

        servicer = SyncServiceServicer(data_dir=args.data_dir, repository=repository)
        sync_resource = SyncServiceResource(servicer)

        root_resource = RootResource(sync_resource, pyramid_resource)
        site = Site(root_resource)

        logger.info(f"Starting Pyramid HTTP + gRPC-Web server on port {args.http_port}...")
        reactor.listenTCP(args.http_port, site)

    reactor.callWhenRunning(startup)
    
    # Run the temp-file garbage collector (every 10 minutes)
    gc_task = task.LoopingCall(clean_temp_files, args.data_dir)
    gc_task.start(600, now=False)
    
    logger.info("Starting Twisted reactor...")
    reactor.run()

if __name__ == "__main__":
    main()
