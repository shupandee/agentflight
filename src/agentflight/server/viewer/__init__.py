"""Server-rendered run viewer (Jinja2 + htmx) under ``/ui``.

The viewer reads through the same service functions and response models as the public
``/v1`` API, so another frontend could be built on the API alone. Everything a run recorded
is untrusted input: templates autoescape everything, nothing is marked safe, and pages are
served with a Content-Security-Policy that allows no inline script.
"""

from agentflight.server.viewer.routes import install_viewer

__all__ = ["install_viewer"]
