# Inventário de branches em GitHub Actions

Esta ferramenta percorre os repositórios acessíveis a uma instalação de GitHub App, lê pela API os arquivos `.yml` e `.yaml` em `.github/workflows` na **branch padrão** e grava no CSV os filtros de branch configurados nos eventos:

- `push`
- `pull_request`
- `pull_request_target`
- `workflow_run`

Uma linha com `filter_type=implicit_all` e `branch_pattern=*` significa que o evento não restringe branches. Um workflow sem um desses eventos também aparece no CSV, mas com os campos de evento/filtro vazios.

> O script inventaria branches **referenciadas pelos workflows**, não todas as branches Git nas quais cada arquivo existe. Enumerar toda combinação branch × repositório seria uma operação muito mais cara para 11 mil repositórios.

## GitHub App

Crie/instale um GitHub App na organização com:

- Repository permissions → **Contents: Read-only**
- Repository access → **All repositories** (ou todos os repositórios que devem entrar no inventário)
- Nenhum webhook é necessário.

Anote o Client ID (recomendado) ou App ID e gere uma chave privada PEM. O Installation ID é descoberto automaticamente pela organização; se ele for informado e estiver incorreto, o script usa o valor descoberto e emite um aviso. A chave nunca deve ser versionada.

## Instalação e execução

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

$env:GITHUB_ORG = "minha-org"
$env:GITHUB_APP_ID = "123456"
# Alternativa recomendada no GitHub.com: $env:GITHUB_CLIENT_ID = "Iv1..."
# GITHUB_INSTALLATION_ID é opcional
$env:GITHUB_PRIVATE_KEY_PATH = "C:\segredos\app.private-key.pem"

python workflow_branch_inventory.py --workers 4
```

Arquivos gerados:

- `workflow_inventory.sqlite3`: checkpoint e resultados transacionais.
- `workflow_branches.csv`: resultado final em UTF-8 com BOM, adequado para Excel.

Ao reiniciar com os mesmos argumentos, repositórios concluídos são ignorados; itens interrompidos ou com erro são tentados novamente. O token de instalação, válido por uma hora, é renovado automaticamente. Ao atingir limite primário ou secundário, o processo respeita `Retry-After` ou `X-RateLimit-Reset` e continua depois da espera.

Todas as chamadas ignoram a validação de certificado SSL, inclusive as chamadas de autenticação. Isso permite certificados self-signed, expirados ou assinados por uma CA corporativa não instalada, mas remove a proteção contra interceptação TLS.

Antes da coleta, o script valida `GET /app`, descobre a instalação com `GET /orgs/{org}/installation`, cria o token com `POST /app/installations/{id}/access_tokens` e testa `GET /installation/repositories`. Erros incluem agora o status HTTP, a URL efetiva e a resposta do GitHub.

No GitHub.com, use `https://api.github.com`. No GitHub Enterprise Server, use obrigatoriamente a base `https://HOSTNAME/api/v3`:

```powershell
$env:GITHUB_API_URL = "https://github.empresa.local/api/v3"
```

Para validar apenas autenticação, Installation ID e endpoints, sem iniciar a coleta:

```powershell
python workflow_branch_inventory.py --validate-only --log-level INFO
```

Para apenas reconstruir o CSV a partir do checkpoint:

```powershell
python workflow_branch_inventory.py --export-only --output workflow_branches.csv
```

Opções úteis:

```text
--workers 4             paralelismo (comece com 4 para evitar limite secundário)
--include-archived      inclui repositórios arquivados/desabilitados
--no-retry-errors       não tenta novamente os erros salvos
--checkpoint ARQUIVO    caminho do banco/checkpoint
--output ARQUIVO        caminho do CSV
--api-url URL           endpoint de GitHub Enterprise Server
```

## Colunas do CSV

`repository`, `default_branch`, `workflow_path`, `workflow_name`, `event`, `filter_type`, `branch_pattern`, `parse_status`, `detail`.

Se um YAML estiver inválido, ele é preservado no relatório com `parse_status=yaml_error`, em vez de interromper a coleta.
