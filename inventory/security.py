"""Protezioni in più rispetto a quelle di Django, valide per tutte le pagine.

- `RequestSizeLimitMiddleware`: una richiesta più grande di quanto l'app accetti mai (una foto da
  MAX_UPLOAD_MB più il resto del modulo) viene rifiutata subito, prima di leggerla. Senza, chiunque,
  anche senza login, potrebbe mandare file enormi che Django scriverebbe su disco prima di rispondere.
- `ContentSecurityPolicyMiddleware`: dice al browser di eseguire solo gli script dell'app stessa e di
  non caricare nulla da altri siti. Se un giorno qualcuno riuscisse a infilare del codice in una
  pagina (XSS), il browser non lo eseguirebbe.
"""
from django.conf import settings
from django.http import HttpResponse

MARGIN_MB = 1  # spazio per gli altri campi del modulo oltre alla foto


class RequestSizeLimitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        limit = (settings.MAX_UPLOAD_MB + MARGIN_MB) * 1024 * 1024
        try:
            length = int(request.META.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        if length > limit:
            return HttpResponse(
                f"Richiesta troppo grande: il massimo è {settings.MAX_UPLOAD_MB} MB.",
                status=413,
                content_type="text/plain; charset=utf-8",
            )
        return self.get_response(request)


class ContentSecurityPolicyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        policy = getattr(settings, "CONTENT_SECURITY_POLICY", "")
        if policy and "Content-Security-Policy" not in response:
            response["Content-Security-Policy"] = policy
        return response
