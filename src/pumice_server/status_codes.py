"""Replacement for grpc.StatusCode. PR5 (see pumice's 13_PR5_계획.md) drops the grpcio
dependency entirely now that every RPC method speaks wire_types, but service.py/
ws_sync_resource.py still need a small fixed set of named error categories for
context.abort(code, details) -- the RPC-style error signaling the codebase (and the WS
protocol's own error op) were built around. Only the members actually used anywhere in this
codebase are defined here -- add more if a future call site needs one, rather than
reproducing grpc's full ~17-member StatusCode enum speculatively.
"""

from enum import Enum


class StatusCode(Enum):
    OK = "OK"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    NOT_FOUND = "NOT_FOUND"
    INTERNAL = "INTERNAL"
