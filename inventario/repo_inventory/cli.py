"""Command line interface for the repository inventory scanner."""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import unquote, urlparse

from . import __version__
from .github import (
    DEFAULT_API_URL,
    DEFAULT_API_VERSION,
    GitHubClient,
    GitHubError,
    RepositoryRef,
    clone_repository,
    parse_github_full_name,
    resolve_token,
)
from .scanner import scan_repository, summarize


SCHEMA_VERSION = "1.0.0"
CSV_LAYOUT_VERSION = "1.0.0"

CSV_COLUMNS = [
    "row_type",
    "csv_layout_version",
    "schema_version",
    "generated_at",
    "repository_name",
    "repository_full_name",
    "repository_url",
    "default_branch",
    "commit",
    "archived",
    "visibility",
    "github_languages",
    "repository_runtime_os",
    "repository_runtime_confidence",
    "technology_id",
    "technology_name",
    "versions",
    "version_details_json",
    "primary_build_files",
    "build_files_json",
    "build_tools_json",
    "frameworks_json",
    "technology_runtime_os",
    "technology_runtime_confidence",
    "runtime_evidence_json",
    "repository_warnings_json",
    "technology_warnings_json",
    "error_code",
    "error_repository",
    "error_message",
]


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="repo-inventory",
        description="Gera JSON com tecnologias, versoes, builds e runtime provavel.",
    )
    root.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = root.add_subparsers(dest="command", required=True)

    local = subparsers.add_parser("local", help="Analisa diretorios ja existentes")
    local.add_argument("paths", nargs="+", type=Path, help="Diretorio(s) de repositorio")
    add_output_options(local)

    git = subparsers.add_parser("git", help="Clona e analisa uma ou mais URLs Git")
    git.add_argument("urls", nargs="+", help="URL(s) HTTPS ou SSH")
    git.add_argument("--branch", help="Branch especifica")
    git.add_argument("--depth", type=int, default=1, help="Profundidade do clone (0 = completo)")
    git.add_argument("--no-github-languages", action="store_true", help="Nao consulta o endpoint de linguagens")
    add_auth_options(git)
    add_output_options(git)

    github = subparsers.add_parser("github", help="Descobre ou recebe repositorios do GitHub")
    github.add_argument("--org", help="Login da organizacao; obrigatorio para descoberta ou nomes curtos")
    github.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Repositorio especifico: nome, owner/nome ou URL; pode ser repetido",
    )
    github.add_argument(
        "--repo-file",
        action="append",
        default=[],
        type=Path,
        help="Arquivo UTF-8 com um repositorio por linha; pode ser repetido",
    )
    github.add_argument("--include-archived", action="store_true")
    github.add_argument("--include-forks", action="store_true")
    github.add_argument("--repo-regex", help="Expressao regular aplicada ao nome completo")
    github.add_argument("--limit", type=int, help="Limita repositorios apos os filtros")
    github.add_argument("--depth", type=int, default=1, help="Profundidade do clone (0 = completo)")
    github.add_argument("--no-github-languages", action="store_true", help="Nao consulta o endpoint de linguagens")
    add_auth_options(github)
    add_output_options(github)
    return root


def add_auth_options(command: argparse.ArgumentParser) -> None:
    group = command.add_argument_group("autenticacao GitHub")
    group.add_argument("--token-env", default="GITHUB_TOKEN", help="Variavel que contem PAT/installation token")
    group.add_argument("--app-id", help="GitHub App ID (ou GITHUB_APP_ID)")
    group.add_argument("--installation-id", help="Installation ID (ou GITHUB_APP_INSTALLATION_ID)")
    group.add_argument("--private-key", type=Path, help="Chave PEM (ou GITHUB_APP_PRIVATE_KEY_PATH)")
    group.add_argument("--api-url", default=DEFAULT_API_URL, help="API do GitHub ou GitHub Enterprise")
    group.add_argument("--api-version", default=DEFAULT_API_VERSION)


def add_output_options(command: argparse.ArgumentParser) -> None:
    command.add_argument("--output", "-o", default="-", help="Arquivo JSON/CSV; '-' escreve no stdout")
    command.add_argument(
        "--format",
        choices=("auto", "json", "csv"),
        default="auto",
        help="Formato de saida; auto usa a extensao .csv ou JSON nos demais casos",
    )
    command.add_argument("--compact", action="store_true", help="JSON sem indentacao")
    command.add_argument("--max-files", type=int, default=200_000, help="Limite por repositorio")


def empty_inventory(source: str, args: argparse.Namespace) -> dict[str, Any]:
    scan: dict[str, Any] = {
        "source": source,
        "max_files_per_repository": args.max_files,
    }
    if source.startswith("github-"):
        scan["organization"] = args.org
        scan["filters"] = {
            "include_archived": args.include_archived,
            "include_forks": args.include_forks,
            "repo_regex": args.repo_regex,
            "limit": args.limit,
        }
        scan["requested_repositories"] = len(args.repo)
        scan["repository_files"] = [str(path) for path in args.repo_file]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "generator": {"name": "b3sa-repo-inventory", "version": __version__},
        "scan": scan,
        "summary": {},
        "repositories": [],
        "errors": [],
    }


def error_item(code: str, message: str, repository: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"code": code, "message": message}
    if repository:
        item["repository"] = repository
    return item


def scan_local(args: argparse.Namespace, inventory: dict[str, Any]) -> None:
    for path in args.paths:
        resolved = path.resolve()
        if not resolved.is_dir():
            inventory["errors"].append(error_item("invalid_local_path", "Diretorio nao encontrado", str(resolved)))
            continue
        try:
            inventory["repositories"].append(scan_repository(resolved, max_files=args.max_files))
        except Exception as exc:  # repository isolation is intentional
            inventory["errors"].append(error_item("scan_failed", safe_message(exc), str(resolved)))


def auth_client(args: argparse.Namespace) -> tuple[GitHubClient, str | None, str]:
    token, auth_method = resolve_token(
        token_env=args.token_env,
        app_id=args.app_id,
        installation_id=args.installation_id,
        private_key_path=args.private_key,
        api_url=args.api_url,
    )
    return GitHubClient(token, args.api_url, args.api_version), token, auth_method


def scan_git(args: argparse.Namespace, inventory: dict[str, Any]) -> None:
    client, token, auth_method = auth_client(args)
    inventory["scan"]["authentication"] = auth_method
    with tempfile.TemporaryDirectory(prefix="repo-inventory-") as temp:
        temp_root = Path(temp)
        for position, url in enumerate(args.urls, start=1):
            full_name = parse_github_full_name(url)
            name = (full_name.split("/", 1)[1] if full_name else Path(url.rstrip("/")).stem) or f"repository-{position}"
            destination = temp_root / f"{position:05d}-{safe_directory_name(name)}"
            try:
                clone_repository(url, destination, token=token, branch=args.branch, depth=args.depth)
                languages = None
                if full_name and not args.no_github_languages:
                    try:
                        languages = client.repository_languages(full_name)
                    except GitHubError as exc:
                        inventory["errors"].append(error_item("github_languages_failed", safe_message(exc), full_name))
                inventory["repositories"].append(
                    scan_repository(
                        destination,
                        metadata={"name": name, "full_name": full_name, "url": url},
                        github_languages=languages,
                        max_files=args.max_files,
                    )
                )
            except Exception as exc:
                inventory["errors"].append(error_item("repository_failed", safe_message(exc), full_name or url))


def normalize_repository_identifier(value: str, organization: str | None) -> str:
    clean = value.strip()
    if not clean:
        raise GitHubError("Nome de repositorio vazio")

    parsed_full_name = parse_github_full_name(clean)
    if parsed_full_name:
        clean = parsed_full_name
    elif re.match(r"^[^@\s]+@[^:\s]+:", clean):
        clean = clean.split(":", 1)[1]
    elif "://" in clean:
        parsed = urlparse(clean)
        segments = [unquote(segment) for segment in parsed.path.strip("/").split("/") if segment]
        if len(segments) != 2:
            raise GitHubError(f"URL de repositorio invalida: {value}")
        clean = "/".join(segments)

    clean = clean.rstrip("/")
    if clean.lower().endswith(".git"):
        clean = clean[:-4]
    segments = clean.split("/")
    if len(segments) == 1:
        if not organization:
            raise GitHubError(f"Repositorio '{value}' precisa de --org ou do formato owner/nome")
        segments.insert(0, organization)
    if len(segments) != 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", segment) for segment in segments):
        raise GitHubError(f"Repositorio invalido: {value}")
    return "/".join(segments)


def requested_repository_names(args: argparse.Namespace) -> list[str]:
    values = list(args.repo)
    for repository_file in args.repo_file:
        try:
            lines = repository_file.read_text(encoding="utf-8-sig").splitlines()
        except OSError as exc:
            raise GitHubError(f"Nao foi possivel ler {repository_file}: {exc}") from exc
        values.extend(line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#"))

    names: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = normalize_repository_identifier(value, args.org)
        key = name.lower()
        if key not in seen:
            seen.add(key)
            names.append(name)
    if (args.repo or args.repo_file) and not names:
        raise GitHubError("--repo/--repo-file foi informado, mas nenhum repositorio valido foi encontrado")
    return names


def scan_github(args: argparse.Namespace, inventory: dict[str, Any]) -> None:
    client, token, auth_method = auth_client(args)
    inventory["scan"]["authentication"] = auth_method
    requested = requested_repository_names(args)
    if requested:
        inventory["scan"]["selection_mode"] = "explicit"
        inventory["scan"]["requested_repository_names"] = requested
        repositories = []
        for full_name in requested:
            try:
                repositories.append(client.repository(full_name))
            except GitHubError as exc:
                inventory["errors"].append(
                    error_item("repository_lookup_failed", safe_message(exc), full_name)
                )
    else:
        if not args.org:
            raise GitHubError("Informe --org para descobrir repositorios ou use --repo/--repo-file")
        inventory["scan"]["selection_mode"] = "organization"
        repositories = client.list_org_repositories(
            args.org, include_archived=args.include_archived, include_forks=args.include_forks
        )
    if args.repo_regex:
        try:
            pattern = re.compile(args.repo_regex)
        except re.error as exc:
            raise GitHubError(f"--repo-regex invalida: {exc}") from exc
        repositories = [repository for repository in repositories if pattern.search(repository.full_name)]
    if args.limit is not None:
        if args.limit < 0:
            raise GitHubError("--limit nao pode ser negativo")
        repositories = repositories[: args.limit]
    inventory["scan"]["repositories_selected"] = len(repositories)

    with tempfile.TemporaryDirectory(prefix="repo-inventory-") as temp:
        temp_root = Path(temp)
        for position, repository in enumerate(repositories, start=1):
            print(f"[{position}/{len(repositories)}] {repository.full_name}", file=sys.stderr, flush=True)
            destination = temp_root / f"{position:05d}-{safe_directory_name(repository.name)}"
            try:
                clone_repository(repository.clone_url, destination, token=token, depth=args.depth)
                languages = None
                if not args.no_github_languages:
                    try:
                        languages = client.repository_languages(repository.full_name)
                    except GitHubError as exc:
                        inventory["errors"].append(
                            error_item("github_languages_failed", safe_message(exc), repository.full_name)
                        )
                inventory["repositories"].append(
                    scan_repository(
                        destination,
                        metadata=repository_metadata(repository),
                        github_languages=languages,
                        max_files=args.max_files,
                    )
                )
            except Exception as exc:
                inventory["errors"].append(error_item("repository_failed", safe_message(exc), repository.full_name))


def repository_metadata(repository: RepositoryRef) -> dict[str, Any]:
    return {
        "name": repository.name,
        "full_name": repository.full_name,
        "url": repository.html_url,
        "default_branch": repository.default_branch,
        "archived": repository.archived,
        "visibility": "private" if repository.private else "public" if repository.private is not None else None,
    }


def safe_directory_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-") or "repository"


def safe_message(exc: Exception) -> str:
    # Tokens are never passed into exception text by this program.
    return str(exc).replace("\r", " ").replace("\n", " ")[:2000]


def resolve_output_format(output: str, requested_format: str) -> str:
    if requested_format != "auto":
        return requested_format
    return "csv" if output != "-" and Path(output).suffix.lower() == ".csv" else "json"


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def base_csv_row(inventory: dict[str, Any]) -> dict[str, Any]:
    return {
        "csv_layout_version": CSV_LAYOUT_VERSION,
        "schema_version": inventory["schema_version"],
        "generated_at": inventory["generated_at"],
    }


def repository_csv_values(repository: dict[str, Any]) -> dict[str, Any]:
    languages = repository.get("github_languages", [])
    deployment = repository.get("deployment", {})
    return {
        "repository_name": repository.get("name"),
        "repository_full_name": repository.get("full_name"),
        "repository_url": repository.get("url"),
        "default_branch": repository.get("default_branch"),
        "commit": repository.get("commit"),
        "archived": repository.get("archived"),
        "visibility": repository.get("visibility"),
        "github_languages": " | ".join(
            f"{item.get('name')}={item.get('percentage')}%" for item in languages
        ),
        "repository_runtime_os": deployment.get("runtime_os"),
        "repository_runtime_confidence": deployment.get("confidence"),
        "repository_warnings_json": compact_json(repository.get("warnings", [])),
    }


def technology_csv_values(technology: dict[str, Any]) -> dict[str, Any]:
    versions = technology.get("versions", [])
    builds = technology.get("build_files", [])
    runtime = technology.get("runtime", {})
    return {
        "technology_id": technology.get("id"),
        "technology_name": technology.get("display_name"),
        "versions": " | ".join(
            f"{item.get('kind')}:{item.get('value')}" for item in versions
        ),
        "version_details_json": compact_json(versions),
        "primary_build_files": " | ".join(
            item.get("path", "") for item in builds if item.get("primary")
        ),
        "build_files_json": compact_json(builds),
        "build_tools_json": compact_json(technology.get("build_tools", [])),
        "frameworks_json": compact_json(technology.get("frameworks", [])),
        "technology_runtime_os": runtime.get("classification"),
        "technology_runtime_confidence": runtime.get("confidence"),
        "runtime_evidence_json": compact_json(runtime.get("evidence", [])),
        "technology_warnings_json": compact_json(technology.get("warnings", [])),
    }


def inventory_csv_rows(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    base = base_csv_row(inventory)
    for repository in inventory.get("repositories", []):
        repository_values = repository_csv_values(repository)
        technologies = repository.get("technologies", [])
        if not technologies:
            rows.append({**base, **repository_values, "row_type": "repository"})
            continue
        for technology in technologies:
            rows.append(
                {
                    **base,
                    **repository_values,
                    **technology_csv_values(technology),
                    "row_type": "technology",
                }
            )
    for error in inventory.get("errors", []):
        rows.append(
            {
                **base,
                "row_type": "error",
                "error_code": error.get("code"),
                "error_repository": error.get("repository"),
                "error_message": error.get("message"),
            }
        )
    return rows


def render_csv(inventory: dict[str, Any]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(inventory_csv_rows(inventory))
    return buffer.getvalue()


def write_inventory(
    inventory: dict[str, Any], output: str, compact: bool, requested_format: str = "auto"
) -> None:
    output_format = resolve_output_format(output, requested_format)
    if output_format == "csv":
        text = render_csv(inventory)
    else:
        text = json.dumps(
            inventory,
            ensure_ascii=False,
            indent=None if compact else 2,
            separators=(",", ":") if compact else None,
        ) + "\n"
    if output == "-":
        sys.stdout.write(text)
        return
    path = Path(output).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8-sig" if output_format == "csv" else "utf-8")
    temporary.replace(path)


def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if args.command == "github":
        source = "github-explicit" if args.repo or args.repo_file else "github-organization"
    else:
        source = {"local": "local", "git": "git-clone"}[args.command]
    inventory = empty_inventory(source, args)
    try:
        if args.command == "local":
            scan_local(args, inventory)
        elif args.command == "git":
            scan_git(args, inventory)
        else:
            scan_github(args, inventory)
    except Exception as exc:
        inventory["errors"].append(error_item("fatal", safe_message(exc)))
    inventory["repositories"].sort(key=lambda item: (item.get("full_name") or item.get("name") or "").lower())
    inventory["summary"] = summarize(inventory["repositories"])
    return inventory, 1 if inventory["errors"] and not inventory["repositories"] else 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    inventory, exit_code = run(args)
    try:
        write_inventory(inventory, args.output, args.compact, args.format)
    except OSError as exc:
        print(f"Falha ao escrever JSON: {exc}", file=sys.stderr)
        return 2
    return exit_code
