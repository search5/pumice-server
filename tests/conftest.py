import os
import sys

# The generated sync_pb2_grpc.py does `import sync_pb2` (unqualified, protoc's own
# convention) rather than a relative import -- main.py works around this by inserting the
# package directory into sys.path before importing anything that touches it. Tests need the
# same workaround the first time something under pumice_server.service (or any other module
# that transitively imports sync_pb2_grpc) is imported.
_package_dir = os.path.join(os.path.dirname(__file__), "..", "src", "pumice_server")
_package_dir = os.path.abspath(_package_dir)
if _package_dir not in sys.path:
    sys.path.insert(0, _package_dir)
