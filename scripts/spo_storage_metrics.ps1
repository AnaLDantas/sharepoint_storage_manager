<#
    spo_storage_metrics.ps1

    Coleta metricas por site via SharePoint Online Management Shell:
      - StorageUsageCurrent (total, em MB)
      - VersionCount        (contagem de versoes)
      - VersionSize         (tamanho das versoes, em bytes)

    Conecta UMA vez ao endpoint de admin com login delegado (Connect-SPOService)
    e percorre os sites. Cada site roda em try/catch: se der erro (sem acesso,
    bloqueado, etc.) os valores voltam null e o site e marcado como "failed",
    sem interromper o restante.

    Requer o modulo Microsoft.Online.SharePoint.PowerShell (somente Windows).
    Se ele nao carregar no PowerShell 7, rode via Windows PowerShell 5.1
    (defina PWSH_PATH=powershell no .env).

    Entrada/saida em JSON (arquivos).
#>
param(
    [Parameter(Mandatory = $true)][string]$InputPath,
    [Parameter(Mandatory = $true)][string]$OutputPath,
    [Parameter(Mandatory = $true)][string]$AdminUrl
)

$ErrorActionPreference = "Stop"

Import-Module Microsoft.Online.SharePoint.PowerShell -DisableNameChecking -ErrorAction Stop

# Login delegado (abre o prompt de autenticacao moderna do seu usuario).
Connect-SPOService -Url $AdminUrl

$items = Get-Content -Raw -Path $InputPath | ConvertFrom-Json
$results = New-Object System.Collections.Generic.List[object]

foreach ($item in $items) {
    $out = [ordered]@{
        site_id            = $item.site_id
        site_url           = $item.site_url
        status             = "failed"
        storage_used_mb    = $null
        version_count      = $null
        version_size_bytes = $null
        message            = $null
    }

    try {
        $s = Get-SPOSite -Identity $item.site_url -ErrorAction Stop
        $out.status = "ok"
        $out.storage_used_mb = $s.StorageUsageCurrent
        # VersionCount / VersionSize podem vir null se o tenant ainda nao
        # calculou a metrica de versao; nesse caso saem como null mesmo.
        $out.version_count = $s.VersionCount
        $out.version_size_bytes = $s.VersionSize
    }
    catch {
        $out.message = $_.Exception.Message
    }

    $results.Add([pscustomobject]$out)
}

$results | ConvertTo-Json -Depth 5 | Out-File -FilePath $OutputPath -Encoding utf8

try { Disconnect-SPOService -ErrorAction SilentlyContinue } catch {}
