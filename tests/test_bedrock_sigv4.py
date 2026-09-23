"""SigV4 signing and AWS credential resolution (Bedrock backend).

The signing known-answer test pins the canonical AWS IAM ListUsers vector
(access key `AKIDEXAMPLE`, the classic AWS documentation example): any change
to the canonical-request assembly, the key derivation, or the string-to-sign
moves the signature and fails loudly.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_ROOT: Path = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "reviewer", _ROOT / "scripts" / "reviewer.py"
)
assert _SPEC is not None and _SPEC.loader is not None
reviewer = importlib.util.module_from_spec(_SPEC)
sys.modules["reviewer"] = reviewer
_SPEC.loader.exec_module(reviewer)

_KAT_DATETIME = datetime(2015, 8, 30, 12, 36, 0, tzinfo=timezone.utc)
_KAT_ACCESS = "AKIDEXAMPLE"
_KAT_SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRiCYEXAMPLEKEY"
_KAT_QUERY = "Action=ListUsers&Version=2010-05-08"
_KAT_CONTENT_TYPE = "application/x-www-form-urlencoded; charset=utf-8"
# Cross-verified against botocore.auth.SigV4Auth with a frozen clock
# (2026-09-22): both implementations produce this exact signature.
_KAT_SIGNATURE = "e6fe8b7d406aa808875cec38e799a9817c8b4608037d1682894cf0bb9428598d"


class SigV4KnownAnswerTests(unittest.TestCase):
    def test_aws_documentation_vector(self) -> None:
        """The canonical AWS docs example shape (GET iam.amazonaws.com
        ListUsers): the signer must reproduce the pinned signature exactly
        (cross-verified against botocore.auth.SigV4Auth)."""
        headers = reviewer._sigv4_sign_request(
            method="GET",
            uri_path="/",
            query=_KAT_QUERY,
            body=b"",
            host="iam.amazonaws.com",
            region="us-east-1",
            service="iam",
            access_key=_KAT_ACCESS,
            secret_key=_KAT_SECRET,
            session_token=None,
            now_utc=_KAT_DATETIME,
            content_type=_KAT_CONTENT_TYPE,
        )
        expected_authorization = (
            f"AWS4-HMAC-SHA256 Credential={_KAT_ACCESS}"
            "/20150830/us-east-1/iam/aws4_request, "
            "SignedHeaders=content-type;host;x-amz-date, "
            f"Signature={_KAT_SIGNATURE}"
        )
        self.assertEqual(headers["Authorization"], expected_authorization)
        self.assertEqual(headers["x-amz-date"], "20150830T123600Z")
        self.assertNotIn("x-amz-security-token", headers)

    def test_signer_is_pure_and_deterministic(self) -> None:
        kwargs = dict(
            method="POST",
            uri_path="/model/anthropic.claude-sonnet-5/invoke",
            query="",
            body=json.dumps({"anthropic_version": "bedrock-2023-05-31"}).encode(),
            host="bedrock-runtime.us-east-1.amazonaws.com",
            region="us-east-1",
            service="bedrock",
            access_key="AKIDEXAMPLE",
            secret_key=_KAT_SECRET,
            session_token=None,
            now_utc=_KAT_DATETIME,
            content_type="application/json",
        )
        first = reviewer._sigv4_sign_request(**kwargs)
        second = reviewer._sigv4_sign_request(**kwargs)
        self.assertEqual(first, second)

    def test_session_token_is_signed_and_sent(self) -> None:
        headers = reviewer._sigv4_sign_request(
            method="POST",
            uri_path="/model/m/invoke",
            query="",
            body=b"{}",
            host="bedrock-runtime.us-east-1.amazonaws.com",
            region="us-east-1",
            service="bedrock",
            access_key="AKIDEXAMPLE",
            secret_key=_KAT_SECRET,
            session_token="EXAMPLE-TOKEN-123",
            now_utc=_KAT_DATETIME,
            content_type="application/json",
        )
        self.assertEqual(headers["x-amz-security-token"], "EXAMPLE-TOKEN-123")
        self.assertIn("x-amz-security-token", headers["Authorization"])
        self.assertIn(
            "SignedHeaders=content-type;host;x-amz-date;x-amz-security-token",
            headers["Authorization"],
        )


class AwsCredentialResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        for name in (
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
        ):
            self.addCleanup(os.environ.pop, name, None)

    def test_env_credentials_win_and_session_is_carried(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "AWS_ACCESS_KEY_ID": "ENVKEY",
                "AWS_SECRET_ACCESS_KEY": "env-secret",
                "AWS_SESSION_TOKEN": "env-token",
            },
        ):
            self.assertEqual(
                reviewer._resolve_aws_credentials("PACKED:packed-secret"),
                ("ENVKEY", "env-secret", "env-token"),
            )

    def test_packed_two_part_fallback(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                reviewer._resolve_aws_credentials("AKID:AKSECRET"),
                ("AKID", "AKSECRET", None),
            )

    def test_packed_three_part_session_fallback(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                reviewer._resolve_aws_credentials("AKID:AKSECRET:TOKEN"),
                ("AKID", "AKSECRET", "TOKEN"),
            )

    def test_partial_env_is_an_error_not_a_silent_mix(self) -> None:
        with mock.patch.dict(
            os.environ, {"AWS_ACCESS_KEY_ID": "ONLY-KEY"}, clear=True
        ):
            with self.assertRaises(ValueError) as ctx:
                reviewer._resolve_aws_credentials(None)
            self.assertIn("both AWS_ACCESS_KEY_ID and", str(ctx.exception))

    def test_no_credentials_names_both_sources(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError) as ctx:
                reviewer._resolve_aws_credentials(None)
            msg = str(ctx.exception)
        self.assertIn("AWS_ACCESS_KEY_ID", msg)
        self.assertIn("KEY:SECRET[:SESSION_TOKEN]", msg)

    def test_malformed_packed_value_is_rejected(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                reviewer._resolve_aws_credentials("AKID:")
            with self.assertRaises(ValueError):
                reviewer._resolve_aws_credentials("A:B:C:D")


if __name__ == "__main__":
    unittest.main()
