"""Testes de desencriptação AES-128 (HLS cifrado)."""
from __future__ import annotations

import os

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.core.downloader.crypto import AesKey, decrypt_segment, parse_iv


def encrypt(plaintext: bytes, key: bytes, iv: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    return encryptor.update(padded) + encryptor.finalize()


class TestParseIv:
    def test_hex_with_prefix(self) -> None:
        iv = parse_iv("0x000102030405060708090A0B0C0D0E0F")
        assert iv == bytes(range(16))

    def test_hex_without_prefix(self) -> None:
        assert parse_iv("000102030405060708090A0B0C0D0E0F") == bytes(range(16))

    def test_short_hex_is_left_padded(self) -> None:
        iv = parse_iv("0A0B0C0D0E0F101112131415161718")
        assert iv is not None
        assert len(iv) == 16
        assert iv == bytes.fromhex("00" + "0A0B0C0D0E0F101112131415161718")

    def test_none(self) -> None:
        assert parse_iv(None) is None
        assert parse_iv("") is None


class TestAesKey:
    def test_derived_iv_from_sequence(self) -> None:
        key = AesKey.from_material(os.urandom(16), None, sequence=5)
        assert len(key.iv) == 16
        assert key.iv[:8] == (5).to_bytes(8, "big")
        assert key.iv[8:] == b"\x00" * 8

    def test_explicit_iv(self) -> None:
        iv = bytes(range(16))
        key = AesKey.from_material(os.urandom(16), iv, sequence=0)
        assert key.iv == iv

    def test_invalid_key_length(self) -> None:
        try:
            AesKey.from_material(b"curto", None, 0)
        except Exception as exc:
            assert "16" in str(exc)
        else:
            raise AssertionError("deveria falhar com chave inválida")


class TestDecrypt:
    def test_roundtrip(self) -> None:
        key_bytes = os.urandom(16)
        iv = os.urandom(16)
        plaintext = b"conteudo de um segmento de video bem formatado" * 4
        encrypted = encrypt(plaintext, key_bytes, iv)
        key = AesKey.from_material(key_bytes, iv, 0)
        assert decrypt_segment(encrypted, key) == plaintext

    def test_empty_segment(self) -> None:
        key = AesKey.from_material(os.urandom(16), os.urandom(16), 0)
        assert decrypt_segment(b"", key) == b""
