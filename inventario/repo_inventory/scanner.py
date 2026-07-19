"""Static, evidence-based repository technology scanner."""

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .github import git_value, parse_github_full_name


IGNORED_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vs",
    ".venv",
    "venv",
    "node_modules",
    "vendor",
    "target",
    "bin",
    "obj",
    "dist",
    "coverage",
    ".next",
    ".nuxt",
    ".gradle",
}

CONFIDENCE_WEIGHT = {"low": 1, "medium": 2, "high": 3}


class FileIndex:
    def __init__(
        self,
        root: Path,
        max_files: int = 200_000,
        max_text_bytes: int = 2_000_000,
    ) -> None:
        self.root = root.resolve()
        self.max_files = max_files
        self.max_text_bytes = max_text_bytes
        self.files: list[Path] = []
        self.skipped_directories = 0
        self.limit_reached = False
        self._text_cache: dict[Path, str | None] = {}
        self._build()

    def _build(self) -> None:
        stop = False
        for current, directories, filenames in os.walk(self.root, followlinks=False):
            allowed = []
            for directory in directories:
                if directory.lower() in IGNORED_DIRECTORIES:
                    self.skipped_directories += 1
                else:
                    allowed.append(directory)
            directories[:] = allowed
            for filename in filenames:
                path = Path(current) / filename
                if path.is_symlink():
                    continue
                self.files.append(path)
                if len(self.files) >= self.max_files:
                    self.limit_reached = True
                    stop = True
                    break
            if stop:
                break
        self.files.sort(key=lambda p: self.relative(p).lower())

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def by_name(self, *names: str) -> list[Path]:
        expected = {name.lower() for name in names}
        return [path for path in self.files if path.name.lower() in expected]

    def name_starts(self, prefix: str) -> list[Path]:
        lowered = prefix.lower()
        return [path for path in self.files if path.name.lower().startswith(lowered)]

    def by_suffix(self, *suffixes: str) -> list[Path]:
        expected = tuple(suffix.lower() for suffix in suffixes)
        return [path for path in self.files if path.name.lower().endswith(expected)]

    def text(self, path: Path) -> str | None:
        if path in self._text_cache:
            return self._text_cache[path]
        try:
            if path.stat().st_size > self.max_text_bytes:
                self._text_cache[path] = None
                return None
            raw = path.read_bytes()
            if b"\x00" in raw[:4096]:
                self._text_cache[path] = None
                return None
            text = raw.decode("utf-8-sig", errors="replace")
            self._text_cache[path] = text
            return text
        except OSError:
            self._text_cache[path] = None
            return None


def _source(index: FileIndex, path: Path, selector: str, detail: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"path": index.relative(path), "selector": selector}
    if detail:
        result["detail"] = detail[:500]
    return result


def _technology(identifier: str, display_name: str) -> dict[str, Any]:
    return {
        "id": identifier,
        "display_name": display_name,
        "versions": [],
        "build_files": [],
        "build_tools": [],
        "frameworks": [],
        "evidence": [],
        "runtime": {},
        "warnings": [],
    }


def _append_unique(items: list[dict[str, Any]], item: dict[str, Any], keys: tuple[str, ...]) -> None:
    signature = tuple(json.dumps(item.get(key), sort_keys=True) for key in keys)
    for current in items:
        if tuple(json.dumps(current.get(key), sort_keys=True) for key in keys) == signature:
            return
    items.append(item)


def _add_version(
    technology: dict[str, Any],
    value: str | None,
    kind: str,
    confidence: str,
    source: dict[str, Any],
    normalized: str | None = None,
) -> None:
    if value is None:
        return
    value = str(value).strip()
    if not value or value.startswith("${") or value.startswith("$("):
        return
    item: dict[str, Any] = {
        "value": value,
        "kind": kind,
        "confidence": confidence,
        "source": source,
    }
    if normalized:
        item["normalized"] = normalized
    _append_unique(technology["versions"], item, ("value", "kind", "source"))


def _add_build_file(
    technology: dict[str, Any],
    index: FileIndex,
    path: Path,
    file_type: str,
    role: str,
    primary: bool,
) -> None:
    _append_unique(
        technology["build_files"],
        {
            "path": index.relative(path),
            "type": file_type,
            "role": role,
            "primary": primary,
        },
        ("path", "type"),
    )


def _add_build_tool(
    technology: dict[str, Any],
    name: str,
    version: str | None,
    confidence: str,
    source: dict[str, Any],
) -> None:
    item: dict[str, Any] = {"name": name, "confidence": confidence, "source": source}
    if version:
        item["version"] = version
    _append_unique(technology["build_tools"], item, ("name", "version", "source"))


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml(path: Path) -> ET.Element | None:
    try:
        return ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None


def _direct_child(element: ET.Element, name: str) -> ET.Element | None:
    for child in element:
        if _local_name(child.tag) == name:
            return child
    return None


def _direct_text(element: ET.Element, name: str) -> str | None:
    child = _direct_child(element, name)
    return child.text.strip() if child is not None and child.text else None


def _all_text(element: ET.Element, name: str) -> list[str]:
    values = []
    for child in element.iter():
        if _local_name(child.tag) == name and child.text and child.text.strip():
            values.append(child.text.strip())
    return values


def _has_ancestor_manifest(index: FileIndex, path: Path, manifest_name: str) -> bool:
    current = path.parent
    while current != index.root and index.root in current.parents:
        current = current.parent
        if (current / manifest_name).is_file():
            return True
    return False


def _resolve_maven_value(value: str | None, properties: dict[str, str]) -> str | None:
    if value is None:
        return None
    result = value.strip()
    for _ in range(5):
        match = re.fullmatch(r"\$\{([^}]+)\}", result)
        if not match or match.group(1) not in properties:
            break
        result = properties[match.group(1)].strip()
    return result


def analyze_java(index: FileIndex) -> dict[str, Any] | None:
    poms = index.by_name("pom.xml")
    gradle_builds = index.by_name("build.gradle", "build.gradle.kts")
    java_files = index.by_suffix(".java")
    if not (poms or gradle_builds or java_files):
        return None

    technology = _technology("java", "Java")
    technology["evidence"].append(
        {"type": "source_files", "extension": ".java", "count": len(java_files)}
    )

    for pom in poms:
        root = _xml(pom)
        has_modules = bool(root is not None and _direct_child(root, "modules") is not None)
        packaging = _direct_text(root, "packaging") if root is not None else None
        is_top_manifest = not _has_ancestor_manifest(index, pom, "pom.xml")
        role = "parent" if has_modules or packaging == "pom" else ("root" if is_top_manifest else "module")
        _add_build_file(technology, index, pom, "maven-pom", role, is_top_manifest)
        if root is None:
            technology["warnings"].append(
                {"code": "invalid_pom", "message": "pom.xml nao pode ser interpretado", "path": index.relative(pom)}
            )
            continue

        properties: dict[str, str] = {}
        props = _direct_child(root, "properties")
        if props is not None:
            for child in props:
                if child.text and child.text.strip():
                    properties[_local_name(child.tag)] = child.text.strip()

        candidates: list[tuple[str, str, str]] = []
        for key, kind in (
            ("maven.compiler.release", "release"),
            ("java.version", "declared"),
            ("jdk.version", "declared"),
            ("maven.compiler.source", "source"),
            ("maven.compiler.target", "target"),
        ):
            if key in properties:
                candidates.append((properties[key], kind, f"project.properties.{key}"))
        for plugin in root.iter():
            if _local_name(plugin.tag) != "plugin" or _direct_text(plugin, "artifactId") != "maven-compiler-plugin":
                continue
            configuration = _direct_child(plugin, "configuration")
            if configuration is None:
                continue
            for element_name, kind in (("release", "release"), ("source", "source"), ("target", "target")):
                for value in _all_text(configuration, element_name):
                    candidates.append((value, kind, f"maven-compiler-plugin.configuration.{element_name}"))

        for value, kind, selector in candidates:
            resolved = _resolve_maven_value(value, properties)
            _add_version(
                technology,
                resolved,
                kind,
                "high" if kind == "release" else "medium",
                _source(index, pom, selector, value if resolved != value else None),
                normalize_java_version(resolved),
            )
        _add_build_tool(technology, "Maven", None, "high", _source(index, pom, "project"))

    for build in gradle_builds:
        current = build.parent
        has_ancestor_build = False
        while current != index.root:
            current = current.parent
            if (current / "build.gradle").is_file() or (current / "build.gradle.kts").is_file():
                has_ancestor_build = True
                break
        is_top_manifest = not has_ancestor_build
        _add_build_file(
            technology, index, build, "gradle-build", "root" if is_top_manifest else "module", is_top_manifest
        )
        text = index.text(build) or ""
        patterns = (
            (r"JavaLanguageVersion\.of\([\"']?(\d+)", "toolchain", "high"),
            (r"languageVersion\s*=\s*JavaLanguageVersion\.of\([\"']?(\d+)", "toolchain", "high"),
            (r"sourceCompatibility\s*=\s*[\"']?(?:JavaVersion\.VERSION_)?([\d_\.]+)", "source", "medium"),
            (r"targetCompatibility\s*=\s*[\"']?(?:JavaVersion\.VERSION_)?([\d_\.]+)", "target", "medium"),
        )
        for pattern, kind, confidence in patterns:
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                value = match.group(1).replace("_", ".")
                _add_version(
                    technology,
                    value,
                    kind,
                    confidence,
                    _source(index, build, pattern),
                    normalize_java_version(value),
                )
        _add_build_tool(technology, "Gradle", None, "high", _source(index, build, "build script"))

    for wrapper in index.by_name("gradle-wrapper.properties"):
        text = index.text(wrapper) or ""
        match = re.search(r"gradle-([0-9][0-9.]*?)-(?:bin|all)\.zip", text)
        _add_build_tool(
            technology,
            "Gradle",
            match.group(1) if match else None,
            "high",
            _source(index, wrapper, "distributionUrl"),
        )
    for wrapper in index.by_name("maven-wrapper.properties"):
        text = index.text(wrapper) or ""
        match = re.search(r"apache-maven-([0-9][0-9.]*)-bin\.zip", text)
        _add_build_tool(
            technology,
            "Maven",
            match.group(1) if match else None,
            "high",
            _source(index, wrapper, "distributionUrl"),
        )
    for version_file in index.by_name(".java-version"):
        lines = (index.text(version_file) or "").strip().splitlines()
        value = lines[0] if lines else None
        _add_version(
            technology,
            value,
            "toolchain",
            "high",
            _source(index, version_file, "first-line"),
            normalize_java_version(value),
        )
    for sdkmanrc in index.by_name(".sdkmanrc"):
        text = index.text(sdkmanrc) or ""
        match = re.search(r"(?im)^\s*java\s*=\s*([^\s#]+)", text)
        value = match.group(1) if match else None
        _add_version(
            technology,
            value,
            "toolchain",
            "high",
            _source(index, sdkmanrc, "java"),
            normalize_java_version(value),
        )
    for settings in index.by_name("settings.gradle", "settings.gradle.kts"):
        _add_build_file(technology, index, settings, "gradle-settings", "workspace-root", True)

    if not technology["versions"]:
        technology["warnings"].append(
            {"code": "java_version_unknown", "message": "Java detectado, mas a versao nao foi declarada nos arquivos analisados"}
        )
    return technology


def normalize_java_version(value: str | None) -> str | None:
    if not value:
        return None
    clean = value.strip().lower().removeprefix("java_version_").replace("_", ".")
    match = re.match(r"1\.(\d+)", clean)
    if match:
        return match.group(1)
    match = re.match(r"(\d+)", clean)
    return match.group(1) if match else value


def analyze_node(index: FileIndex) -> dict[str, Any] | None:
    manifests = index.by_name("package.json")
    js_files = index.by_suffix(".js", ".mjs", ".cjs", ".ts", ".tsx")
    if not manifests:
        return None

    technology = _technology("nodejs", "Node.js / ecossistema JavaScript")
    technology["evidence"].append(
        {"type": "source_files", "extensions": [".js", ".mjs", ".cjs", ".ts", ".tsx"], "count": len(js_files)}
    )
    known_frameworks = {
        "next": "Next.js",
        "@nestjs/core": "NestJS",
        "express": "Express",
        "@angular/core": "Angular",
        "react": "React",
        "vue": "Vue",
        "fastify": "Fastify",
    }
    for manifest in manifests:
        text = index.text(manifest)
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError:
            technology["warnings"].append(
                {"code": "invalid_package_json", "message": "package.json invalido", "path": index.relative(manifest)}
            )
            data = {}
        top_manifest = not _has_ancestor_manifest(index, manifest, "package.json")
        workspaces = data.get("workspaces")
        role = "workspace-root" if workspaces else ("root" if top_manifest else "package")
        _add_build_file(technology, index, manifest, "npm-package", role, top_manifest or bool(workspaces))

        engines = data.get("engines") if isinstance(data.get("engines"), dict) else {}
        _add_version(
            technology,
            engines.get("node"),
            "engine-range",
            "high",
            _source(index, manifest, "engines.node"),
        )
        volta = data.get("volta") if isinstance(data.get("volta"), dict) else {}
        _add_version(
            technology,
            volta.get("node"),
            "toolchain",
            "high",
            _source(index, manifest, "volta.node"),
        )
        package_manager = data.get("packageManager")
        if isinstance(package_manager, str) and "@" in package_manager:
            manager, version = package_manager.rsplit("@", 1)
            _add_build_tool(
                technology,
                manager,
                version,
                "high",
                _source(index, manifest, "packageManager"),
            )
        elif (manifest.parent / "package-lock.json").is_file():
            _add_build_tool(technology, "npm", None, "medium", _source(index, manifest, "package-lock.json"))
        elif (manifest.parent / "yarn.lock").is_file():
            _add_build_tool(technology, "Yarn", None, "medium", _source(index, manifest, "yarn.lock"))
        elif (manifest.parent / "pnpm-lock.yaml").is_file():
            _add_build_tool(technology, "pnpm", None, "medium", _source(index, manifest, "pnpm-lock.yaml"))
        else:
            _add_build_tool(technology, "npm-compatible", None, "low", _source(index, manifest, "package.json"))

        dependencies: dict[str, Any] = {}
        for key in ("dependencies", "devDependencies", "peerDependencies"):
            if isinstance(data.get(key), dict):
                dependencies.update(data[key])
        for package, display_name in known_frameworks.items():
            if package in dependencies:
                _append_unique(
                    technology["frameworks"],
                    {
                        "name": display_name,
                        "version": str(dependencies[package]),
                        "source": _source(index, manifest, f"dependencies.{package}"),
                    },
                    ("name", "source"),
                )

    for version_file in index.by_name(".nvmrc", ".node-version"):
        value = (index.text(version_file) or "").strip().splitlines()
        _add_version(
            technology,
            value[0] if value else None,
            "toolchain",
            "high",
            _source(index, version_file, "first-line"),
        )
    for lock_name, tool in (("package-lock.json", "npm"), ("yarn.lock", "Yarn"), ("pnpm-lock.yaml", "pnpm")):
        for lockfile in index.by_name(lock_name):
            _add_build_file(technology, index, lockfile, f"{tool.lower()}-lock", "lockfile", False)

    if not technology["versions"]:
        technology["warnings"].append(
            {"code": "node_version_unknown", "message": "Ecossistema Node.js detectado, mas a versao do runtime nao foi declarada"}
        )
    return technology


def normalize_target_framework(value: str) -> tuple[str, str | None]:
    clean = value.strip()
    lower = clean.lower()
    if lower.startswith("v") and re.fullmatch(r"v\d+(?:\.\d+){1,2}", lower):
        version = lower[1:]
        return f".NET Framework {version}", f"net{version.replace('.', '')}"
    match = re.fullmatch(r"net(\d)(\d)(\d?)", lower)
    if match and int(match.group(1)) <= 4:
        digits = "".join(group for group in match.groups() if group)
        version = ".".join(digits)
        return f".NET Framework {version}", lower
    match = re.match(r"net(\d+)\.(\d+)", lower)
    if match:
        return f".NET {match.group(1)}.{match.group(2)}", lower
    match = re.match(r"netcoreapp(\d+)\.(\d+)", lower)
    if match:
        return f".NET Core {match.group(1)}.{match.group(2)}", lower
    match = re.match(r"netstandard(\d+)\.(\d+)", lower)
    if match:
        return f".NET Standard {match.group(1)}.{match.group(2)}", lower
    return clean, lower or None


def _project_values(root: ET.Element, name: str) -> list[str]:
    values: list[str] = []
    for element in root.iter():
        if _local_name(element.tag) == name and element.text:
            for item in element.text.split(";"):
                if item.strip():
                    values.append(item.strip())
    return values


def analyze_dotnet(index: FileIndex) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[dict[str, Any]]]:
    projects = index.by_suffix(".csproj", ".fsproj", ".vbproj")
    solutions = index.by_suffix(".sln", ".slnx")
    csharp_files = index.by_suffix(".cs")
    legacy_project_json = index.by_name("project.json")
    if not (projects or solutions or csharp_files or legacy_project_json):
        return None, None, []

    dotnet = _technology("dotnet", ".NET") if (projects or solutions or legacy_project_json) else None
    csharp = _technology("csharp", "C#") if (csharp_files or any(p.suffix.lower() == ".csproj" for p in projects)) else None
    runtime_evidence: list[dict[str, Any]] = []

    if csharp is not None:
        csharp["evidence"].append({"type": "source_files", "extension": ".cs", "count": len(csharp_files)})

    referenced_projects: set[str] = set()
    for solution in solutions:
        text = index.text(solution) or ""
        for match in re.finditer(r'=\s*"[^"]+",\s*"([^"]+\.(?:cs|fs|vb)proj)"', text, flags=re.IGNORECASE):
            try:
                referenced_projects.add(index.relative((solution.parent / match.group(1).replace("\\", "/")).resolve()).lower())
            except ValueError:
                pass
        if dotnet is not None:
            _add_build_file(dotnet, index, solution, "dotnet-solution", "solution", True)
        if csharp is not None:
            _add_build_file(csharp, index, solution, "dotnet-solution", "solution", True)

    for project in projects:
        relative = index.relative(project).lower()
        primary = not solutions or relative not in referenced_projects
        language = {".csproj": "C#", ".fsproj": "F#", ".vbproj": "Visual Basic"}.get(project.suffix.lower(), ".NET")
        file_type = project.suffix.lower().removeprefix(".")
        if dotnet is not None:
            _add_build_file(dotnet, index, project, file_type, "project", primary)
        if csharp is not None and project.suffix.lower() == ".csproj":
            _add_build_file(csharp, index, project, file_type, "project", primary)
        root = _xml(project)
        if root is None:
            if dotnet is not None:
                dotnet["warnings"].append(
                    {"code": "invalid_project", "message": "Projeto MSBuild invalido", "path": index.relative(project)}
                )
            continue

        target_values = []
        for field in ("TargetFramework", "TargetFrameworks", "TargetFrameworkVersion"):
            target_values.extend((value, field) for value in _project_values(root, field))
        for value, field in target_values:
            display, normalized = normalize_target_framework(value)
            if dotnet is not None:
                _add_version(
                    dotnet,
                    value,
                    "target-framework",
                    "high",
                    _source(index, project, field, display),
                    normalized,
                )
            if is_windows_only_framework(value):
                runtime_evidence.append(
                    {
                        "os": "windows",
                        "confidence": "high",
                        "path": index.relative(project),
                        "rule": "windows_only_target_framework",
                        "detail": f"{language} direciona para {display}",
                        "applies_to": ["dotnet", "csharp"],
                    }
                )

        sdk = root.attrib.get("Sdk") or next(
            (element.attrib.get("Sdk") for element in root.iter() if element.attrib.get("Sdk")), None
        )
        if sdk and dotnet is not None:
            _append_unique(
                dotnet["frameworks"],
                {"name": sdk, "source": _source(index, project, "Project@Sdk")},
                ("name", "source"),
            )
        lang_versions = _project_values(root, "LangVersion")
        if csharp is not None:
            for value in lang_versions:
                _add_version(
                    csharp,
                    value,
                    "language-version",
                    "high",
                    _source(index, project, "LangVersion"),
                )

        use_wpf = any(value.lower() == "true" for value in _project_values(root, "UseWPF"))
        use_winforms = any(value.lower() == "true" for value in _project_values(root, "UseWindowsForms"))
        if use_wpf or use_winforms:
            runtime_evidence.append(
                {
                    "os": "windows",
                    "confidence": "high",
                    "path": index.relative(project),
                    "rule": "windows_desktop_framework",
                    "detail": "UseWPF/UseWindowsForms habilitado",
                    "applies_to": ["dotnet", "csharp"],
                }
            )

    for global_json in index.by_name("global.json"):
        if dotnet is None:
            continue
        try:
            data = json.loads(index.text(global_json) or "{}")
            version = data.get("sdk", {}).get("version")
        except (json.JSONDecodeError, AttributeError):
            version = None
        _add_build_file(dotnet, index, global_json, "dotnet-sdk-config", "toolchain", True)
        _add_build_tool(dotnet, ".NET SDK", version, "high", _source(index, global_json, "sdk.version"))
        _add_version(dotnet, version, "sdk", "high", _source(index, global_json, "sdk.version"))

    for props in index.by_name("Directory.Build.props", "Directory.Build.targets", "packages.config"):
        if dotnet is not None:
            file_type = "msbuild-shared" if props.name.lower().startswith("directory.build") else "nuget-packages"
            _add_build_file(dotnet, index, props, file_type, "shared-config", props.name.lower().startswith("directory.build"))
    for project_json in legacy_project_json:
        if dotnet is not None:
            _add_build_file(dotnet, index, project_json, "legacy-dotnet-project", "project", True)
            try:
                data = json.loads(index.text(project_json) or "{}")
                frameworks = data.get("frameworks", {})
                if isinstance(frameworks, dict):
                    for value in frameworks:
                        _, normalized = normalize_target_framework(value)
                        _add_version(dotnet, value, "target-framework", "high", _source(index, project_json, f"frameworks.{value}"), normalized)
            except json.JSONDecodeError:
                pass

    if dotnet is not None and not dotnet["versions"]:
        dotnet["warnings"].append(
            {"code": "dotnet_version_unknown", "message": ".NET detectado, mas nenhum TargetFramework foi encontrado"}
        )
    if csharp is not None and not csharp["versions"]:
        csharp["warnings"].append(
            {"code": "csharp_version_unknown", "message": "C# detectado; LangVersion nao foi declarada (pode usar o padrao do SDK)"}
        )
    return dotnet, csharp, runtime_evidence


def is_windows_only_framework(value: str) -> bool:
    lower = value.strip().lower()
    if "-windows" in lower or lower.startswith("v"):
        return True
    return bool(re.fullmatch(r"net(?:1|2|3|4)\d{1,2}", lower))


SQL_SERVER_DSP = {
    "Sql80": "SQL Server 2000",
    "Sql90": "SQL Server 2005",
    "Sql100": "SQL Server 2008",
    "Sql110": "SQL Server 2012",
    "Sql120": "SQL Server 2014",
    "Sql130": "SQL Server 2016",
    "Sql140": "SQL Server 2017",
    "Sql150": "SQL Server 2019",
    "Sql160": "SQL Server 2022",
    "Sql170": "SQL Server 2025",
}


TSQL_RULES: tuple[tuple[str, str], ...] = (
    (r"(?im)^\s*GO\s*(?:--.*)?$", "go_batch_separator"),
    (r"(?i)\bSET\s+(?:ANSI_NULLS|QUOTED_IDENTIFIER)\s+(?:ON|OFF)\b", "sql_server_session_setting"),
    (r"(?i)\b(?:RAISERROR|TRY_CONVERT|TRY_CAST|SCOPE_IDENTITY|@@ROWCOUNT|@@IDENTITY)\b", "tsql_builtin"),
    (r"(?i)\b(?:sys\.(?:objects|tables|columns)|INFORMATION_SCHEMA)\b", "sql_server_catalog"),
    (r"(?i)\bCREATE\s+(?:OR\s+ALTER\s+)?(?:PROC|PROCEDURE)\s+\[?(?:dbo\.)", "tsql_procedure"),
    (r"(?i)\b(?:NVARCHAR|UNIQUEIDENTIFIER|DATETIME2|MONEY|BIT)\b", "sql_server_type"),
    (r"(?i)\bSELECT\s+TOP\s*(?:\(|\d)", "select_top"),
)


def analyze_sql(index: FileIndex) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[dict[str, Any]]]:
    sql_files = index.by_suffix(".sql")
    sql_projects = index.by_suffix(".sqlproj")
    migration_files = index.by_name("flyway.conf", "liquibase.properties", "changelog.xml", "databasechangelog.xml")
    if not (sql_files or sql_projects or migration_files):
        return None, None, []

    sql = _technology("sql", "SQL")
    sql["evidence"].append({"type": "source_files", "extension": ".sql", "count": len(sql_files)})
    tsql_matches: list[dict[str, Any]] = []
    runtime_evidence: list[dict[str, Any]] = []

    for project in sql_projects:
        _add_build_file(sql, index, project, "sql-server-database-project", "database-project", True)
        tsql_matches.append(
            {"path": index.relative(project), "rule": "sqlproj", "confidence": "high", "detail": "Projeto de banco SQL Server"}
        )
        root = _xml(project)
        if root is not None:
            for provider in _project_values(root, "DSP"):
                match = re.search(r"\.(Sql\d+)DatabaseSchemaProvider", provider)
                key = match.group(1) if match else None
                version = SQL_SERVER_DSP.get(key or "")
                if version:
                    _add_version(sql, version, "database-target", "high", _source(index, project, "DSP", provider))
                    if key and key in {"Sql80", "Sql90", "Sql100", "Sql110", "Sql120", "Sql130"}:
                        runtime_evidence.append(
                            {
                                "os": "windows",
                                "confidence": "medium",
                                "path": index.relative(project),
                                "rule": "legacy_sql_server_target",
                                "detail": f"{version} nao possui servidor SQL Server nativo em Linux",
                                "applies_to": ["sql", "tsql"],
                            }
                        )

    for config in migration_files:
        kind = "flyway-config" if config.name.lower() == "flyway.conf" else "liquibase-config"
        _add_build_file(sql, index, config, kind, "migration-config", True)

    for sql_file in sql_files[:1000]:
        text = index.text(sql_file) or ""
        matched_in_file = 0
        for pattern, rule in TSQL_RULES:
            match = re.search(pattern, text)
            if match:
                matched_in_file += 1
                tsql_matches.append(
                    {
                        "path": index.relative(sql_file),
                        "rule": rule,
                        "confidence": "medium" if rule in {"select_top", "sql_server_type", "go_batch_separator"} else "high",
                        "detail": re.sub(r"\s+", " ", match.group(0)).strip()[:160],
                    }
                )
            if matched_in_file >= 4:
                break

    tsql: dict[str, Any] | None = None
    strong_matches = [item for item in tsql_matches if item["confidence"] == "high"]
    if sql_projects or strong_matches or len(tsql_matches) >= 2:
        tsql = _technology("tsql", "T-SQL / SQL Server")
        tsql["evidence"] = [{"type": "dialect_rules", **item} for item in tsql_matches[:50]]
        for build_file in sql["build_files"]:
            if build_file["type"] == "sql-server-database-project":
                tsql["build_files"].append(dict(build_file))
        for version in sql["versions"]:
            tsql["versions"].append(dict(version))
        if not tsql["versions"]:
            tsql["warnings"].append(
                {"code": "sql_server_version_unknown", "message": "T-SQL detectado, mas a versao alvo do SQL Server nao foi encontrada"}
            )
    return sql, tsql, runtime_evidence


def deployment_evidence(index: FileIndex) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    dockerfiles = [
        path
        for path in index.files
        if path.name.lower().startswith("dockerfile") or path.name.lower().endswith(".dockerfile")
    ]
    for dockerfile in dockerfiles:
        text = index.text(dockerfile) or ""
        for match in re.finditer(r"(?im)^\s*FROM\s+(?:--platform=\S+\s+)?([^\s]+)", text):
            image = match.group(1).strip()
            lowered = image.lower()
            applies_to = infer_image_technologies(lowered)
            if any(marker in lowered for marker in ("windowsservercore", "nanoserver", "servercore", "mcr.microsoft.com/windows")):
                evidence.append(
                    runtime_item(index, dockerfile, "windows", "high", "windows_container_base", f"FROM {image}", applies_to)
                )
            elif any(marker in lowered for marker in ("alpine", "ubuntu", "debian", "bookworm", "bullseye", "jammy", "noble", "ubi", "linux")):
                evidence.append(
                    runtime_item(index, dockerfile, "linux", "high", "linux_container_base", f"FROM {image}", applies_to)
                )
            elif applies_to:
                evidence.append(
                    runtime_item(index, dockerfile, "linux", "medium", "default_linux_container_image", f"FROM {image}", applies_to)
                )

    for web_config in index.by_name("web.config", "applicationhost.config"):
        text = index.text(web_config) or ""
        if re.search(r"(?i)<system\.webServer\b|<system\.web\b", text):
            evidence.append(
                runtime_item(index, web_config, "windows", "high", "iis_configuration", "Configuracao IIS/ASP.NET", ["dotnet", "csharp"])
            )

    for service in index.by_suffix(".service"):
        text = index.text(service) or ""
        if "[Service]" in text:
            evidence.append(runtime_item(index, service, "linux", "high", "systemd_unit", "Unidade systemd", ["all"]))

    for manifest in index.by_suffix(".yaml", ".yml"):
        relative = index.relative(manifest).lower()
        if not any(marker in relative for marker in ("k8s", "kubernetes", "helm", "deploy", "chart", "openshift")):
            continue
        text = index.text(manifest) or ""
        os_match = re.search(r"(?im)(?:kubernetes\.io/os|operating-system)\s*:\s*[\"']?(linux|windows)", text)
        if os_match:
            evidence.append(
                runtime_item(index, manifest, os_match.group(1).lower(), "high", "kubernetes_os_selector", os_match.group(0), ["all"])
            )
        elif re.search(r"(?m)^\s*kind\s*:\s*(?:Deployment|StatefulSet|DaemonSet)\s*$", text):
            evidence.append(
                runtime_item(index, manifest, "linux", "low", "kubernetes_default_os", "Workload Kubernetes sem seletor de Windows", ["all"])
            )

    # Script extensions are weak signals and never override explicit deployment evidence.
    shell_scripts = index.by_suffix(".sh")
    if shell_scripts:
        evidence.append(
            runtime_item(index, shell_scripts[0], "linux", "low", "shell_operational_script", f"{len(shell_scripts)} arquivo(s) .sh", ["all"])
        )
    windows_scripts = index.by_suffix(".bat", ".cmd")
    if windows_scripts:
        evidence.append(
            runtime_item(index, windows_scripts[0], "windows", "low", "windows_operational_script", f"{len(windows_scripts)} arquivo(s) .bat/.cmd", ["all"])
        )
    return evidence


def augment_versions_from_container_images(
    technologies: list[dict[str, Any]], evidence: list[dict[str, Any]]
) -> None:
    by_id = {technology["id"]: technology for technology in technologies}
    for item in evidence:
        if "container" not in item.get("rule", ""):
            continue
        detail = item.get("detail", "")
        image_match = re.search(r"(?i)\bFROM\s+([^\s]+)", detail)
        if not image_match:
            continue
        image = image_match.group(1)
        tag = image.rsplit(":", 1)[1] if ":" in image.rsplit("/", 1)[-1] else ""
        version_match = re.match(r"v?(\d+(?:\.\d+){0,2})(?:u\d+)?", tag)
        if not version_match:
            continue
        value = version_match.group(1)
        for identifier in item.get("applies_to", []):
            technology = by_id.get(identifier)
            if technology is None:
                continue
            normalized = normalize_java_version(value) if identifier == "java" else value
            _add_version(
                technology,
                value,
                "container-image",
                "medium",
                {"path": item["path"], "selector": "Dockerfile FROM", "detail": image},
                normalized,
            )
    unknown_codes = {
        "java": "java_version_unknown",
        "nodejs": "node_version_unknown",
        "dotnet": "dotnet_version_unknown",
        "tsql": "sql_server_version_unknown",
    }
    for identifier, technology in by_id.items():
        if technology["versions"] and identifier in unknown_codes:
            technology["warnings"] = [
                warning for warning in technology["warnings"] if warning.get("code") != unknown_codes[identifier]
            ]


def infer_image_technologies(image: str) -> list[str]:
    technologies = []
    if any(marker in image for marker in ("openjdk", "temurin", "corretto", "liberica", "sapmachine", "java")):
        technologies.append("java")
    if re.search(r"(?:^|/)node(?::|@|$)", image):
        technologies.append("nodejs")
    if "mcr.microsoft.com/dotnet" in image or "aspnet" in image:
        technologies.extend(["dotnet", "csharp"])
    if "mssql" in image or "sqlserver" in image:
        technologies.extend(["sql", "tsql"])
    return technologies or ["all"]


def runtime_item(
    index: FileIndex,
    path: Path,
    os_name: str,
    confidence: str,
    rule: str,
    detail: str,
    applies_to: list[str],
) -> dict[str, Any]:
    return {
        "os": os_name,
        "confidence": confidence,
        "path": index.relative(path),
        "rule": rule,
        "detail": re.sub(r"\s+", " ", detail).strip()[:300],
        "applies_to": applies_to,
    }


def infer_technology_runtime(
    technology: dict[str, Any],
    all_evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    identifier = technology["id"]
    if identifier in {"sql", "tsql"}:
        applicable = [item for item in all_evidence if identifier in item.get("applies_to", [])]
        return {
            "classification": classify_runtime(applicable)[0] if applicable else "unknown",
            "confidence": classify_runtime(applicable)[1] if applicable else "low",
            "scope": "database-engine-or-deployment-tooling",
            "evidence": applicable,
            "note": "Arquivos SQL podem ser executados remotamente; o SO do repositorio nao prova o SO do servidor de banco.",
        }

    applicable = [
        item
        for item in all_evidence
        if identifier in item.get("applies_to", []) or "all" in item.get("applies_to", [])
    ]
    explicit = [item for item in applicable if item.get("confidence") in {"high", "medium"}]
    if not explicit and identifier in {"java", "nodejs"}:
        applicable.append(
            {
                "os": "linux",
                "confidence": "low",
                "path": None,
                "rule": "common_server_runtime_heuristic",
                "detail": f"{technology['display_name']} e frequentemente implantado em Linux; confirmar na esteira/CMDB",
                "applies_to": [identifier],
            }
        )
    classification, confidence = classify_runtime(applicable)
    candidates = sorted({item["os"] for item in applicable})
    return {
        "classification": classification,
        "confidence": confidence,
        "candidates": candidates,
        "evidence": applicable,
    }


def classify_runtime(evidence: list[dict[str, Any]]) -> tuple[str, str]:
    scores: Counter[str] = Counter()
    strongest: dict[str, int] = {"linux": 0, "windows": 0}
    for item in evidence:
        os_name = item.get("os")
        if os_name not in {"linux", "windows"}:
            continue
        weight = CONFIDENCE_WEIGHT.get(item.get("confidence", "low"), 1)
        scores[os_name] += weight
        strongest[os_name] = max(strongest[os_name], weight)
    if not scores:
        return "unknown", "low"
    linux, windows = scores["linux"], scores["windows"]
    if linux and windows:
        if strongest["linux"] == 1 and strongest["windows"] == 1:
            return "unknown", "low"
        if strongest["linux"] >= 2 and strongest["windows"] >= 2:
            return "mixed", confidence_name(max(strongest.values()))
        winner = "linux" if linux > windows else "windows" if windows > linux else "mixed"
        if winner == "mixed":
            return winner, "low"
        return winner, confidence_name(strongest[winner])
    winner = "linux" if linux else "windows"
    return winner, confidence_name(strongest[winner])


def confidence_name(weight: int) -> str:
    return {3: "high", 2: "medium"}.get(weight, "low")


def scan_repository(
    root: Path,
    metadata: dict[str, Any] | None = None,
    github_languages: dict[str, int] | None = None,
    max_files: int = 200_000,
) -> dict[str, Any]:
    root = root.resolve()
    index = FileIndex(root, max_files=max_files)
    metadata = metadata or {}
    remote_url = metadata.get("url") or git_value(root, "remote", "get-url", "origin")
    full_name = metadata.get("full_name") or (parse_github_full_name(remote_url) if remote_url else None)
    technologies: list[dict[str, Any]] = []
    extra_runtime_evidence: list[dict[str, Any]] = []

    java = analyze_java(index)
    node = analyze_node(index)
    dotnet, csharp, dotnet_runtime = analyze_dotnet(index)
    sql, tsql, sql_runtime = analyze_sql(index)
    for technology in (java, node, dotnet, csharp, sql, tsql):
        if technology is not None:
            technologies.append(technology)
    extra_runtime_evidence.extend(dotnet_runtime)
    extra_runtime_evidence.extend(sql_runtime)
    all_runtime_evidence = deployment_evidence(index) + extra_runtime_evidence
    augment_versions_from_container_images(technologies, all_runtime_evidence)
    for technology in technologies:
        technology["runtime"] = infer_technology_runtime(technology, all_runtime_evidence)
        technology["build_files"].sort(key=lambda item: (not item["primary"], item["path"]))
        technology["versions"].sort(key=lambda item: (item["kind"], item["value"], item["source"]["path"]))

    language_data = language_percentages(github_languages or {})
    warnings: list[dict[str, Any]] = []
    if index.limit_reached:
        warnings.append(
            {"code": "file_limit_reached", "message": f"Analise interrompida no limite de {max_files} arquivos"}
        )
    if not technologies:
        warnings.append(
            {"code": "no_target_technology", "message": "Nenhuma das tecnologias alvo foi detectada"}
        )
    if any(technology["runtime"]["classification"] == "unknown" for technology in technologies):
        warnings.append(
            {"code": "runtime_not_fully_determined", "message": "O sistema operacional nao pode ser determinado para todas as tecnologias"}
        )

    primary_build_files = []
    for technology in technologies:
        for build_file in technology["build_files"]:
            if build_file["primary"]:
                _append_unique(
                    primary_build_files,
                    {**build_file, "technology": technology["id"]},
                    ("path", "type", "technology"),
                )

    overall_runtime, overall_confidence = classify_runtime(all_runtime_evidence)
    result = {
        "name": metadata.get("name") or root.name,
        "full_name": full_name,
        "url": remote_url,
        "default_branch": metadata.get("default_branch") or git_value(root, "branch", "--show-current"),
        "commit": git_value(root, "rev-parse", "HEAD"),
        "archived": metadata.get("archived"),
        "visibility": metadata.get("visibility"),
        "github_languages": language_data,
        "technologies": technologies,
        "primary_build_files": primary_build_files,
        "deployment": {
            "runtime_os": overall_runtime,
            "confidence": overall_confidence,
            "evidence": all_runtime_evidence,
            "note": "Inferencia estatica; valide o host real em CMDB, manifests de deploy ou inventario de servidores.",
        },
        "warnings": warnings,
        "scan_stats": {
            "files_considered": len(index.files),
            "ignored_directories": index.skipped_directories,
            "file_limit_reached": index.limit_reached,
        },
    }
    return result


def language_percentages(languages: dict[str, int]) -> list[dict[str, Any]]:
    total = sum(max(value, 0) for value in languages.values())
    if total <= 0:
        return []
    return [
        {"name": name, "bytes": value, "percentage": round(value * 100 / total, 2)}
        for name, value in sorted(languages.items(), key=lambda item: (-item[1], item[0].lower()))
    ]


def summarize(repositories: Iterable[dict[str, Any]]) -> dict[str, Any]:
    repos = list(repositories)
    technologies: Counter[str] = Counter()
    runtime_os: Counter[str] = Counter()
    warning_repositories = 0
    for repository in repos:
        technologies.update(item["id"] for item in repository.get("technologies", []))
        runtime_os[repository.get("deployment", {}).get("runtime_os", "unknown")] += 1
        if repository.get("warnings") or any(item.get("warnings") for item in repository.get("technologies", [])):
            warning_repositories += 1
    return {
        "repository_count": len(repos),
        "technology_counts": dict(sorted(technologies.items())),
        "runtime_os_counts": dict(sorted(runtime_os.items())),
        "repositories_with_warnings": warning_repositories,
    }
