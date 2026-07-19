from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from repo_inventory.cli import (
    normalize_repository_identifier,
    parser,
    requested_repository_names,
    resolve_output_format,
    run,
    write_inventory,
)
from repo_inventory.github import GitHubError
from repo_inventory.scanner import scan_repository


class ScannerTests(unittest.TestCase):
    def write(self, root: Path, relative: str, content: str) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def technologies(self, result: dict) -> dict[str, dict]:
        return {item["id"]: item for item in result["technologies"]}

    def test_java_node_monorepo_versions_builds_and_linux_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "monorepo"
            root.mkdir()
            self.write(
                root,
                "pom.xml",
                """<project xmlns="http://maven.apache.org/POM/4.0.0">
                <modelVersion>4.0.0</modelVersion><groupId>x</groupId><artifactId>parent</artifactId>
                <version>1</version><packaging>pom</packaging><modules><module>backend</module></modules>
                <properties><java.version>17</java.version><maven.compiler.release>${java.version}</maven.compiler.release></properties>
                </project>""",
            )
            self.write(
                root,
                "backend/pom.xml",
                """<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>
                <parent><groupId>x</groupId><artifactId>parent</artifactId><version>1</version></parent>
                <artifactId>backend</artifactId></project>""",
            )
            self.write(root, "backend/src/main/java/X.java", "class X {}")
            self.write(root, "backend/Dockerfile", "FROM eclipse-temurin:17-jre-alpine\n")
            self.write(
                root,
                "frontend/package.json",
                json.dumps(
                    {
                        "name": "frontend",
                        "engines": {"node": ">=18 <21"},
                        "packageManager": "pnpm@8.15.0",
                        "dependencies": {"react": "^18.2.0"},
                    }
                ),
            )
            self.write(root, "frontend/Dockerfile", "FROM node:18-alpine\n")

            result = scan_repository(root)
            technologies = self.technologies(result)
            self.assertEqual({"java", "nodejs"}, set(technologies))
            java_versions = technologies["java"]["versions"]
            self.assertTrue(any(item.get("normalized") == "17" for item in java_versions))
            root_pom = next(item for item in technologies["java"]["build_files"] if item["path"] == "pom.xml")
            self.assertTrue(root_pom["primary"])
            self.assertEqual("parent", root_pom["role"])
            self.assertEqual("linux", technologies["java"]["runtime"]["classification"])
            self.assertEqual("high", technologies["java"]["runtime"]["confidence"])
            self.assertEqual("linux", technologies["nodejs"]["runtime"]["classification"])
            self.assertTrue(any(item["name"] == "pnpm" and item["version"] == "8.15.0" for item in technologies["nodejs"]["build_tools"]))

    def test_legacy_dotnet_and_tsql_are_identified(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "legacy"
            root.mkdir()
            self.write(
                root,
                "Legacy/Legacy.csproj",
                """<Project ToolsVersion="15.0" xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
                <PropertyGroup><TargetFrameworkVersion>v4.7.2</TargetFrameworkVersion></PropertyGroup></Project>""",
            )
            self.write(root, "Legacy/Program.cs", "class Program { static void Main() {} }")
            self.write(root, "Legacy/web.config", "<configuration><system.webServer /></configuration>")
            self.write(
                root,
                "Database/Database.sqlproj",
                """<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003"><PropertyGroup>
                <DSP>Microsoft.Data.Tools.Schema.Sql.Sql130DatabaseSchemaProvider</DSP>
                </PropertyGroup></Project>""",
            )
            self.write(
                root,
                "Database/procedure.sql",
                "SET ANSI_NULLS ON\nGO\nCREATE PROCEDURE [dbo].[p] AS SELECT TOP 1 [Id] FROM [dbo].[T];\n",
            )

            result = scan_repository(root)
            technologies = self.technologies(result)
            self.assertTrue({"dotnet", "csharp", "sql", "tsql"}.issubset(technologies))
            self.assertTrue(any(item.get("normalized") == "net472" for item in technologies["dotnet"]["versions"]))
            self.assertEqual("windows", technologies["dotnet"]["runtime"]["classification"])
            self.assertEqual("high", technologies["dotnet"]["runtime"]["confidence"])
            self.assertTrue(any(item["value"] == "SQL Server 2016" for item in technologies["tsql"]["versions"]))
            self.assertEqual("windows", result["deployment"]["runtime_os"])

    def test_cli_result_has_versioned_structured_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "simple"
            root.mkdir()
            self.write(root, "package.json", '{"name":"simple","engines":{"node":"20"}}')
            args = parser().parse_args(["local", str(root)])
            inventory, exit_code = run(args)
            self.assertEqual(0, exit_code)
            self.assertEqual("1.0.0", inventory["schema_version"])
            self.assertEqual(1, inventory["summary"]["repository_count"])
            json.dumps(inventory)

    def test_output_validates_against_bundled_schema_when_jsonschema_is_available(self) -> None:
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema optional dependency is not installed")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "simple"
            root.mkdir()
            self.write(root, "pom.xml", "<project><modelVersion>4.0.0</modelVersion></project>")
            args = parser().parse_args(["local", str(root)])
            inventory, _ = run(args)
            schema_path = Path(__file__).parents[1] / "repo_inventory" / "schema.json"
            jsonschema.Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8"))).validate(inventory)

    def test_csv_has_one_row_per_technology_and_preserves_details(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "mixed"
            root.mkdir()
            self.write(root, "package.json", '{"name":"simple","engines":{"node":"20"}}')
            self.write(
                root,
                "pom.xml",
                "<project><modelVersion>4.0.0</modelVersion><properties><java.version>17</java.version></properties></project>",
            )
            args = parser().parse_args(["local", str(root)])
            inventory, _ = run(args)
            output = Path(temp) / "inventory.csv"
            write_inventory(inventory, str(output), compact=False, requested_format="auto")

            self.assertEqual("csv", resolve_output_format(str(output), "auto"))
            with output.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual({"java", "nodejs"}, {row["technology_id"] for row in rows})
            node = next(row for row in rows if row["technology_id"] == "nodejs")
            self.assertEqual("technology", node["row_type"])
            self.assertIn("engine-range:20", node["versions"])
            self.assertEqual("package.json", node["primary_build_files"])
            self.assertTrue(json.loads(node["version_details_json"]))

    def test_explicit_github_repository_list_accepts_names_urls_and_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repository_file = Path(temp) / "repositories.txt"
            repository_file.write_text(
                "# repositorios do lote\n"
                "acme/repo-a\n"
                "https://github.com/ACME/repo-b.git\n"
                "git@github.com:ACME/repo-c.git\n",
                encoding="utf-8",
            )
            args = parser().parse_args(
                [
                    "github",
                    "--org",
                    "ACME",
                    "--repo",
                    "repo-a",
                    "--repo-file",
                    str(repository_file),
                ]
            )
            self.assertEqual(
                ["ACME/repo-a", "ACME/repo-b", "ACME/repo-c"],
                requested_repository_names(args),
            )

    def test_short_repository_requires_organization(self) -> None:
        with self.assertRaises(GitHubError):
            normalize_repository_identifier("repo-a", None)

    def test_empty_explicit_repository_file_does_not_fall_back_to_full_organization(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repository_file = Path(temp) / "repositories.txt"
            repository_file.write_text("# nenhum repositorio selecionado\n", encoding="utf-8")
            args = parser().parse_args(
                ["github", "--org", "ACME", "--repo-file", str(repository_file)]
            )
            with self.assertRaises(GitHubError):
                requested_repository_names(args)


if __name__ == "__main__":
    unittest.main()
