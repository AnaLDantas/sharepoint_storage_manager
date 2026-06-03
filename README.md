# Inventario Microsoft 365 SharePoint / OneDrive

Aplicacao Python para catalogar sites, bibliotecas, pastas e arquivos de um tenant Microsoft 365 usando Microsoft Graph API, com checkpoint em SQLite para execucoes longas.

## Recursos

- Autenticacao por Azure App Registration com `TENANT_ID`, `CLIENT_ID` e `CLIENT_SECRET`.
- Descoberta de sites do SharePoint Online e drives/bibliotecas.
- Opcionalmente, descoberta de OneDrive for Business por usuario.
- Fila local de pastas em SQLite para retomar coletas interrompidas.
- Paginacao por `@odata.nextLink`.
- Retry para 408, 429, 500, 502, 503 e 504.
- Respeito ao header `Retry-After` em HTTP 429 e exponential backoff quando o header nao vem.
- Concorrencia limitada por `MAX_WORKERS`.
- Exportacao CSV e Parquet.
- Resumos por extensao e por pasta.

## Estrutura

```text
main.py
config.py
graph_client.py
database.py
crawler.py
exporter.py
models.py
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

Edite o arquivo `.env`:

```env
TENANT_ID=seu-tenant-id
CLIENT_ID=seu-client-id
CLIENT_SECRET=seu-client-secret
MAX_WORKERS=4
REQUEST_TIMEOUT=60
SQLITE_DB_PATH=./sharepoint_inventory.sqlite3
EXPORT_PATH=./exports
LOG_LEVEL=INFO
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

Executar coleta completa:

```bash
python main.py crawl
```

Executar coleta limitada a uma lista de IDs de sites:

```env
SITE_IDS_FILE=./exports/site_ids_over_1tb.txt
```

```bash
python main.py crawl
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

Conferir metadados do SQLite com PySpark:

```bash
.venv/bin/python -m pip install pyspark
.venv/bin/python scripts/inspect_sqlite_metadata_pyspark.py \
  --db inventory/sharepoint_inventory.sqlite3 \
  --sqlite-jdbc-jar ./drivers/sqlite-jdbc.jar
```

Esse script nao faz novas chamadas ao Microsoft Graph. Ele apenas le o SQLite local e mostra schema, contagens, itens por tipo/status, principais extensoes, bibliotecas por volume, amostra de arquivos e erros em aberto.

Observacao: para o Spark ler SQLite direto, e necessario ter o driver JDBC do SQLite. Baixe o `sqlite-jdbc` e informe o caminho no parametro `--sqlite-jdbc-jar`. Se o jar ja estiver configurado no ambiente do Spark, o parametro pode ser omitido.



Esse comando usa o relatorio Microsoft Graph `getSharePointSiteUsageDetail` e requer a permissao Application `Reports.Read.All`. Ele nao varre arquivo por arquivo; baixa o relatorio de uso do Microsoft 365 e gera:

- `exports/site_priority.csv`
- `exports/site_priority.json`
- `exports/site_priority_groups.json`
- `exports/sites_priority_order.txt`
- `exports/sites_over_1tb.txt`
- `exports/sites_500gb_to_1tb.txt`
- `exports/sites_100gb_to_500gb.txt`
- `exports/sites_under_100gb.txt`
- `exports/site_ids_priority_order.txt`
- `exports/site_ids_over_1tb.txt`
- `exports/site_ids_500gb_to_1tb.txt`
- `exports/site_ids_100gb_to_500gb.txt`
- `exports/site_ids_under_100gb.txt`
- `exports/over_1tb.json`
- `exports/over_500gb_to_1tb.json`
- `exports/over_100gb_to_500gb.json`
- `exports/under_100gb.json`

Depois voce pode usar a lista dos sites maiores para orientar a coleta prioritaria. Em alguns tenants, a coluna `Site URL` do relatorio vem vazia por configuracao de privacidade dos reports; nesse caso, os arquivos `site_ids_*.txt` e os JSONs ainda ficam preenchidos com `Site Id`, tamanho e demais metadados.

## Como o checkpoint funciona

O SQLite guarda tudo que ja foi descoberto e lido:

- `sites`: sites encontrados.
- `drives`: bibliotecas/drives encontrados.
- `items`: arquivos e pastas catalogados.
- `folders_queue`: fila de pastas pendentes, em andamento, concluidas ou com falha.
- `errors`: falhas para auditoria e reprocessamento.
- `runs`: historico de execucoes.

Uma pasta so e marcada como `done` depois que todos os seus filhos foram lidos e gravados. Se o processo parar no meio, pastas `in_progress` voltam para `pending` no proximo `crawl` ou `resume`. Como `items` usa chave primaria `(drive_id, id)`, reler uma pasta nao duplica registros: os itens existentes sao atualizados.

## Como a fila de pastas funciona

1. O script descobre um drive.
2. Busca a raiz do drive.
3. Insere a raiz em `folders_queue`.
4. Cada worker pega uma pasta `pending`.
5. Lista filhos por `/drives/{drive-id}/items/{item-id}/children`.
6. Grava arquivos em `items`.
7. Grava pastas em `items` e tambem em `folders_queue`.
8. Marca a pasta atual como `done`.

Esse modelo evita carregar a arvore inteira em memoria.

## Controle de HTTP 429

O Microsoft Graph pode responder `429 Too Many Requests`. O cliente:

- Le o header `Retry-After`.
- Aguarda o numero de segundos informado.
- Repete a chamada.
- Se nao houver `Retry-After`, usa exponential backoff com jitter.
- Limita o numero de tentativas.
- Conta throttles e retries nas estatisticas finais.

Evite concorrencia alta. Em ambientes com dezenas de TB, e normal a coleta durar horas ou dias.

## Paginacao

Todas as colecoes do Graph sao lidas em streaming por pagina. O script segue `@odata.nextLink` ate o fim da colecao e grava cada item no SQLite conforme ele chega.

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

- A API de `children` lista filhos imediatos; a recursao e feita pelo script.
- Nem todos os tenants/itens retornam `lastAccessedDateTime`.
- Metadados podem variar conforme tipo de drive, biblioteca, politica do tenant e permissao concedida.
- Para extracoes massivas de dados Microsoft 365, a Microsoft recomenda avaliar Microsoft Graph Data Connect quando aplicavel, pois cargas muito grandes podem sofrer throttling na API REST.
- OneDrive for Business de usuarios exige enumerar usuarios e acessar `/users/{id}/drive`, com permissoes extras.

## Referencias Microsoft

- Microsoft Graph throttling: https://learn.microsoft.com/en-us/graph/throttling
- Microsoft Graph service-specific throttling limits: https://learn.microsoft.com/en-us/graph/throttling-limits
- List children of a driveItem: https://learn.microsoft.com/en-us/graph/api/driveitem-list-children
- Microsoft Graph permissions reference: https://learn.microsoft.com/en-us/graph/permissions-reference
- SharePoint Online throttling guidance: https://learn.microsoft.com/en-us/sharepoint/dev/general-development/how-to-avoid-getting-throttled-or-blocked-in-sharepoint-online

## Boas praticas para 22 TB / milhoes de arquivos

- Rode em uma VM/servidor estavel, com disco local rapido para o SQLite.
- Use `MAX_WORKERS` baixo no inicio.
- Monitore logs de 429 e retries.
- Faca backup periodico do arquivo `.sqlite3` se a execucao durar varios dias.
- Execute exportacoes depois da coleta ou em janelas de baixa atividade.
- Nao apague o SQLite entre `resume`.


## Melhorias futuras
- Criar um de/para para fazer o mapeamento dos sites fazendo a relação entre o site id e o site url para não precisar mexer tanto na tenant
