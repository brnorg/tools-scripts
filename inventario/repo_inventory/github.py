"""Minimal GitHub REST and Git client used by the inventory scanner."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import base64
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


DEFAULT_API_URL = "https://api.github.com"
DEFAULT_API_VERSION = "2022-11-28"


class GitHubError(RuntimeError):
    pass


@dataclass(frozen=True)
class RepositoryRef:
    name: str
    full_name: str
    clone_url: str
    html_url: str | None = None
    default_branch: str | None = None
    archived: bool = False
    fork: bool = False
    private: bool | None = None


class GitHubClient:
    def __init__(
        self,
        token: str | None = None,
        api_url: str = DEFAULT_API_URL,
        api_version: str = DEFAULT_API_VERSION,
    ) -> None:
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.api_version = api_version

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[Any, dict[str, str]]:
        url = path if path.startswith("http") else f"{self.api_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "b3sa-repo-inventory/0.1",
            "X-GitHub-Api-Version": self.api_version,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
                result = json.loads(raw) if raw else None
                return result, {k.lower(): v for k, v in response.headers.items()}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                message = json.loads(detail).get("message", detail)
            except json.JSONDecodeError:
                message = detail
            raise GitHubError(f"GitHub API {exc.code} em {url}: {message}") from exc
        except urllib.error.URLError as exc:
            raise GitHubError(f"Falha de rede ao acessar {url}: {exc.reason}") from exc

    def get(self, path: str) -> Any:
        return self._request("GET", path)[0]

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self._request("POST", path, body)[0]

    def list_org_repositories(
        self,
        org: str,
        include_archived: bool = False,
        include_forks: bool = False,
    ) -> list[RepositoryRef]:
        repositories: list[RepositoryRef] = []
        page = 1
        quoted_org = urllib.parse.quote(org, safe="")
        while True:
            items = self.get(
                f"/orgs/{quoted_org}/repos?type=all&sort=full_name&direction=asc"
                f"&per_page=100&page={page}"
            )
            if not isinstance(items, list):
                raise GitHubError("Resposta inesperada ao listar repositorios")
            for item in items:
                if item.get("archived") and not include_archived:
                    continue
                if item.get("fork") and not include_forks:
                    continue
                repositories.append(repository_ref_from_api(item))
            if len(items) < 100:
                break
            page += 1
        return repositories

    def repository(self, full_name: str) -> RepositoryRef:
        try:
            owner, name = full_name.split("/", 1)
        except ValueError as exc:
            raise GitHubError(f"Repositorio invalido: {full_name}") from exc
        item = self.get(
            "/repos/{}/{}".format(
                urllib.parse.quote(owner, safe=""), urllib.parse.quote(name, safe="")
            )
        )
        if not isinstance(item, dict) or not item.get("clone_url"):
            raise GitHubError(f"Resposta inesperada ao consultar {full_name}")
        return repository_ref_from_api(item)

    def repository_languages(self, full_name: str) -> dict[str, int]:
        owner, name = full_name.split("/", 1)
        path = "/repos/{}/{}/languages".format(
            urllib.parse.quote(owner, safe=""), urllib.parse.quote(name, safe="")
        )
        data = self.get(path)
        return {str(k): int(v) for k, v in data.items()}


def repository_ref_from_api(item: dict[str, Any]) -> RepositoryRef:
    return RepositoryRef(
        name=item["name"],
        full_name=item["full_name"],
        clone_url=item["clone_url"],
        html_url=item.get("html_url"),
        default_branch=item.get("default_branch"),
        archived=bool(item.get("archived")),
        fork=bool(item.get("fork")),
        private=item.get("private"),
    )


def github_app_installation_token(
    app_id: str, installation_id: str, private_key_path: Path, api_url: str
) -> str:
    try:
        import jwt  # type: ignore
    except ImportError as exc:
        raise GitHubError(
            "Autenticacao GitHub App requer: pip install -r requirements-github-app.txt"
        ) from exc

    try:
        private_key = private_key_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GitHubError(f"Nao foi possivel ler a chave privada: {exc}") from exc

    now = int(time.time())
    app_jwt = jwt.encode(
        {"iat": now - 60, "exp": now + 9 * 60, "iss": str(app_id)},
        private_key,
        algorithm="RS256",
    )
    client = GitHubClient(token=app_jwt, api_url=api_url)
    response = client.post(f"/app/installations/{installation_id}/access_tokens")
    token = response.get("token") if isinstance(response, dict) else None
    if not token:
        raise GitHubError("GitHub nao retornou um installation access token")
    return str(token)


def resolve_token(
    token_env: str,
    app_id: str | None,
    installation_id: str | None,
    private_key_path: Path | None,
    api_url: str,
) -> tuple[str | None, str]:
    token = os.environ.get(token_env)
    if token:
        return token, f"environment:{token_env}"

    resolved_app_id = app_id or os.environ.get("GITHUB_APP_ID")
    resolved_installation_id = installation_id or os.environ.get(
        "GITHUB_APP_INSTALLATION_ID"
    )
    key_value = private_key_path or (
        Path(os.environ["GITHUB_APP_PRIVATE_KEY_PATH"])
        if os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH")
        else None
    )
    configured = [resolved_app_id, resolved_installation_id, key_value]
    if any(configured) and not all(configured):
        raise GitHubError(
            "GitHub App incompleto: informe app id, installation id e private key path"
        )
    if all(configured):
        return (
            github_app_installation_token(
                str(resolved_app_id),
                str(resolved_installation_id),
                Path(key_value),
                api_url,
            ),
            "github-app",
        )
    return None, "anonymous"


def clone_repository(
    clone_url: str,
    destination: Path,
    token: str | None = None,
    branch: str | None = None,
    depth: int = 1,
) -> None:
    command = ["git", "clone", "--quiet"]
    if depth > 0:
        command.extend(["--depth", str(depth)])
    if branch:
        command.extend(["--branch", branch, "--single-branch"])
    command.extend([clone_url, str(destination)])
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    if token:
        # Avoid embedding credentials in the clone URL or command arguments.
        basic_credential = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
        env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {basic_credential}"
    try:
        subprocess.run(
            command,
            env=env,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=600,
        )
    except FileNotFoundError as exc:
        raise GitHubError("git nao encontrado no PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitHubError(f"Timeout ao clonar {clone_url}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "falha sem detalhes").strip()
        # Git normally does not print the extraHeader, but scrub token defensively.
        if token:
            detail = detail.replace(token, "***")
        raise GitHubError(f"Falha ao clonar {clone_url}: {detail}") from exc


def parse_github_full_name(url: str) -> str | None:
    patterns: Iterable[str] = (
        r"github\.com[/:](?P<owner>[^/]+?)/(?P<repo>[^/]+?)(?:\.git)?$",
        r"/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)$",
    )
    clean = url.rstrip("/")
    for pattern in patterns:
        match = re.search(pattern, clean, flags=re.IGNORECASE)
        if match:
            return f"{match.group('owner')}/{match.group('repo')}"
    return None


def git_value(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
        )
        return result.stdout.strip() or None
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
