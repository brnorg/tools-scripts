# Inventario de tecnologias de repositorios

CLI em Python que analisa repositorios locais ou GitHub e gera inventario em **JSON estruturado ou CSV** (o progresso vai para `stderr`). O foco atual e:

- Java: Maven, Gradle, POM pai, modulos, wrappers e versao do JDK;
- Node.js: `package.json`, workspaces, engines, Volta, `.nvmrc`, lockfiles e package manager;
- .NET e C#: `.sln`, `.csproj`, MSBuild, `global.json`, TFM, SDK e `LangVersion`;
- SQL e T-SQL: scripts, regras do dialeto, `.sqlproj`, Flyway/Liquibase e plataforma SQL Server;
- runtime: Linux, Windows, misto ou desconhecido, sempre com confianca e evidencias.

O JSON segue o contrato versionado em [`repo_inventory/schema.json`](repo_inventory/schema.json). Falhas em um repositorio entram em `errors[]` e nao impedem a analise dos demais. O CSV possui layout versionado e uma linha por tecnologia encontrada em cada repositorio.

## Instalacao

Requer Python 3.10+ e Git no `PATH`.

```powershell
python -m pip install -e .
```

Para usar uma GitHub App, instale tambem a dependencia opcional:

```powershell
python -m pip install -e ".[github-app]"
```

O modo local e o uso de PAT/installation token nao requerem bibliotecas Python externas.

## Uso

### Diretorios locais

```powershell
python -m repo_inventory local C:\repos\sistema-a C:\repos\sistema-b -o inventario.json
```

### Uma ou mais URLs Git

```powershell
$env:GITHUB_TOKEN = "seu-token"
python -m repo_inventory git `
  https://github.com/ORGANIZACAO/repositorio-a.git `
  https://github.com/ORGANIZACAO/repositorio-b.git `
  -o inventario.json
```

### Todos os repositorios de uma organizacao

```powershell
$env:GITHUB_TOKEN = "seu-token"
python -m repo_inventory github `
  --org ORGANIZACAO `
  --repo-regex "^(ORGANIZACAO)/(back|front|database)-" `
  -o inventario.json
```

Por padrao, forks e arquivados sao ignorados. Use `--include-forks`, `--include-archived` ou `--limit 10` quando necessario. Cada clone e raso (`--depth 1`) e fica em um diretorio temporario removido ao fim.

### Somente repositorios escolhidos

Repita `--repo` para analisar apenas os repositorios informados:

```powershell
$env:GITHUB_TOKEN = "seu-token"
python -m repo_inventory github `
  --org ORGANIZACAO `
  --repo sistema-a `
  --repo sistema-b `
  --repo outra-org/biblioteca-compartilhada `
  -o inventario.json
```

Tambem sao aceitas URLs:

```powershell
python -m repo_inventory github `
  --repo https://github.com/ORGANIZACAO/sistema-a.git `
  --repo https://github.com/ORGANIZACAO/sistema-b.git `
  -o inventario.csv
```

Para lotes maiores, use um arquivo UTF-8 com um repositorio por linha. Linhas vazias e linhas iniciadas por `#` sao ignoradas:

```text
# repositorios do inventario de pagamentos
sistema-a
ORGANIZACAO/sistema-b
https://github.com/ORGANIZACAO/database-c.git
```

```powershell
python -m repo_inventory github `
  --org ORGANIZACAO `
  --repo-file repositorios.txt `
  -o inventario.json
```

Nomes curtos, como `sistema-a`, usam o valor de `--org`. Para `owner/repositorio` ou URL completa, `--org` e opcional. Quando `--repo` ou `--repo-file` esta presente, o script consulta diretamente apenas esses repositorios, remove duplicados e nao lista toda a organizacao. Repositorios explicitamente escolhidos sao processados mesmo quando sao forks ou arquivados.

Sem `-o`, o JSON e escrito no `stdout`:

```powershell
python -m repo_inventory local C:\repos\sistema | ConvertFrom-Json
```

### Saida CSV

A extensao `.csv` seleciona o formato automaticamente:

```powershell
python -m repo_inventory github `
  --org ORGANIZACAO `
  -o inventario.csv
```

Para enviar CSV ao `stdout`, informe o formato explicitamente:

```powershell
python -m repo_inventory local C:\repos\sistema --format csv
```

Tambem e possivel forcar JSON independentemente da extensao com `--format json`.

O CSV usa:

- `row_type=technology`: uma linha para cada tecnologia de cada repositorio;
- `row_type=repository`: repositorio analisado sem uma tecnologia alvo detectada;
- `row_type=error`: falha de descoberta, clone ou analise.

As colunas `versions`, `primary_build_files`, `technology_runtime_os` e `technology_runtime_confidence` sao apropriadas para filtros e tabelas. Informacoes aninhadas completas continuam disponiveis em `version_details_json`, `build_files_json`, `runtime_evidence_json` e demais colunas `*_json`.

## Autenticacao

### Token

Defina `GITHUB_TOKEN` com um fine-grained PAT ou um installation access token. Para repositorios privados, o token precisa conseguir ler o conteudo; para listar a organizacao e consultar linguagens, precisa de leitura de metadados.

O nome da variavel pode ser alterado sem expor seu valor na linha de comando:

```powershell
$env:B3SA_INVENTORY_TOKEN = "seu-token"
python -m repo_inventory github --org ORGANIZACAO --token-env B3SA_INVENTORY_TOKEN -o inventario.json
```

### GitHub App

A App deve ser instalada nos repositorios desejados, com permissoes de repositorio **Contents: read-only** e **Metadata: read-only**. Configure:

```powershell
$env:GITHUB_APP_ID = "123456"
$env:GITHUB_APP_INSTALLATION_ID = "789012"
$env:GITHUB_APP_PRIVATE_KEY_PATH = "C:\segredos\inventory-app.pem"
python -m repo_inventory github --org ORGANIZACAO -o inventario.json
```

O script assina um JWT RS256, solicita um installation access token e o utiliza na API e no clone. O token nao e gravado no JSON nem inserido na URL do repositorio. Para GitHub Enterprise Server, informe a raiz da API, por exemplo `--api-url https://github.empresa/api/v3`.

## Estrutura resumida da saida

```json
{
  "schema_version": "1.0.0",
  "generated_at": "2026-07-18T12:00:00Z",
  "generator": {
    "name": "b3sa-repo-inventory",
    "version": "0.1.0"
  },
  "scan": {
    "source": "github-organization",
    "organization": "ORGANIZACAO"
  },
  "summary": {
    "repository_count": 1,
    "technology_counts": { "java": 1, "nodejs": 1 },
    "runtime_os_counts": { "linux": 1 },
    "repositories_with_warnings": 0
  },
  "repositories": [
    {
      "name": "exemplo",
      "technologies": [
        {
          "id": "java",
          "versions": [
            {
              "value": "17",
              "normalized": "17",
              "kind": "release",
              "confidence": "high",
              "source": {
                "path": "pom.xml",
                "selector": "project.properties.maven.compiler.release"
              }
            }
          ],
          "build_files": [
            {
              "path": "pom.xml",
              "type": "maven-pom",
              "role": "parent",
              "primary": true
            }
          ],
          "runtime": {
            "classification": "linux",
            "confidence": "high",
            "candidates": ["linux"],
            "evidence": [
              {
                "os": "linux",
                "confidence": "high",
                "path": "Dockerfile",
                "rule": "linux_container_base",
                "detail": "FROM eclipse-temurin:17-jre-alpine",
                "applies_to": ["java"]
              }
            ]
          }
        }
      ]
    }
  ],
  "errors": []
}
```

O exemplo foi abreviado; o arquivo real contem todas as propriedades exigidas pelo schema.

## Como a decisao e feita

O endpoint de linguagens do GitHub e consultado como informacao complementar. Ele informa bytes por linguagem, por isso ajuda a localizar repositorios relevantes, mas nao determina versao ou ambiente de execucao.

As versoes vem de declaracoes concretas, entre outras:

- Java: `maven.compiler.release`, `java.version`, compiler plugin, Gradle toolchain, `.java-version`, SDKMAN e tag de imagem;
- Node.js: `engines.node`, Volta, `.nvmrc`, `.node-version` e tag de imagem;
- .NET: `TargetFramework(s)`, `TargetFrameworkVersion`, `global.json` e tag de imagem;
- SQL Server: `DSP` do `.sqlproj` e imagem de container.

Arquivos de build recebem `role` e `primary`. Um POM com `packaging=pom` ou `modules` e marcado como `parent`; manifests sem outro manifest ancestral sao candidatos principais; solucoes `.sln` sao principais e seus projetos sao listados separadamente.

Para runtime, as regras fortes incluem imagens Windows/Linux, IIS, systemd, seletor de SO em Kubernetes, .NET Framework/WPF/Windows Forms e versoes antigas de SQL Server. Scripts `.sh`/`.cmd` sao apenas sinais fracos. Java e Node.js sem evidencia de deploy recebem Linux com confianca baixa, explicitamente como heuristica.

## Limites intencionais

- A analise e estatica. O host real deve ser confirmado em CMDB, manifests de deploy, Ansible, pipelines ou inventario de servidores.
- `net6.0`, `net8.0` etc. sao multiplataforma; a versao sozinha nao prova Linux ou Windows. `net48`/`.NET Framework`, WPF, Windows Forms e IIS sao evidencias de Windows.
- Um repositorio de scripts SQL pode executar contra um banco remoto. O SO das ferramentas do repositorio nao prova o SO do servidor do banco.
- Propriedades herdadas de POMs/props externos e versoes injetadas apenas pela pipeline podem permanecer desconhecidas.
- O clone raso analisa o estado da branch padrao, nao o historico.

Esses casos ficam como `unknown` ou geram `warnings[]`; o script nao inventa uma versao ausente.

## Testes

```powershell
python -m unittest discover -v
```

Os testes cobrem monorepo Maven + Node em Linux, .NET Framework + SQL Server/T-SQL em Windows, serializacao e validacao do JSON Schema.

## Referencias de implementacao

- [GitHub REST: repositorios e linguagens](https://docs.github.com/en/rest/repos/repos)
- [GitHub App: installation access token](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app)
- [Microsoft: target frameworks .NET](https://learn.microsoft.com/en-us/dotnet/standard/frameworks)
- [Microsoft: target platform de SQL projects](https://learn.microsoft.com/en-us/sql/tools/sql-database-projects/concepts/target-platform)
