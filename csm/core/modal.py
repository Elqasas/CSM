"""Forms that open in a dialog on the page they were started from.

A link marked ``data-modal`` loads its page into ``#form-modal`` through HTMX
instead of navigating. Nothing about the view changes: the same URL, the same
form, the same validation. Only two things differ when the request is aimed at
the dialog, and both live here rather than in each view:

- the template extends ``_modal.html`` instead of ``base.html`` - the context
  processor below supplies which, and a converted template extends
  ``{{ base_template }}``;
- a successful save, which every view answers with a redirect, becomes
  "refresh the page you are on". An XHR would otherwise follow the redirect and
  paint a whole page inside the dialog. Refreshing rather than patching is
  deliberate: saving a CSAT or completing a call rescores the account on
  commit, and a partial update would leave the old score showing beside it.

Opened in a new tab, or with JavaScript off, the link is an ordinary page.
"""

from __future__ import annotations

from django.http import HttpResponse

#: The id of the dialog's content element. HTMX sends the target's id as the
#: ``HX-Target`` header, which is how a request says it is for the dialog - a
#: bare ``HX-Request`` also comes from the delete confirmations and the
#: contact-options fragment, which must be left alone.
MODAL_TARGET = "form-modal-content"


def is_modal(request) -> bool:
    return request.headers.get("HX-Target") == MODAL_TARGET


def base_template(request):
    """Context processor: the layout a converted page extends."""
    return {"base_template": "_modal.html" if is_modal(request) else "base.html"}


class ModalMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if is_modal(request) and response.status_code in (301, 302, 303):
            refresh = HttpResponse(status=204)
            refresh["HX-Refresh"] = "true"
            return refresh
        return response
