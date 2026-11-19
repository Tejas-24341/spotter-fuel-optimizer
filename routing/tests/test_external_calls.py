from __future__ import annotations

from django.http import HttpResponse
from django.test import RequestFactory, override_settings

from routing.middleware import ExternalCallCountMiddleware
from routing.services.external_calls import count_external_call


def test_debug_response_reports_request_scoped_external_call_count():
    request = RequestFactory().get("/api/route/")

    def get_response(_request):
        count_external_call()
        count_external_call()
        return HttpResponse("ok")

    with override_settings(DEBUG=True):
        response = ExternalCallCountMiddleware(get_response)(request)

    assert response["X-External-Calls"] == "2"


def test_external_call_count_header_is_not_exposed_when_debug_is_off():
    request = RequestFactory().get("/api/route/")
    middleware = ExternalCallCountMiddleware(lambda _request: HttpResponse("ok"))

    with override_settings(DEBUG=False):
        response = middleware(request)

    assert "X-External-Calls" not in response
