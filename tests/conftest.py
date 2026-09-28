import os

# Keep API tests deterministic and independent from real RCON endpoints.
os.environ.setdefault("WARDOGS_DISABLE_BACKGROUND", "1")