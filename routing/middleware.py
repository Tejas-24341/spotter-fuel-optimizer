"""Debug-only response diagnostics for upstream HTTP attempts."""

from __future__ import annotations

import logging

from django.conf import settings

from routing.services.external_calls import (
    get_external_call_count,
    start_external_call_count,
    stop_external_call_count,
)

logger = logging.getLogger(__name__)


class ExternalCallCountMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not settings.DEBUG:
            return self.get_response(request)

        token = start_external_call_count()
        try:
            response = self.get_response(request)
            count = get_external_call_count()
            response["X-External-Calls"] = str(count)
            logger.debug("Upstream HTTP attempts for %s: %d", request.path, count)
            return response
        finally:
            stop_external_call_count(token)
