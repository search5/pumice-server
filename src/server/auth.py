import secrets

def generate_token() -> str:
    # 256-bit token (32 bytes -> 64 hex chars)
    return secrets.token_hex(32)
