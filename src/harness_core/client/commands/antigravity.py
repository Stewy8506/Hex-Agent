from __future__ import annotations

import json
import os
import sys
from argparse import Namespace


def run_auth_command(args: Namespace) -> int:
    from harness_core.providers.antigravity_provider import AntigravityCredential, AntigravityOAuthManager

    mgr = AntigravityOAuthManager(auth_file=args.auth_file)
    if args.auth_url:
        client_id = (
            getattr(args, "client_id", None)
            or os.getenv("HEX_ANTIGRAVITY_CLIENT_ID")
            or os.getenv("ANTIGRAVITY_CLIENT_ID")
            or ""
        ).strip()
        if not client_id:
            print(
                json.dumps(
                    {
                        "status": "error",
                        "error": (
                            "Missing Google OAuth Client ID. Browser OAuth requires a Google OAuth Client ID. "
                            "Set HEX_ANTIGRAVITY_CLIENT_ID=<id> or pass --client-id, or use manual token authentication "
                            "(`hex auth antigravity --token 'ya29...'`) or `hex setup`."
                        ),
                    },
                    indent=2,
                ),
                file=sys.stderr,
            )
            return 1
        pkce = mgr.generate_pkce_pair()
        url = mgr.build_authorization_url(pkce["challenge"], client_id=client_id)
        print(
            json.dumps(
                {
                    "status": "pkce_ready",
                    "authorization_url": url,
                    "code_verifier": pkce["verifier"],
                    "instructions": (
                        "1. Open authorization_url in your browser and sign in with your Google Antigravity account.\n"
                        "2. Copy the redirected localhost URL (or `code` parameter) and run:\n"
                        f"   hex auth antigravity --code '<REDIRECT_URL_OR_CODE>' --verifier '{pkce['verifier']}'"
                    ),
                },
                indent=2,
            )
        )
        return 0

    if args.code and args.verifier:
        cred = mgr.exchange_code_for_tokens(args.code, args.verifier)
        print(
            json.dumps(
                {
                    "status": "authenticated",
                    "source": cred.source,
                    "auth_file": str(mgr.auth_file),
                    "has_access_token": bool(cred.access_token),
                    "has_refresh_token": bool(cred.refresh_token),
                },
                indent=2,
            )
        )
        return 0

    if args.token or args.refresh_token:
        cred = AntigravityCredential(
            access_token=args.token or "",
            refresh_token=args.refresh_token or "",
            project_id=args.project_id,
            source="cli_manual",
        )
        saved_path = mgr.save_credential(cred)
        print(
            json.dumps(
                {
                    "status": "saved",
                    "auth_file": str(saved_path),
                    "has_access_token": bool(cred.access_token),
                    "has_refresh_token": bool(cred.refresh_token),
                    "project_id": cred.project_id,
                },
                indent=2,
            )
        )
        return 0

    cred = mgr.resolve_credential(auto_refresh=False)
    if cred and args.import_ide:
        mgr.save_credential(cred)
    print(
        json.dumps(
            {
                "status": "authenticated" if (cred and (cred.access_token or cred.refresh_token)) else "unauthenticated",
                "provider": "google-antigravity",
                "source": cred.source if cred else None,
                "auth_file": str(mgr.auth_file),
                "has_access_token": bool(cred and cred.access_token),
                "has_refresh_token": bool(cred and cred.refresh_token),
                "project_id": cred.project_id if cred else None,
            },
            indent=2,
        )
    )
    return 0


def run_quota_command(args: Namespace) -> int:
    from harness_core.providers.antigravity_provider import AntigravityModelProvider, AntigravityOAuthManager

    mgr = AntigravityOAuthManager(auth_file=args.auth_file)
    prov = AntigravityModelProvider(oauth_manager=mgr)
    try:
        snap = prov.fetch_quota_status()
        print(json.dumps(snap.to_dict(), indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "unavailable", "error": str(exc)}, indent=2))
        return 1
