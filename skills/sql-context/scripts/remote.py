#!/usr/bin/env python3
"""Self-contained authenticated HTTPS client for the SQL context service."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024


class ClientError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ClientError("Service redirect refused; configure the final HTTPS URL")


def service_url(value):
    try:
        parsed = urllib.parse.urlsplit(value)
        _ = parsed.port
    except ValueError:
        raise ClientError("Invalid service URL") from None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or any(c.isspace() or ord(c) < 32 for c in value)):
        raise ClientError("Service URL must be HTTPS without credentials, query, or fragment")
    return value.rstrip("/")


def token_value(path=None):
    if path:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(Path(path).expanduser(), flags)
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) & 0o077):
                    raise ClientError("Token file must be an owned private regular file (mode 0600)")
                token = stream.read(8193).strip()
                if token.startswith("{"):
                    try:
                        token = json.loads(token)["token"]
                    except (ValueError, KeyError, TypeError):
                        raise ClientError("Invalid token file") from None
        except (OSError, UnicodeError):
            raise ClientError("Cannot read private token file") from None
    else:
        token = os.environ.get("CONTEXT_SQL_TOKEN", "").strip()
    if not isinstance(token, str) or not token or len(token) > 8192 or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in token):
        raise ClientError("Provide a valid token using CONTEXT_SQL_TOKEN or --token-file")
    return token


def request(url, token, method, endpoint, body=None, timeout=15):
    payload = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if payload is not None and len(payload) > MAX_REQUEST_BYTES:
        raise ClientError("Request exceeds 1 MiB")
    req = urllib.request.Request(service_url(url) + endpoint, data=payload, method=method,
                                 headers={"Authorization": "Bearer " + token,
                                          "Content-Type": "application/json", "Accept": "application/json"})
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(req, timeout=timeout) as response:
            if response.status != 200:
                raise ClientError("Unexpected service response status")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        # A proxy or server error can echo SQL, prompt text, or credentials.
        raise ClientError(f"Service request failed (HTTP {exc.code}); response body withheld; execution may have started, so inspect trace before retrying writes") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ClientError("HTTPS request failed; check connectivity and certificate trust; execution may have started, so inspect trace before retrying writes") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ClientError("Response exceeds 1 MiB; narrow the query")
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ClientError("Service returned invalid JSON") from None
    if not isinstance(result, dict):
        raise ClientError("Service returned a non-object response")
    return result


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", newline="") as stream:
            value = stream.read(MAX_REQUEST_BYTES + 1)
    except (OSError, UnicodeError):
        raise ClientError("Cannot read UTF-8 input file") from None
    if len(value.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise ClientError("Input exceeds 1 MiB")
    return value


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--url", default=os.environ.get("CONTEXT_SQL_URL", ""))
    result.add_argument("--token-file", default=os.environ.get("CONTEXT_SQL_TOKEN_FILE"))
    result.add_argument("--timeout", type=float, default=15)
    sub = result.add_subparsers(dest="command", required=True)
    prompt = sub.add_parser("prompt", help="Record explicitly supplied prompt text or a labeled summary")
    source = prompt.add_mutually_exclusive_group(required=True)
    source.add_argument("--text")
    source.add_argument("--file")
    prompt.add_argument("--kind", choices=("prompt", "summary"), default="prompt")
    prompt.add_argument("--project", required=True)
    prompt.add_argument("--task", required=True)
    prompt.add_argument("--parent-id", type=uuid.UUID)
    prompt.add_argument("--session-id")
    sql = sub.add_parser("sql", help="Execute SQL within the authenticated account's permissions")
    query = sql.add_mutually_exclusive_group(required=True)
    query.add_argument("--query")
    query.add_argument("--file")
    params = sql.add_mutually_exclusive_group()
    params.add_argument("--params", help="JSON array or object of bound parameters")
    params.add_argument("--params-file", help="UTF-8 file containing JSON parameters")
    sql.add_argument("--prompt-id", required=True, type=uuid.UUID)
    sql.add_argument("--role", choices=("reader", "writer"), default="reader")
    sql.add_argument("--capture-result", action="store_true")
    sub.add_parser("schema", help="Retrieve the authenticated service's current schema reference")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if not 0 < args.timeout <= 120:
            raise ClientError("Timeout must be greater than zero and at most 120 seconds")
        url = service_url(args.url)
        token = token_value(args.token_file)
        if args.command == "schema":
            result = request(url, token, "GET", "/v1/schema", timeout=args.timeout)
        else:
            if args.command == "prompt":
                text = read_text(args.file) if args.file else args.text
                body = {"text": text, "kind": args.kind, "project": args.project, "task": args.task}
                if args.parent_id:
                    body["parent_id"] = str(args.parent_id)
                if args.session_id is not None:
                    body["session_id"] = args.session_id
                endpoint = "/v1/prompts"
            else:
                sql = read_text(args.file) if args.file else args.query
                raw = read_text(args.params_file) if args.params_file else args.params
                params = None if raw is None else json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                if params is not None and not isinstance(params, (list, dict)):
                    raise ClientError("Parameters must be a JSON array or object")
                body = {"prompt_id": str(args.prompt_id), "sql": sql, "role": args.role,
                        "capture_result": args.capture_result}
                if params is not None:
                    body["params"] = params
                endpoint = "/v1/sql"
            result = request(url, token, "POST", endpoint, body, args.timeout)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0 if result.get("outcome") not in ("error", "failed", "unknown") else 1
    except (ClientError, ValueError) as exc:
        message = str(exc) if isinstance(exc, ClientError) else "Invalid JSON input"
        print(json.dumps({"error": message}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
