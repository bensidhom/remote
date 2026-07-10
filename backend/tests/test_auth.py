import os
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.auth import (
    AuthMiddleware,
    AuthUser,
    _required_role,
    issue_session,
    role_for_email,
    router,
    verify_cloudflare_request,
    verify_session,
)


ENV = {
    "BRIDGEGUARD_SESSION_SECRET": "test-session-secret-that-is-longer-than-32-characters",
    "BRIDGEGUARD_PASSWORD": "a-long-test-password",
    "BRIDGEGUARD_ALLOWED_DOMAINS": "dot.nj.gov",
    "BRIDGEGUARD_ALLOWED_EMAILS": "viewer@example.com",
    "BRIDGEGUARD_EDITOR_EMAILS": "editor@example.com",
    "BRIDGEGUARD_ADMIN_EMAILS": "admin@example.com",
}


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ENV, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_roles_are_resolved_server_side(self):
        self.assertEqual(role_for_email("person@dot.nj.gov"), "viewer")
        self.assertEqual(role_for_email("editor@example.com"), "editor")
        self.assertEqual(role_for_email("admin@example.com"), "admin")
        self.assertIsNone(role_for_email("stranger@example.net"))

    def test_session_round_trip_and_tamper_rejection(self):
        token = issue_session(AuthUser("admin@example.com", "admin"), now=100)
        self.assertEqual(verify_session(token, now=101), AuthUser("admin@example.com", "admin"))
        self.assertIsNone(verify_session(token + "x", now=101))

    def test_expired_session_is_rejected(self):
        token = issue_session(AuthUser("viewer@example.com", "viewer"), now=100)
        self.assertIsNone(verify_session(token, now=100 + 28801))

    def test_privileged_routes_require_expected_roles(self):
        self.assertEqual(_required_role("GET", "/api/map-data"), "viewer")
        self.assertEqual(_required_role("PUT", "/api/bridge/34/1"), "editor")
        self.assertEqual(_required_role("GET", "/api/tables"), "admin")
        self.assertEqual(_required_role("POST", "/api/sql-query"), "admin")

    def test_login_cookie_and_middleware_authorization(self):
        app = FastAPI()
        app.include_router(router)

        @app.get("/api/map-data")
        def map_data():
            return {"ok": True}

        @app.get("/api/tables")
        def tables():
            return []

        app.add_middleware(AuthMiddleware)
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/map-data").status_code, 401)
            login = client.post(
                "/api/auth/login",
                json={"email": "viewer@example.com", "password": ENV["BRIDGEGUARD_PASSWORD"]},
            )
            self.assertEqual(login.status_code, 200)
            self.assertIn("bridgeguard_session", client.cookies)
            self.assertEqual(client.get("/api/map-data").status_code, 200)
            self.assertEqual(client.get("/api/tables").status_code, 403)

    def test_cloudflare_jwt_claims_are_mapped_to_roles(self):
        cloudflare_env = {
            **ENV,
            "BRIDGEGUARD_AUTH_MODE": "cloudflare",
            "CLOUDFLARE_ACCESS_TEAM_DOMAIN": "bridgeguard.cloudflareaccess.com",
            "CLOUDFLARE_ACCESS_AUD": "test-audience",
        }
        request = Request({
            "type": "http",
            "method": "GET",
            "path": "/api/auth/me",
            "headers": [(b"cf-access-jwt-assertion", b"signed-token")],
        })

        class FakeKey:
            key = "public-key"

        class FakeClient:
            def get_signing_key_from_jwt(self, token):
                self.token = token
                return FakeKey()

        with patch.dict(os.environ, cloudflare_env, clear=True), \
             patch("app.auth._jwks_client", return_value=FakeClient()), \
             patch("app.auth.jwt.decode", return_value={"email": "admin@example.com"}) as decode:
            self.assertEqual(
                verify_cloudflare_request(request),
                AuthUser("admin@example.com", "admin"),
            )
            self.assertEqual(decode.call_args.kwargs["audience"], "test-audience")
            self.assertEqual(
                decode.call_args.kwargs["issuer"],
                "https://bridgeguard.cloudflareaccess.com",
            )


if __name__ == "__main__":
    unittest.main()
