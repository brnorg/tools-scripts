#!/usr/bin/env python3
"""Inventory branch filters configured in GitHub Actions workflow files."""

from __future__ import annotations

import argparse
import base64
import csv
import json
import logging
import os
import random
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import jwt
import requests
import urllib3
import yaml

LOG = logging.getLogger("workflow-branches")
API_VERSION = "2022-11-28"
BRANCH_EVENTS = ("push", "pull_request", "pull_request_target", "workflow_run")

# Requisito deste coletor: aceitar certificados internos, expirados ou self-signed.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class WorkflowLoader(yaml.SafeLoader):
    """YAML loader that does not turn the workflow key `on` into True."""


for first_char, resolvers in list(WorkflowLoader.yaml_implicit_resolvers.items()):
    WorkflowLoader.yaml_implicit_resolvers[first_char] = [
        item for item in resolvers if item[0] != "tag:yaml.org,2002:bool"
    ]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class State:
    def __init__(self, path: Path):
        self.path = path
        self.local = threading.local()
        self._initialize()

    def connection(self) -> sqlite3.Connection:
        conn = getattr(self.local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=60, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=60000")
            self.local.conn = conn
        return conn

    def _initialize(self) -> None:
        conn = self.connection()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS repositories (
                id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                owner TEXT NOT NULL,
                default_branch TEXT,
                archived INTEGER NOT NULL,
                disabled INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS results (
                repo_id INTEGER NOT NULL,
                repository TEXT NOT NULL,
                default_branch TEXT,
                workflow_path TEXT NOT NULL,
                workflow_name TEXT,
                event TEXT,
                filter_type TEXT,
                branch_pattern TEXT,
                parse_status TEXT NOT NULL,
                detail TEXT,
                PRIMARY KEY (repo_id, workflow_path, event, filter_type, branch_pattern),
                FOREIGN KEY(repo_id) REFERENCES repositories(id)
            );
            CREATE INDEX IF NOT EXISTS idx_repositories_status ON repositories(status);
            """
        )

    def set_meta(self, key: str, value: str) -> None:
        self.connection().execute(
            "INSERT INTO metadata(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value)
        )

    def get_meta(self, key: str) -> str | None:
        row = self.connection().execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        conn = getattr(self.local, "conn", None)
        if conn is not None:
            conn.close()
            self.local.conn = None

    def add_repositories(self, repos: Iterable[dict[str, Any]]) -> None:
        self.connection().executemany(
            """INSERT INTO repositories(id,full_name,name,owner,default_branch,archived,disabled)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
               full_name=excluded.full_name, name=excluded.name, owner=excluded.owner,
               default_branch=excluded.default_branch, archived=excluded.archived,
               disabled=excluded.disabled""",
            [
                (r["id"], r["full_name"], r["name"], r["owner"]["login"],
                 r.get("default_branch"), int(r.get("archived", False)), int(r.get("disabled", False)))
                for r in repos
            ],
        )

    def pending(self, include_archived: bool, retry_errors: bool) -> list[sqlite3.Row]:
        statuses = ("pending", "processing", "error") if retry_errors else ("pending", "processing")
        marks = ",".join("?" for _ in statuses)
        sql = f"SELECT * FROM repositories WHERE status IN ({marks})"
        args: list[Any] = list(statuses)
        if not include_archived:
            sql += " AND archived=0 AND disabled=0"
        sql += " ORDER BY id"
        return list(self.connection().execute(sql, args))

    def begin_repo(self, repo_id: int) -> None:
        self.connection().execute(
            "UPDATE repositories SET status='processing', attempts=attempts+1, error=NULL, updated_at=? WHERE id=?",
            (utc_now(), repo_id),
        )

    def finish_repo(self, repo: sqlite3.Row, rows: list[tuple[Any, ...]]) -> None:
        conn = self.connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM results WHERE repo_id=?", (repo["id"],))
            conn.executemany(
                """INSERT INTO results(repo_id,repository,default_branch,workflow_path,workflow_name,
                   event,filter_type,branch_pattern,parse_status,detail) VALUES(?,?,?,?,?,?,?,?,?,?)""", rows
            )
            conn.execute(
                "UPDATE repositories SET status='done', error=NULL, updated_at=? WHERE id=?",
                (utc_now(), repo["id"]),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def fail_repo(self, repo_id: int, error: str) -> None:
        self.connection().execute(
            "UPDATE repositories SET status='error', error=?, updated_at=? WHERE id=?",
            (error[:2000], utc_now(), repo_id),
        )


class GitHubAppClient:
    def __init__(self, jwt_issuer: str, installation_id: str | None, private_key: str,
                 api_url: str, timeout: int, max_retries: int):
        self.jwt_issuer = jwt_issuer
        self.installation_id = installation_id
        self.private_key = private_key
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.local = threading.local()
        self.token: str | None = None
        self.token_expires = datetime.min.replace(tzinfo=timezone.utc)
        self.token_lock = threading.Lock()
        self.rate_lock = threading.Lock()

    def session(self) -> requests.Session:
        session = getattr(self.local, "session", None)
        if session is None:
            session = requests.Session()
            # Deliberadamente desabilitado para ambientes corporativos com CA interna.
            session.verify = False
            session.headers.update({
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "workflow-branch-inventory/1.0",
            })
            self.local.session = session
        return session

    def _app_jwt(self) -> str:
        now = datetime.now(timezone.utc)
        payload = {"iat": int((now - timedelta(seconds=60)).timestamp()),
                   "exp": int((now + timedelta(minutes=9)).timestamp()), "iss": self.jwt_issuer}
        return jwt.encode(payload, self.private_key, algorithm="RS256")

    @staticmethod
    def _raise_api_error(response: requests.Response, operation: str) -> None:
        if response.ok:
            return
        try:
            body = json.dumps(response.json(), ensure_ascii=False)
        except ValueError:
            body = response.text
        body = body.replace("\r", " ").replace("\n", " ")[:2000]
        raise RuntimeError(
            f"{operation} falhou: HTTP {response.status_code} em {response.url}; resposta: {body}"
        )

    def _jwt_get(self, path: str, operation: str) -> dict[str, Any]:
        url = f"{self.api_url}{path}"
        response = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {self._app_jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "workflow-branch-inventory/1.0",
            },
            timeout=self.timeout,
            verify=False,
        )
        self._raise_api_error(response, operation)
        return response.json()

    def validate_and_resolve_installation(self, org: str) -> None:
        """Validate App credentials and obtain the installation ID from the org."""
        app = self._jwt_get("/app", "Validação do GitHub App (GET /app)")
        installation = self._jwt_get(
            f"/orgs/{qpath(org)}/installation",
            "Descoberta da instalação (GET /orgs/{org}/installation)",
        )
        discovered_id = str(installation["id"])
        if self.installation_id and str(self.installation_id) != discovered_id:
            LOG.warning(
                "Installation ID informado (%s) difere do ID da organização (%s); usando %s",
                self.installation_id, discovered_id, discovered_id,
            )
        self.installation_id = discovered_id
        LOG.info(
            "GitHub App validado: app=%s, instalação=%s, API=%s",
            app.get("slug") or app.get("name") or app.get("id"), discovered_id, self.api_url,
        )
        self._refresh_token(force=True)
        # Valida também a rota que será usada na paginação, com custo de uma chamada.
        self.get_json("/installation/repositories", params={"per_page": 1, "page": 1})
        LOG.info("Endpoints de autenticação e repositórios validados com sucesso")

    def _refresh_token(self, force: bool = False) -> None:
        with self.token_lock:
            if not force and self.token and datetime.now(timezone.utc) < self.token_expires - timedelta(minutes=5):
                return
            url = f"{self.api_url}/app/installations/{self.installation_id}/access_tokens"
            headers = {"Authorization": f"Bearer {self._app_jwt()}",
                       "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION}
            response = requests.post(url, headers=headers, timeout=self.timeout, verify=False)
            self._raise_api_error(
                response,
                "Criação do installation token (POST /app/installations/{id}/access_tokens)",
            )
            data = response.json()
            self.token = data["token"]
            self.token_expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
            LOG.info("Token da instalação renovado; expira em %s", self.token_expires.isoformat())

    def request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                accept: str | None = None, allow_404: bool = False) -> requests.Response | None:
        url = path if path.startswith("http") else f"{self.api_url}{path}"
        refreshed_after_401 = False
        for attempt in range(self.max_retries + 1):
            self._refresh_token()
            headers = {"Authorization": f"Bearer {self.token}"}
            if accept:
                headers["Accept"] = accept
            try:
                response = self.session().request(method, url, params=params, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt == self.max_retries:
                    raise
                delay = min(60, 2 ** attempt) + random.random()
                LOG.warning("Falha de rede (%s); nova tentativa em %.1fs", exc, delay)
                time.sleep(delay)
                continue
            if allow_404 and response.status_code == 404:
                return None
            if response.status_code == 401 and not refreshed_after_401:
                self._refresh_token(force=True)
                refreshed_after_401 = True
                continue
            if response.status_code in (403, 429):
                remaining = response.headers.get("X-RateLimit-Remaining")
                reset = response.headers.get("X-RateLimit-Reset")
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    delay = int(retry_after) + 1
                elif remaining == "0" and reset:
                    delay = max(1, int(reset) - int(time.time()) + 2)
                else:
                    delay = min(120, 2 ** min(attempt + 2, 7)) + random.random() * 3
                if attempt == self.max_retries:
                    self._raise_api_error(response, f"Requisição {method}")
                with self.rate_lock:
                    LOG.warning("Limite da API/abuso detectado; aguardando %.1fs", delay)
                    time.sleep(delay)
                continue
            if response.status_code >= 500:
                if attempt == self.max_retries:
                    self._raise_api_error(response, f"Requisição {method}")
                time.sleep(min(60, 2 ** attempt) + random.random())
                continue
            self._raise_api_error(response, f"Requisição {method}")
            return response
        raise RuntimeError("Número máximo de tentativas excedido")

    def get_json(self, path: str, **kwargs: Any) -> Any:
        response = self.request("GET", path, **kwargs)
        return None if response is None else response.json()


def qpath(value: str) -> str:
    return quote(value, safe="")


def enumerate_repositories(client: GitHubAppClient, state: State, org: str) -> None:
    page = int(state.get_meta("repo_page") or "1")
    while True:
        data = client.get_json("/installation/repositories", params={"per_page": 100, "page": page})
        repos = [r for r in data["repositories"] if r["owner"]["login"].casefold() == org.casefold()]
        state.add_repositories(repos)
        LOG.info("Página %d: %d repositórios da organização registrados", page, len(repos))
        if len(data["repositories"]) < 100:
            state.set_meta("repo_enumeration_complete", "1")
            break
        page += 1
        state.set_meta("repo_page", str(page))


def normalize_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x) for x in value]
    return [str(value)]


def extract_filters(content: str) -> tuple[str | None, list[tuple[str, str, str]], str, str | None]:
    try:
        doc = yaml.load(content, Loader=WorkflowLoader)
    except yaml.YAMLError as exc:
        return None, [], "yaml_error", str(exc)
    if not isinstance(doc, dict):
        return None, [], "invalid_workflow", "O documento YAML não é um objeto"
    name = str(doc.get("name")) if doc.get("name") is not None else None
    triggers = doc.get("on")
    if isinstance(triggers, str):
        triggers = {triggers: None}
    elif isinstance(triggers, list):
        triggers = {str(event): None for event in triggers}
    if not isinstance(triggers, dict):
        return name, [], "ok", None
    rows: list[tuple[str, str, str]] = []
    for event in BRANCH_EVENTS:
        if event not in triggers:
            continue
        config = triggers[event]
        if not isinstance(config, dict):
            rows.append((event, "implicit_all", "*"))
            continue
        found = False
        for key in ("branches", "branches-ignore"):
            for pattern in normalize_list(config.get(key)):
                rows.append((event, key, pattern))
                found = True
        if not found:
            rows.append((event, "implicit_all", "*"))
    return name, rows, "ok", None


def process_repository(client: GitHubAppClient, state: State, repo: sqlite3.Row) -> tuple[str, bool, str | None]:
    state.begin_repo(repo["id"])
    owner, name, ref = repo["owner"], repo["name"], repo["default_branch"]
    try:
        if not ref:
            state.finish_repo(repo, [])
            return repo["full_name"], True, None
        directory = client.get_json(
            f"/repos/{qpath(owner)}/{qpath(name)}/contents/.github/workflows",
            params={"ref": ref}, allow_404=True,
        )
        if directory is None:
            state.finish_repo(repo, [])
            return repo["full_name"], True, None
        if not isinstance(directory, list):
            raise RuntimeError(".github/workflows não retornou um diretório")
        output: list[tuple[Any, ...]] = []
        files = [x for x in directory if x.get("type") == "file" and x.get("name", "").lower().endswith((".yml", ".yaml"))]
        for item in files:
            data = client.get_json(
                f"/repos/{qpath(owner)}/{qpath(name)}/contents/{quote(item['path'], safe='/')}",
                params={"ref": ref},
            )
            content = base64.b64decode(data["content"].replace("\n", "")).decode("utf-8", errors="replace")
            workflow_name, filters, status, detail = extract_filters(content)
            if filters:
                for event, filter_type, pattern in filters:
                    output.append((repo["id"], repo["full_name"], ref, item["path"], workflow_name,
                                   event, filter_type, pattern, status, detail))
            else:
                output.append((repo["id"], repo["full_name"], ref, item["path"], workflow_name,
                               None, None, None, status, detail))
        state.finish_repo(repo, output)
        return repo["full_name"], True, None
    except Exception as exc:
        state.fail_repo(repo["id"], f"{type(exc).__name__}: {exc}")
        return repo["full_name"], False, str(exc)


def export_csv(state: State, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".tmp")
    columns = ["repository", "default_branch", "workflow_path", "workflow_name", "event",
               "filter_type", "branch_pattern", "parse_status", "detail"]
    with temp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        query = "SELECT " + ",".join(columns) + " FROM results ORDER BY repository,workflow_path,event,filter_type,branch_pattern"
        for row in state.connection().execute(query):
            writer.writerow(row)
    temp.replace(output)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inventaria filtros de branches em GitHub Actions workflows")
    parser.add_argument("--org", default=os.getenv("GITHUB_ORG"))
    parser.add_argument("--app-id", default=os.getenv("GITHUB_APP_ID"))
    parser.add_argument("--client-id", default=os.getenv("GITHUB_CLIENT_ID"),
                        help="Client ID do GitHub App; preferido como emissor do JWT")
    parser.add_argument("--installation-id", default=os.getenv("GITHUB_INSTALLATION_ID"))
    parser.add_argument("--private-key", type=Path, default=os.getenv("GITHUB_PRIVATE_KEY_PATH"))
    parser.add_argument("--api-url", default=os.getenv("GITHUB_API_URL", "https://api.github.com"))
    parser.add_argument("--checkpoint", type=Path, default=Path("workflow_inventory.sqlite3"))
    parser.add_argument("--output", type=Path, default=Path("workflow_branches.csv"))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--max-retries", type=int, default=10)
    parser.add_argument("--include-archived", action="store_true")
    parser.add_argument("--no-retry-errors", action="store_true")
    parser.add_argument("--export-only", action="store_true")
    parser.add_argument("--validate-only", action="store_true",
                        help="valida App, instalação e endpoints sem iniciar a coleta")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    args = parser.parse_args()
    if not args.export_only:
        missing = [name for name in ("org", "private_key") if not getattr(args, name)]
        if not (args.client_id or args.app_id):
            missing.append("client_id ou app_id")
        if missing:
            parser.error("parâmetros obrigatórios ausentes: " + ", ".join(missing))
        if args.workers < 1:
            parser.error("--workers deve ser >= 1")
    return args


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s %(levelname)s %(threadName)s %(message)s")
    state = State(args.checkpoint)
    if args.export_only:
        export_csv(state, args.output)
        LOG.info("CSV exportado para %s", args.output.resolve())
        state.close()
        return 0
    previous_org = state.get_meta("org")
    if previous_org and previous_org.casefold() != args.org.casefold():
        raise SystemExit(f"Checkpoint pertence à organização {previous_org!r}; use outro arquivo")
    state.set_meta("org", args.org)
    private_key = args.private_key.read_text(encoding="utf-8")
    client = GitHubAppClient(args.client_id or args.app_id, args.installation_id, private_key,
                             args.api_url, args.timeout, args.max_retries)
    client.validate_and_resolve_installation(args.org)
    if args.validate_only:
        LOG.info("Validação concluída; nenhuma coleta foi iniciada")
        state.close()
        return 0
    if state.get_meta("repo_enumeration_complete") != "1":
        enumerate_repositories(client, state, args.org)
    pending = state.pending(args.include_archived, not args.no_retry_errors)
    LOG.info("%d repositórios aguardando processamento", len(pending))
    ok = failed = 0
    try:
        with ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="repo") as pool:
            futures = [pool.submit(process_repository, client, state, repo) for repo in pending]
            for index, future in enumerate(as_completed(futures), 1):
                repo_name, success, error = future.result()
                ok += int(success)
                failed += int(not success)
                if not success:
                    LOG.error("%s falhou: %s", repo_name, error)
                if index % 100 == 0:
                    LOG.info("Progresso: %d/%d (ok=%d, falhas=%d)", index, len(pending), ok, failed)
    except KeyboardInterrupt:
        LOG.warning("Interrompido; o checkpoint foi preservado")
    finally:
        export_csv(state, args.output)
        state.close()
    LOG.info("Concluído: ok=%d, falhas=%d, CSV=%s", ok, failed, args.output.resolve())
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
