# Inventario Microsoft 365 SharePoint / OneDrive

Aplicacao Python para catalogar sites, bibliotecas, pastas e arquivos de um tenant Microsoft 365 usando Microsoft Graph API, com checkpoint em SQLite para execucoes longas.

## Recursos

- Autenticacao por Azure App Registration com `TENANT_ID`, `CLIENT_ID` e `CLIENT_SECRET`.
- Descoberta de sites do SharePoint Online e drives/bibliotecas.
- Opcionalmente, descoberta de OneDrive for Business por usuario.
- Sincronizacao otimizada por drive usando Microsoft Graph `/root/delta`.
- Checkpoint por pagina delta com `@odata.nextLink` e `@odata.deltaLink`.
- Fila local de pastas em SQLite para retomar coletas interrompidas.
- Paginacao por `@odata.nextLink`.
- Retry para 408, 429, 500, 502, 503 e 504.
- Respeito ao header `Retry-After` em HTTP 429 e exponential backoff quando o header nao vem.
- Concorrencia limitada por `MAX_WORKERS`.
- Reducao temporaria de concorrencia no modo delta quando ha retries/throttling.
- Gravacao em lote por pagina/pasta no SQLite.
- Exportacao CSV e Parquet.
- Resumos por extensao e por pasta.
- Analise exploratoria do Parquet com DuckDB em notebook.

## Estrutura

```text
main.py
config.py
graph_client.py
database.py
crawler.py
exporter.py
models.py
priority.py
notebooks/analyze_inventory_parquet_duckdb.ipynb
inventory/sharepoint_inventory.sqlite3
exports/
requirements.txt
.env.example
README.md
```

## Instalacao

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Para usar o notebook de analise do Parquet, instale tambem as dependencias exploratorias:

```bash
python -m pip install duckdb pandas pyarrow jupyter
```

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
GRAPH_PAGE_SIZE=999
STORE_RAW_JSON=false
CALCULATE_FOLDER_AGGREGATES_ON_CRAWL=false
PROGRESS_LOG_INTERVAL_SECONDS=60
ENABLE_USER_ONEDRIVE=false
SITE_SEARCH_QUERY=*
SITE_IDS_FILE=
SITE_IDS=
```

Para ambientes grandes, comece com `MAX_WORKERS=2` ou `4`. Aumente apenas se o tenant nao estiver retornando 429 com frequencia.

## Azure App Registration

1. Acesse Microsoft Entra ID > App registrations > New registration.
2. Crie um client secret em Certificates & secrets.
3. Em API permissions, adicione Microsoft Graph > Application permissions.
4. Permissoes recomendadas para SharePoint:
   - `Sites.Read.All`
   - `Files.Read.All`
5. Para gerar ranking rapido por armazenamento antes da coleta, adicione:
   - `Reports.Read.All`
6. Para inventariar OneDrive for Business por usuario, habilite `ENABLE_USER_ONEDRIVE=true` e adicione:
   - `User.Read.All`
   - `Files.Read.All`
7. Clique em Grant admin consent.

Observacao: `Sites.Selected` pode ser usado para restringir escopo, mas exige concessao explicita em cada site. Para inventario completo do tenant, `Sites.Read.All` e `Files.Read.All` sao mais simples operacionalmente.

## Comandos

Gerar ranking de prioridade dos sites por armazenamento usado:

```bash
python main.py prioritize-sites --period D180
```

Esse comando usa o relatorio Microsoft Graph `getSharePointSiteUsageDetail` e requer a permissao Application `Reports.Read.All`. Ele nao varre arquivo por arquivo; baixa o relatorio de uso do Microsoft 365 e gera arquivos em `exports/` com ranking, grupos por faixa de armazenamento e listas de IDs de sites para orientar a coleta.

Executar coleta completa:

```bash
python main.py crawl
```

Executar coleta otimizada por delta, recomendada para arvores grandes:

```bash
python main.py crawl-delta
```

O modo `crawl-delta` descobre sites/drives e sincroniza cada biblioteca por `/drives/{drive-id}/root/delta`. Na primeira execucao, ele enumera a biblioteca completa por paginas. Ao concluir, salva o `@odata.deltaLink`; nas execucoes seguintes, busca somente alteracoes. Durante a carga, cada `@odata.nextLink` e salvo no SQLite, entao uma interrupcao retoma da ultima pagina confirmada.

Executar coleta limitada a uma lista de IDs de sites:

```env
SITE_IDS_FILE=./exports/site_ids_over_1tb.txt
```

```bash
python main.py crawl
```

Para usar delta com uma lista limitada de sites:

```bash
python main.py crawl-delta
```

Para refazer do zero um site especifico, informe somente esse site em `SITE_IDS`
e use `--full-resync`. Esse modo limpa o checkpoint delta e os itens ja
catalogados daquele site antes de reenumerar as bibliotecas:

```bash
SITE_IDS=d0f8a32b-aa1c-4737-b54b-534aec98e889 python main.py crawl-delta --full-resync
python main.py export --format parquet
```

Tambem e possivel informar IDs diretamente no `.env`:

```env
SITE_IDS=d0f8a32b-aa1c-4737-b54b-534aec98e889,0672061e-ab01-4fc6-aa28-7c701bf96286
```

Quando `SITE_IDS_FILE` ou `SITE_IDS` estiver preenchido, o crawler processa somente esses sites, preservando a ordem informada. Se os IDs vierem do relatorio de uso, o script tenta resolver o ID simples para o ID completo do Graph usando `/sites?search=*`.

Continuar coleta interrompida:

```bash
python main.py resume
```

Continuar coleta delta interrompida:

```bash
python main.py resume-delta
```

Use `resume-delta` quando a execucao parou no meio de uma sincronizacao delta e voce quer continuar dos `nextLink`/`deltaLink` ja salvos, sem redescobrir sites.

Reprocessar erros retryable:

```bash
python main.py retry-errors
```

Exportar CSV:

```bash
python main.py export --format csv
```

Exportar Parquet:

```bash
python main.py export --format parquet
```

Exportar ambos:

```bash
python main.py export --format all
```

Gerar resumo consolidado:

```bash
python main.py summary
```

Analisar metadados do Parquet em notebook:

```bash
python main.py export --format parquet
jupyter notebook notebooks/analyze_inventory_parquet_duckdb.ipynb
```

O notebook usa DuckDB para consultar `exports/inventory.parquet` direto no disco. Ele mostra schema, contagens, amostras de arquivos, extensoes mais comuns, bibliotecas por volume, pastas por volume calculado e verificacoes basicas de qualidade dos metadados.

Se o notebook nao reconhecer `duckdb`, execute a celula de instalacao nele ou instale na venv:

```bash
python -m pip install duckdb pandas pyarrow jupyter
```

Executar dashboard Streamlit:

```bash
python main.py prioritize-sites --period D180
python main.py export --format parquet
streamlit run dashboard/app.py
```

O dashboard le `exports/inventory.parquet` e, quando existir, `exports/site_priority.csv`.
Ele mostra ranking de sites por armazenamento, extensoes que mais ocupam espaco,
arquivos sem modificacao ha mais de 1 ano e sites inativos pelo campo
`last_activity_date` do relatorio de uso do Microsoft 365.

Para navegar entre clientes no dashboard, mantenha cada base em uma pasta dentro
de `clients/` seguindo a mesma estrutura do projeto:

```text
clients/
  Cliente A/
    exports/
      inventory.parquet
      site_priority.csv
    inventory/
      sharepoint_inventory.sqlite3
  Cliente B/
    exports/
      inventory.parquet
      site_priority.csv
    inventory/
      sharepoint_inventory.sqlite3
```

O menu lateral do Streamlit inicia recolhido e pode ser usado como menu interno
para selecionar a base do cliente. O filtro por site fica no topo do dashboard.

## Como o checkpoint funciona

O SQLite guarda tudo que ja foi descoberto e lido:

- `sites`: sites encontrados.
- `drives`: bibliotecas/drives encontrados.
- `items`: arquivos e pastas catalogados.
- `drive_sync_state`: estado delta por drive, incluindo `next_link`, `delta_link`, status, tentativas e ultimo erro.
- `folders_queue`: fila de pastas pendentes, em andamento, concluidas ou com falha.
- `errors`: falhas para auditoria e reprocessamento.
- `runs`: historico de execucoes.

Uma pasta so e marcada como `done` depois que todos os seus filhos foram lidos e gravados. Se o processo parar no meio, pastas `in_progress` voltam para `pending` no proximo `crawl` ou `resume`. Como `items` usa chave primaria `(drive_id, id)`, reler uma pasta nao duplica registros: os itens existentes sao atualizados.

No modo delta, o checkpoint fica em `drive_sync_state`:

- `next_link`: proxima pagina da carga atual. E atualizado depois que a pagina foi gravada no SQLite.
- `delta_link`: token final de sincronizacao. E salvo quando o drive termina a enumeracao.
- `status`: `pending`, `in_progress`, `done` ou `failed`.
- Se o processo parar, drives `in_progress` voltam para `pending` no proximo `resume-delta`.
- Se o Graph retornar token delta expirado/invalido, o drive limpa `next_link`/`delta_link` e refaz carga completa na proxima tentativa.
- Itens removidos retornados pelo delta sao marcados como `deleted` no SQLite e ficam fora das exportacoes e dos resumos.

## Como o modo delta funciona

1. O script descobre sites e drives normalmente.
2. Cada drive entra em `drive_sync_state`.
3. Um worker pega um drive `pending`.
4. Se existe `next_link`, continua a pagina interrompida.
5. Se existe `delta_link`, busca somente alteracoes desde a ultima conclusao.
6. Se nao existe token salvo, inicia `/drives/{drive-id}/root/delta`.
7. Cada pagina grava itens em lote no SQLite e atualiza `next_link`.
8. Quando o Graph retorna `@odata.deltaLink`, o drive vira `done`.

Esse e o caminho recomendado para sites com arvores muito grandes, porque evita uma chamada por pasta e reduz retrabalho entre execucoes.

## Como a fila de pastas funciona

1. O script descobre um drive.
2. Busca a raiz do drive.
3. Insere a raiz em `folders_queue`.
4. Cada worker pega uma pasta `pending`.
5. Lista filhos por `/drives/{drive-id}/items/{item-id}/children`.
6. Grava arquivos em `items`.
7. Grava pastas em `items` e tambem em `folders_queue`.
8. Marca a pasta atual como `done`.

Esse modelo evita carregar a arvore inteira em memoria e permanece disponivel como fallback para casos em que o delta nao seja adequado.

## Controle de HTTP 429

O Microsoft Graph pode responder `429 Too Many Requests`. O cliente:

- Le o header `Retry-After`.
- Aguarda o numero de segundos informado.
- Repete a chamada.
- Se nao houver `Retry-After`, usa exponential backoff com jitter.
- Limita o numero de tentativas.
- Conta throttles e retries nas estatisticas finais.
- No modo delta, se uma janela de trabalho gerar retries/throttles, a concorrencia efetiva e reduzida temporariamente.

Evite concorrencia alta. Em ambientes com dezenas de TB, comece com `MAX_WORKERS=2` ou `4`. O modo delta reduz o numero total de chamadas e o retrabalho, mas nao elimina throttling do Microsoft Graph.

## Paginacao

Todas as colecoes do Graph sao lidas em streaming por pagina. O script segue `@odata.nextLink` ate o fim da colecao. No modo delta, cada pagina e gravada em lote e o `nextLink` e salvo como checkpoint antes de seguir.

## Calculo de volume de diretorios

O Graph retorna tamanho de arquivos e alguns metadados de pastas, mas o script nao depende de chamadas extras para calcular diretorios. Apos o crawl, ele calcula os agregados no SQLite:

- Pastas mais profundas sao processadas primeiro.
- Cada pasta soma arquivos filhos diretos.
- Cada pasta tambem soma os totais ja calculados das subpastas.

Assim o volume total de pastas e a quantidade de arquivos dentro de cada pasta sao derivados dos dados catalogados.

## Exportacoes

Arquivos CSV gerados:

- `exports/inventory.csv`
- `exports/summary_by_extension.csv`
- `exports/summary_by_folder.csv`

Parquet:

- `exports/inventory.parquet`

Campos principais exportados:

- Tenant/site
- URL do site
- Biblioteca
- Caminho completo da pasta
- Nome do arquivo ou pasta
- Tipo do item
- Extensao
- MIME
- Tamanho em bytes
- Tamanho formatado
- Criacao
- Modificacao
- Ultimo uso/acesso, quando a API disponibilizar
- Quantidade de arquivos dentro da pasta
- Volume total da pasta
- ID do item
- ID do drive
- Status da leitura
- Data/hora da coleta

## Tratamento de erros

Tratados com registro em `errors`:

- 401 / 403: normalmente permissao/token. Nao insistir indefinidamente.
- 404: item removido ou inacessivel.
- 408: retryable.
- 429: retry com `Retry-After`.
- 500 / 502 / 503 / 504: retryable.
- Timeout e falha de rede: retryable.

Use:

```bash
python main.py retry-errors
```

para reprocessar falhas marcadas como retryable.

## Limitacoes conhecidas do Microsoft Graph

- O modo delta usa `/root/delta` por drive e e recomendado para arvores grandes.
- A API de `children` lista filhos imediatos; a recursao por pasta continua disponivel como fallback.
- Nem todos os tenants/itens retornam `lastAccessedDateTime`.
- Metadados podem variar conforme tipo de drive, biblioteca, politica do tenant e permissao concedida.
- Para extracoes massivas de dados Microsoft 365, a Microsoft recomenda avaliar Microsoft Graph Data Connect quando aplicavel, pois cargas muito grandes podem sofrer throttling na API REST.
- OneDrive for Business de usuarios exige enumerar usuarios e acessar `/users/{id}/drive`, com permissoes extras.

## Referencias Microsoft

- Microsoft Graph throttling: https://learn.microsoft.com/en-us/graph/throttling
- Microsoft Graph service-specific throttling limits: https://learn.microsoft.com/en-us/graph/throttling-limits
- List children of a driveItem: https://learn.microsoft.com/en-us/graph/api/driveitem-list-children
- driveItem delta: https://learn.microsoft.com/en-us/graph/api/driveitem-delta
- Microsoft Graph permissions reference: https://learn.microsoft.com/en-us/graph/permissions-reference
- SharePoint Online throttling guidance: https://learn.microsoft.com/en-us/sharepoint/dev/general-development/how-to-avoid-getting-throttled-or-blocked-in-sharepoint-online

## Boas praticas para 22 TB / milhoes de arquivos

- Rode em uma VM/servidor estavel, com disco local rapido para o SQLite.
- Use `MAX_WORKERS` baixo no inicio, especialmente em `crawl-delta`.
- Monitore logs de 429 e retries.
- Faca backup periodico do arquivo `.sqlite3` se a execucao durar varios dias.
- Execute exportacoes depois da coleta ou em janelas de baixa atividade.
- Nao apague o SQLite entre `resume` ou `resume-delta`.
- Para sites gigantes, rode primeiro `prioritize-sites` e use os arquivos `site_ids_*` com `crawl-delta`.
