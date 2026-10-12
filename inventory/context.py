def scope(request):
    """Rende disponibile `scope` (ruolo e centro dell'utente) in tutti i template."""
    return {"scope": getattr(request, "scope", None)}
