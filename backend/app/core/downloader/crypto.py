"""Desencriptação AES-128 de segmentos HLS (``EXT-X-KEY``).

Suporta ``METHOD=AES-128`` (CBC) com IV explícito ou derivado do número de
sequência. ``SAMPLE-AES``/``SAMPLE-AES-CTR`` não são suportados (exigem
desencriptação por amostra, dependente do codec) — o caller é notificado.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ..exceptions import DownloadError

log = logging.getLogger(__name__)

SUPPORTED_METHODS = {"AES-128"}
UNSUPPORTED_METHODS = {"SAMPLE-AES", "SAMPLE-AES-CTR", "SAMPLE-AES-CENC"}


@dataclass(slots=True)
class AesKey:
    """Chave + IV para desencriptar segmentos."""

    key: bytes
    iv: bytes

    @classmethod
    def from_material(cls, key_bytes: bytes, iv: bytes | None, sequence: int) -> "AesKey":
        if len(key_bytes) != 16:
            raise DownloadError(f"Chave AES-128 inválida: {len(key_bytes)} bytes (esperado 16)")
        if iv is None:
            # IV derivado: media sequence number em big-endian (8 bytes) + 8 zeros
            iv = sequence.to_bytes(8, "big").ljust(16, b"\x00")
        if len(iv) != 16:
            raise DownloadError(f"IV AES-128 inválido: {len(iv)} bytes (esperado 16)")
        return cls(key=key_bytes, iv=iv)


def parse_iv(raw: str | None) -> bytes | None:
    """Converte ``IV=0x0123...`` em bytes."""
    if not raw:
        return None
    value = raw.strip()
    if value.lower().startswith("0x"):
        value = value[2:]
    try:
        return bytes.fromhex(value.zfill(32))
    except ValueError:
        return None


def decrypt_segment(data: bytes, aes_key: AesKey) -> bytes:
    """Desencripta um segmento AES-128-CBC, removendo o padding PKCS#7."""
    if not data:
        return data
    if len(data) % 16 != 0:
        # Alguns servidores devolvem segmentos não alinhados: tenta mesmo assim.
        log.warning("Segmento com tamanho não múltiplo de 16 (%d bytes)", len(data))
    cipher = Cipher(algorithms.AES(aes_key.key), modes.CBC(aes_key.iv))
    decryptor = cipher.decryptor()
    decrypted = decryptor.update(data) + decryptor.finalize()
    try:
        unpadder = padding.PKCS7(128).unpadder()
        return unpadder.update(decrypted) + unpadder.finalize()
    except ValueError:
        # Segmentos TS podem não ter padding válido -> devolve o bruto.
        return decrypted
