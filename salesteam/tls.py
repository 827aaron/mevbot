"""Verified HTTPS for packaged apps, including macOS and Windows trust stores."""
import os
import ssl
import sys

import certifi


def context() -> ssl.SSLContext:
    # Frozen Python cannot rely on the build machine's OpenSSL CA paths.
    # Native trust also includes certificates installed by the computer owner.
    if sys.platform in ("darwin", "win32"):
        import truststore

        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ca_file = os.environ.get("SSL_CERT_FILE") or None
        ca_dir = os.environ.get("SSL_CERT_DIR") or None
        if ca_file or ca_dir:
            ctx.load_verify_locations(cafile=ca_file, capath=ca_dir)
    else:
        ctx = ssl.create_default_context()
    ctx.load_verify_locations(cafile=certifi.where())
    return ctx
