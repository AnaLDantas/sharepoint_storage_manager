# Inventario Microsoft 365 SharePoint / OneDrive

Aplicacao Python para catalogar sites, bibliotecas, pastas e arquivos de um
tenant Microsoft 365 usando Microsoft Graph API. O projeto foi simplificado para
um fluxo unico: coleta por delta, checkpoint em SQLite, exportacao Parquet
particionada e modelagem Lakehouse.

## Fluxo principal

```bash
python main.py prioritize-sites --period D180
python main.py crawl
python main.py export
python main.py summary
python -m streamlit run dashboard/app.py
```

O comando `crawl` usa Microsoft Graph `/root/delta` por drive. Na primeira
execucao ele faz a carga completa em paginas e salva `nextLink` a cada pagina
gravada. Ao concluir um drive, salva `deltaLink`; nas execucoes seguintes, busca
somente alteracoes.

## O que o projeto faz

- Descobre sites do SharePoint Online e bibliotecas/drives.
- Opcionalmente inventaria OneDrive for Business por usuario.
- Sincroniza arquivos e pastas por delta, com checkpoint por drive em SQLite.
- Retoma execucoes interrompidas sem reiniciar drives concluidos.
- Controla paginacao, retries, throttling `429` e concorrencia.
- Exporta o inventario em dataset Parquet particionado.
- Gera automaticamente Lakehouse em `data/bronze`, `data/silver` e `data/gold`.
- Disponibiliza notebook DuckDB e dashboard Streamlit para analise.

## Estrutura

```text
main.py
config.py
graph_client.py
database.py
crawler.py
exporter.py
lakehouse.py
models.py
priority.py
dashboard/
notebooks/analyze_inventory_parquet_duckdb.ipynb
inventory/sharepoint_inventory.sqlite3
exports/inventory_parquet/
data/
  bronze/
  silver/
  gold/
requirements.txt
.env.example
README.md
```

## Instalacao

Use Python 3.11 ou 3.12.

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

macOS/Linux:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

## Configuracao

Edite o arquivo `.env`:

```env
TENANT_ID=seu-tenant-id
CLIENT_ID=seu-client-id
CLIENT_SECRET=seu-client-secret

MAX_WORKERS=4
REQUEST_TIMEOUT=60
SQLITE_DB_PATH=./inventory/sharepoint_inventory.sqlite3
EXPORT_PATH=./exports
LOG_LEVEL=INFO
PROGRESS_LOG_INTERVAL_SECONDS=60

ENABLE_USER_ONEDRIVE=false
SITE_SEARCH_QUERY=*
SITE_IDS_FILE=
SITE_IDS=
```

Para tenants grandes, comece com `MAX_WORKERS=2` ou `4`. Aumente apenas se o
Graph nao estiver retornando `429 Too Many Requests` com frequencia.

### Azure App Registration

1. Acesse Microsoft Entra ID > App registrations > New registration.
2. Crie um client secret em Certificates & secrets.
3. Em API permissions, adicione Microsoft Graph > Application permissions.
4. Para SharePoint, conceda:
   - `Sites.Read.All`
   - `Files.Read.All`
5. Para ranking por armazenamento, conceda tambem:
   - `Reports.Read.All`
6. Para OneDrive for Business, habilite `ENABLE_USER_ONEDRIVE=true` e conceda:
   - `User.Read.All`
   - `Files.Read.All`
7. Clique em Grant admin consent.

`Sites.Selected` pode restringir escopo, mas exige permissao explicita por site.
Para inventario completo do tenant, `Sites.Read.All` e `Files.Read.All` sao mais
simples operacionalmente.

## Comandos

### Priorizar sites

```bash
python main.py prioritize-sites --period D7
```

Usa o relatorio Microsoft Graph `getSharePointSiteUsageDetail`, requer
`Reports.Read.All` e gera arquivos em `exports/` com ranking e listas de IDs
para orientar a coleta.

### Coletar inventario

```bash
python main.py crawl
```

Para limitar a coleta a sites especificos, preencha uma destas opcoes no `.env`:

```env
SITE_IDS_FILE=./exports/site_ids_over_1tb.txt
```

```env
SITE_IDS=d0f8a32b-aa1c-4737-b54b-534aec98e889,0672061e-ab01-4fc6-aa28-7c701bf96286
```

Quando `SITE_IDS_FILE` ou `SITE_IDS` estiver preenchido, o crawler processa
somente esses sites, preservando a ordem informada.

### Retomar coleta

```bash
python main.py resume
```

Use quando a execucao anterior parou no meio de uma sincronizacao delta. O
comando continua dos `nextLink`/`deltaLink` salvos.

### Resetar sites

Um site:

```bash
python main.py reset-site --site-id "tenant.sharepoint.com,siteCollectionId,webId"
```

Varios sites:

```bash
python main.py reset-site --site-ids-file ./exports/site_ids_over_1tb.txt
```

O reset apaga do SQLite os itens, erros e checkpoints delta do site, incluindo
`next_link` e `delta_link`. Os metadados de site/drive sao preservados, mas
marcados como nao processados. Depois rode:

```bash
python main.py crawl
```

### Exportar

```bash
python main.py export
```

O export gera:

- `exports/inventory_parquet/part-00000.parquet`, `part-00001.parquet` etc.
- `data/bronze/*.parquet`
- `data/silver/*.parquet`
- `data/gold/*.parquet`

Ajustar linhas por parte:

```bash
python main.py export --parquet-rows-per-file 500000
```

Recalcular agregados de pastas antes da exportacao:

```bash
python main.py export --recalculate-folders
```

### Resumo

```bash
python main.py summary
python main.py summary --recalculate-folders
```

Em bases grandes, o recalculo de agregados nao roda por padrao para evitar
atualizar milhoes de registros em toda consulta.

### Reconstruir Lakehouse

```bash
python main.py lakehouse
```

Camada especifica:

```bash
python main.py lakehouse --layer bronze
python main.py lakehouse --layer silver
python main.py lakehouse --layer gold
```

Outro diretorio:

```bash
python main.py lakehouse --data-dir ./clients/ClienteA/data
```

## Lakehouse

### Bronze

Diretorio: `data/bronze/`

- `sites.parquet`
- `drives.parquet`
- `files.parquet`

Preserva dados brutos extraidos do Microsoft Graph, incluindo `raw_json` e
colunas `raw_*` quando disponiveis.

### Silver

Diretorio: `data/silver/`

- `sites.parquet`
- `drives.parquet`
- `files.parquet`

Normaliza dados para analise:

- textos padronizados de site, biblioteca e extensao
- datas convertidas para datetime
- `size_kb`, `size_mb`, `size_gb`
- `created_date`, `modified_date`
- `days_since_modified`
- `usage_status`
- `extension_category`
- enriquecimento com nome/URL do site e biblioteca

Regras de `usage_status`:

- 0 a 90 dias: `Active`
- 91 a 180 dias: `Low Usage`
- 181 a 365 dias: `Unused`
- Mais de 365 dias: `Archive Candidate`

### Gold

Diretorio: `data/gold/`

- `storage_kpis.parquet`: indicadores por site.
- `top_sites.parquet`: ranking dos maiores sites.
- `top_extensions.parquet`: ranking de extensoes.
- `inactive_sites.parquet`: sites sem atualizacao recente.
- `archive_candidates.parquet`: arquivos elegiveis para arquivamento.
- `storage_savings.parquet`: estimativa de economia por site.

A camada Gold e a camada recomendada para dashboards, relatorios executivos e
consultas com DuckDB, Power BI, Fabric, Databricks ou Streamlit.

## Dashboard e notebook

Dashboard:

```bash
python main.py prioritize-sites --period D180
python main.py export
python -m streamlit run dashboard/app.py
```

O dashboard le `exports/inventory_parquet/` e, quando existir,
`exports/site_priority.csv`. Se o inventario exportado nao existir, mas
`data/gold/storage_kpis.parquet` estiver disponivel, ele abre uma visao
executiva baseada na camada Gold.

Estrutura para multiplos clientes:

```text
clients/
  Cliente A/
    exports/
      inventory_parquet/
      site_priority.csv
    data/
      gold/
    inventory/
      sharepoint_inventory.sqlite3
```

Notebook:

```bash
python main.py export
jupyter notebook notebooks/analyze_inventory_parquet_duckdb.ipynb
```

O notebook consulta `exports/inventory_parquet/*.parquet` com DuckDB.

## Checkpoints

O SQLite guarda:

- `sites`: sites encontrados.
- `drives`: bibliotecas/drives encontrados.
- `items`: arquivos e pastas catalogados.
- `drive_sync_state`: estado delta por drive.
- `errors`: falhas para auditoria.
- `runs`: historico de execucoes.

Estado delta principal:

- `next_link`: proxima pagina da carga atual, salvo apos gravar a pagina.
- `delta_link`: token final salvo quando o drive termina a sincronizacao.
- `status`: `pending`, `in_progress`, `done` ou `failed`.

Se o processo parar, drives `in_progress` voltam para `pending` no proximo
`resume`. Se o Graph retornar token delta expirado ou invalido, o drive limpa
`next_link`/`delta_link` e refaz a carga completa na proxima tentativa. Itens
removidos retornados pelo delta sao marcados como `deleted` no SQLite e ficam
fora das exportacoes e dos resumos.

## Throttling e erros

O Microsoft Graph pode responder `429 Too Many Requests`. O cliente:

- le o header `Retry-After`
- aguarda o numero de segundos informado
- usa exponential backoff com jitter quando o header nao vem
- limita o numero de tentativas
- registra throttles e retries nas estatisticas finais
- reduz temporariamente a concorrencia efetiva quando ha retries

Erros tratados:

- `401` / `403`: normalmente permissao ou token.
- `404`: item removido ou inacessivel.
- `408`: retryable.
- `429`: retry com `Retry-After`.
- `500` / `502` / `503` / `504`: retryable.
- Timeout e falha de rede: retryable.

## Exportacao gerada

Dataset principal:

- `exports/inventory_parquet/`

Campos principais:

- tenant/site e URL do site
- biblioteca/drive
- caminho completo da pasta
- nome do arquivo ou pasta
- tipo do item
- extensao e MIME
- tamanho em bytes e tamanho formatado
- criacao e modificacao
- ultimo uso/acesso, quando a API disponibilizar
- quantidade de arquivos dentro da pasta
- volume total da pasta
- ID do item e ID do drive
- status da leitura
- data/hora da coleta

## Limitacoes conhecidas

- Nem todos os tenants ou itens retornam `lastAccessedDateTime`.
- Metadados variam conforme tipo de drive, biblioteca, politica do tenant e
  permissao concedida.
- Para extracoes massivas de Microsoft 365, avalie Microsoft Graph Data Connect
  quando aplicavel, pois cargas muito grandes podem sofrer throttling na API
  REST.
- OneDrive for Business exige enumerar usuarios e acessar `/users/{id}/drive`,
  com permissoes extras.

## Boas praticas para tenants grandes

- Rode em uma VM/servidor estavel, com disco local rapido para o SQLite.
- Comece com `MAX_WORKERS=2` ou `4`.
- Monitore logs de `429` e retries.
- Faca backup periodico do arquivo `.sqlite3` se a execucao durar varios dias.
- Execute exportacoes depois da coleta ou em janelas de baixa atividade.
- Nao apague o SQLite entre `crawl` e `resume`.
- Para sites gigantes, rode primeiro `prioritize-sites` e use os arquivos
  `site_ids_*` com `crawl`.

## Referencias Microsoft

- Microsoft Graph throttling: https://learn.microsoft.com/en-us/graph/throttling
- Microsoft Graph service-specific throttling limits: https://learn.microsoft.com/en-us/graph/throttling-limits
- driveItem delta: https://learn.microsoft.com/en-us/graph/api/driveitem-delta
- Microsoft Graph permissions reference: https://learn.microsoft.com/en-us/graph/permissions-reference
- SharePoint Online throttling guidance: https://learn.microsoft.com/en-us/sharepoint/dev/general-development/how-to-avoid-getting-throttled-or-blocked-in-sharepoint-online
