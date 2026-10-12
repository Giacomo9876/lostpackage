"""Elaborazione delle foto caricate dal telefono.

Una foto da smartphone pesa 4-10 MB, ha l'orientamento salvato nei metadati
EXIF e spesso contiene le coordinate GPS. Qui la foto viene ricodificata:
raddrizzata, ridimensionata, convertita in JPEG e privata dei metadati.
Ricodificare serve anche alla sicurezza: ciò che finisce su disco è sempre
un JPEG generato da noi, mai il file originale caricato.
"""
from io import BytesIO

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError

PHOTO_MAX_SIDE = 1600
THUMB_MAX_SIDE = 480


def _to_jpeg(img, max_side, quality):
    copy = img.copy()
    copy.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buffer = BytesIO()
    copy.save(buffer, "JPEG", quality=quality, optimize=True, progressive=True)
    return ContentFile(buffer.getvalue())


def process_upload(uploaded):
    """Ritorna (foto, miniatura) come ContentFile JPEG, oppure solleva ValidationError."""
    try:
        with Image.open(uploaded) as img:
            img.draft("RGB", (PHOTO_MAX_SIDE, PHOTO_MAX_SIDE))  # decodifica ridotta per i JPEG grandi
            img.load()
            img = ImageOps.exif_transpose(img)
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                img = img.convert("RGBA")
                flat = Image.new("RGB", img.size, "white")
                flat.paste(img, mask=img.getchannel("A"))
                img = flat
            else:
                img = img.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        raise ValidationError("Il file non è una foto leggibile. Scatta di nuovo o scegli una foto JPEG o PNG.")

    return _to_jpeg(img, PHOTO_MAX_SIDE, 82), _to_jpeg(img, THUMB_MAX_SIDE, 75)
