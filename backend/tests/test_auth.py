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
    cloudflare_logout_url,
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
    "BRIDGEGUARD_OWNER_EMAIL": "owner@example.com",
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
        self.assertEqual(role_for_email("bsidhom.ahmed@gmail.com"), "admin")
        self.assertEqual(role_for_email("owner@example.com"), "admin")
        self.assertEqual(role_for_email("admin@example.com"), "admin")
        for email in (
            "douglas.tintle@dot.nj.gov",
            "vijay.sampat@dot.nj.gov",
            "harjit.bal@dot.nj.gov",
            "mantawy@rowan.edu",
            "bahria76@rowan.edu",
        ):
            self.assertEqual(role_for_email(email), "editor")
        self.assertIsNone(role_for_email("stranger@example.net"))

    def test_session_round_trip_and_tamper_rejection(self):
        token = issue_session(AuthUser("admin@example.com", "admin"), now=100)
        self.assertEqual(verify_session(token, now=101), AuthUser("admin@example.com", "admin"))
        self.assertIsNone(verify_session(token + "x", now=101))

    def test_expired_session_is_rejected(self):
        token = issue_session(AuthUser("viewer@example.com", "viewer"), now=100)
        self.assertIsNone(verify_session(token, now=100 + 28801))

    def test_cloudflare_logout_uses_application_domain(self):
        self.assertEqual(cloudflare_logout_url(), "/cdn-cgi/access/logout")

    def test_privileged_routes_require_expected_roles(self):
        self.assertEqual(_required_role("GET", "/api/map-data"), "viewer")
        self.assertEqual(_required_role("PUT", "/api/bridge/34/1"), "editor")
        self.assertEqual(_required_role("POST", "/api/bridges/34/1/point-cloud"), "editor")
        self.assertEqual(_required_role("POST", "/api/bridges/34/1/point-cloud/analyze"), "editor")
        self.assertEqual(_required_role("POST", "/api/bridges/34/1/point-cloud/segment"), "editor")
        self.assertEqual(_required_role("POST", "/api/bridges/34/1/point-cloud/clearance"), "editor")
        self.assertEqual(_required_role("POST", "/api/point-cloud/analyze"), "editor")
        self.assertEqual(_required_role("POST", "/api/point-cloud/segment"), "editor")
        self.assertEqual(_required_role("POST", "/api/point-cloud/smooth"), "editor")
        self.assertEqual(_required_role("POST", "/api/point-cloud/clearance"), "editor")
        self.assertEqual(_required_role("POST", "/api/point-cloud/preview"), "viewer")
        self.assertEqual(_required_role("POST", "/api/point-cloud/preview-npz"), "viewer")
        self.assertEqual(_required_role("POST", "/api/inspection-reports/extract"), "editor")
        self.assertEqual(_required_role("POST", "/api/inspection-reports/extract/"), "editor")
        self.assertEqual(_required_role("GET", "/api/tables"), "admin")
        self.assertEqual(_required_role("POST", "/api/sql-query"), "admin")
        self.assertEqual(_required_role("POST", "/api/convert"), "editor")
        self.assertEqual(_required_role("POST", "/api/convert-v2"), "editor")
        self.assertEqual(
            _required_role("POST", "/api/convert-v2/inspection-history"),
            "editor",
        )
        self.assertEqual(_required_role("POST", "/api/revisions/stage"), "editor")
        self.assertEqual(_required_role("POST", "/api/imports"), "editor")
        self.assertEqual(
            _required_role("POST", "/api/imports/example/commit"), "editor"
        )
        self.assertEqual(
            _required_role("POST", "/api/imports/example/rollback"), "admin"
        )
        self.assertEqual(_required_role("GET", "/api/imports/example"), "viewer")
        self.assertEqual(_required_role("POST", "/api/revisions/example/publish"), "editor")
        self.assertEqual(_required_role("POST", "/api/revisions/example/exports"), "viewer")
        self.assertEqual(_required_role("GET", "/api/revisions/snapshots/34/1/2026"), "viewer")
        self.assertEqual(
            _required_role("DELETE", "/api/ai/sessions/91f36d6c-fb54-43d5-94f8-94703a43e6c0"),
            "viewer",
        )
        self.assertEqual(_required_role("POST", "/api/vision-reset"), "viewer")
        self.assertEqual(_required_role("DELETE", "/api/bridge/34/1"), "editor")

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

    def test_auth_config_is_public_but_data_apis_are_not(self):
        app = FastAPI()
        app.include_router(router)

        @app.get("/api/map-data")
        def map_data():
            return {"ok": True}

        app.add_middleware(AuthMiddleware)
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/auth/config").json(), {"mode": "password"})
            self.assertEqual(client.get("/api/map-data").status_code, 401)

    def test_dedicated_and_commercial_modes_disable_builtin_pilot_roles(self):
        builtin_admin = "bsidhom.ahmed@gmail.com"
        builtin_editor = "mantawy@rowan.edu"
        for mode_env in (
            {"BRIDGEGUARD_DEPLOYMENT_MODE": "dedicated_customer"},
            {"BRIDGEGUARD_REQUIRE_COMMERCIAL_READY": "true"},
        ):
            with patch.dict(os.environ, {**ENV, **mode_env}, clear=True):
                self.assertIsNone(role_for_email(builtin_admin))
                self.assertIsNone(role_for_email(builtin_editor))
                self.assertIsNone(role_for_email("unlisted@dot.nj.gov"))
                self.assertEqual(role_for_email("owner@example.com"), "admin")
                self.assertEqual(role_for_email("editor@example.com"), "editor")

    def test_dedicated_builtin_email_can_be_granted_only_explicitly(self):
        dedicated = {
            **ENV,
            "BRIDGEGUARD_DEPLOYMENT_MODE": "dedicated_customer",
            "BRIDGEGUARD_ADMIN_EMAILS": "bsidhom.ahmed@gmail.com",
            "BRIDGEGUARD_EDITOR_EMAILS": "mantawy@rowan.edu",
        }
        with patch.dict(os.environ, dedicated, clear=True):
            self.assertEqual(role_for_email("bsidhom.ahmed@gmail.com"), "admin")
            self.assertEqual(role_for_email("mantawy@rowan.edu"), "editor")

    def test_viewer_cannot_reach_side_effecting_extract_or_point_cloud_routes(self):
        app = FastAPI()
        app.include_router(router)

        @app.post("/api/inspection-reports/extract")
        def extract():
            return {"ok": True}

        @app.post("/api/bridges/34/1/point-cloud/analyze")
        def analyze():
            return {"ok": True}

        @app.post("/api/point-cloud/preview")
        def preview():
            return {"ok": True}

        app.add_middleware(AuthMiddleware)
        with TestClient(app) as client:
            viewer_login = client.post(
                "/api/auth/login",
                json={"email": "viewer@example.com", "password": ENV["BRIDGEGUARD_PASSWORD"]},
            )
            self.assertEqual(viewer_login.status_code, 200)
            self.assertEqual(client.post("/api/inspection-reports/extract").status_code, 403)
            self.assertEqual(client.post("/api/bridges/34/1/point-cloud/analyze").status_code, 403)
            self.assertEqual(client.post("/api/point-cloud/preview").status_code, 200)

        with TestClient(app) as client:
            editor_login = client.post(
                "/api/auth/login",
                json={"email": "editor@example.com", "password": ENV["BRIDGEGUARD_PASSWORD"]},
            )
            self.assertEqual(editor_login.status_code, 200)
            self.assertEqual(client.post("/api/inspection-reports/extract").status_code, 200)
            self.assertEqual(client.post("/api/bridges/34/1/point-cloud/analyze").status_code, 200)

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
             patch(
                 "app.auth.jwt.decode",
                 return_value={
                     "email": "admin@example.com",
                     "type": "app",
                     "sub": "identity-subject",
                 },
             ) as decode:
            self.assertEqual(
                verify_cloudflare_request(request),
                AuthUser("admin@example.com", "admin"),
            )
            self.assertEqual(decode.call_args.kwargs["audience"], "test-audience")
            self.assertEqual(
                decode.call_args.kwargs["issuer"],
                "https://bridgeguard.cloudflareaccess.com",
            )
            required = decode.call_args.kwargs["options"]["require"]
            self.assertIn("nbf", required)
            self.assertIn("type", required)
            self.assertIn("sub", required)

    def test_cloudflare_rejects_global_or_service_identity_tokens(self):
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
                return FakeKey()

        with patch.dict(os.environ, cloudflare_env, clear=True), \
             patch("app.auth._jwks_client", return_value=FakeClient()):
            for claims in (
                {"email": "admin@example.com", "type": "org", "sub": "person"},
                {"email": "admin@example.com", "type": "app", "sub": ""},
            ):
                with patch("app.auth.jwt.decode", return_value=claims):
                    self.assertIsNone(verify_cloudflare_request(request))


if __name__ == "__main__":
    unittest.main()
