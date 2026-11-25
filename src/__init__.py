import os

# Injected at image build time from the release tag (see Dockerfile / docker-image workflow).
__version__ = os.environ.get("APP_VERSION") or "dev"
