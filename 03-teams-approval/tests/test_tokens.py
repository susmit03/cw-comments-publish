from __future__ import annotations

import os
import sys
import time

import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, ".."))

from teams_cards import create_action_token, verify_action_token


def test_round_trip_token():
    secret = "abc123"
    payload = {"source_env": "dev", "target_env": "uat"}
    token = create_action_token(payload, secret, ttl_seconds=60)
    decoded = verify_action_token(token, secret)
    assert decoded["source_env"] == "dev"
    assert decoded["target_env"] == "uat"
    assert "exp" in decoded


def test_bad_signature_rejected():
    secret = "abc123"
    token = create_action_token({"a": 1}, secret, ttl_seconds=60)
    with pytest.raises(ValueError, match="signature"):
        verify_action_token(token, "wrong-secret")


def test_expired_token_rejected():
    secret = "abc123"
    token = create_action_token({"a": 1}, secret, ttl_seconds=0)
    time.sleep(0.1)
    with pytest.raises(ValueError, match="expired"):
        verify_action_token(token, secret)

